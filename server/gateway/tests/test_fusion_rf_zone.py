"""An RF alert names no node; its `location.zone` says which RF node heard it ("zone-wbt-1" is "wbt-1")."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from pytak_gateway.db import DiscoveredRow, HeldRow, NodeRow, NodeStateRow, SiteRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.fusion_mapper import FusionMapper, attributed_node
from pytak_gateway.registry import Registry
from pytak_gateway.state import GatewayState


def alert(zone=None, n=1, node_id=None) -> Event:
    now = datetime.now(timezone.utc).isoformat()
    data = {"rule_id": "loiter", "emitter": {"id": "aa:bb"}}
    if zone is not ...:
        data["location"] = {"zone": zone, "confidence": 1.0}
    return Event.model_validate(
        {
            "schema": "fusion.event.v0",
            "cursor": n,
            "id": f"ev-{n}",
            "source_id": "central",
            "source_schema": "rf.alert.v0",
            "timestamp": now,
            "received_at": now,
            "site_id": "central",
            "subsystem": "rf",
            "node_id": node_id,
            "type": "rf.alert",
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
        s.add(NodeRow(site_id="central", site=site, subsystem="rf", node_id="wbt-1", lat=1.0, lon=2.0))
    yield r
    r.close()


def apply(registry, *events):
    mapper = FusionMapper(GatewayState(registry))
    with registry.begin() as s:
        return mapper.apply(s, list(events))


def test_the_zone_names_the_node():
    assert attributed_node(alert("zone-wbt-1")) == "wbt-1"
    assert attributed_node(alert("zone-wbt-1", node_id="wbt-9")) == "wbt-9"  # a named node is never overridden
    assert attributed_node(alert(None)) is None
    assert attributed_node(alert(...)) is None
    assert attributed_node(alert("lobby")) is None
    assert attributed_node(alert("zone-bad id")) is None


def test_an_alert_in_a_registered_nodes_zone_trips_that_node(registry):
    out = apply(registry, alert("zone-wbt-1"))
    assert out
    with registry.begin() as s:
        assert s.get(NodeStateRow, ("central", "rf", "wbt-1")).status == "tripped"
        assert s.scalars(select(DiscoveredRow)).all() == []


def test_an_alert_in_an_unregistered_nodes_zone_is_remembered_and_held_for_it(registry):
    # At a registered site it is a node the admin has not placed: it still trips, for the pane only.
    (sent,) = apply(registry, alert("zone-wbt-2"))
    assert b'node="wbt-2"' in sent and b'placed="false"' in sent and b'status="tripped"' in sent
    with registry.begin() as s:
        assert [r.node_id for r in s.scalars(select(DiscoveredRow))] == ["wbt-2"]
        assert [h.node_id for h in s.scalars(select(HeldRow))] == ["wbt-2"]
        assert s.get(NodeStateRow, ("central", "rf", "wbt-1")) is None


def test_an_alert_with_no_zone_is_the_subsystems_any_node(registry):
    (sent,) = apply(registry, alert(None))
    assert b'node="*"' in sent and b'placed="false"' in sent
    with registry.begin() as s:
        assert [r.node_id for r in s.scalars(select(DiscoveredRow))] == ["*"]
