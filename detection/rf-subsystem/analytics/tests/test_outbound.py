"""Outbound wire format and durable outbox."""

from __future__ import annotations

import json

from rfanalytics.alerts import Message
from rfanalytics.formats import to_wire
from rfanalytics.outbox import Outbox

from conftest import T0


class FakeBroker:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[tuple[int, str, dict]] = []
        self._mid = 0

    def publish(self, topic: str, payload: str):
        if not self.connected:
            return None
        self._mid += 1
        self.sent.append((self._mid, topic, json.loads(payload)))
        return self._mid


def test_wire_format_topics_and_envelope():
    msg = Message("alert", T0.timestamp(), {"rule_id": "loiter", "severity": "medium"})
    topic, payload = to_wire(msg)
    assert topic == "detection/rf/alerts"
    assert payload["schema"] == "rf.alert.v0"
    assert payload["id"] == msg.id
    assert payload["timestamp"] == "2026-11-02T12:00:00.000Z"
    assert payload["rule_id"] == "loiter"
    assert to_wire(Message("camera_cue", 0, {}))[0] == "detection/rf/cues/camera"
    assert to_wire(Message("node_status", 0, {}), "test/rf")[0] == "test/rf/status/nodes"


def test_outbox_removes_only_acknowledged(tmp_path):
    box, broker = Outbox(tmp_path / "out.jsonl"), FakeBroker()
    box.add("a", "t", {"n": 1})
    box.add("b", "t", {"n": 2})
    box.send_pending(broker)
    assert len(broker.sent) == 2
    box.acknowledge(broker.sent[0][0])
    assert len(box) == 1
    box.send_pending(broker)
    assert len(broker.sent) == 2, "in-flight messages are not re-sent while connected"


def test_outbox_survives_restart_and_replays(tmp_path):
    path = tmp_path / "out.jsonl"
    box = Outbox(path)
    box.add("a", "detection/rf/alerts", {"n": 1})
    box.send_pending(FakeBroker(connected=False))  # broker down
    assert len(box) == 1

    restarted, broker = Outbox(path), FakeBroker()
    assert len(restarted) == 1
    restarted.send_pending(broker)
    assert broker.sent[0][1:] == ("detection/rf/alerts", {"n": 1})
    restarted.acknowledge(broker.sent[0][0])
    assert len(Outbox(path)) == 0


def test_outbox_resends_in_flight_after_disconnect(tmp_path):
    box, broker = Outbox(tmp_path / "out.jsonl"), FakeBroker()
    box.add("a", "t", {"n": 1})
    box.send_pending(broker)
    box.connection_lost()  # no ack arrived before the connection dropped
    box.send_pending(broker)
    assert len(broker.sent) == 2
    box.acknowledge(broker.sent[1][0])
    assert len(box) == 0


def test_ack_during_publish_does_not_deadlock(tmp_path):
    """Regression: paho calls on_publish (-> acknowledge) while holding the
    mutex that publish() needs. Holding the outbox lock across publish()
    deadlocked the live service when two alerts were sent back to back."""
    import threading
    import time

    box = Outbox(tmp_path / "out.jsonl")
    client_mutex = threading.Lock()  # stands in for paho's _out_message_mutex
    holding = threading.Event()
    mids = iter(range(1, 100))

    class PahoLike:
        def publish(self, topic, payload):
            with client_mutex:
                mid = next(mids)
            if mid == 1:
                threading.Thread(target=network_thread_ack, args=(mid,), daemon=True).start()
                holding.wait(1)
            return mid

    def network_thread_ack(mid):
        with client_mutex:  # PUBACK handled under the client mutex
            holding.set()
            time.sleep(0.2)  # main thread starts the next publish meanwhile
            box.acknowledge(mid)

    def send_two():
        box.add("a", "t", {"n": 1})
        box.send_pending(PahoLike())
        box.add("b", "t", {"n": 2})
        box.send_pending(PahoLike())

    worker = threading.Thread(target=send_two, daemon=True)
    worker.start()
    worker.join(3)
    assert not worker.is_alive(), "send_pending deadlocked against an acknowledgement"
    assert len(box) == 1  # "a" acknowledged, "b" still in flight
    box.acknowledge(2)
    assert len(box) == 0


def test_ack_before_mid_registered_is_applied(tmp_path):
    box = Outbox(tmp_path / "out.jsonl")

    class InstantAck:
        def publish(self, topic, payload):
            box.acknowledge(7)  # broker acks before publish() returns the mid
            return 7

    box.add("a", "t", {"n": 1})
    box.send_pending(InstantAck())
    assert len(box) == 0
