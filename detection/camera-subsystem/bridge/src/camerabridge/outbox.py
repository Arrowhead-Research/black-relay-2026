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

# A publish callback: publish(topic, payload_bytes, qos) -> broker message id.
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
        self._load()

    # --- construction / reconstruction --------------------------------------

    def _load(self) -> None:
        """Rebuild the in-memory index from the file, in on-disk (FIFO) order.

        Pure reconstruction: no overflow pruning here (overflow is enforced at
        enqueue). Blank or malformed lines are skipped so a partially written
        tail from a crash does not poison the whole queue.
        """
        if not self._path.exists():
            return
        max_seen = 0
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    entry = _Entry(
                        mid=int(record["mid"]),
                        topic=record["topic"],
                        qos=int(record["qos"]),
                        ts=float(record["ts"]),
                        payload=record["bytes"].encode("utf-8"),
                    )
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                self._entries[entry.mid] = entry
                max_seen = max(max_seen, entry.mid)
        self._next_mid = max_seen + 1

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

        payload = json.dumps(message, separators=_SEP).encode("utf-8")
        entry = _Entry(
            mid=self._next_mid,
            topic=topic,
            qos=_DURABLE_QOS,
            ts=float(self._time_fn()),
            payload=payload,
        )
        self._next_mid += 1
        self._entries[entry.mid] = entry
        self._append(entry)
        self._enforce_limits()

    # --- send / replay ------------------------------------------------------

    def flush(self, publish: PublishFn) -> list:
        """Publish queued durable entries not yet sent, FIFO, at QoS 1.

        Returns the broker message ids in send order. An entry already handed to
        publish on this connection is skipped so a repeated flush does not
        duplicate it; use :meth:`replay` to resend everything after a reconnect.
        """
        mids = []
        for entry in list(self._entries.values()):
            if entry.mid in self._sent:
                continue
            mids.append(self._publish_entry(entry, publish))
        return mids

    def replay(self, publish: PublishFn) -> list:
        """Re-publish every unacked entry, FIFO, with its ORIGINAL bytes.

        Called on reconnect. The previous connection's broker ids are void, so
        the in-flight map and the sent-set are reset and all pending entries are
        re-sent verbatim — no re-serialize, no re-stamp — preserving
        ``event_id``/``timestamp`` and order. Returns the new broker ids.
        """
        self._inflight.clear()
        self._sent.clear()
        mids = []
        for entry in list(self._entries.values()):
            mids.append(self._publish_entry(entry, publish))
        return mids

    def _publish_entry(self, entry: _Entry, publish: PublishFn) -> object:
        """Send one entry, mark it sent, and map its broker id back for ack()."""
        broker_mid = publish(entry.topic, entry.payload, entry.qos)
        self._sent.add(entry.mid)
        if broker_mid is not None:
            self._inflight[broker_mid] = entry.mid
        return broker_mid

    # --- ack ----------------------------------------------------------------

    def ack(self, mid) -> None:
        """Remove the acked message from the file.

        ``mid`` is normally the broker id returned by ``publish`` (resolved via
        the in-flight map); an entry's own stable mid is also accepted. Unknown
        or already-removed ids are a no-op (acks can arrive late or twice).
        """
        entry_mid = self._inflight.pop(mid, None)
        if entry_mid is None and mid in self._entries:
            entry_mid = mid
        if entry_mid is None:
            return
        if self._entries.pop(entry_mid, None) is None:
            return
        self._sent.discard(entry_mid)
        # Drop any other broker ids that pointed at this now-removed entry.
        for broker_mid, target in list(self._inflight.items()):
            if target == entry_mid:
                del self._inflight[broker_mid]
        self._rewrite()

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
                    del self._entries[entry_mid]
                    self._discard_entry_state(entry_mid)
                    dropped += 1

        while self._entries and self._serialized_size() > self._max_bytes:
            oldest_mid = next(iter(self._entries))
            del self._entries[oldest_mid]
            self._discard_entry_state(oldest_mid)
            dropped += 1

        if dropped:
            self._rewrite()
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
        """Serialize one entry to its JSONL line (with trailing newline)."""
        record = {
            "mid": entry.mid,
            "topic": entry.topic,
            "qos": entry.qos,
            "ts": entry.ts,
            "bytes": entry.payload.decode("utf-8"),
        }
        return json.dumps(record, separators=_SEP) + "\n"

    def _serialized_size(self) -> int:
        """Total on-disk byte size the current index would occupy."""
        return sum(len(self._line(entry).encode("utf-8")) for entry in self._entries.values())

    def _append(self, entry: _Entry) -> None:
        """Append one entry's line to the file and fsync it (durable before send)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(self._line(entry))
            handle.flush()
            os.fsync(handle.fileno())

    def _rewrite(self) -> None:
        """Atomically rewrite the file from the index (used by ack/overflow).

        Writes a temp file, fsyncs it, then ``os.replace`` swaps it in so a crash
        never leaves a half-written queue.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in self._entries.values():
                handle.write(self._line(entry))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
