"""A trip's detail line: the gateway's own words from the Fusion event's fields, never the event's text."""

from datetime import datetime, timezone

import pytest

from pytak_gateway.db import HeldRow, NodeRow, NodeStateRow, SiteRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.fusion_mapper import FusionMapper
from pytak_gateway.registry import Registry
from pytak_gateway.state import GatewayState
from pytak_gateway.trip_words import MAX_LEN, describe, severity

# Shapes seen on Fusion's central broker, 2026-11-21.
LOITER = {
    "rule_id": "loiter",
    "rule_type": "loiter",
    "severity": "medium",
    "emitter": {"id": "5e:12:21:26:5e:a1", "type": "wifi_client", "randomized_address": True},
    "location": {"zone": "zone-wbt-1", "confidence": 0.3},
    "context": {"dwell_s": 601, "since": "2026-11-21T04:17:17.000Z"},
}
REPEAT = {
    "rule_id": "repeat-visitor",
    "rule_type": "repeat_visitor",
    "severity": "medium",
    "emitter": {"id": "bc:24:11:76:dd:83", "type": "ble", "randomized_address": False},
    "location": {"zone": "zone-wbt-2", "confidence": None},
    "context": {"visits": 3, "window_s": 86400.0},
}
OFFLINE = {"rule_id": "node-offline", "rule_type": "node_offline", "severity": "high", "context": {"node_id": "wbt-1"}}
DOOR = {
    "event_type": "zone_opened",
    "zone": {"id": "zone-6", "name": "Back Door", "kind": "contact"},
    "state": "open",
    "previous_state": "closed",
    "stale": False,
    "synthetic": False,
}


@pytest.mark.parametrize(
    "event_type,data,line",
    [
        ("rf.alert", LOITER, "Wi-Fi device lingering for 10 min"),
        ("rf.alert", REPEAT, "Bluetooth device back 3 times in 24 h"),
        ("rf.alert", {**LOITER, "emitter": {"type": "wifi_ap"}, "context": {}}, "Wi-Fi access point lingering"),
        ("rf.alert", {**LOITER, "emitter": {"type": "zigbee"}}, "Radio device lingering for 10 min"),
        ("rf.alert", {"rule_type": "new_rule"}, "RF alert"),
        ("camera.detection", {"event": "object_start", "label": "person", "confidence": 0.87}, "Person seen on camera · 87% sure"),
        ("camera.detection", {"event": "object_start", "label": "Some <b>thing</b>"}, "Object seen on camera"),
        ("physical.event", DOOR, "Door or window opened"),
        ("physical.event", {**DOOR, "zone": {"kind": "motion", "name": "Hall"}}, "Motion detected"),
        ("physical.event", {**DOOR, "tamper": True}, "Sensor tampered with"),
        ("physical.event", {"event_type": "tamper"}, "Sensor tampered with"),
        ("physical.event", {"zone": "not a dict"}, "Alarm zone triggered"),
        ("something.new", {}, "Detection"),
    ],
)
def test_the_line(event_type, data, line):
    assert describe(event_type, data) == line
    assert len(line) <= MAX_LEN


def test_no_text_of_the_event_is_passed_through():
    line = describe("physical.event", DOOR) + describe("camera.detection", {"label": "Back Door"})
    assert "Back" not in line


def test_fusions_severity_word_is_used_when_there_is_one():
    assert severity(LOITER, 5) == 5
    assert severity({"severity": "high"}, 5) == 8
    assert severity({"severity": 7}, 5) == 5
    assert severity({}, 4) == 4


def event(data, node_id=None, n=1, type_="rf.alert", subsystem="rf") -> Event:
    now = datetime.now(timezone.utc).isoformat()
    return Event.model_validate(
        {
            "schema": "fusion.event.v1",
            "cursor": n,
            "id": f"ev-{n}",
            "source_id": "central",
            "source_schema": "x",
            "timestamp": now,
            "received_at": now,
            "site_id": "central",
            "subsystem": subsystem,
            "node_id": node_id,
            "type": type_,
            "data": data,
        }
    )


@pytest.fixture
def registry():
    r = Registry(":memory:")
    with r.begin() as s:
        site = SiteRow(site_id="central", lat=1.0, lon=2.0, mission_package_name="m", mission_package_hash="h")
        s.add(site)
        s.flush()
        for node in ("wbt-1", "wbt-2"):
            s.add(NodeRow(site_id="central", site=site, subsystem="rf", node_id=node, lat=1.0, lon=2.0))
    yield r
    r.close()


def test_a_trip_carries_its_line_and_severity_in_the_cot(registry):
    mapper = FusionMapper(GatewayState(registry))
    with registry.begin() as s:
        out = mapper.apply(s, [event({**LOITER, "severity": "high"})])
    xml = out[0].decode()
    assert 'detail="Wi-Fi device lingering for 10 min"' in xml
    assert 'severity="alert"' in xml


def test_node_offline_greys_the_node_out_and_trips_nothing(registry):
    mapper = FusionMapper(GatewayState(registry))
    with registry.begin() as s:
        out = mapper.apply(s, [event(OFFLINE)])
        assert s.get(NodeStateRow, ("central", "rf", "wbt-1")).status == "offline"
        assert s.get(NodeStateRow, ("central", "rf", "wbt-2")) is None
    assert len(out) == 1 and 'status="offline"' in out[0].decode()
    assert "threat" not in out[0].decode()


def test_a_held_trip_keeps_its_line_for_the_replay(registry):
    mapper = FusionMapper(GatewayState(registry))
    with registry.begin() as s:
        mapper.apply(s, [event({**REPEAT, "location": {"zone": "zone-wbt-3"}})])
        held = s.get(HeldRow, "ev-1")
        assert (held.detail, held.severity) == ("Bluetooth device back 3 times in 24 h", 5)
        site = s.get(SiteRow, "central")
        s.add(NodeRow(site_id="central", site=site, subsystem="rf", node_id="wbt-3", lat=1.0, lon=2.0))
        s.flush()
        out = mapper.replay(s, datetime.now(timezone.utc))
    assert 'detail="Bluetooth device back 3 times in 24 h"' in out[0].decode()

