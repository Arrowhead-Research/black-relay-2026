"""The allow list: an admin allows a friendly emitter at a site, and from then on its RF alerts trip nothing there."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pytak_gateway.db import NodeRow, SiteRow
from pytak_gateway.fusion_client import Event
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.models import ALLOWED_EMITTERS_MAX, DetectionEvent, Status
from pytak_gateway.registry import Registry

from .conftest import ADMIN_HASH, ADMIN_PASSWORD
from .test_api_mission_package import FakeMissionPackages

ADMIN = ("admin", ADMIN_PASSWORD)
MISSION = {"X-Mission-Package-Password": "mission-password-1"}
MAC = "0a:ec:f3:7d:6b:94"
OTHER = "11:22:33:44:55:66"


class Sent(list):
    """Stands in for the connection to TAK: keeps the CoT it was handed."""

    def __bool__(self) -> bool:  # an empty list is falsy, and the server takes a falsy `tak` for "no TAK"
        return True

    async def send_many(self, *events: bytes) -> None:
        self.extend(events)

    async def start(self) -> bool:
        return True

    def nodes(self) -> list[dict]:
        attrs = [ET.fromstring(e).find("detail/__safehouse") for e in self]
        return [a.attrib for a in attrs if a is not None and a.attrib["role"] == "node"]


@pytest.fixture
def server():
    registry = Registry(":memory:")
    sent = Sent()
    server = GatewayServer(registry, sent, ADMIN_HASH, resend=False, mission_packages=FakeMissionPackages())
    with TestClient(server.app) as client:
        server.client, server.sent = client, sent
        for site in ("sh1", "sh2"):
            body = {
                "site_id": site,
                "lat": 38.8895,
                "lon": -77.0353,
                "mission_package_name": f"{site}-mission",
                "mission_package_password": "mission-password-1",
            }
            assert client.post("/sites", auth=ADMIN, json=body).status_code == 201
            with registry.begin() as s:
                row = s.scalars(select(SiteRow).where(SiteRow.site_id == site)).one()
                for subsystem, node in (("rf", "wbt-1"), ("rf", "wbt-2"), ("camera", "cam-01")):
                    s.add(NodeRow(site_id=site, site=row, subsystem=subsystem, node_id=node, lat=38.8895, lon=-77.0353))
        sent.clear()
        yield server
    registry.close()


def alert(server, n: int, emitter: str | None = MAC, zone="zone-wbt-1", site="sh1") -> list[bytes]:
    at = (datetime.now(timezone.utc) - timedelta(seconds=30) + timedelta(seconds=n)).isoformat()
    data = {"rule_id": "loiter", "rule_type": "loiter", "location": {"zone": zone, "confidence": 1.0}}
    if emitter is not None:
        data["emitter"] = {"id": emitter, "type": "ble"}
    ev = Event.model_validate(
        {
            "schema": "fusion.event.v1",
            "cursor": n,
            "id": f"ev-{site}-{n}",
            "source_id": f"src-{n}",
            "source_schema": "rf.alert.v0",
            "timestamp": at,
            "received_at": at,
            "site_id": site,
            "subsystem": "rf",
            "node_id": None,
            "type": "rf.alert",
            "data": data,
        }
    )
    with server.registry.begin() as s:
        return server.fusion.apply(s, [ev])


def status_of(server, node="wbt-1", site="sh1") -> Status | None:
    return next(
        (s.status for s in server.gateway.state.snapshot() if (s.node.site_id, s.node.node_id) == (site, node)), None
    )


def allow(server, emitter=MAC, site="sh1", auth=ADMIN, **body):
    return server.client.post(f"/sites/{site}/emitters", json={"emitter": emitter, **body}, auth=auth)


def test_an_allowed_emitters_alert_trips_nothing(server):
    assert allow(server).status_code == 201
    assert alert(server, 1) == []
    assert status_of(server) is None
    assert server.fusion.stats["allowed"] == 1


def test_allowing_an_emitter_ends_every_open_trip_it_caused_at_that_site(server):
    alert(server, 1)
    alert(server, 2, zone="zone-wbt-2")
    alert(server, 3, emitter=OTHER, site="sh1", zone="zone-nowhere")  # no such node: held, not a trip
    r = allow(server)
    assert r.status_code == 201
    assert r.json() == {"site": "sh1", "emitter": MAC, "created": True, "cleared": 2}
    assert status_of(server, "wbt-1") == Status.ok
    assert status_of(server, "wbt-2") == Status.ok
    cleared = [n for n in server.sent.nodes() if n["status"] == "ok"]
    assert sorted(n["node"] for n in cleared) == ["wbt-1", "wbt-2"]
    assert all("emitter" not in n for n in cleared)
    deletes = [e for e in server.sent if b"t-x-d-d" in e]
    assert len(deletes) == 2  # each trip's threat area comes off the map


def test_allowing_leaves_the_trips_of_other_emitters_and_other_sites(server):
    alert(server, 1, emitter=OTHER)
    alert(server, 2, zone="zone-wbt-2")
    alert(server, 3, site="sh2")
    assert allow(server).json()["cleared"] == 1
    assert status_of(server, "wbt-1") == Status.tripped  # OTHER caused it
    assert status_of(server, "wbt-2") == Status.ok
    assert status_of(server, "wbt-1", site="sh2") == Status.tripped  # the same emitter, another site
    assert alert(server, 4, site="sh2", zone="zone-wbt-2") != []  # and it still trips there


def test_an_alert_that_names_no_emitter_still_trips(server):
    allow(server)
    assert alert(server, 1, emitter=None) != []
    assert status_of(server) == Status.tripped
    assert allow(server).json()["cleared"] == 0  # and allowing again does not end a trip that named nobody


def test_a_trip_a_camera_made_is_never_ended_by_allowing_an_emitter(server):
    server.gateway.state.apply_event(
        "sh1",
        DetectionEvent(
            event_id="cam",
            type="camera.detection",
            time=datetime.now(timezone.utc),
            severity=5,
            subsystem="camera",
            node_id="cam-01",
        ),
    )
    assert allow(server).json()["cleared"] == 0
    assert status_of(server, "cam-01") == Status.tripped


def test_the_list_says_what_is_allowed_and_removing_one_lets_it_trip_again(server):
    assert allow(server, name="  kitchen TV ").status_code == 201
    assert allow(server, emitter=OTHER).status_code == 201
    listed = server.client.get("/sites/sh1/emitters", auth=ADMIN).json()
    assert listed["site"] == "sh1"
    assert [(e["emitter"], e["name"]) for e in listed["emitters"]] == [(MAC, "kitchen TV"), (OTHER, None)]
    assert server.client.get("/sites/sh2/emitters", auth=ADMIN).json()["emitters"] == []

    r = server.client.delete(f"/sites/sh1/emitters/{MAC}", auth=ADMIN)
    assert r.status_code == 200 and r.json() == {"site": "sh1", "emitter": MAC}
    assert [e["emitter"] for e in server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"]] == [OTHER]
    assert alert(server, 1) != []
    assert status_of(server) == Status.tripped


def test_allowing_twice_is_safe_and_keeps_or_takes_the_name(server):
    assert allow(server, name="router").status_code == 201
    again = allow(server)
    assert again.status_code == 200 and again.json()["created"] is False
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"][0]["name"] == "router"
    assert allow(server, name="the router").status_code == 200
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"][0]["name"] == "the router"


def test_a_mac_address_may_be_typed_with_dashes_or_capitals(server):
    assert allow(server, emitter="0A-EC-F3-7D-6B-94").json()["emitter"] == MAC
    assert alert(server, 1) == []
    assert server.client.delete("/sites/sh1/emitters/0A-EC-F3-7D-6B-94", auth=ADMIN).status_code == 200


@pytest.mark.parametrize(
    "bad", ["", "kitchen TV", "0a:ec:f3:7d:6b", "0a:ec:f3:7d:6b:zz", "0aecf37d6b94", "x" * 40, 12, None]
)
def test_what_is_not_a_mac_address_is_refused(server, bad):
    assert allow(server, emitter=bad).status_code == 422
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"] == []


def test_a_name_is_plain_text_of_a_bounded_length_and_no_other_field_is_taken(server):
    assert allow(server, name="x" * 65).status_code == 422
    assert allow(server, name="tv\x00").status_code == 422
    assert allow(server, site_id="sh2").status_code == 422
    assert allow(server, name="   ").status_code == 201
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"][0]["name"] is None


def test_the_allow_list_needs_the_admin_password_and_the_mission_package_password_opens_none_of_it(server):
    allow(server)
    for how in ({}, {"headers": MISSION}, {"auth": ("admin", "wrong")}, {"auth": ("admin", "mission-password-1")}):
        assert server.client.post("/sites/sh1/emitters", json={"emitter": OTHER}, **how).status_code == 401
        assert server.client.get("/sites/sh1/emitters", **how).status_code == 401
        assert server.client.delete(f"/sites/sh1/emitters/{MAC}", **how).status_code == 401
    listed = server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"]
    assert [e["emitter"] for e in listed] == [MAC]


def test_a_site_or_an_emitter_that_is_not_there_is_404(server):
    assert allow(server, site="nope").status_code == 404
    assert server.client.get("/sites/nope/emitters", auth=ADMIN).status_code == 404
    assert server.client.delete(f"/sites/sh1/emitters/{MAC}", auth=ADMIN).status_code == 404
    assert server.client.delete("/sites/sh1/emitters/not-a-mac", auth=ADMIN).status_code == 404
    allow(server)
    assert server.client.delete(f"/sites/sh2/emitters/{MAC}", auth=ADMIN).status_code == 404  # another site's


def test_a_sites_list_is_bounded(server):
    with server.registry.begin() as s:
        for i in range(ALLOWED_EMITTERS_MAX):
            server.registry.allow_emitter("sh1", f"00:00:00:00:{i // 256:02x}:{i % 256:02x}", None, s)
    assert allow(server).status_code == 422
    assert allow(server, emitter="00:00:00:00:00:01").status_code == 200  # one already listed is still taken
    assert allow(server, site="sh2").status_code == 201  # the bound is per site


def test_deleting_a_site_takes_its_list_with_it(server):
    allow(server)
    assert server.client.delete("/sites/sh1", auth=ADMIN, headers=MISSION).status_code == 200
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).status_code == 404
    body = {
        "site_id": "sh1",
        "lat": 1.0,
        "lon": 2.0,
        "mission_package_name": "again",
        "mission_package_password": "mission-password-1",
    }
    assert server.client.post("/sites", auth=ADMIN, json=body).status_code == 201
    assert server.client.get("/sites/sh1/emitters", auth=ADMIN).json()["emitters"] == []


def test_an_allowed_emitters_alert_for_an_unregistered_node_is_not_held(server):
    allow(server)
    alert(server, 1, zone="zone-wbt-9")
    assert server.fusion.stats["held"] == 0
    assert server.fusion.stats["allowed"] == 1
