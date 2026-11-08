"""Outbound positions and node status: throttling, node states, and the
contract (every payload validates against schemas/*.schema.json)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publish import NODE_HEARTBEAT_S, POS_MAX_S, Publisher, node_states  # noqa: E402

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def validator(name):
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


POSITION, NODE_STATUS = validator("position.v0"), validator("node-status.v0")
SITE = {"id": "test", "name": "Test site"}


class FakeLive:
    def __init__(self, up=True):
        self.up, self.sent = up, []

    def publish(self, topic, payload):
        if self.up:
            self.sent.append((topic, json.loads(payload)))
        return self.up


class FakeDurable:
    def __init__(self):
        self.sent = []

    def send(self, msg_id, topic, payload):
        self.sent.append((topic, payload))


def pos(x, y, t, updated=True, **extra):
    return {"x": x, "y": y, "t": t, "updated": updated, "nodes": 3, "ids": ["wbt-1", "wbt-2", "wbt-3"],
            "calibrated": False, "err_ft": None, **extra}


DEV = {"id": "aa:bb:cc:00:00:01", "type": "wifi_ap", "band": "wifi", "name": "lab", "manufacturer": None, "randomized": False}
RADIO = {**DEV, "id": "dc:a6:32:00:00:01", "type": "wifi_client"}


def test_positions_are_throttled_and_skip_node_radios():
    live = FakeLive()
    pub = Publisher(live, FakeDurable())
    devs = {DEV["id"]: DEV, RADIO["id"]: RADIO}
    skip = {RADIO["id"]}
    pub.positions({DEV["id"]: pos(1, 2, 1000), RADIO["id"]: pos(0, 0, 1000)}, devs, SITE, skip, now=1000)
    assert [p["emitter"]["id"] for _, p in live.sent] == [DEV["id"]]            # node radio not published
    topic, payload = live.sent[0]
    assert topic == "detection/rf/positions"
    POSITION.validate(payload)
    assert payload["timestamp"].endswith("Z") and payload["position"]["error_ft"] is None
    pub.positions({DEV["id"]: pos(5, 2, 1001)}, devs, SITE, skip, now=1001)       # moved, but too soon
    pub.positions({DEV["id"]: pos(1.2, 2, 1005)}, devs, SITE, skip, now=1005)     # later, but hardly moved
    pub.positions({DEV["id"]: pos(9, 9, 1006, updated=False)}, devs, SITE, skip, now=1006)  # no new readings
    assert len(live.sent) == 1
    pub.positions({DEV["id"]: pos(4, 2, 1007)}, devs, SITE, skip, now=1007)       # moved 3 ft
    pub.positions({DEV["id"]: pos(4, 2, 1007 + POS_MAX_S)}, devs, SITE, skip, now=1007 + POS_MAX_S)  # still: heartbeat
    assert len(live.sent) == 3


def test_positions_with_lat_lon_validate_and_are_dropped_while_disconnected():
    live = FakeLive(up=False)
    pub = Publisher(live, FakeDurable())
    p = pos(1, 2, 1000, calibrated=True, err_ft=6.4, lat=45.0001, lon=10.0002, err_m=2.1)
    pub.positions({DEV["id"]: p}, {DEV["id"]: DEV}, SITE, set(), now=1000)
    assert pub.stats["positions_dropped"] == 1 and live.sent == []
    live.up = True                                            # reconnected: sent on the next update, not replayed
    pub.positions({DEV["id"]: p}, {DEV["id"]: DEV}, SITE, set(), now=1003)
    payload = live.sent[0][1]
    POSITION.validate(payload)
    assert payload["position"] == {"x_ft": 1, "y_ft": 2, "calibrated": True, "error_ft": 6.4,
                                   "lat": 45.0001, "lon": 10.0002, "error_m": 2.1}


def test_node_states_from_capture_sources():
    report = [{"name": "wbt-1:wifi", "status": "ok"}, {"name": "wbt-1:bt", "status": "ok"},
              {"name": "wbt-2:wifi", "status": "ok"}, {"name": "wbt-2:bt", "status": "silent"},
              {"name": "wbt-3:wifi", "status": "stopped"}, {"name": "wbt-3:bt", "status": "error"},
              {"name": "orphan", "status": "ok"}]
    st = node_states(report)
    assert {k: v["state"] for k, v in st.items()} == {"wbt-1": "online", "wbt-2": "degraded", "wbt-3": "offline"}
    assert st["wbt-2"]["sources"] == {"bt": "silent", "wifi": "ok"}


def test_node_status_on_change_and_heartbeat_through_the_outbox():
    durable = FakeDurable()
    pub = Publisher(FakeLive(), durable)
    ok = [{"name": "wbt-1:wifi", "status": "ok"}, {"name": "wbt-1:bt", "status": "ok"}]
    pub.nodes(ok, SITE, now=1000)
    pub.nodes(ok, SITE, now=1010)                                                   # unchanged: nothing
    pub.nodes([ok[0], {"name": "wbt-1:bt", "status": "silent"}], SITE, now=1020)   # changed
    pub.nodes([ok[0], {"name": "wbt-1:bt", "status": "silent"}], SITE, now=1020 + NODE_HEARTBEAT_S)
    assert [p["state"] for _, p in durable.sent] == ["online", "degraded", "degraded"]
    for topic, payload in durable.sent:
        assert topic == "detection/rf/status/nodes"
        NODE_STATUS.validate(payload)


def test_rf_analytics_node_status_matches_the_same_schema():
    from rfanalytics.alerts import Message
    from rfanalytics.formats import to_wire
    _, payload = to_wire(Message("node_status", 1000.0, {"node_id": "wbt-2", "state": "offline", "last_heard": None}))
    NODE_STATUS.validate(payload)


def test_console_publishes_on_poll(tmp_path):
    """End to end: a poll places the fake Kismet's device and publishes it, plus node status."""
    from test_server import LAYOUT, FakePoller
    from server import App, load_layout
    live, durable = FakeLive(), FakeDurable()
    app = App(FakePoller(), load_layout(LAYOUT), window_s=60, poll_s=3, publisher=Publisher(live, durable))
    app.health.update([{"uuid": "u1", "name": "wbt-1:wifi", "packets": 5, "running": True, "error": None}], now=0)
    app.poll_once()
    assert len(live.sent) == 1
    POSITION.validate(live.sent[0][1])
    assert live.sent[0][1]["heard_by"] == ["wbt-1", "wbt-2", "wbt-3"]
    assert [p["node_id"] for _, p in durable.sent] == ["wbt-1"]
    assert app.state()["publish"]["positions_sent"] == 1
