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
