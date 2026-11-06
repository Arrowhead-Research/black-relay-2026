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
import os
import stat
from pathlib import Path

import pytest

from camerabridge import formats
from camerabridge import outbox as outbox_module
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


# --- rewrites: what the SD card pays for an ack ------------------------------


def _big_box(tmp_path, count, **kwargs):
    path = tmp_path / "big.jsonl"
    box = Outbox(str(path), max_bytes=kwargs.pop("max_bytes", 64_000_000), max_age_s=1e9, **kwargs)
    box.enqueue_many(
        [(formats.TOPIC_DETECTIONS, make_detection("object_start", i)) for i in range(count)]
    )
    return path, box


def _event_ids_in(path):
    return [json.loads(json.loads(line)["bytes"])["event_id"] for line in path.read_text().splitlines()]


def test_acking_the_last_entry_empties_the_file_with_no_sync_or_new_file(tmp_path, monkeypatch):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    mids = box.flush(RecordingPublish())
    fsyncs, replaces = [], []
    monkeypatch.setattr(outbox_module.os, "fsync", lambda fd: fsyncs.append(fd))
    monkeypatch.setattr(outbox_module.os, "replace", lambda a, b: replaces.append(b))
    box.ack_many(mids)
    assert path.read_bytes() == b""
    assert fsyncs == [] and replaces == []


def test_an_enqueue_still_syncs_before_it_can_be_sent(tmp_path, monkeypatch):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path), max_bytes=10_000_000, max_age_s=1e9)
    events = []
    real_fsync = outbox_module.os.fsync
    monkeypatch.setattr(
        outbox_module.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1]
    )
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", 1), durable=True)
    publish = RecordingPublish()
    real_call = publish.__call__
    box.flush(lambda *args: (events.append("publish"), real_call(*args))[1])
    assert events == ["fsync", "publish"]


def test_a_rewrite_of_a_real_backlog_still_syncs_before_it_replaces(tmp_path, monkeypatch):
    # An empty file can lose nothing, but this holds entries that are not on any broker.
    path, box = _big_box(tmp_path, 40)
    mids = box.flush(RecordingPublish())
    events = []
    real_fsync, real_replace = outbox_module.os.fsync, outbox_module.os.replace
    monkeypatch.setattr(
        outbox_module.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        outbox_module.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )
    box.ack_many(mids)
    assert events == ["fsync", "replace", "fsync"]  # the file, the swap, the directory


def test_a_large_replay_writes_a_small_multiple_of_the_backlog(tmp_path, monkeypatch):
    path, box = _big_box(tmp_path, 2000)
    backlog = path.stat().st_size
    assert backlog > 16 * outbox_module._DEAD_MIN_BYTES
    written = [0]
    real = outbox_module.os.replace

    def counting(source, target):
        written[0] += os.path.getsize(source)
        real(source, target)

    monkeypatch.setattr(outbox_module.os, "replace", counting)
    publish = RecordingPublish()
    box.replay(publish)
    sent = 0
    while box._entries:
        mids = list(box._inflight)
        sent += len(mids)
        for mid in mids:  # one at a time, as paho's network thread delivers them
            box.ack(mid)
            box.flush(publish)
    assert sent == 2000
    assert path.read_bytes() == b""
    assert written[0] < 20 * backlog


def test_a_crash_during_a_replay_loses_nothing_and_resends_only_a_bounded_tail(tmp_path):
    path, box = _big_box(tmp_path, 2000)
    all_ids = _event_ids_in(path)
    backlog = path.stat().st_size
    publish = RecordingPublish()
    box.replay(publish)
    acked = 0
    while acked < 700:
        mids = list(box._inflight)
        box.ack_many(mids)
        acked += len(mids)
        box.flush(publish)

    # The process dies here: a new Outbox sees only the file.
    reopened = Outbox(str(path), max_bytes=64_000_000, max_age_s=1e9)
    on_disk = [json.loads(e.payload)["event_id"] for e in reopened._entries.values()]
    still_owed = all_ids[acked:]
    assert on_disk[-len(still_owed):] == still_owed
    assert on_disk == all_ids[all_ids.index(on_disk[0]):]
    allowed = max(min(outbox_module._DEAD_MAX_BYTES, backlog // 8), outbox_module._DEAD_MIN_BYTES)
    assert sum(entry.size for entry in reopened._entries.values()) - box._live_bytes <= allowed


def test_new_entries_after_kept_lines_survive_a_crash_in_order(tmp_path):
    path, box = _big_box(tmp_path, 2000)
    publish = RecordingPublish()
    box.replay(publish)
    box.ack_many(list(box._inflight))
    assert box._dead_bytes > 0
    message = make_detection("object_end", 9999)
    box.enqueue(formats.TOPIC_DETECTIONS, message, durable=True)

    reopened = Outbox(str(path), max_bytes=64_000_000, max_age_s=1e9)
    ids = [json.loads(e.payload)["event_id"] for e in reopened._entries.values()]
    assert ids[-1] == message["event_id"]
    assert len(ids) == len(set(ids))


def test_the_file_never_passes_max_bytes_even_while_it_keeps_acked_lines(tmp_path):
    path = tmp_path / "cap.jsonl"
    probe_line = len(json.dumps(make_detection("object_start", 0)))
    cap = 1000 * probe_line
    box = Outbox(str(path), max_bytes=cap, max_age_s=1e9)
    publish = RecordingPublish()
    for i in range(1500):
        box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_start", i), durable=True)
        assert path.stat().st_size <= cap
        if i % 7 == 0:
            box.flush(publish)
            box.ack_many(list(box._inflight)[:1])
            assert path.stat().st_size <= cap


def test_age_shed_lines_may_wait_for_a_rewrite_but_never_replay_in_memory(tmp_path):
    clock = [0.0]
    path, box = _big_box(tmp_path, 2000, time_fn=lambda: clock[0])
    box._max_age_s = 10.0
    clock[0] = 5.0
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_end", 1), durable=True)
    clock[0] = 12.0  # only the first 2000 are too old
    box.enqueue(formats.TOPIC_DETECTIONS, make_detection("object_end", 2), durable=True)
    assert len(box._entries) == 2
    publish = RecordingPublish()
    box.replay(publish)
    assert len(publish.payloads) == 2


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


# --- torn tails and corrupt lines -------------------------------------------


def _pending_ids(box: Outbox) -> list[str]:
    return [json.loads(entry.payload)["event_id"] for entry in box._entries.values()]


def _outbox_at(path: Path) -> Outbox:
    return Outbox(str(path), max_bytes=64_000_000, max_age_s=1e9)


def _seed(path: Path, count: int) -> list[str]:
    box = _outbox_at(path)
    ids = []
    for i in range(count):
        message = make_detection("object_start", i)
        box.enqueue(formats.TOPIC_DETECTIONS, message, durable=True)
        ids.append(message["event_id"])
    return ids


def _intact_ids(data: bytes, ids: list[str]) -> list[str]:
    """The ids whose whole JSON line, newline aside, lies inside ``data``."""
    complete, _, tail = data.rpartition(b"\n")
    whole_lines = len(complete.splitlines()) if complete else 0
    try:
        json.loads(tail)
    except ValueError:
        return ids[:whole_lines]
    return ids[: whole_lines + 1]


def _enqueue_new(box: Outbox, i: int) -> str:
    message = make_detection("object_end", i)
    box.enqueue(formats.TOPIC_DETECTIONS, message, durable=True)
    return message["event_id"]


def test_no_cut_point_can_lose_an_intact_entry_across_two_crashes(tmp_path, monkeypatch):
    """Every byte cut of an append, then a crash, then every byte cut of the next one.

    The first crash leaves ``base[:cut]``. The node restarts and durably appends a new
    entry, and a second crash tears the append after it at every byte. After each
    restart every entry that was intact must still be there, in order. Short entries
    keep the number of pairs testable. fsync is a no-op here only for speed: the next
    test keeps it real, and another checks that a repair syncs.
    """

    def short_ids(box: Outbox) -> list[str]:
        return [json.loads(entry.payload)["id"] for entry in box._entries.values()]

    def put(box: Outbox, event_id: str) -> None:
        box.enqueue("t", {"id": event_id}, durable=True)

    monkeypatch.setattr(outbox_module.os, "fsync", lambda fd: None)
    logging.disable(logging.CRITICAL)
    try:
        path = tmp_path / "cut.jsonl"
        seed = _outbox_at(path)
        put(seed, "a")
        put(seed, "b")
        base = path.read_bytes()

        template = tmp_path / "template.jsonl"
        template_box = _outbox_at(template)
        template_box._next_mid = 1000  # the id it would have had, clear of the others'
        put(template_box, "second")
        second_line = template.read_bytes()

        lost = []
        checked = 0
        for cut in range(len(base) + 1):
            intact = _intact_ids(base[:cut], ["a", "b"])

            path.write_bytes(base[:cut])
            put(_outbox_at(path), "first")
            after_first = path.read_bytes()

            for second_cut in range(len(second_line) + 1):
                torn_second = second_line[:second_cut]
                path.write_bytes(after_first + torn_second)
                box = _outbox_at(path)
                survivors = [*intact, "first"]
                if second_cut >= len(second_line) - 1:  # whole, with or without its newline
                    survivors.append("second")
                checked += 1
                if short_ids(box) != survivors:
                    lost.append((cut, second_cut))
                    continue
                put(box, "third")
                if short_ids(_outbox_at(path)) != [*survivors, "third"]:
                    lost.append((cut, second_cut))
    finally:
        logging.disable(logging.NOTSET)
    assert checked == (len(base) + 1) * (len(second_line) + 1)
    assert not lost, f"{len(lost)} of {checked} cut pairs lost an intact entry: {lost[:5]}"


def test_every_single_cut_with_real_fsyncs_loses_nothing_after_a_second_crash(tmp_path):
    path = tmp_path / "cut.jsonl"
    ids = _seed(path, 3)
    base = path.read_bytes()
    lost = []
    for cut in range(len(base) + 1):
        path.write_bytes(base[:cut])
        new_id = _enqueue_new(_outbox_at(path), 1)
        expected = _intact_ids(base[:cut], ids) + [new_id]
        if _pending_ids(_outbox_at(path)) != expected:
            lost.append(cut)
    assert not lost, f"{len(lost)} of {len(base) + 1} cuts lost the next entry: {lost[:5]}"


def test_a_torn_tail_is_gone_from_the_file_durably_before_the_constructor_returns(
    tmp_path, monkeypatch
):
    path = tmp_path / "torn.jsonl"
    _seed(path, 2)
    whole = path.read_bytes()
    first_line_end = whole.index(b"\n") + 1
    path.write_bytes(whole[:-5])

    seen_at_fsync = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        seen_at_fsync.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(outbox_module.os, "fsync", recording_fsync)
    _outbox_at(path)
    assert path.read_bytes() == whole[:first_line_end]
    assert first_line_end in seen_at_fsync, "the truncation was not fsynced"


def test_a_complete_entry_missing_only_its_newline_is_kept_and_terminated(tmp_path):
    path = tmp_path / "newline.jsonl"
    ids = _seed(path, 2)
    whole = path.read_bytes()
    path.write_bytes(whole[:-1])
    box = _outbox_at(path)
    assert _pending_ids(box) == ids
    assert path.read_bytes() == whole
    assert not path.with_name(path.name + ".corrupt").exists()


def test_a_nul_filled_tail_does_not_swallow_the_next_entry(tmp_path):
    path = tmp_path / "nul.jsonl"
    ids = _seed(path, 2)
    path.write_bytes(path.read_bytes() + b"\0" * 4096)
    new_id = _enqueue_new(_outbox_at(path), 1)
    assert _pending_ids(_outbox_at(path)) == ids + [new_id]


def test_acked_lines_the_file_still_holds_behave_as_before_with_a_torn_tail(tmp_path):
    """Compaction may keep acked lines. A repair must neither drop nor reorder them."""
    path, box = _big_box(tmp_path, 2000)
    publish = RecordingPublish()
    box.replay(publish)
    box.ack_many(list(box._inflight))
    assert box._dead_bytes > 0
    kept = path.read_bytes()

    path.write_bytes(kept + b'{"mid":99999,"topic":"det')
    reopened = _outbox_at(path)
    assert path.read_bytes() == kept
    assert len(reopened._entries) == 2000  # the acked lines replay, as after any crash


def test_a_clean_file_is_not_touched_on_load(tmp_path, monkeypatch):
    path = tmp_path / "clean.jsonl"
    ids = _seed(path, 3)
    before = path.stat()
    fsyncs = []
    monkeypatch.setattr(outbox_module.os, "fsync", fsyncs.append)
    box = _outbox_at(path)
    assert _pending_ids(box) == ids
    assert fsyncs == []
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert not path.with_name(path.name + ".corrupt").exists()


def test_a_failed_repair_does_not_stop_the_bridge_and_is_retried_before_the_next_append(
    tmp_path, monkeypatch
):
    path = tmp_path / "retry.jsonl"
    ids = _seed(path, 2)
    path.write_bytes(path.read_bytes() + b'{"mid":3,"top')

    real_fsync = os.fsync
    failing = [True]

    def flaky_fsync(fd):
        if failing[0]:
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(outbox_module.os, "fsync", flaky_fsync)
    box = _outbox_at(path)
    assert _pending_ids(box) == ids

    failing[0] = False
    new_id = _enqueue_new(box, 1)
    assert _pending_ids(_outbox_at(path)) == ids + [new_id]


def test_an_append_that_fails_partway_cannot_merge_with_the_next_one(tmp_path, monkeypatch):
    """A full disk tears an append in a running bridge as well as across a restart."""
    path = tmp_path / "enospc.jsonl"
    ids = _seed(path, 1)
    box = _outbox_at(path)
    real_fsync = os.fsync
    failing = [True]

    def flaky_fsync(fd):
        if failing[0]:
            raise OSError(28, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(outbox_module.os, "fsync", flaky_fsync)
    try:
        _enqueue_new(box, 1)
    except OSError:
        pass
    else:
        raise AssertionError("the append should have failed")
    failing[0] = False
    after = _enqueue_new(box, 2)

    reloaded = _pending_ids(_outbox_at(path))
    assert reloaded[0] == ids[0]
    assert reloaded[-1] == after


def _bad_bytes_at(where: str, lines: list[bytes], bad: bytes) -> bytes:
    if where == "start":
        return bad + b"\n" + b"".join(lines)
    if where == "middle":
        return lines[0] + bad + b"\n" + b"".join(lines[1:])
    if where == "end":
        return b"".join(lines) + bad + b"\n"
    return b"".join(lines) + bad  # torn: the bad bytes have no newline


@pytest.mark.parametrize("where", ["start", "middle", "end", "torn"])
@pytest.mark.parametrize(
    "bad",
    [
        b"\xff\xfe\x80garbage",
        b"\x80",
        b'{"mid":7,"topic":"t\xff","qos":1,"ts":1.0,"bytes":"{}"}',
        b"not json",
        b"null",
        b"[1,2]",
        b'{"mid":1e999,"topic":"t","qos":1,"ts":1.0,"bytes":"{}"}',
        b'{"mid":7,"topic":"t","qos":1,"ts":1.0,"bytes":5}',
        b'{"mid":7,"topic":5,"qos":1,"ts":1.0,"bytes":"{}"}',
        b'{"mid":7,"topic":"","qos":1,"ts":1.0,"bytes":"{}"}',
        b'{"mid":7,"topic":"a/#","qos":1,"ts":1.0,"bytes":"{}"}',
        b'{"mid":7,"topic":"t","qos":7,"ts":1.0,"bytes":"{}"}',
        b'{"mid":7,"topic":"t","qos":1,"ts":1.0,"bytes":"\\ud800"}',
        b"[" * 200_000,
        b"\0" * 64,
    ],
    ids=lambda value: value[:12].decode("latin-1") if isinstance(value, bytes) else value,
)
def test_corrupt_lines_keep_every_intact_entry_and_move_aside(tmp_path, where, bad):
    path = tmp_path / "bad.jsonl"
    ids = _seed(path, 3)
    whole = path.read_bytes()
    lines = whole.splitlines(keepends=True)
    path.write_bytes(_bad_bytes_at(where, lines, bad))

    box = _outbox_at(path)

    assert _pending_ids(box) == ids
    assert path.read_bytes() == whole
    assert bad in path.with_name(path.name + ".corrupt").read_bytes()
    new_id = _enqueue_new(box, 1)
    assert _pending_ids(_outbox_at(path)) == ids + [new_id]


def test_corrupt_lines_are_counted_in_the_log_without_their_content(tmp_path, caplog):
    path = tmp_path / "log.jsonl"
    ids = _seed(path, 2)
    lines = path.read_bytes().splitlines(keepends=True)
    secret = b"SECRET-PAYLOAD"
    path.write_bytes(
        b'{"mid":9,"bytes":"SECRET-PAYLOAD"\n' + lines[0] + secret + b"\xff\n" + lines[1] + secret
    )
    with caplog.at_level(logging.WARNING, logger="camerabridge.outbox"):
        box = _outbox_at(path)
    assert _pending_ids(box) == ids
    assert "SECRET" not in caplog.text
    assert any("3 corrupt" in record.getMessage() for record in caplog.records)
    corrupt = path.with_name(path.name + ".corrupt")
    assert corrupt.read_bytes().count(b"SECRET-PAYLOAD") == 3
    assert stat.S_IMODE(corrupt.stat().st_mode) == 0o600


def test_a_file_of_nothing_but_corruption_ends_empty_and_durable(tmp_path, monkeypatch):
    path = tmp_path / "all-bad.jsonl"
    path.write_bytes(b"\xff\xfe\n\x80\n")
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(outbox_module.os, "fsync", recording_fsync)
    box = _outbox_at(path)
    assert box._entries == {}
    assert path.read_bytes() == b""
    assert 0 in synced


def test_a_full_disk_while_moving_aside_still_repairs_the_outbox(tmp_path, caplog):
    path = tmp_path / "aside.jsonl"
    ids = _seed(path, 2)
    whole = path.read_bytes()
    path.write_bytes(whole + b"\xff\xfe")
    path.with_name(path.name + ".corrupt").mkdir()  # any write to it fails
    with caplog.at_level(logging.WARNING, logger="camerabridge.outbox"):
        box = _outbox_at(path)
    assert _pending_ids(box) == ids
    assert path.read_bytes() == whole
    assert "could not" in caplog.text


def test_the_aside_file_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox_module, "_CORRUPT_KEEP_BYTES", 100)
    path = tmp_path / "cap.jsonl"
    corrupt = path.with_name(path.name + ".corrupt")
    for _ in range(5):
        path.write_bytes(b"\xff" * 60 + b"\n")
        _outbox_at(path)
    assert corrupt.stat().st_size <= 100


def test_the_rename_of_a_rewrite_is_durable_before_it_returns(tmp_path, monkeypatch):
    path = tmp_path / "rename.jsonl"
    _seed(path, 3)
    box = _outbox_at(path)
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def recording_fsync(fd):
        calls.append(("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file"))
        real_fsync(fd)

    def recording_replace(src, dst):
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(outbox_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(outbox_module.os, "replace", recording_replace)
    box._rewrite()
    assert calls == ["fsync-file", "replace", "fsync-dir"]
