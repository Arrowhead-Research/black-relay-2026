"""An RF trip names the emitter that caused it (a MAC address) and where Fusion last put it (`rf.position`, feet
from the site's middle point), and both go out in the trip's CoT."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from pytak_gateway import fusion_mapper
from pytak_gateway.db import EmitterRow, NodeRow, SiteRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.fusion_mapper import EMITTER_POSITION_TTL, FusionMapper, emitter_of, position_of
from pytak_gateway.models import ClearRequest, NodeRegistration, Status, Subsystem
from pytak_gateway.registry import Registry
from pytak_gateway.state import GatewayState

MAC = "0a:ec:f3:7d:6b:94"
# A minute ago: positions older than EMITTER_POSITION_TTL are dropped, so the times here must be recent.
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)


def event(n: int, type_: str, data: dict, *, at: datetime = T0, site="central", subsystem="rf", node_id=None) -> Event:
    return Event.model_validate(
        {
            "schema": "fusion.event.v1",
            "cursor": n,
            "id": f"ev-{n}",
            "source_id": f"src-{n}",
            "source_schema": f"{type_}.v0",
            "timestamp": at.isoformat(),
            "received_at": at.isoformat(),
            "site_id": site,
            "subsystem": subsystem,
            "node_id": node_id,
            "type": type_,
            "data": data,
        }
    )


def alert(n=1, emitter=MAC, zone="zone-wbt-1", **kw) -> Event:
    data = {"rule_id": "loiter", "rule_type": "loiter", "location": {"zone": zone, "confidence": 1.0}}
    if emitter is not None:
        data["emitter"] = {"id": emitter, "type": "ble", "randomized_address": False}
    return event(n, "rf.alert", data, **kw)


def position(n=2, emitter=MAC, x=12.5, y=-40.0, **kw) -> Event:
    return event(
        n, "rf.position", {"emitter": {"id": emitter}, "position": {"x_ft": x, "y_ft": y}, "heard_by": []}, **kw
    )


@pytest.fixture
def registry():
    r = Registry(":memory:")
    with r.begin() as s:
        for site_id in ("central", "other"):
            site = SiteRow(site_id=site_id, lat=1.0, lon=2.0, mission_package_name=site_id, mission_package_hash="h")
            s.add(site)
            s.flush()
            for subsystem, node in (("rf", "wbt-1"), ("rf", "wbt-2"), ("camera", "cam-01")):
                s.add(NodeRow(site_id=site_id, site=site, subsystem=subsystem, node_id=node, lat=1.0, lon=2.0))
    yield r
    r.close()


@pytest.fixture
def mapper(registry):
    return FusionMapper(GatewayState(registry))


def apply(registry, mapper, *events) -> list[bytes]:
    with registry.begin() as s:
        return mapper.apply(s, list(events))


def state_of(mapper, node="wbt-1", site="central"):
    return next(s for s in mapper.state.snapshot() if s.node.site_id == site and s.node.node_id == node)


def safehouse(cot_bytes: bytes) -> dict:
    return ET.fromstring(cot_bytes).find("detail/__safehouse").attrib


def node_cots(cots: list[bytes]) -> list[dict]:
    return [a for a in map(safehouse, cots) if a["role"] == "node"]


def test_a_trip_names_the_emitter_that_caused_it(registry, mapper):
    sent = node_cots(apply(registry, mapper, alert()))
    assert state_of(mapper).emitter == MAC
    assert sent[0]["emitter"] == MAC
    assert "x_ft" not in sent[0] and "y_ft" not in sent[0]  # Fusion has not said where it is


def test_a_trip_carries_where_fusion_last_put_its_emitter(registry, mapper):
    sent = node_cots(apply(registry, mapper, position(1, at=T0 - timedelta(seconds=30)), alert(2)))
    assert (state_of(mapper).x_ft, state_of(mapper).y_ft) == (12.5, -40.0)
    assert (sent[0]["emitter"], sent[0]["x_ft"], sent[0]["y_ft"]) == (MAC, "12.5", "-40.0")


def test_a_position_that_arrives_after_the_alert_moves_the_open_trip_and_is_sent(registry, mapper):
    apply(registry, mapper, alert(1))
    before = state_of(mapper).updated
    sent = node_cots(apply(registry, mapper, position(2, at=T0 + timedelta(seconds=3))))
    assert len(sent) == 1
    assert (sent[0]["status"], sent[0]["x_ft"], sent[0]["y_ft"]) == ("tripped", "12.5", "-40.0")
    assert state_of(mapper).updated == before  # the same trip: no phone takes it for a new one


def test_an_emitter_heard_in_two_zones_moves_both_of_its_trips(registry, mapper):
    apply(registry, mapper, alert(1), alert(2, zone="zone-wbt-2"))
    sent = node_cots(apply(registry, mapper, position(3, at=T0 + timedelta(seconds=3))))
    assert sorted(a["node"] for a in sent) == ["wbt-1", "wbt-2"]


def test_the_same_position_again_sends_nothing(registry, mapper):
    apply(registry, mapper, alert(1), position(2, at=T0 + timedelta(seconds=3)))
    assert apply(registry, mapper, position(3, at=T0 + timedelta(seconds=6))) == []


def test_an_older_position_than_the_one_held_changes_nothing(registry, mapper):
    apply(registry, mapper, alert(1), position(2, at=T0 + timedelta(seconds=9)))
    assert apply(registry, mapper, position(3, x=99.0, at=T0 + timedelta(seconds=3))) == []
    assert state_of(mapper).x_ft == 12.5


def test_a_position_moves_only_the_trips_of_its_own_emitter_and_site(registry, mapper):
    apply(registry, mapper, alert(1), alert(2, site="other"))
    assert apply(registry, mapper, position(3, emitter="11:22:33:44:55:66", at=T0 + timedelta(seconds=3))) == []
    sent = node_cots(apply(registry, mapper, position(4, site="other", at=T0 + timedelta(seconds=3))))
    assert len(sent) == 1 and sent[0]["site"] == "other"
    assert state_of(mapper).x_ft is None


def test_a_position_too_old_for_the_alert_is_not_where_the_emitter_is(registry, mapper):
    old = T0 - EMITTER_POSITION_TTL - timedelta(seconds=1)
    with registry.begin() as s:
        s.add(EmitterRow(site_id="central", emitter=MAC, x_ft=1.0, y_ft=2.0, heard_at=old))
        detail = mapper._emitter(s, "central", MAC, T0)
    assert detail == {"emitter": MAC}


@pytest.mark.parametrize(
    "raw",
    [
        "Bob's <b>phone</b>",
        "aa:bb",
        "0a:ec:f3:7d:6b:94:00",
        "0a:ec:f3:7d:6b:zz",
        "",
        None,
        12,
        MAC + '" x="',
        "x" * 200,
    ],
)
def test_an_emitter_id_that_is_not_a_mac_address_never_reaches_a_phone(registry, mapper, raw):
    sent = node_cots(apply(registry, mapper, alert(emitter=raw)))
    assert sent[0]["status"] == "tripped"  # the alert still trips the node
    assert "emitter" not in sent[0]
    assert state_of(mapper).emitter is None


def test_a_mac_address_is_always_sent_lower_case_with_colons():
    assert emitter_of({"emitter": {"id": "0A-EC-F3-7D-6B-94"}}) == MAC
    assert emitter_of({"emitter": {"id": " 0A:EC:F3:7D:6B:94 "}}) == MAC
    assert emitter_of({"emitter": "0a:ec:f3:7d:6b:94"}) is None
    assert emitter_of({}) is None


@pytest.mark.parametrize(
    "where",
    [
        {"x_ft": "12", "y_ft": 1},
        {"x_ft": 1},
        {"x_ft": float("nan"), "y_ft": 1},
        {"x_ft": float("inf"), "y_ft": 1},
        {"x_ft": 1e9, "y_ft": 1},
        {"x_ft": True, "y_ft": 1},
        "12,1",
        None,
    ],
)
def test_a_position_that_is_not_two_plain_numbers_is_no_position(registry, mapper, where):
    assert position_of({"position": where}) is None
    apply(registry, mapper, alert(1))
    bad = event(2, "rf.position", {"emitter": {"id": MAC}, "position": where}, at=T0 + timedelta(seconds=3))
    assert apply(registry, mapper, bad) == []
    assert state_of(mapper).x_ft is None


def test_feet_may_be_whole_numbers_and_negative():
    assert position_of({"position": {"x_ft": -3, "y_ft": 0}}) == (-3.0, 0.0)


def test_a_camera_trip_names_no_emitter(registry, mapper):
    cam = event(
        1,
        "camera.detection",
        {"event": "object_start", "label": "person", "emitter": {"id": MAC}},
        subsystem="camera",
        node_id="cam-01",
    )
    sent = node_cots(apply(registry, mapper, cam))
    assert sent[0]["status"] == "tripped"
    assert "emitter" not in sent[0]


def test_a_clear_forgets_the_emitter(registry, mapper):
    apply(registry, mapper, position(1, at=T0 - timedelta(seconds=5)), alert(2))
    cleared = mapper.state.clear(
        "central", ClearRequest(subsystem="rf", node_id="wbt-1", time=T0 + timedelta(seconds=9))
    )
    assert cleared.status == Status.ok
    assert (cleared.emitter, cleared.x_ft, cleared.y_ft) == (None, None, None)
    assert apply(registry, mapper, position(3, x=1.0, at=T0 + timedelta(seconds=12))) == []  # nothing left to move


def test_an_alert_held_for_an_unregistered_node_keeps_its_emitter(registry, mapper):
    now = datetime.now(timezone.utc)
    apply(registry, mapper, alert(1, zone="zone-wbt-9", at=now), position(2, at=now))
    with registry.begin() as s:
        registry.register_node("central", Subsystem.rf, "wbt-9", NodeRegistration(lat=1.0, lon=2.0), session=s)
        sent = node_cots(mapper.replay(s, datetime.now(timezone.utc)))
    assert (sent[0]["node"], sent[0]["emitter"], sent[0]["x_ft"]) == ("wbt-9", MAC, "12.5")


def test_positions_are_capped_and_old_ones_are_dropped(registry, mapper, monkeypatch):
    monkeypatch.setattr(fusion_mapper, "EMITTERS_MAX", 2)
    now = datetime.now(timezone.utc)
    macs = [f"00:00:00:00:00:0{i}" for i in range(3)]
    apply(registry, mapper, *(position(i + 1, emitter=m, at=now) for i, m in enumerate(macs)))
    with registry.begin() as s:
        assert s.scalar(select(func.count()).select_from(EmitterRow)) == 2
    stale = position(9, emitter=macs[0], at=now - EMITTER_POSITION_TTL - timedelta(minutes=1))
    apply(registry, mapper, stale)  # older than the one held: ignored, and the prune leaves the fresh ones
    with registry.begin() as s:
        s.get(EmitterRow, ("central", macs[0])).heard_at = now - EMITTER_POSITION_TTL - timedelta(minutes=1)
    apply(registry, mapper)
    with registry.begin() as s:
        assert s.get(EmitterRow, ("central", macs[0])) is None
        assert s.get(EmitterRow, ("central", macs[1])) is not None
