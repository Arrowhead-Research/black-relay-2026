"""POST /sites/{site}/clear: who may end a trip. The admin password, or the mission package password of that site."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pytak_gateway.db import NodeRow, SiteRow
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.models import DetectionEvent, Status
from pytak_gateway.registry import Registry

from .conftest import ADMIN_HASH, ADMIN_PASSWORD
from .test_api_mission_package import FakeMissionPackages

ADMIN = ("admin", ADMIN_PASSWORD)
PASSWORD = {"sh1": "mission-password-1", "sh2": "mission-password-2"}
HEADER = "X-Mission-Package-Password"


@pytest.fixture
def server():
    registry = Registry(":memory:")
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=FakeMissionPackages())
    with TestClient(server.app) as client:
        server.client = client
        for site, password in PASSWORD.items():
            built = client.post(
                "/sites",
                auth=ADMIN,
                json={
                    "site_id": site,
                    "lat": 38.8895,
                    "lon": -77.0353,
                    "mission_package_name": f"{site}-mission",
                    "mission_package_password": password,
                },
            )
            assert built.status_code == 201
            with registry.begin() as s:
                row = s.scalars(select(SiteRow).where(SiteRow.site_id == site)).one()
                s.add(NodeRow(site_id=site, site=row, subsystem="camera", node_id="cam-01", lat=38.8895, lon=-77.0353))
            trip(server, site)
        yield server
    registry.close()


def trip(server, site: str) -> None:
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    event = DetectionEvent(
        event_id=f"ev-{site}", type="camera.detection", time=now, severity=5, subsystem="camera", node_id="cam-01"
    )
    assert server.gateway.state.apply_event(site, event) is not None


def status_of(server, site: str) -> Status:
    return next(s.status for s in server.gateway.state.snapshot() if s.node.site_id == site)


def clear(server, site: str = "sh1", **how):
    body = {"subsystem": "camera", "node_id": "cam-01", "time": datetime.now(timezone.utc).isoformat()}
    return server.client.post(f"/sites/{site}/clear", json=body, **how)


def test_the_sites_mission_package_password_ends_the_trip_without_the_admin_password(server):
    r = clear(server, headers={HEADER: PASSWORD["sh1"]})
    assert r.status_code == 202
    assert r.json() == {"accepted": True, "status": "ok"}
    assert status_of(server, "sh1") == Status.ok
    assert status_of(server, "sh2") == Status.tripped  # and no other site's


def test_the_admin_password_still_ends_the_trip(server):
    r = clear(server, auth=ADMIN)
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert status_of(server, "sh1") == Status.ok


def test_a_wrong_mission_package_password_is_401_and_ends_nothing(server):
    r = clear(server, headers={HEADER: "not-the-password"})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Basic"
    assert status_of(server, "sh1") == Status.tripped


def test_another_sites_mission_package_password_is_401_and_ends_nothing(server):
    r = clear(server, headers={HEADER: PASSWORD["sh2"]})
    assert r.status_code == 401
    assert status_of(server, "sh1") == Status.tripped


def test_no_password_at_all_is_401(server):
    assert clear(server).status_code == 401
    assert clear(server, headers={HEADER: ""}).status_code == 401
    assert status_of(server, "sh1") == Status.tripped


def test_a_wrong_admin_password_alone_is_401(server):
    assert clear(server, auth=("admin", "not-the-admin-password")).status_code == 401
    assert status_of(server, "sh1") == Status.tripped


def test_a_wrong_admin_password_does_not_spoil_a_right_mission_package_password(server):
    r = clear(server, auth=("admin", "not-the-admin-password"), headers={HEADER: PASSWORD["sh1"]})
    assert r.status_code == 202
    assert status_of(server, "sh1") == Status.ok


def test_the_admin_password_is_never_taken_as_a_mission_package_password(server):
    assert clear(server, headers={HEADER: ADMIN_PASSWORD}).status_code == 401
    assert status_of(server, "sh1") == Status.tripped


def test_a_mission_package_password_for_a_site_that_does_not_exist_is_404(server):
    assert clear(server, site="nope", headers={HEADER: PASSWORD["sh1"]}).status_code == 404


def test_a_node_the_site_does_not_have_is_404_with_either_password(server):
    body = {"subsystem": "camera", "node_id": "cam-99", "time": datetime.now(timezone.utc).isoformat()}
    for how in ({"auth": ADMIN}, {"headers": {HEADER: PASSWORD["sh1"]}}):
        assert server.client.post("/sites/sh1/clear", json=body, **how).status_code == 404


def test_the_mission_package_password_opens_no_admin_route(server):
    headers = {HEADER: PASSWORD["sh1"]}
    assert server.client.get("/unregistered", headers=headers).status_code == 401
    assert server.client.delete("/sites/sh1", headers=headers).status_code == 401
    node = {"lat": 38.8895, "lon": -77.0353}
    assert server.client.put("/sites/sh1/nodes/camera/cam-02", json=node, headers=headers).status_code == 401
