"""Durable, bounded, crash-safe outbox for the team (fusion) output.

Storage: one append-only journal, `<dir>/outbox.journal`, one record per line: `<crc32 hex> <json>\n`.
  m    a message (id, topic, payload, enqueue time, class, bridge seq)
  a    PUBACK received: the message is done
  x    dropped by a cap (reason size|age)
  cur  producer cursor (position in the SQLite event log) -- written in the SAME write()+fsync as the messages it
       covers, so after a crash the cursor never runs ahead of the messages that are durable
  meta producer bookkeeping (e.g. last-known stale flags)
A torn or corrupt line (crash mid-write, bad sector) fails its CRC and is skipped; a torn tail is truncated before
the next append. On open the journal is replayed, then compacted (tmp file + fsync + rename + dir fsync), so the
file never grows without bound. Messages are only ever removed by an ack record or a drop record.

Bounds: max_messages, max_bytes, max_age_s. Over a cap, the OLDEST goes first, periodic `status` messages before
`event` messages (a backlog of old heartbeats must not push out real events). Every drop is counted.

Locking: `self._lock` guards the in-memory state and the journal file. It is NEVER held while calling paho
(`publish()` or anything else): paho invokes on_publish while holding its own mutex, so holding this lock across
publish() could deadlock against the ack path. The outbox itself does not import or know paho.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("br2.outbox")

EVENT, STATUS = "event", "status"
COMPACT_RETRY_S = 30
_OVERHEAD = 160          # rough per-message bookkeeping cost counted against max_bytes


@dataclass
class Entry:
    id: str
    topic: str
    payload: bytes
    at: float
    cls: str
    seq: int

    @property
    def size(self) -> int:
        return len(self.payload) + len(self.topic) + _OVERHEAD


@dataclass
class Item:
    """A message to enqueue. `build(seq)` produces the payload bytes once the outbox has assigned the seq."""
    id: str
    topic: str
    cls: str
    build: Callable[[int], bytes]


def _line(rec: dict) -> bytes:
    body = json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"%08x " % (zlib.crc32(body) & 0xFFFFFFFF) + body + b"\n"


def _parse(line: bytes) -> dict | None:
    if len(line) < 11 or line[8:9] != b" ":
        return None
    try:
        crc = int(line[:8], 16)
    except ValueError:
        return None
    body = line[9:]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    try:
        rec = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


class Outbox:
    def __init__(self, directory: str, max_bytes: int = 10 * 1024 * 1024, max_messages: int = 20000,
                 max_age_s: float = 86400.0, clock: Callable[[], float] = time.time):
        self.dir = directory
        self.path = os.path.join(directory, "outbox.journal")
        self.max_bytes, self.max_messages, self.max_age_s = int(max_bytes), int(max_messages), float(max_age_s)
        self.clock = clock
        self._lock = threading.Lock()
        self._msgs: "OrderedDict[str, Entry]" = OrderedDict()
        self._bytes = 0
        self._seq = 0
        self.cursor: dict | None = None
        self.meta: dict[str, Any] = {}
        self.dropped = {"size": 0, "age": 0}
        self.acked_total = 0
        self.enqueued_total = 0
        self.recovered_bad_lines = 0
        self.cursor_rewinds = 0                  # times a corrupt NON-final journal record made us rewind the cursor
        self.oversize_rejected = 0
        self.write_errors = 0
        self.compact_errors = 0
        self.last_write_error: str | None = None
        self._needs_repair = False
        self._compact_retry_at = 0.0
        self._f = None
        self._journal_bytes = 0                  # offset of the last known-good end of the journal file
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        self._load()
        self._compact()

    # ------------------------------------------------------------ load / compact
    def _apply(self, r: dict) -> None:
        o = r.get("o")
        if o == "m":
            e = Entry(r["k"], r["t"], r["p"].encode("utf-8"), float(r["at"]), r.get("cl", STATUS), int(r["q"]))
            self._add(e)
            self._seq = max(self._seq, e.seq)
        elif o == "a":
            self._remove(r.get("k"))
        elif o == "x":
            if self._remove(r.get("k")):
                why = r.get("why") if r.get("why") in self.dropped else "size"
                self.dropped[why] += 1
        elif o == "cur":
            self.cursor = r.get("v")
        elif o == "meta":
            self.meta[r["k"]] = r.get("v")
        elif o == "s":      # snapshot header written by compaction
            self._seq = max(self._seq, int(r.get("seq", 0)))
            self.cursor = r.get("cur")
            self.meta = dict(r.get("meta") or {})
            for k, v in (r.get("dropped") or {}).items():
                if k in self.dropped:
                    self.dropped[k] = int(v)

    def _load(self) -> None:
        good_end = 0
        try:
            data = open(self.path, "rb").read()
        except OSError:
            data = b""
        pos = 0
        rewind_to: dict | None = None            # cursor as it stood just before the FIRST corrupt record
        corrupt_seen = False
        first_bad_at = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl < 0:
                break                                   # torn tail (no newline): ignore, truncated below
            rec = _parse(data[pos:nl])
            bad = rec is None
            if not bad:
                try:
                    self._apply(rec)
                except (KeyError, TypeError, ValueError, AttributeError):
                    bad = True
            if bad:
                self.recovered_bad_lines += 1
                if not corrupt_seen:
                    corrupt_seen = True
                    rewind_to = dict(self.cursor) if isinstance(self.cursor, dict) else None
                    first_bad_at = pos
            pos = nl + 1
            good_end = pos
        if data and good_end != len(data):
            self.recovered_bad_lines += 1
            log.warning("fusion outbox: discarded %d torn trailing bytes", len(data) - good_end)
        if corrupt_seen and first_bad_at < good_end and data.find(b"\n", first_bad_at) + 1 < good_end:
            # A record in the MIDDLE of the journal is unreadable while later records are intact. If it was a message
            # (or the message part of a batch), the cursor in a later record already moved past it, and the event
            # would never be re-derived. Events are re-derivable from the SQLite log and their ids are derived from
            # the log seq, so rewind the cursor to where it stood before the first unreadable record: everything after
            # is rebuilt, and messages that are still queued are skipped by id (append dedupes), the rest are re-sent
            # under the same id (consumers dedupe).
            self.cursor_rewinds += 1
            self.cursor = rewind_to if rewind_to is not None else {"seq": 0, "db_id": None, "pub": None}
            log.error("fusion outbox: corrupt record in the middle of the journal (offset %d); event cursor rewound "
                      "to log seq %s so that possibly lost events are re-sent (same ids)", first_bad_at,
                      self.cursor.get("seq"))

    def _snapshot_records(self) -> list[bytes]:
        out = [_line({"o": "s", "seq": self._seq, "cur": self.cursor, "meta": self.meta, "dropped": self.dropped})]
        for e in self._msgs.values():
            out.append(_line({"o": "m", "k": e.id, "t": e.topic, "p": e.payload.decode("utf-8"), "at": e.at,
                              "cl": e.cls, "q": e.seq}))
        return out

    def _compact(self) -> None:
        tmp = self.path + ".tmp"
        try:
            total = 0
            with open(tmp, "wb") as f:
                for ln in self._snapshot_records():
                    f.write(ln)
                    total += len(ln)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise                                        # the live journal and handle are untouched
        old, self._f = self._f, None
        self._journal_bytes = total                      # the file IS the snapshot now, whatever happens to the reopen
        self._needs_repair = False
        if old:
            try:
                old.close()
            except OSError:
                pass
        self._fsync_dir()
        self._reopen()                                   # may raise: _f stays None, _write reopens on demand
        self._live_at_compact = self._bytes

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _write(self, recs: list[dict], sync: bool) -> None:
        """Append records. Atomic in effect: on ANY write/fsync error the file is truncated back to the last
        known-good offset (so no torn line is ever left in front of the next record), the error is counted and
        re-raised. A failed append therefore changes nothing; the caller does not advance its cursor and retries."""
        buf = b"".join(_line(r) for r in recs)
        if not buf:
            return
        good: int | None = None
        try:
            self._repair_tail()
            if self._f is None:
                self._reopen()                           # also re-reads the true file size
            good = self._journal_bytes
            n = self._f.write(buf)
            if n is not None and n != len(buf):          # unbuffered file: a short write is possible
                raise OSError(errno.ENOSPC, "short write to outbox journal")
            if sync:
                os.fsync(self._f.fileno())
        except BaseException as e:
            self.write_errors += 1
            self.last_write_error = f"{type(e).__name__}: {e}"
            if good is not None:
                self._rollback(good)
            raise
        self._journal_bytes = good + len(buf)
        self.last_write_error = None

    def _rollback(self, good: int) -> None:
        """Cut the journal back to `good` bytes. If even that fails, reopen: the next _load discards a torn tail,
        and `_needs_repair` makes the next append re-verify the tail before writing."""
        try:
            os.truncate(self.path, good)
            self._needs_repair = False
        except OSError as e:
            self._needs_repair = True
            log.error("fusion outbox: cannot truncate journal back to %d bytes: %s", good, e)

    def _reopen(self) -> None:
        self._f = open(self.path, "ab", buffering=0)
        self._journal_bytes = os.path.getsize(self.path)

    def _repair_tail(self) -> None:
        """After a failed rollback: make sure the file ends exactly at the last good offset before appending."""
        if not self._needs_repair:
            return
        os.truncate(self.path, self._journal_bytes)
        self._needs_repair = False

    # ------------------------------------------------------------ in-memory helpers
    def _add(self, e: Entry) -> None:
        old = self._msgs.pop(e.id, None)
        if old:
            self._bytes -= old.size
        self._msgs[e.id] = e
        self._bytes += e.size

    def _remove(self, k: str | None) -> bool:
        e = self._msgs.pop(k, None) if k else None
        if e:
            self._bytes -= e.size
            return True
        return False

    def _enforce(self, now: float) -> list[dict]:
        """Apply caps; returns the drop records to journal. Caller holds the lock."""
        recs: list[dict] = []

        def drop(e: Entry, why: str) -> None:
            self._remove(e.id)
            self.dropped[why] += 1
            recs.append({"o": "x", "k": e.id, "why": why})
        if self.max_age_s > 0:
            for e in [e for e in self._msgs.values() if now - e.at > self.max_age_s]:
                drop(e, "age")
        while self._msgs and (len(self._msgs) > self.max_messages or self._bytes > self.max_bytes):
            victim = next((e for e in self._msgs.values() if e.cls == STATUS), None) or next(iter(self._msgs.values()))
            drop(victim, "size")
        return recs

    def _maybe_compact(self) -> None:
        """Compaction is housekeeping: a failure (disk full, EIO, ...) is counted and retried later, never raised.
        This runs inside paho's on_publish (via ack) and must not be able to kill paho's network thread."""
        if self._journal_bytes <= max(256 * 1024, 4 * self._bytes + 64 * 1024):
            return
        now = self.clock()
        if now < self._compact_retry_at:
            return
        try:
            self._compact()
            self._compact_retry_at = 0.0
        except Exception as e:  # noqa: BLE001
            self.compact_errors += 1
            self._compact_retry_at = now + COMPACT_RETRY_S
            log.warning("fusion outbox: compaction failed (%s); will retry in %ds", e, COMPACT_RETRY_S)

    def _journal_drops(self, drops: list[dict]) -> None:
        """Drop records are advisory (a restart re-applies the caps): failing to write them is not an error."""
        if not drops:
            return
        try:
            self._write(drops, sync=False)
        except Exception as e:  # noqa: BLE001
            log.warning("fusion outbox: cannot journal %d drop record(s): %s", len(drops), e)

    # ------------------------------------------------------------ public API
    def append(self, items: list[Item], cursor: dict | None = None, meta: dict[str, Any] | None = None) -> int:
        """Durably enqueue `items` (assigning gap-free seq numbers), optionally advancing the producer cursor and
        updating meta, in ONE write+fsync. Items whose id is already queued are skipped. Returns how many were added."""
        with self._lock:
            now = self.clock()
            recs: list[dict] = []
            new: list[Entry] = []
            seq = self._seq
            for it in items:
                if it.id in self._msgs:
                    continue
                payload = it.build(seq + 1)
                if len(payload) + len(it.topic) + _OVERHEAD > self.max_bytes:
                    # a single message larger than the whole cap: reject IT, never evict the queue for it
                    self.oversize_rejected += 1
                    log.error("fusion outbox: rejecting %s message %s of %d bytes (cap %d)", it.cls, it.id,
                              len(payload), self.max_bytes)
                    continue
                seq += 1
                new.append(Entry(it.id, it.topic, payload, now, it.cls, seq))
                recs.append({"o": "m", "k": it.id, "t": it.topic, "p": payload.decode("utf-8"), "at": now,
                             "cl": it.cls, "q": seq})
            if cursor is not None:
                recs.append({"o": "cur", "v": cursor})
            for k, v in (meta or {}).items():
                recs.append({"o": "meta", "k": k, "v": v})
            if not recs:
                return 0
            self._write(recs, sync=True)               # durable first; memory only after the write succeeded
            for e in new:
                self._add(e)
            self._seq = seq
            self.enqueued_total += len(new)
            if cursor is not None:
                self.cursor = cursor
            for k, v in (meta or {}).items():
                self.meta[k] = v
            self._journal_drops(self._enforce(now))
            self._maybe_compact()
            return len(new)

    def pending(self, limit: int, exclude: set[str] | None = None) -> list[Entry]:
        """Oldest-first messages not in `exclude` (the caller's in-flight set). Snapshot only: no lock is held after
        return, so the caller may publish freely."""
        out: list[Entry] = []
        with self._lock:
            for e in self._msgs.values():
                if exclude and e.id in exclude:
                    continue
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def ack(self, k: str) -> bool:
        """PUBACK received. The ONLY way a delivered message leaves the outbox."""
        with self._lock:
            if not self._remove(k):
                return False
            self.acked_total += 1
            try:
                self._write([{"o": "a", "k": k}], sync=False)   # a lost ack record only causes a harmless re-send
            except Exception as e:  # noqa: BLE001
                log.warning("fusion outbox: cannot journal ack: %s", e)
            self._maybe_compact()
            return True

    def expire(self) -> int:
        with self._lock:
            n0 = len(self._msgs)
            self._journal_drops(self._enforce(self.clock()))
            return n0 - len(self._msgs)

    def stats(self) -> dict:
        with self._lock:
            oldest = next(iter(self._msgs.values()), None)
            return {"depth": len(self._msgs), "bytes": self._bytes, "dropped_size": self.dropped["size"],
                    "dropped_age": self.dropped["age"], "acked_total": self.acked_total,
                    "enqueued_total": self.enqueued_total, "last_seq": self._seq,
                    "oversize_rejected": self.oversize_rejected, "cursor_rewinds": self.cursor_rewinds,
                    "recovered_bad_lines": self.recovered_bad_lines, "write_errors": self.write_errors,
                    "last_write_error": self.last_write_error, "compact_errors": self.compact_errors,
                    "oldest_age_s": (self.clock() - oldest.at) if oldest else 0.0}

    def close(self) -> None:
        with self._lock:
            if self._f:
                try:
                    os.fsync(self._f.fileno())
                except OSError:
                    pass
                self._f.close()
                self._f = None
