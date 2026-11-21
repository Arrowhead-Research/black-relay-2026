"""A placed node is shown from the moment it is built, and a node Fusion reports at a registered site that the
admin has not placed is shown in the pane (placed="false"): its trips alert and clear like any other."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from pytak_gateway.db import DiscoveredRow, NodeStateRow, UnplacedStateRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.registry import Registry

from .conftest import ADMIN_HASH, ADMIN_PASSWORD
from .test_api_mission_package import FakeMissionPackages

ADMIN = ("admin", ADMIN_PASSWORD)
MISSION = {"X-Mission-Package-Password": "mission-password-1"}
SITE = {
    "site_id": "sh1",
    "lat": 38.8895,
    "lon": -77.0353,
    "mission_package_name": "sh1-mission",
    "mission_package_password": "mission-password-1",
}
CAM1 = {"subsystem": "rf", "node_id": "rf-1", "lat": 38.8896, "lon": -77.0354, "accuracy_m": 30}


def rf_alert(node_id: str, n: int = 1, site: str = "sh1", at: datetime | None = None) -> Event:
    t = (at or datetime.now(timezone.utc)).isoformat()
    return Event.model_validate(
        {
            "schema": "fusion.event.v0",
            "cursor": n,
            "id": f"ev-{site}-{node_id}-{n}",
            "source_id": "central",
            "source_schema": "rf.alert.v0",
            "timestamp": t,
            "received_at": t,
            "site_id": site,
            "subsystem": "rf",
            "node_id": node_id,
            "type": "rf.alert",
            "data": {"rule_id": "loiter", "emitter": {"id": "aa:bb"}},
        }
    )


@pytest.fixture
def registry():
    r = Registry(":memory:")
    yield r
    r.close()


@pytest.fixture
def gw(registry):
    """(client, the CoT the server sent, the server)."""
    out: list[bytes] = []
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=FakeMissionPackages())

    async def keep(*events: bytes) -> None:
        out.extend(events)

    server.gateway.sender = keep
    with TestClient(server.app) as c:
        yield c, out, server


def reported(registry, *node_ids, site="sh1"):
    now = datetime.now(timezone.utc)
    with registry.begin() as s:
        for n in node_ids:
            s.add(DiscoveredRow(site_id=site, subsystem="rf", node_id=n, first_seen=now, last_seen=now))


def fusion(server, registry, *events) -> list[bytes]:
    with registry.begin() as s:
        return server.fusion.apply(s, list(events))


def nodes_in(out: list[bytes], node_id: str) -> list[bytes]:
    return [e for e in out if b'role="node"' in e and f'node="{node_id}"'.encode() in e]


# --- a placed node is shown from the moment it is built ------------------------------------------------------


def test_a_built_node_is_sent_at_once_as_ok(registry, gw):
    client, out, _ = gw
    reported(registry, "rf-1")
    assert client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN).status_code == 201
    (node,) = nodes_in(out, "rf-1")
    assert b'status="ok"' in node and b'<dest mission="sh1-mission" />' in node and b"placed=" not in node
    with registry.begin() as s:
        assert s.get(NodeStateRow, ("sh1", "rf", "rf-1")).status == "ok"


def test_the_resend_carries_a_node_that_never_reported(registry, gw):
    client, out, server = gw
    reported(registry, "rf-1")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    out.clear()
    asyncio.run(server.gateway.resend_all())
    assert len(nodes_in(out, "rf-1")) == 1


def test_a_detection_from_before_the_build_still_trips_the_node(registry, gw):
    client, out, server = gw
    reported(registry, "rf-1")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    out.clear()
    sent = fusion(server, registry, rf_alert("rf-1", at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert any(b'status="tripped"' in e for e in nodes_in(sent, "rf-1"))


def test_an_update_keeps_a_nodes_state(registry, gw):
    client, _, server = gw
    reported(registry, "rf-1")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    fusion(server, registry, rf_alert("rf-1"))
    update = {k: v for k, v in SITE.items() if not k.startswith("mission")}
    assert client.put("/sites/sh1", json={**update, "nodes": [CAM1]}, headers=MISSION).status_code == 200
    with registry.begin() as s:
        assert s.get(NodeStateRow, ("sh1", "rf", "rf-1")).status == "tripped"


# --- a node at a registered site that the admin has not placed -----------------------------------------------


@pytest.fixture
def built(registry, gw):
    client, out, server = gw
    assert client.post("/sites", json=SITE, auth=ADMIN).status_code == 201
    out.clear()
    return client, out, server


def test_an_unplaced_nodes_trip_is_sent_for_the_pane_only(registry, built):
    _, _, server = built
    (sent,) = fusion(server, registry, rf_alert("rf-9"))
    assert b'uid="sh1-rf-rf-9"' in sent
    assert b'placed="false"' in sent and b'status="tripped"' in sent
    assert b'lat="38.8895' in sent and b'lon="-77.0353' in sent  # stands at the site: it has no place of its own
    assert b'<dest mission="sh1-mission" />' in sent
    with registry.begin() as s:
        assert s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")).status == "tripped"
        assert s.get(NodeStateRow, ("sh1", "rf", "rf-9")) is None  # not a placed node
        assert s.get(DiscoveredRow, ("sh1", "rf", "rf-9")) is not None  # still offered to register


def test_nothing_is_sent_for_a_site_nobody_registered(registry, built):
    _, _, server = built
    assert fusion(server, registry, rf_alert("rf-9", site="elsewhere")) == []


def test_a_repeat_of_the_same_trip_is_not_sent_again(registry, built):
    _, _, server = built
    at = datetime.now(timezone.utc)
    fusion(server, registry, rf_alert("rf-9", n=1, at=at))
    assert fusion(server, registry, rf_alert("rf-9", n=2, at=at)) == []


def test_the_resend_carries_unplaced_nodes(registry, built):
    _, out, server = built
    fusion(server, registry, rf_alert("rf-9"))
    asyncio.run(server.gateway.resend_all())
    (again,) = nodes_in(out, "rf-9")
    assert b'placed="false"' in again
    assert not [e for e in out if b'role="threat"' in e]  # nowhere to draw one


def test_an_unplaced_trip_is_cleared_like_any_other(registry, built):
    client, out, server = built
    fusion(server, registry, rf_alert("rf-9"))
    with registry.begin() as s:
        tripped = s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")).updated
    clear = {
        "subsystem": "rf",
        "node_id": "rf-9",
        "time": (tripped + timedelta(seconds=1)).isoformat(),
        "tripped": tripped.isoformat(),
    }
    assert client.post("/sites/sh1/clear", json=clear, auth=ADMIN).status_code == 202
    (node,) = nodes_in(out, "rf-9")
    assert b'status="ok"' in node and b'placed="false"' in node
    with registry.begin() as s:
        assert s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")).status == "ok"


def test_placing_a_tripped_unplaced_node_carries_the_trip_onto_the_map(registry, built):
    client, out, server = built
    fusion(server, registry, rf_alert("rf-9"))
    out.clear()
    update = {k: v for k, v in SITE.items() if not k.startswith("mission")}
    placed = {**CAM1, "node_id": "rf-9"}
    assert client.put("/sites/sh1", json={**update, "nodes": [placed]}, headers=MISSION).status_code == 200
    node = nodes_in(out, "rf-9")
    assert node and all(b"placed=" not in e for e in node) and b'status="tripped"' in node[0]
    # No circle round the node (as for any trip whose detection did not say where it is): the phone draws the trip.
    assert not [e for e in out if b'role="threat"' in e and b"rf-9" in e]
    with registry.begin() as s:
        assert s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")) is None
        assert s.get(NodeStateRow, ("sh1", "rf", "rf-9")).status == "tripped"
    # The held detection was the same trip: it was used up, and does not trip the node again.
    with registry.begin() as s:
        assert s.scalar(text("select count(*) from fusion_held where node_id='rf-9'")) == 0


def test_health_lists_every_unplaced_node_and_never_ends_a_trip(registry, built):
    _, _, server = built
    state = server.gateway.state
    from pytak_gateway.models import HealthReport, Subsystem

    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    down = HealthReport(subsystem=Subsystem.rf, node_id="rf-8", up=False, time=later)
    listed = state.apply_unplaced_health("sh1", down)  # the pane shows every sensor Fusion reports at the house
    assert listed is not None and listed.status.value == "offline" and not listed.placed
    up = HealthReport(subsystem=Subsystem.rf, node_id="rf-7", up=True, time=later)
    assert state.apply_unplaced_health("sh1", up).status.value == "ok"
    assert state.apply_unplaced_health("elsewhere", up) is None  # a site nobody registered
    fusion(server, registry, rf_alert("rf-9"))
    down9 = HealthReport(subsystem=Subsystem.rf, node_id="rf-9", up=False, time=later)
    assert state.apply_unplaced_health("sh1", down9) is None  # an outage never ends a trip


def test_a_build_lists_every_reported_node_it_does_not_place(registry, gw):
    client, out, _ = gw
    reported(registry, "rf-1", "rf-2", "rf-3")
    assert client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN).status_code == 201
    for n in ("rf-2", "rf-3"):
        (sent,) = nodes_in(out, n)
        assert b'placed="false"' in sent and b'status="ok"' in sent
    assert all(b"placed=" not in e for e in nodes_in(out, "rf-1"))
    with registry.begin() as s:
        assert {r.node_id for r in s.query(UnplacedStateRow)} == {"rf-2", "rf-3"}


def test_placing_the_any_node_drops_the_unplaced_nodes_it_covers(registry, gw):
    client, out, _ = gw
    reported(registry, "rf-2", "*")
    assert client.post("/sites", json=SITE, auth=ADMIN).status_code == 201
    out.clear()
    update = {k: v for k, v in SITE.items() if not k.startswith("mission")}
    anyrf = {**CAM1, "node_id": "*"}
    assert client.put("/sites/sh1", json={**update, "nodes": [anyrf]}, headers=MISSION).status_code == 200
    assert [e for e in out if b'uid="sh1-rf-rf-2"' in e and b'type="t-x-d-d"' in e]
    with registry.begin() as s:
        assert s.query(UnplacedStateRow).all() == []


def test_deleting_the_site_takes_its_unplaced_nodes_off_the_map(registry, built):
    client, out, server = built
    fusion(server, registry, rf_alert("rf-9"))
    out.clear()
    r = client.delete("/sites/sh1", auth=ADMIN, headers={"X-Mission-Package-Password": "mission-password-1"})
    assert r.status_code == 200
    assert [e for e in out if b'uid="sh1-rf-rf-9"' in e and b'type="t-x-d-d"' in e]
    with registry.begin() as s:
        assert s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")) is None


# --- when Fusion goes out of reach, nothing it speaks for reads ok ---------------------------------------------


def unreachable(server, registry, at=None) -> list[bytes]:
    with registry.begin() as s:
        return server.fusion.mark_unreachable(s, at or datetime.now(timezone.utc) + timedelta(minutes=5))


def test_a_node_registered_from_fusions_list_is_fusions_from_the_start(registry, gw):
    client, _, _ = gw
    reported(registry, "rf-1")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    from pytak_gateway.db import NodeRow

    with registry.begin() as s:
        assert s.get(NodeRow, ("sh1", "rf", "rf-1")).fusion_seen_at is not None


def test_a_node_fusion_never_reported_is_not_fusions(registry, gw):
    client, _, _ = gw
    with registry.begin() as s:
        s.add(
            DiscoveredRow(
                site_id="sh1",
                subsystem="rf",
                node_id="rf-1",
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
            )
        )
    other = {**CAM1, "node_id": "rf-x"}
    with registry.begin() as s:
        s.add(
            DiscoveredRow(
                site_id="sh1",
                subsystem="rf",
                node_id="rf-x",
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
            )
        )
    client.post("/sites", json={**SITE, "nodes": [other]}, auth=ADMIN)
    from pytak_gateway.db import NodeRow

    with registry.begin() as s:
        s.get(NodeRow, ("sh1", "rf", "rf-x")).fusion_seen_at = None  # as a node only a direct pusher reports
    sent = unreachable(gw[2], registry)
    assert not nodes_in(sent, "rf-x")


def test_fusion_out_of_reach_takes_a_new_house_and_its_unplaced_nodes_offline(registry, gw):
    client, out, server = gw
    reported(registry, "rf-1", "rf-2")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    sent = unreachable(server, registry)
    (placed,) = nodes_in(sent, "rf-1")
    (unplaced,) = nodes_in(sent, "rf-2")
    assert b'status="offline"' in placed and b"placed=" not in placed
    assert b'status="offline"' in unplaced and b'placed="false"' in unplaced
    # Every cycle of the outage runs it again: nothing already offline is sent again.
    assert unreachable(server, registry, datetime.now(timezone.utc) + timedelta(minutes=6)) == []


def test_fusion_out_of_reach_never_ends_a_trip(registry, gw):
    client, out, server = gw
    reported(registry, "rf-1")
    client.post("/sites", json={**SITE, "nodes": [CAM1]}, auth=ADMIN)
    fusion(server, registry, rf_alert("rf-1"), rf_alert("rf-9", n=2))
    assert unreachable(server, registry) == []
    with registry.begin() as s:
        assert s.get(NodeStateRow, ("sh1", "rf", "rf-1")).status == "tripped"
        assert s.get(UnplacedStateRow, ("sh1", "rf", "rf-9")).status == "tripped"
