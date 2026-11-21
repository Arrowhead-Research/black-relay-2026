"""POST /sites and DELETE /sites/{site} with a fake TAK mission API: what the routes do with each answer."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pytak_gateway.cot_publisher import CotPublisher
from pytak_gateway.db import DiscoveredRow
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.mission_package import MissionPackageError, MissionPackageExists
from pytak_gateway.registry import Registry

from .conftest import ADMIN_HASH, ADMIN_PASSWORD

MP_PASSWORD = "mission-password-1"
ADMIN = ("admin", ADMIN_PASSWORD)
SITE = {
    "site_id": "sh1",
    "lat": 38.8895,
    "lon": -77.0353,
    "mission_package_name": "sh1-mission",
    "mission_package_password": MP_PASSWORD,
}


class FakeMissionPackages:
    """Stands in for AtakMissionPackages: records the calls, and raises `fail` when set."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls: list[tuple] = []

    async def create(self, name: str, password: str) -> None:
        self.calls.append(("create", name, password))
        if self.fail:
            raise self.fail

    async def remove(self, name: str, password: str) -> None:
        self.calls.append(("remove", name, password))
        if self.fail:
            raise self.fail

    async def remove_items(self, name: str, uids: list[str]) -> None:
        self.calls.append(("remove_items", name, sorted(uids)))
        if self.fail:
            raise self.fail

    async def close(self) -> None:
        pass


@pytest.fixture
def mps():
    return FakeMissionPackages()


@pytest.fixture
def registry():
    r = Registry(":memory:")
    yield r
    r.close()


@pytest.fixture
def client(registry, mps):
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=mps)
    with TestClient(server.app) as c:
        yield c


def build(client, **over):
    return client.post("/sites", json={**SITE, **over}, auth=ADMIN)


def has_site(registry, site_id="sh1") -> bool:
    return any(sid == site_id for sid, *_ in registry.sites())


def test_build_creates_the_mission_package(client, registry, mps):
    r = build(client)
    assert r.status_code == 201
    assert r.json() == {"site": "sh1", "nodes": 0, "mission_package": "sh1-mission"}
    assert mps.calls == [("create", "sh1-mission", MP_PASSWORD)]
    assert has_site(registry)


def test_build_when_the_mission_package_exists_is_409_and_leaves_no_site(client, registry, mps):
    mps.fail = MissionPackageExists("the mission package already exists")
    r = build(client)
    assert r.status_code == 409
    assert r.json()["detail"] == "mission package already exists"
    assert not has_site(registry)


def test_build_when_tak_fails_is_502_and_leaves_no_site(client, registry, mps):
    mps.fail = MissionPackageError("TAK Server did not answer (TimeoutError)")
    r = build(client)
    assert r.status_code == 502
    assert not has_site(registry)
    assert MP_PASSWORD not in r.text


def test_a_failed_build_can_be_retried(client, registry, mps):
    mps.fail = MissionPackageError("down")
    assert build(client).status_code == 502
    mps.fail = None
    assert build(client).status_code == 201
    assert has_site(registry)


def test_a_taken_site_id_never_reaches_tak(client, mps):
    assert build(client).status_code == 201
    mps.calls.clear()
    assert build(client).status_code == 409
    assert mps.calls == []


def test_delete_removes_the_mission_package_then_the_site(client, registry, mps):
    build(client)
    mps.calls.clear()
    r = client.delete("/sites/sh1", auth=ADMIN, headers={"X-Mission-Package-Password": MP_PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"site": "sh1", "nodes_removed": 0}
    assert mps.calls == [("remove", "sh1-mission", MP_PASSWORD)]
    assert not has_site(registry)


def test_delete_when_tak_fails_is_502_and_the_site_stays(client, registry, mps):
    build(client)
    mps.fail = MissionPackageError("TAK Server could not remove it (HTTP 500)")
    r = client.delete("/sites/sh1", auth=ADMIN, headers={"X-Mission-Package-Password": MP_PASSWORD})
    assert r.status_code == 502
    assert has_site(registry)


def test_delete_with_a_wrong_mission_package_password_is_401_and_never_reaches_tak(client, registry, mps):
    build(client)
    mps.calls.clear()
    r = client.delete("/sites/sh1", auth=ADMIN, headers={"X-Mission-Package-Password": "not-the-password"})
    assert r.status_code == 401
    assert mps.calls == []
    assert has_site(registry)


def test_delete_without_the_admin_password_is_401(client, mps):
    build(client)
    mps.calls.clear()
    r = client.delete("/sites/sh1", headers={"X-Mission-Package-Password": MP_PASSWORD})
    assert r.status_code == 401
    assert mps.calls == []


def test_delete_of_an_unknown_site_is_404(client, mps):
    r = client.delete("/sites/nope", auth=ADMIN, headers={"X-Mission-Package-Password": MP_PASSWORD})
    assert r.status_code == 404
    assert mps.calls == []


# --- what goes into the mission package and comes out of it -------------------------------------------------

NODES = [
    {"subsystem": "camera", "node_id": "cam1", "lat": 38.8896, "lon": -77.0354, "range_m": 50},
    {"subsystem": "camera", "node_id": "cam2", "lat": 38.8894, "lon": -77.0352, "range_m": 50},
]
UPDATE = {"site_id": "sh1", "lat": 38.8895, "lon": -77.0353}
MP_HEADER = {"X-Mission-Package-Password": MP_PASSWORD}


def reported(registry, *node_ids):
    """As if Fusion had reported these camera nodes at sh1, so a package may carry them."""
    now = datetime.now(timezone.utc)
    with registry.begin() as s:
        for n in node_ids:
            s.add(DiscoveredRow(site_id="sh1", subsystem="camera", node_id=n, first_seen=now, last_seen=now))


@pytest.fixture
def sent(registry, mps):
    """A server whose CoT is kept instead of sent: (client, the CoT it sent)."""
    out: list[bytes] = []
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=mps)

    async def keep(*events: bytes) -> None:
        out.extend(events)

    server.gateway.sender = keep
    with TestClient(server.app) as c:
        yield c, out


def test_an_update_that_leaves_a_node_out_takes_it_out_of_the_mission_package(registry, mps, sent):
    client, out = sent
    reported(registry, "cam1", "cam2")
    assert build(client, nodes=NODES).status_code == 201
    mps.calls.clear()
    r = client.put("/sites/sh1", json={**UPDATE, "nodes": NODES[:1]}, headers=MP_HEADER)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert mps.calls == [("remove_items", "sh1-mission", ["sh1-camera-cam2", "sh1-camera-cam2-threat"])]


def test_an_update_that_keeps_every_node_takes_nothing_out(registry, mps, sent):
    client, _ = sent
    reported(registry, "cam1")
    build(client, nodes=NODES[:1])
    mps.calls.clear()
    assert client.put("/sites/sh1", json={**UPDATE, "nodes": NODES[:1]}, headers=MP_HEADER).status_code == 200
    assert mps.calls == []


def test_an_update_whose_take_out_fails_still_succeeds(registry, mps, sent):
    client, _ = sent
    reported(registry, "cam1", "cam2")
    build(client, nodes=NODES)
    mps.fail = MissionPackageError("TAK Server could not take an item out of it (HTTP 500)")
    r = client.put("/sites/sh1", json={**UPDATE, "nodes": NODES[:1]}, headers=MP_HEADER)
    assert r.status_code == 200 and r.json()["removed"] == 1


def test_build_and_update_send_the_site_and_nodes_to_the_mission_package(registry, mps, sent):
    client, out = sent
    reported(registry, "cam1")
    build(client, nodes=NODES[:1])
    assert out and all(b'<dest mission="sh1-mission" />' in e for e in out)
    out.clear()
    client.put("/sites/sh1", json={**UPDATE, "lat": 38.89, "nodes": NODES[:1]}, headers=MP_HEADER)
    assert out and all(b'<dest mission="sh1-mission" />' in e for e in out)
    assert any(b'lat="38.890000"' in e and b"sh1-site" in e for e in out)


def test_deleting_the_site_takes_nothing_out_its_package_is_gone(registry, mps, sent):
    client, out = sent
    reported(registry, "cam1")
    build(client, nodes=NODES[:1])
    mps.calls.clear()
    assert client.delete("/sites/sh1", auth=ADMIN, headers=MP_HEADER).status_code == 200
    assert mps.calls == [("remove", "sh1-mission", MP_PASSWORD)]
    assert out and not any(b"<marti>" in e for e in out if b"t-x-d-d" in e)


BOUNDARY = [[38.8897, -77.0356], [38.8897, -77.0350], [38.8893, -77.0350], [38.8893, -77.0356]]


def boundaries_in(out: list[bytes]) -> list[bytes]:
    return [e for e in out if b'type="u-d-f"' in e]


def test_build_sends_the_boundary_to_the_mission_package(registry, mps, sent):
    client, out = sent
    assert build(client, boundary=BOUNDARY).status_code == 201
    (b,) = boundaries_in(out)
    assert b'uid="sh1-boundary"' in b and b'<dest mission="sh1-mission" />' in b


def test_a_site_built_without_a_boundary_sends_none(registry, mps, sent):
    client, out = sent
    build(client)
    assert boundaries_in(out) == []


def test_an_update_that_moves_the_boundary_sends_it_again(registry, mps, sent):
    client, out = sent
    build(client, boundary=BOUNDARY)
    out.clear()
    moved = [[lat + 0.0001, lon] for lat, lon in BOUNDARY]
    assert client.put("/sites/sh1", json={**UPDATE, "boundary": moved}, headers=MP_HEADER).status_code == 200
    (b,) = boundaries_in(out)
    assert b'point="38.889800,-77.035600"' in b
    assert mps.calls == [("create", "sh1-mission", MP_PASSWORD)]  # nothing taken out


def test_an_update_that_drops_the_boundary_takes_it_off_the_map_and_out_of_the_package(registry, mps, sent):
    client, out = sent
    build(client, boundary=BOUNDARY)
    out.clear()
    mps.calls.clear()
    assert client.put("/sites/sh1", json=UPDATE, headers=MP_HEADER).status_code == 200
    assert boundaries_in(out) == []
    assert any(b"t-x-d-d" in e and b'link uid="sh1-boundary"' in e for e in out)
    assert mps.calls == [("remove_items", "sh1-mission", ["sh1-boundary"])]


def test_deleting_the_site_takes_its_boundary_off_the_map(registry, mps, sent):
    client, out = sent
    build(client, boundary=BOUNDARY)
    out.clear()
    client.delete("/sites/sh1", auth=ADMIN, headers=MP_HEADER)
    assert any(b"t-x-d-d" in e and b'link uid="sh1-boundary"' in e for e in out)


def test_the_resend_carries_the_boundary(registry, mps, sent):
    client, _ = sent
    build(client, boundary=BOUNDARY)
    resent: list[bytes] = []

    async def keep(*events: bytes) -> None:
        resent.extend(events)

    asyncio.run(CotPublisher(registry, keep).resend_all())
    (b,) = boundaries_in(resent)
    assert b'<dest mission="sh1-mission" />' in b


# --- any admin can edit any site ------------------------------------------------------------------------------

RF = {"subsystem": "rf", "node_id": "*", "lat": 38.8897, "lon": -77.0351, "accuracy_m": None}
RING = [[38.8890, -77.0360], [38.8900, -77.0360], [38.8900, -77.0345]]


def test_get_site_gives_back_the_whole_package_as_built(registry, mps, sent):
    client, _ = sent
    reported(registry, "cam1")
    with registry.begin() as s:
        now = datetime.now(timezone.utc)
        s.add(DiscoveredRow(site_id="sh1", subsystem="rf", node_id="*", first_seen=now, last_seen=now))
    cam = {**NODES[0], "stream": "rtsp://10.0.0.5/cam1", "watch": [38.8899, -77.0356]}
    assert build(client, nodes=[cam, RF], boundary=RING).status_code == 201
    r = client.get("/sites/sh1", auth=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "site_id": "sh1",
        "lat": 38.8895,
        "lon": -77.0353,
        "boundary": RING,
        "nodes": [cam, RF],
        "mission_package_name": "sh1-mission",
    }
    assert MP_PASSWORD not in r.text and "hash" not in r.text
    # What comes back is a package the update takes as it is.
    body.pop("mission_package_name")
    assert client.put("/sites/sh1", json=body, headers=MP_HEADER).status_code == 200


def test_get_site_needs_the_admin_or_the_sites_mission_password(client):
    assert build(client).status_code == 201
    assert client.get("/sites/sh1").status_code == 401
    assert client.get("/sites/sh1", auth=("admin", "wrong")).status_code == 401
    bad = {"X-Mission-Package-Password": "not-the-password"}
    assert client.get("/sites/sh1", headers=bad).status_code == 401
    assert client.get("/sites/sh1", auth=("admin", "wrong"), headers=bad).status_code == 401
    assert client.get("/sites/sh1", auth=ADMIN).status_code == 200
    assert client.get("/sites/sh1", headers=MP_HEADER).status_code == 200


def test_another_sites_mission_password_does_not_get_this_one(client):
    assert build(client).status_code == 201
    other = {**SITE, "site_id": "sh2", "mission_package_name": "sh2-mission", "mission_package_password": "other-pw-2"}
    assert client.post("/sites", auth=ADMIN, json=other).status_code == 201
    r = client.get("/sites/sh1", headers={"X-Mission-Package-Password": "other-pw-2"})
    assert r.status_code == 401 and "sh1" not in r.text


def test_anyone_with_the_sites_mission_password_can_edit_it(client):
    """No admin password anywhere: the site password gets the site and puts it back changed."""
    assert build(client).status_code == 201
    got = client.get("/sites/sh1", headers=MP_HEADER)
    assert got.status_code == 200
    body = got.json()
    body.pop("mission_package_name")
    body["lat"] = 38.89
    assert client.put("/sites/sh1", json=body, headers=MP_HEADER).status_code == 200
    assert client.get("/sites/sh1", headers=MP_HEADER).json()["lat"] == 38.89


def test_get_site_of_an_unknown_site_is_404_with_a_mission_password_too(client):
    assert client.get("/sites/nope", headers=MP_HEADER).status_code == 404


def test_get_site_of_an_unknown_site_is_404(client):
    assert client.get("/sites/nope", auth=ADMIN).status_code == 404


def test_an_update_is_not_let_in_by_the_admin_password_alone(registry, mps, sent):
    client, _ = sent
    reported(registry, "cam1", "cam2")
    assert build(client, nodes=NODES).status_code == 201
    r = client.put("/sites/sh1", json={**UPDATE, "nodes": NODES[:1]}, auth=ADMIN)
    assert r.status_code == 401
    assert not [c for c in mps.calls if c[0] == "remove_items"]


def test_an_update_with_neither_password_is_401(client):
    assert build(client).status_code == 201
    assert client.put("/sites/sh1", json=UPDATE).status_code == 401
    assert client.put("/sites/sh1", json=UPDATE, auth=("admin", "wrong")).status_code == 401
    bad = {"X-Mission-Package-Password": "not-the-password"}
    assert client.put("/sites/sh1", json=UPDATE, auth=("admin", "wrong"), headers=bad).status_code == 401


def test_a_wrong_admin_password_still_lets_the_mission_password_in(client):
    assert build(client).status_code == 201
    r = client.put("/sites/sh1", json=UPDATE, auth=("admin", "wrong"), headers=MP_HEADER)
    assert r.status_code == 200


def test_an_admin_update_of_an_unknown_site_is_404(client):
    assert client.put("/sites/nope", json={**UPDATE, "site_id": "nope"}, auth=ADMIN).status_code == 404
