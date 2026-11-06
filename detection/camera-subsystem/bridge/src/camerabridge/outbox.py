"""Durable on-disk delivery queue for the camera subsystem's ``camera.*`` events.

A node keeps detecting and recording through a broker outage or a Wi-Fi drop
(DESIGN.md 5.1 / 5.5). Significant events must not be lost in the gap: each
**durable** message is written to this on-disk outbox *before* it is published,
sent at QoS 1, and removed only when the broker acknowledges it. If the link (or
the process) dies in between, the event is still on disk and is re-sent on
reconnect with its **original** bytes — so fusion's ``event_id``/``timestamp``
dedupe key survives the round-trip unchanged. Delivery is therefore
at-least-once: replays can produce duplicates, which fusion drops by ``event_id``.

Durability model (what "durable" means here)
---------------------------------------------
"Durable" = *survives a process restart via the file*. The on-disk form is one
JSON object per line (JSONL) — ``{mid, topic, qos, ts, bytes}`` — where ``bytes``
is the exact serialized payload captured at :meth:`Outbox.enqueue`. An in-memory
index mirrors the file in FIFO order. A fresh :class:`Outbox` constructed on the
same path reconstructs the pending entries from that file and :meth:`Outbox.replay`
re-sends them verbatim.

QoS split and the overflow ruling
----------------------------------
Only durable messages are persisted — ``object_start`` / ``object_end`` /
``cue-ack`` / ``node-status``, all QoS 1. ``object_update`` is QoS 0 and is
**never written**: :meth:`enqueue` with ``durable=False`` is a no-op for the
file, and the caller publishes those itself at QoS 0. This is deliberate
(DESIGN.md 5.2): a long outage must not backlog a flood of stale object
positions, and a replay must never resurrect an out-of-date track location.

Because updates are never written, there are none to "evict first" on overflow.
The controller ruling that governs this module therefore sets a single overflow
policy: **FIFO drop-oldest-first**. When the file would exceed ``max_bytes`` or an
entry's age exceeds ``max_age_s``, the oldest queued entries are shed (they are
the stalest events of a long outage) with a logged warning; the newest are kept
and order is preserved. There is intentionally no "evict ``object_update`` first"
behaviour — that rule is dead once updates are never persisted.

Message-id handling
--------------------
Each entry gets a stable internal ``mid`` at enqueue (a monotonic counter,
persisted so it is reconstructed on restart) — this is the file's identity for a
line. The ``publish`` callback returns the broker-assigned message id; the outbox
maps that returned id back to the entry so :meth:`ack` (wired to the broker's
publish-acknowledgement) removes the right line. :meth:`ack` also accepts an
entry's own ``mid`` directly and is idempotent for an unknown id.

Send window
-----------
At most :data:`_SEND_WINDOW` entries are in flight (sent and unacked) at once;
acknowledgements open the window for the next ones. A large backlog therefore never
sits in the MQTT client's memory, and a replay costs a bounded amount of work per
round trip. Acks are applied in batches (:meth:`ack_many`), because acks can arrive
faster than a rewrite per ack can keep up with.

File rewrites
-------------
A rewrite is proportional to the backlog, and this file lives on an SD card, so an ack
does not always rewrite. Past a small backlog the file may keep lines already acked,
up to a share of its size (:data:`_DEAD_MAX_BYTES` at most). A crash then re-sends
those lines, which at-least-once delivery allows and fusion dedupes by ``event_id``.
An unacked line is never removed, so nothing can be lost by this. A rewrite syncs its
directory, so entries appended after it live in the file the next boot will see, on any
filesystem. Emptying the file needs no sync: a crash can bring back only acked lines.

Damaged files
-------------
A power cut or a full disk can leave a torn last line, and a bad card can garble any
line. Loading keeps every intact entry and never raises for what a line holds. Bad lines
are counted and logged without their content, and moved to ``<path>.corrupt`` (0600, at
most :data:`_CORRUPT_KEEP_BYTES`), for diagnosis. The file is repaired durably before any
new append, because a line appended after a torn one would merge with it and be lost at
the next load. A last line that is whole and lacks only its newline is an intact entry,
and is kept. A repair that fails is logged, not raised, and is retried before the next
append: the bridge must stay up, and an append that cannot repair raises as any failed
append does.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = ["Outbox"]

logger = logging.getLogger(__name__)

# Compact, deterministic serialization. The payload bytes are minted once at
# enqueue and never re-serialized, so this only governs how new payloads and the
# JSONL envelope are written.
_SEP = (",", ":")

# The one QoS the outbox persists at. Durable messages are QoS 1; object_update
# (QoS 0) is never written here.
_DURABLE_QOS = 1

# Most entries in flight at once. Matches paho's default max_inflight_messages.
_SEND_WINDOW = 20

# A rewrite costs the whole backlog, and PUBACKs arrive one at a time on the thread doing the
# rewrite, so a rewrite per ack wrote 2.3 GB to the SD card to replay a day's 1.4 MB outage. So the
# file may hold acked lines, up to this share of its live bytes, within these limits.
_DEAD_SHARE_DIVISOR = 8
_DEAD_MIN_BYTES = 16 * 1024
_DEAD_MAX_BYTES = 1024 * 1024

# The most that the aside file for corrupt lines may hold. Further bad bytes are only counted.
_CORRUPT_KEEP_BYTES = 1024 * 1024

# What a topic may not hold: paho refuses these at publish, which would stop a replay for good.
_BAD_TOPIC_CHARS = frozenset("+#\0")

# A publish callback: publish(topic, payload_bytes, qos) -> broker message id, or None
# if the client did not accept the message (the entry stays unsent).
PublishFn = Callable[[str, bytes, int], object]


@dataclass
class _Entry:
    """One queued durable message, mirrored between the file and the index.

    ``payload`` is the exact bytes captured at enqueue; replay re-sends it
    verbatim. ``ts`` is wall-clock epoch seconds at enqueue, used only for
    age-based overflow (it is independent of the message's own ``timestamp``
    field, which lives inside ``payload``).
    """

    mid: int
    topic: str
    qos: int
    ts: float
    payload: bytes
    size: int = 0  # bytes of this entry's line in the file


class Outbox:
    """Durable FIFO delivery queue backed by a JSONL file.

    Construct with the file ``path`` and the overflow caps ``max_bytes`` (on-disk
    size) and ``max_age_s`` (per-entry age). ``time_fn`` is injectable only so
    tests can drive age-based overflow deterministically; production uses the
    wall clock. Constructing on an existing path reconstructs any pending
    (unacked) entries so they can be replayed.
    """

    def __init__(
        self,
        path: str,
        max_bytes: int,
        max_age_s: float,
        *,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._max_age_s = max_age_s
        self._time_fn = time_fn
        # Insertion order == FIFO order; deletion preserves the order of the rest.
        self._entries: dict[int, _Entry] = {}
        # Broker-returned publish id -> our stable entry mid (bridges ack()).
        self._inflight: dict[object, int] = {}
        # Entry mids already handed to publish this connection (so a repeated
        # flush does not re-send). Cleared on replay (new connection) and restart.
        self._sent: set[int] = set()
        self._next_mid = 1
        # What the index occupies on disk, so that a size check does not measure the queue.
        self._live_bytes = 0
        # What the file still holds for entries no longer in the index.
        self._dead_bytes = 0
        # A repair still owed to the file's tail, retried before the next append.
        self._truncate_to: int | None = None
        self._missing_newline = False
        self._load()

    # --- construction / reconstruction --------------------------------------

    def _load(self) -> None:
        """Rebuild the in-memory index from the file, in on-disk (FIFO) order.

        No overflow pruning here (overflow is enforced at enqueue). Anything but an intact
        entry is set aside and the file repaired. See "Damaged files" in the module docstring.
        """
        self._remove_tmp()  # left by a crash or a failed removal. Only the file itself counts
        if not self._path.exists():
            return
        max_seen = 0
        complete_end = 0  # where the last newline-terminated line ends
        corrupt: list[bytes] = []
        corrupt_lines = 0
        corrupt_bytes = 0
        corrupt_complete = False
        torn = False
        unterminated = False
        with self._path.open("rb") as handle:
            for raw in handle:
                complete = raw.endswith(b"\n")
                if complete:
                    complete_end += len(raw)
                    if not raw.strip():
                        continue
                entry = self._parse(raw)
                if entry is None:
                    corrupt_lines += 1
                    corrupt_bytes += len(raw)
                    if corrupt_bytes <= _CORRUPT_KEEP_BYTES:
                        corrupt.append(raw if complete else raw + b"\n")
                    corrupt_complete = corrupt_complete or complete
                    torn = torn or not complete
                    continue
                unterminated = not complete
                self._forget_size(self._entries.get(entry.mid))
                entry.size = len(self._line(entry))
                self._live_bytes += entry.size
                self._entries[entry.mid] = entry
                max_seen = max(max_seen, entry.mid)
        self._next_mid = max_seen + 1
        if corrupt_lines:
            logger.warning(
                "outbox: %d corrupt line%s (%d bytes) in %s; kept %d intact entries",
                corrupt_lines,
                "" if corrupt_lines == 1 else "s",
                corrupt_bytes,
                self._path,
                len(self._entries),
            )
            self._move_aside(corrupt)
        self._repair(complete_end, torn, unterminated, corrupt_complete)

    @staticmethod
    def _parse(raw: bytes) -> _Entry | None:
        """One file line as an entry, or None if it is not one that could ever be sent."""
        try:
            record = json.loads(raw.decode("utf-8"))
            topic = record["topic"]
            qos = int(record["qos"])
            if not isinstance(topic, str) or not topic or _BAD_TOPIC_CHARS & set(topic):
                return None
            if qos != _DURABLE_QOS:
                return None
            topic.encode("utf-8")
            return _Entry(
                mid=int(record["mid"]),
                topic=topic,
                qos=qos,
                ts=float(record["ts"]),
                payload=record["bytes"].encode("utf-8"),
            )
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            return None

    def _move_aside(self, corrupt: list[bytes]) -> None:
        """Keep corrupt lines for diagnosis, up to the cap. Failing here costs only that."""
        aside = self._path.with_name(self._path.name + ".corrupt")
        try:
            fd = os.open(aside, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "ab") as handle:
                room = _CORRUPT_KEEP_BYTES - handle.tell()
                data = b"".join(corrupt)
                if room > 0:
                    handle.write(data[:room])
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("outbox: could not move corrupt lines to %s: %s", aside, exc.strerror)

    def _repair(self, complete_end: int, torn: bool, unterminated: bool, corrupt: bool) -> None:
        """Make the file safe to append to. On failure the tail's repair stays owed."""
        self._truncate_to = complete_end if torn else None
        self._missing_newline = unterminated and not torn
        try:
            self._settle_tail()
            if self._missing_newline:
                with self._path.open("ab") as handle:
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._missing_newline = False
            if corrupt and self._entries:
                self._rewrite()
            elif corrupt:
                self._truncate_to = 0
                self._settle_tail()
        except OSError as exc:
            logger.error("outbox: repairing %s failed, will retry: %s", self._path, exc.strerror)

    def _settle_tail(self) -> None:
        """Do any truncation still owed to the file, durably."""
        if self._truncate_to is None:
            return
        with self._path.open("r+b") as handle:
            handle.truncate(self._truncate_to)
            os.fsync(handle.fileno())
        self._truncate_to = None

    # --- enqueue ------------------------------------------------------------

    def enqueue(self, topic: str, message: dict, *, durable: bool) -> None:
        """Queue ``message`` for ``topic``.

        ``durable=True``: serialize the message once, append it to the file
        (fsync'd) **before** it becomes eligible for a QoS-1 send, then enforce
        the overflow caps. ``durable=False``: a no-op for persistence — the
        message is QoS 0 (an ``object_update``) and the caller publishes it
        itself; nothing is written and the file is untouched.
        """
        if not durable:
            return

        self.enqueue_many([(topic, message)])

    def enqueue_many(self, messages: list[tuple[str, dict]]) -> None:
        """Durably queue ``(topic, message)`` pairs, in order, as one batch.

        One write + fsync and one overflow pass for the whole batch.
        """
        if not messages:
            return
        entries = []
        for topic, message in messages:
            payload = json.dumps(message, separators=_SEP).encode("utf-8")
            entries.append(
                _Entry(
                    mid=self._next_mid,
                    topic=topic,
                    qos=_DURABLE_QOS,
                    ts=float(self._time_fn()),
                    payload=payload,
                )
            )
            entries[-1].size = len(self._line(entries[-1]))
            self._live_bytes += entries[-1].size
            self._next_mid += 1
            self._entries[entries[-1].mid] = entries[-1]
        self._append(entries)
        self._enforce_limits()

    # --- send / replay ------------------------------------------------------

    def flush(self, publish: PublishFn) -> list:
        """Publish unsent durable entries, FIFO, at QoS 1, up to the send window.

        Returns the broker message ids in send order. An entry already handed to
        publish on this connection is skipped so a repeated flush does not duplicate
        it; a further flush after acks sends the next ones. A rejected publish stops
        the flush, so nothing overtakes the entry that will be retried.
        """
        mids = []
        for entry in list(self._entries.values()):
            if len(self._sent) >= _SEND_WINDOW:
                break
            if entry.mid in self._sent or entry.mid not in self._entries:
                continue
            broker_mid = publish(entry.topic, entry.payload, entry.qos)
            if broker_mid is None:
                break
            self._sent.add(entry.mid)
            self._inflight[broker_mid] = entry.mid
            mids.append(broker_mid)
        return mids

    def replay(self, publish: PublishFn) -> list:
        """Re-publish unacked entries, FIFO, with their ORIGINAL bytes, up to the window.

        Called on reconnect. The previous connection's broker ids are void, so the
        in-flight map and the sent-set are reset and sending restarts from the oldest
        pending entry, verbatim — no re-serialize, no re-stamp — preserving
        ``event_id``/``timestamp`` and order. Returns the new broker ids. The caller
        must ensure its MQTT client holds no copies of its own from the old connection.
        """
        self._inflight.clear()
        self._sent.clear()
        return self.flush(publish)

    # --- ack ----------------------------------------------------------------

    def ack(self, mid) -> None:
        """Remove one acked message from the file. See :meth:`ack_many`."""
        self.ack_many([mid])

    def ack_many(self, mids) -> None:
        """Remove the acked messages from the file, with one rewrite for the batch.

        Each mid is a broker id returned by ``publish`` (paho's PUBACK mid), resolved
        to an entry **only** via the in-flight map. A mid absent from that map — a
        stray or duplicate PUBACK, or one whose integer happens to collide with some
        entry's own internal id — is a no-op. Entry-own ids are never consulted here,
        so the two id spaces (broker mids vs internal entry mids) can never be confused
        and a stray PUBACK cannot evict the wrong entry. Acks can arrive late or twice;
        both are safely ignored.
        """
        removed = False
        for mid in mids:
            entry_mid = self._inflight.pop(mid, None)
            entry = self._entries.pop(entry_mid, None) if entry_mid is not None else None
            if entry is None:
                continue
            self._forget_size(entry)
            self._sent.discard(entry_mid)
            removed = True
        if removed:
            self._rewrite_if_due()

    # --- overflow -----------------------------------------------------------

    def _enforce_limits(self) -> None:
        """Shed oldest entries until within the age and size caps (FIFO).

        Age first (an entry older than ``max_age_s`` can never be fresh again),
        then size (drop the oldest until the serialized file is within
        ``max_bytes``). Both keep the newest and preserve order. A single warning
        is logged naming how many entries were dropped and why.
        """
        dropped = 0
        now = float(self._time_fn())

        if self._max_age_s is not None:
            for entry_mid in list(self._entries.keys()):
                if now - self._entries[entry_mid].ts > self._max_age_s:
                    self._forget_size(self._entries.pop(entry_mid))
                    self._discard_entry_state(entry_mid)
                    dropped += 1

        while self._entries and self._live_bytes > self._max_bytes:
            oldest_mid = next(iter(self._entries))
            oldest = self._entries[oldest_mid]
            # A single entry whose own on-disk line already exceeds the whole cap
            # can never fit; it is still dropped (keeping the cap), but with a
            # DISTINCT warning so it is not hidden inside the normal FIFO-overflow
            # message — a lone oversized durable event is a signal worth seeing.
            line_size = oldest.size
            if line_size > self._max_bytes:
                logger.warning(
                    "outbox: dropping a single durable entry (mid=%s topic=%s) whose "
                    "%d-byte line alone exceeds max_bytes=%s; it can never fit the cap",
                    oldest.mid,
                    oldest.topic,
                    line_size,
                    self._max_bytes,
                )
            self._forget_size(self._entries.pop(oldest_mid))
            self._discard_entry_state(oldest_mid)
            dropped += 1

        if dropped or self._live_bytes + self._dead_bytes > self._max_bytes:
            self._rewrite_if_due()
        if dropped:
            logger.warning(
                "outbox overflow: dropped %d oldest durable entr%s (FIFO) to stay "
                "within max_bytes=%s / max_age_s=%s",
                dropped,
                "y" if dropped == 1 else "ies",
                self._max_bytes,
                self._max_age_s,
            )

    def _discard_entry_state(self, entry_mid: int) -> None:
        """Forget the in-flight/sent bookkeeping for a dropped entry."""
        self._sent.discard(entry_mid)
        for broker_mid, target in list(self._inflight.items()):
            if target == entry_mid:
                del self._inflight[broker_mid]

    # --- on-disk form -------------------------------------------------------

    def _line(self, entry: _Entry) -> str:
        """Serialize one entry to its JSONL line (with trailing newline). All ASCII, so its
        length is its size on disk."""
        record = {
            "mid": entry.mid,
            "topic": entry.topic,
            "qos": entry.qos,
            "ts": entry.ts,
            "bytes": entry.payload.decode("utf-8"),
        }
        return json.dumps(record, separators=_SEP) + "\n"

    def _forget_size(self, entry: _Entry | None) -> None:
        if entry is not None:
            self._live_bytes -= entry.size
            self._dead_bytes += entry.size

    def _rewrite_if_due(self) -> None:
        """Rewrite once the file's acked lines are worth the cost. See the module docstring."""
        allowed = min(_DEAD_MAX_BYTES, self._live_bytes // _DEAD_SHARE_DIVISOR)
        if (
            self._live_bytes <= _DEAD_MIN_BYTES
            or self._dead_bytes >= max(allowed, _DEAD_MIN_BYTES)
            or self._live_bytes + self._dead_bytes > self._max_bytes
        ):
            self._rewrite()

    def _append(self, entries: list[_Entry]) -> None:
        """Append entries' lines to the file and fsync once (durable before send).

        A failure partway leaves a torn tail, which is cut off before the next append.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._settle_tail()
        data = "".join(self._line(entry) for entry in entries).encode("ascii")
        if self._missing_newline:
            data = b"\n" + data
        start = None
        try:
            with self._path.open("ab") as handle:
                start = handle.tell()
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            self._truncate_to = start
            raise
        self._missing_newline = False

    def _rewrite(self) -> None:
        """Atomically rewrite the file from the index (used by ack/overflow).

        Writes a temp file, fsyncs it, then ``os.replace`` swaps it in and the directory is
        fsynced, so a crash never leaves a half-written queue. An empty queue just truncates:
        this is the usual case, once per message, and a crash can bring back only acked lines.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._entries:
            self._path.open("w").close()
            self._forget_old_file()
            return
        tmp = self._tmp_path()
        try:
            with tmp.open("wb") as handle:
                for entry in self._entries.values():
                    handle.write(self._line(entry).encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError:
            self._remove_tmp()  # on a full disk, this is the space the next heartbeat needs
            raise
        self._forget_old_file()  # at once: an offset still owed to the old file would cut the new one
        directory = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _forget_old_file(self) -> None:
        """The file was just replaced, so nothing owed to the old one applies."""
        self._dead_bytes = 0
        self._truncate_to = None
        self._missing_newline = False

    def _tmp_path(self) -> Path:
        return self._path.with_name(self._path.name + ".tmp")

    def _remove_tmp(self) -> None:
        """Drop a rewrite's temp file. The queue is never in it until the rename."""
        try:
            self._tmp_path().unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("outbox: could not remove %s: %s", self._tmp_path(), exc.strerror)
