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


# --- ack id namespacing: a stray/colliding mid never evicts the wrong entry ---


def test_stray_puback_colliding_with_entry_mid_does_not_evict(tmp_path):
    path = str(tmp_path / "outbox.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    a = make_detection("object_start", 1)
    box.enqueue(formats.TOPIC_DETECTIONS, a, durable=True)  # entry's own mid == 1
    # Broker mids start well above entry mids, so the in-flight map is {1000: 1}.
    p = RecordingPublish(start_mid=1000)
    mids = box.flush(p)

    # A stray/duplicate PUBACK whose mid (1) collides with the entry's OWN internal
    # id must NOT evict it — only the genuine broker mid may. (Pre-fix, the
    # entry-own-mid fallback would have wrongly removed entry 1 here.)
    box.ack(1)
    assert read_event_ids(Path(path)) == [a["event_id"]]

    # The real broker ack still removes it.
    box.ack(mids[0])
    assert read_event_ids(Path(path)) == []


# --- overflow: a lone oversized entry is dropped with a DISTINCT warning -------


def test_oversized_single_entry_logs_distinct_warning(tmp_path, caplog):
    path = tmp_path / "outbox.jsonl"
    # A cap far smaller than one serialized durable line: the lone entry can't fit.
    box = Outbox(str(path), max_bytes=10, max_age_s=1e9)
    with caplog.at_level(logging.WARNING):
        box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)

    # It is still dropped (the cap is honoured) ...
    assert read_event_ids(path) == []
    # ... but a DISTINCT warning names the single-entry-exceeds-cap case, so a lone
    # oversized durable event is not hidden inside the normal FIFO-overflow message.
    messages = [r.getMessage() for r in caplog.records]
    assert any("single durable entry" in m and "exceeds max_bytes" in m for m in messages)


# --- the size check: a running total that must stay exact ---------------------


def _file_size(path) -> int:
    return path.stat().st_size if path.exists() else 0


def test_the_running_size_matches_the_file_through_every_operation(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    assert box._live_bytes == _file_size(path)
    box.enqueue_many(
        [(formats.TOPIC_DETECTIONS, make_detection("object_end", i)) for i in range(5)]
    )
    assert box._live_bytes == _file_size(path)
    mids = box.flush(RecordingPublish())
    box.ack_many(mids[:2])
    assert box._live_bytes == _file_size(path)
    reopened = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    assert reopened._live_bytes == box._live_bytes
    box.ack_many(mids[2:])
    assert box._live_bytes == 0


def test_a_repeated_mid_in_the_file_is_not_counted_twice(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    line = path.read_text()
    path.write_text(line + line)
    reopened = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    assert reopened._live_bytes == len(line)


def test_a_payload_outside_ascii_is_sized_as_it_is_written(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    message = make_detection("object_start", 1)
    message["label"] = "caf\u00e9 \U0001f600"
    box.enqueue(formats.TOPIC_DETECTIONS, message, durable=True)
    assert box._live_bytes == _file_size(path)


def test_enqueue_does_not_serialize_the_backlog_again(tmp_path, monkeypatch):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue_many(
        [(formats.TOPIC_DETECTIONS, make_detection("object_start", i)) for i in range(200)]
    )
    calls = []
    real = box._line
    monkeypatch.setattr(box, "_line", lambda entry: (calls.append(1), real(entry))[1])
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_end", 1), durable=True)
    assert len(calls) <= 2


# --- enqueue_many: one fsync and one overflow pass for a whole batch ---------


def _batch_msg(i: int) -> dict:
    return {"event_id": f"e{i}", "payload": "x" * 20}


def test_enqueue_many_persists_in_order_across_restart(tmp_path):
    path = str(tmp_path / "o.jsonl")
    box = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue("t", _batch_msg(0), durable=True)
    box.enqueue_many([("t", _batch_msg(1)), ("u", _batch_msg(2))])

    restarted = Outbox(path, max_bytes=10_000_000, max_age_s=1e9)
    sent = []
    restarted.replay(lambda topic, payload, qos: sent.append((topic, payload, qos)) or len(sent))
    assert [(t, json.loads(p)["event_id"], q) for t, p, q in sent] == [
        ("t", "e0", 1),
        ("t", "e1", 1),
        ("u", "e2", 1),
    ]


def test_enqueue_many_entries_flush_and_ack_like_enqueued_ones(tmp_path):
    box = Outbox(str(tmp_path / "o.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue_many([("t", _batch_msg(1)), ("t", _batch_msg(2))])
    sent = []
    box.flush(lambda topic, payload, qos: sent.append(payload) or len(sent))
    assert len(sent) == 2
    box.ack(1)
    box.ack(2)
    assert Outbox(str(tmp_path / "o.jsonl"), max_bytes=10_000_000, max_age_s=1e9).flush(
        lambda *_a: 1
    ) == []


def test_enqueue_many_enforces_overflow_oldest_first(tmp_path):
    box = Outbox(str(tmp_path / "o.jsonl"), max_bytes=400, max_age_s=1e9)
    box.enqueue_many([("t", _batch_msg(i)) for i in range(20)])
    sent = []
    box.flush(lambda topic, payload, qos: sent.append(json.loads(payload)["event_id"]) or len(sent))
    assert sent and len(sent) < 20
    assert sent[-1] == "e19"  # the newest survive


def test_enqueue_many_fsyncs_once(tmp_path, monkeypatch):
    import camerabridge.outbox as outbox_module

    calls = []
    real = outbox_module.os.fsync
    monkeypatch.setattr(outbox_module.os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    box = Outbox(str(tmp_path / "o.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue_many([("t", _batch_msg(i)) for i in range(50)])
    assert len(calls) == 1


def test_enqueue_many_empty_is_a_noop(tmp_path):
    Outbox(str(tmp_path / "o.jsonl"), max_bytes=10, max_age_s=1e9).enqueue_many([])
    assert not (tmp_path / "o.jsonl").exists()


# --- send window -------------------------------------------------------------


def _filled(tmp_path, count, *, name="w.jsonl"):
    box = Outbox(str(tmp_path / name), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue_many([("t", _batch_msg(i)) for i in range(count)])
    return box


def _ids(publish) -> list[str]:
    return [json.loads(p)["event_id"] for p in publish.payloads]


def test_flush_sends_at_most_a_window_of_unacked_entries(tmp_path):
    box = _filled(tmp_path, 50)
    p = RecordingPublish()
    box.flush(p)
    assert _ids(p) == [f"e{i}" for i in range(20)]
    box.flush(p)
    assert len(p.calls) == 20  # the window is full: nothing more until acks


def test_acks_open_the_window_in_fifo_order(tmp_path):
    box = _filled(tmp_path, 50)
    p = RecordingPublish()
    mids = box.flush(p)
    box.ack_many(mids[:3])
    box.flush(p)
    assert _ids(p) == [f"e{i}" for i in range(23)]


def test_replay_sends_a_window_from_the_oldest_unacked(tmp_path):
    box = _filled(tmp_path, 50)
    p1 = RecordingPublish()
    mids = box.flush(p1)
    box.ack(mids[0])

    p2 = RecordingPublish(start_mid=5000)
    box.replay(p2)
    assert _ids(p2) == [f"e{i}" for i in range(1, 21)]


def test_rejected_publish_is_not_marked_sent_and_stops_the_flush(tmp_path):
    box = _filled(tmp_path, 3)
    calls = []

    def rejecting(topic, payload, qos):
        calls.append(payload)
        return None

    assert box.flush(rejecting) == []
    assert len(calls) == 1  # stopped at the head, so nothing overtakes it

    p = RecordingPublish()
    box.flush(p)
    assert _ids(p) == ["e0", "e1", "e2"]  # the head was not marked sent


# --- ack_many: one rewrite per batch -----------------------------------------


def test_ack_many_removes_all_with_one_rewrite(tmp_path, monkeypatch):
    box = _filled(tmp_path, 10)
    mids = box.flush(RecordingPublish())
    calls = []
    real = box._rewrite
    monkeypatch.setattr(box, "_rewrite", lambda: (calls.append(1), real())[1])
    box.ack_many(mids[:7])
    assert len(calls) == 1
    p = RecordingPublish()
    Outbox(str(tmp_path / "w.jsonl"), max_bytes=10_000_000, max_age_s=1e9).replay(p)
    assert _ids(p) == ["e7", "e8", "e9"]


def test_ack_many_with_only_unknown_mids_does_not_rewrite(tmp_path, monkeypatch):
    box = _filled(tmp_path, 2)
    box.flush(RecordingPublish())
    calls = []
    monkeypatch.setattr(box, "_rewrite", lambda: calls.append(1))
    box.ack_many([999_999, 888_888])
    box.ack_many([])
    assert calls == []
