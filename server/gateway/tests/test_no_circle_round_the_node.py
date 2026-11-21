"""A tripped node sends no circle of its own, and a node's watch point reaches every phone."""

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pytak_gateway import cot
from pytak_gateway.cot_publisher import CotPublisher
from pytak_gateway.db import DiscoveredRow, NodeRow, NodeStateRow, SiteRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.fusion_mapper import FusionMapper
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.models import ClearRequest, DetectionEvent, Location, Subsystem
from pytak_gateway.registry import Registry
from pytak_gateway.state import DELETE_ROUNDS, GatewayState

from .conftest import ADMIN_HASH, ADMIN_PASSWORD
from .test_api_mission_package import FakeMissionPackages

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def fusion(n: int, type_: str, subsystem: str, node_id, data: dict) -> Event:
    at = (NOW - timedelta(seconds=60 - n)).isoformat()
    return Event.model_validate(
        {
            "schema": "fusion.event.v1",
            "cursor": n,
            "id": f"ev-{n}",
            "source_id": f"s-{n}",
            "source_schema": f"{type_}.v0",
            "timestamp": at,
            "received_at": at,
            "site_id": "sh1",
            "subsystem": subsystem,
            "node_id": node_id,
            "type": type_,
            "data": data,
        }
    )


CAMERA = fusion(1, "camera.detection", "camera", "cam-01", {"event": "object_start", "label": "person"})
RF = fusion(2, "rf.alert", "rf", None, {"rule_type": "loiter", "location": {"zone": "zone-wbt-1"}})
PANEL = fusion(3, "physical.event", "physical", "panel-1", {"tamper": True})


@pytest.fixture
def registry():
    r = Registry(":memory:")
    with r.begin() as s:
        site = SiteRow(site_id="sh1", lat=38.0, lon=-77.0, mission_package_name="m", mission_package_hash="h")
        s.add(site)
        s.flush()
        s.add(
            NodeRow(
                site_id="sh1",
                site=site,
                subsystem="camera",
                node_id="cam-01",
                lat=38.0001,
                lon=-77.0001,
                range_m=30.0,
                watch_lat=38.0002,
                watch_lon=-77.0003,
            )
        )
        s.add(NodeRow(site_id="sh1", site=site, subsystem="rf", node_id="wbt-1", lat=38.0, lon=-77.0, accuracy_m=10.0))
        s.add(
            NodeRow(
                site_id="sh1", site=site, subsystem="physical", node_id="panel-1", lat=38.0, lon=-77.0, range_m=20.0
            )
        )
    yield r
    r.close()


def roles(cots: list[bytes]) -> list[str]:
    return [ET.fromstring(c).find("detail/__safehouse").attrib["role"] for c in cots]


def node_attrs(cot_bytes: bytes) -> dict:
    return ET.fromstring(cot_bytes).find("detail/__safehouse").attrib


@pytest.mark.parametrize("ev", [CAMERA, RF, PANEL], ids=["camera", "rf", "panel"])
def test_a_fusion_trip_sends_the_node_and_no_circle(registry, ev):
    state = GatewayState(registry)
    with registry.begin() as s:
        sent = FusionMapper(state).apply(s, [ev])
    assert roles(sent) == ["node"]  # tripped, and nothing drawn round it, whatever range the admin gave it
    assert all(b"ellipse" not in c for c in sent)
    assert state.active_threats(datetime.now(timezone.utc)) == []
    assert all(s.threat is None for s in state.snapshot())


def test_nothing_is_drawn_on_a_re_send_either(registry):
    sent: list[bytes] = []

    async def sender(*events: bytes) -> None:
        sent.extend(events)

    publisher = CotPublisher(registry, sender)
    with registry.begin() as s:
        FusionMapper(publisher.state).apply(s, [CAMERA, RF, PANEL])
    asyncio.run(publisher.resend_all())
    assert "threat" not in roles([c for c in sent if b"__safehouse" in c])
    assert all(b"ellipse" not in c for c in sent)


def test_a_detection_that_says_where_it_is_still_draws_its_own_area(registry):
    state = GatewayState(registry)
    where = Location(lat=38.0005, lon=-77.0005, ce=12.0)
    new = state.apply_event(
        "sh1",
        DetectionEvent(
            event_id="d1",
            type="camera.detect",
            time=NOW,
            severity=5,
            subsystem=Subsystem.camera,
            node_id="cam-01",
            location=where,
        ),
    )
    sent = cot.trip_events(new, NOW)
    assert roles(sent) == ["node", "threat"]
    point = ET.fromstring(sent[1]).find("point").attrib
    assert (point["lat"], point["lon"]) == ("38.000500", "-77.000500")  # at the detection, never at the node


def test_a_circle_drawn_before_this_comes_off_the_map_at_the_nodes_next_trip(registry):
    state = GatewayState(registry)
    with registry.begin() as s:  # a trip from before the gateway stopped drawing circles round nodes
        s.add(
            NodeStateRow(
                site_id="sh1",
                subsystem="camera",
                node_id="cam-01",
                status="tripped",
                updated=NOW - timedelta(hours=1),
                tripped_at=NOW - timedelta(hours=1),
                threat_lat=38.0001,
                threat_lon=-77.0001,
                threat_ce=30.0,
                threat_expires_at=NOW + timedelta(hours=1),
            )
        )
    assert len(state.active_threats(NOW)) == 1
    with registry.begin() as s:
        FusionMapper(state).apply(s, [CAMERA])
    assert state.active_threats(datetime.now(timezone.utc)) == []
    assert state.threat_deletes() == {"sh1-camera-cam-01-threat": DELETE_ROUNDS}


def test_a_clear_still_ends_a_trip_that_drew_nothing(registry):
    state = GatewayState(registry)
    with registry.begin() as s:
        FusionMapper(state).apply(s, [CAMERA])
    cleared = state.clear("sh1", ClearRequest(subsystem="camera", node_id="cam-01", time=datetime.now(timezone.utc)))
    assert cleared is not None and cleared.status.value == "ok"


def test_a_nodes_watch_point_is_in_its_cot_tripped_or_not(registry):
    state = GatewayState(registry)
    with registry.begin() as s:
        tripped = node_attrs(FusionMapper(state).apply(s, [CAMERA])[0])
    assert (tripped["watch_lat"], tripped["watch_lon"]) == ("38.000200", "-77.000300")
    cleared = state.clear("sh1", ClearRequest(subsystem="camera", node_id="cam-01", time=datetime.now(timezone.utc)))
    ok = node_attrs(cot.node_event(cleared, NOW))
    assert (ok["status"], ok["watch_lat"], ok["watch_lon"]) == ("ok", "38.000200", "-77.000300")


def test_a_node_with_no_watch_point_says_nothing_of_one(registry):
    state = GatewayState(registry)
    with registry.begin() as s:
        sent = FusionMapper(state).apply(s, [RF, PANEL])
    for c in sent:
        assert "watch_lat" not in node_attrs(c) and "watch_lon" not in node_attrs(c)


# --- the build package: a range or an accuracy is no longer needed, and one that is sent is not refused ---

ADMIN = ("admin", ADMIN_PASSWORD)


@pytest.fixture
def client():
    registry = Registry(":memory:")
    with registry.begin() as s:  # what Fusion has reported for sh2, so its nodes may be built
        for subsystem, node in (("camera", "cam-01"), ("rf", "wbt-1"), ("physical", "panel-1")):
            s.add(DiscoveredRow(site_id="sh2", subsystem=subsystem, node_id=node, first_seen=NOW, last_seen=NOW))
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=FakeMissionPackages())
    with TestClient(server.app) as c:
        c.registry = registry
        yield c
    registry.close()


def package(**nodes) -> dict:
    return {
        "site_id": "sh2",
        "lat": 38.0,
        "lon": -77.0,
        "mission_package_name": "sh2-mission",
        "mission_package_password": "mission-password-1",
        "nodes": [
            {
                "subsystem": "camera",
                "node_id": "cam-01",
                "lat": 38.0,
                "lon": -77.0,
                "watch": [38.0002, -77.0003],
                **nodes.get("camera", {}),
            },
            {"subsystem": "rf", "node_id": "wbt-1", "lat": 38.0, "lon": -77.0, **nodes.get("rf", {})},
            {"subsystem": "physical", "node_id": "panel-1", "lat": 38.0, "lon": -77.0, **nodes.get("physical", {})},
        ],
    }


def test_a_build_package_with_no_range_or_accuracy_is_taken(client):
    r = client.post("/sites", auth=ADMIN, json=package())
    assert r.status_code == 201, r.text
    assert r.json()["nodes"] == 3
    with client.registry.begin() as s:
        rows = s.scalars(select(NodeRow).where(NodeRow.site_id == "sh2")).all()
    assert [(r.range_m, r.accuracy_m) for r in rows] == [(None, None)] * 3


def test_a_build_package_that_still_sends_them_is_not_refused(client):
    older = package(camera={"range_m": 30}, rf={"accuracy_m": 10}, physical={"range_m": None})
    r = client.post("/sites", auth=ADMIN, json=older)
    assert r.status_code == 201, r.text
    assert r.json()["nodes"] == 3
