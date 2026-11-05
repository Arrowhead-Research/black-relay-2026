"""Tests for ``camerabridge.outbox`` — the durable on-disk delivery queue.

The bridge must not silently lose a significant detection event across a broker
outage or a Wi-Fi drop, and a replay must never corrupt fusion by re-minting ids
or re-stamping timestamps (DESIGN.md 5.1 / 5.5). This module exercises
``outbox.py`` against the **controller ruling** that overrides the brief's
self-contradictory overflow text:

* ``object_update`` is QoS 0 and is **never persisted** — ``enqueue(durable=False)``
  is a no-op for the file; the caller publishes those itself at QoS 0.
* The outbox persists **only durable** messages (object_start / object_end /
  cue-ack / node-status) at QoS 1.
* Overflow policy is **FIFO drop-oldest-first** when the on-disk file exceeds
  ``max_bytes`` or an entry's age exceeds ``max_age_s`` — there is no
  "evict object_update first" rule, because updates are never written.

Review-focus invariants proven here:

* replay re-sends the **byte-identical** bytes captured at enqueue, so
  ``event_id``/``timestamp`` are preserved (no re-mint, no re-serialize);
* overflow drops the **oldest** durable entries first, keeps the newest, and
  preserves order;
* a fresh ``Outbox`` on the same path reconstructs pending entries from disk and
  ``replay`` resends them (durability across a process restart).

Messages are built with the real ``formats`` builders so the queued bytes are the
actual ``camera.*`` contract payloads.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from camerabridge import formats
from camerabridge.outbox import Outbox

FIXED_TS = "2026-11-04T19:40:01Z"


class FakeClock:
    """A ready clock returning a fixed RFC-3339 ``Z`` timestamp for payloads."""

    def __init__(self, ts: str = FIXED_TS) -> None:
        self._ts = ts

    def now_rfc3339(self) -> str:
        return self._ts


class RecordingPublish:
    """A fake ``publish(topic, payload_bytes, qos) -> mid`` that records calls.

    Returns a fresh, monotonically increasing ``mid`` per call, mirroring the
    broker-assigned message id paho hands back from ``publish()``. The outbox
    maps that returned mid to the queued entry so ``ack(mid)`` can remove it.
    """

    def __init__(self, start_mid: int = 1000) -> None:
        self.calls: list[tuple[str, bytes, int]] = []
        self._next = start_mid

    def __call__(self, topic: str, payload: bytes, qos: int) -> int:
        self.calls.append((topic, payload, qos))
        mid = self._next
        self._next += 1
        return mid

    @property
    def topics(self) -> list[str]:
        return [t for (t, _p, _q) in self.calls]

    @property
    def payloads(self) -> list[bytes]:
        return [p for (_t, p, _q) in self.calls]

    @property
    def qoses(self) -> list[int]:
        return [q for (_t, _p, q) in self.calls]


def make_detection(event: str, i: int = 0, clock: FakeClock | None = None) -> dict:
    """Build a real ``camera.detection.v0`` payload with a minted ``event_id``.

    Fixed-width ``object_id`` and a fixed timestamp keep serialized lines equal
    length for the size-overflow test (UUIDv4 ``event_id`` is already 36 chars).
    """
    return formats.detection_event(
        node_id="cam-01",
        event=event,
        object_id=f"obj-{i:03d}",
        label="person",
        confidence=0.9,
        zones=["perimeter-north"],
        attributes={},
        clock=clock or FakeClock(),
    )


def read_event_ids(path: Path) -> list[str]:
    """Return the ``event_id`` of each persisted entry, in on-disk (FIFO) order."""
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        payload = json.loads(record["bytes"])
        ids.append(payload["event_id"])
    return ids


# --- enqueue: durable persists, non-durable (object_update) does not ----------


def test_durable_enqueue_writes_line_nondurable_does_not(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_update", 1), durable=False)
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    payload = json.loads(json.loads(lines[0])["bytes"])
    assert payload["event"] == "object_start"


def test_nondurable_only_persists_nothing(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_update", 1), durable=False)
    # Nothing on disk, and flush/replay publish nothing via the outbox.
    assert (not path.exists()) or path.read_text(encoding="utf-8").strip() == ""
    p = RecordingPublish()
    box.flush(p)
    assert p.calls == []
    box2 = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    p2 = RecordingPublish()
    box2.replay(p2)
    assert p2.calls == []


# --- flush: FIFO order, QoS 1 ------------------------------------------------


def test_flush_publishes_fifo_order_at_qos1(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    a = make_detection("object_start", 1)
    b = make_detection("object_end", 1)
    box.enqueue(formats.TOPIC_CUE_ACKS, a, durable=True)
    box.enqueue(formats.TOPIC_STATUS, b, durable=True)
    p = RecordingPublish()
    box.flush(p)
    assert p.topics == [formats.TOPIC_CUE_ACKS, formats.TOPIC_STATUS]
    assert p.qoses == [1, 1]
    assert [json.loads(x)["event_id"] for x in p.payloads] == [a["event_id"], b["event_id"]]


def test_flush_does_not_resend_already_sent(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    p = RecordingPublish()
    box.flush(p)
    box.flush(p)
    assert len(p.calls) == 1


# --- replay: byte-identical resend preserves event_id/timestamp across restart


def test_replay_preserves_bytes_and_ids_across_restart(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box1 = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    start = make_detection("object_start", 1)
    update = make_detection("object_update", 1)
    end = make_detection("object_end", 1)
    box1.enqueue(formats.TOPIC_DETECTIONS, start, durable=True)
    box1.enqueue(formats.TOPIC_DETECTIONS, update, durable=False)  # QoS 0, not persisted
    box1.enqueue(formats.TOPIC_DETECTIONS, end, durable=True)

    p1 = RecordingPublish()
    box1.flush(p1)
    # Only the two durable messages flush, in order, at QoS 1.
    assert len(p1.calls) == 2
    assert p1.qoses == [1, 1]
    assert [json.loads(x)["event"] for x in p1.payloads] == ["object_start", "object_end"]

    # Simulate a process restart: a brand-new Outbox on the same path.
    box2 = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    p2 = RecordingPublish(start_mid=5000)
    box2.replay(p2)

    # Byte-identical resend: no re-serialize, no re-stamp.
    assert p2.payloads == p1.payloads
    resent_start = json.loads(p2.payloads[0])
    resent_end = json.loads(p2.payloads[1])
    assert resent_start["event_id"] == start["event_id"]
    assert resent_start["timestamp"] == start["timestamp"] == FIXED_TS
    assert resent_end["event_id"] == end["event_id"]
    assert resent_end["timestamp"] == end["timestamp"]


# --- ack: removes the acked entry from the file ------------------------------


def test_ack_removes_entry_from_file(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    a = make_detection("object_start", 1)
    b = make_detection("object_end", 1)
    box.enqueue(formats.TOPIC_DETECTIONS, a, durable=True)
    box.enqueue(formats.TOPIC_DETECTIONS, b, durable=True)
    p = RecordingPublish()
    mids = box.flush(p)
    box.ack(mids[0])  # ack the first (object_start)

    # The acked entry is gone from disk; a fresh Outbox sees only the unacked one.
    assert read_event_ids(Path(path)) == [b["event_id"]]
    box2 = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    p2 = RecordingPublish()
    box2.replay(p2)
    assert [json.loads(x)["event_id"] for x in p2.payloads] == [b["event_id"]]


def test_replay_resends_only_unacked(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    a = make_detection("object_start", 1)
    b = make_detection("object_end", 1)
    box.enqueue(formats.TOPIC_DETECTIONS, a, durable=True)
    box.enqueue(formats.TOPIC_DETECTIONS, b, durable=True)
    p = RecordingPublish()
    mids = box.flush(p)
    box.ack(mids[0])
    p2 = RecordingPublish()
    box.replay(p2)
    assert [json.loads(x)["event_id"] for x in p2.payloads] == [b["event_id"]]


def test_ack_unknown_mid_is_noop(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    box.ack(987654)  # never issued
    p = RecordingPublish()
    box.flush(p)
    assert len(p.calls) == 1


# --- overflow: FIFO drop-oldest-first (ruling) -------------------------------


def test_overflow_drops_oldest_durable_first_fifo(tmp_path, caplog):
    const_time = lambda: 1000.0  # noqa: E731 - fixed ts keeps lines equal length

    # Measure one durable line's on-disk size with a throwaway probe.
    probe_path = tmp_path / "probe.jsonl"
    probe = Outbox(str(probe_path), max_bytes=10_000_000, max_age_s=1e9, time_fn=const_time)
    probe.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 99), durable=True)
    line_size = probe_path.stat().st_size
    assert line_size > 0

    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=3 * line_size, max_age_s=1e9, time_fn=const_time)
    msgs = [make_detection("object_start", i) for i in range(5)]
    eids = [m["event_id"] for m in msgs]
    with caplog.at_level(logging.WARNING):
        for m in msgs:
            box.enqueue(formats.TOPIC_DETECTIONS, m, durable=True)

    # Oldest-first dropped: only the newest three survive, order preserved.
    assert read_event_ids(path) == eids[2:]
    assert any("overflow" in r.getMessage() for r in caplog.records)


def test_overflow_by_age_drops_old_entries_first(tmp_path, caplog):
    clock = [0.0]
    time_fn = lambda: clock[0]  # noqa: E731

    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=10.0, time_fn=time_fn)
    old = make_detection("object_start", 0)
    box.enqueue(formats.TOPIC_DETECTIONS, old, durable=True)

    clock[0] = 100.0  # well past max_age_s for the old entry
    new = make_detection("object_start", 1)
    with caplog.at_level(logging.WARNING):
        box.enqueue(formats.TOPIC_DETECTIONS, new, durable=True)

    assert read_event_ids(path) == [new["event_id"]]
    assert any("overflow" in r.getMessage() for r in caplog.records)


def test_overflow_survives_restart(tmp_path):
    const_time = lambda: 1000.0  # noqa: E731

    probe_path = tmp_path / "probe.jsonl"
    probe = Outbox(str(probe_path), max_bytes=10_000_000, max_age_s=1e9, time_fn=const_time)
    probe.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 99), durable=True)
    line_size = probe_path.stat().st_size

    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=3 * line_size, max_age_s=1e9, time_fn=const_time)
    msgs = [make_detection("object_start", i) for i in range(5)]
    eids = [m["event_id"] for m in msgs]
    for m in msgs:
        box.enqueue(formats.TOPIC_DETECTIONS, m, durable=True)

    # A fresh Outbox reconstructs exactly the post-overflow survivors in order.
    box2 = Outbox(str(path), max_bytes=3 * line_size, max_age_s=1e9, time_fn=const_time)
    p2 = RecordingPublish()
    box2.replay(p2)
    assert [json.loads(x)["event_id"] for x in p2.payloads] == eids[2:]
