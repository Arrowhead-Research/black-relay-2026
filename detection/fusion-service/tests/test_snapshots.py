"""GET /api/v1/events/{id}/snapshot: the image is fetched from the kit named by its heartbeat."""

import hashlib
from uuid import uuid4

import httpx
import pytest
from conftest import FIXED_NOW, fixed_clock
from fastapi.testclient import TestClient

from fusion_service.app import create_app
from fusion_service.domain.events import Event, Source
from fusion_service.domain.time import stamp
from fusion_service.snapshots import FrigateSnapshots

PASSWORD = "kits-broker-password"
SECRET = hashlib.sha256(b"black-relay-frigate-proxy:" + PASSWORD.encode()).hexdigest()
JPEG = b"\xff\xd8\xff\xe0 not really a jpeg \xff\xd9"


def _event(event_type, topic, node_id, attributes) -> Event:
    now = stamp(FIXED_NOW)
    return Event(
        cursor=0,
        id=str(uuid4()),
        source_id=str(uuid4()),
        source_schema=f"camera.{topic.rsplit('/', 1)[-1]}.v0",
        timestamp=now,
        received_at=now,
        site_id="site-test",
        subsystem="camera",
        node_id=node_id,
        type=event_type,
        data={"attributes": attributes},
        source=Source(broker_id="test", topic=topic, payload={"schema": "x"}),
    )


def detection(
    node_id="cam-1",
    snapshot_url="https://cam-1.local:8971/api/events/17.5-ab/snapshot.jpg",
):
    return _event(
        "camera.detection",
        "detection/camera/detections",
        node_id,
        {"snapshot_url": snapshot_url},
    )


def heartbeat(node_id="cam-1", address="172.22.100.21"):
    return _event(
        "camera.node_status",
        "detection/camera/status/nodes",
        node_id,
        {"node_address": address},
    )


@pytest.fixture
def snapshot_settings(settings, monkeypatch):
    monkeypatch.setenv("FUSION_CAMERA_PASSWORD", PASSWORD)
    return settings.model_copy(update={"camera_snapshot_password_env": "FUSION_CAMERA_PASSWORD"})


@pytest.fixture
def kit():
    """A fake kit: records the requests it gets and answers as told."""

    class Kit:
        requests: list[httpx.Request] = []
        respond = staticmethod(
            lambda request: httpx.Response(
                200,
                content=JPEG,
                headers={"content-type": "image/jpeg"},
            ),
        )

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.respond(request)

    return Kit()


@pytest.fixture
def snapshot_client(snapshot_settings, kit):
    app = create_app(snapshot_settings, start_mqtt=False, clock=fixed_clock)
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.snapshots.close()
        runtime.snapshots = FrigateSnapshots(
            PASSWORD,
            timeout_s=1,
            max_bytes=1024,
            transport=httpx.MockTransport(kit.handler),
        )
        yield client


def test_not_configured_is_501(client, auth):
    response = client.get("/api/v1/events/anything/snapshot", headers=auth)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_configured"


def test_unknown_event_is_404(snapshot_client, auth):
    assert snapshot_client.get("/api/v1/events/nope/snapshot", headers=auth).status_code == 404


def test_snapshot_is_fetched_from_the_heartbeat_address(snapshot_client, auth, kit):
    store = snapshot_client.app.state.runtime.store
    event = detection()
    store.insert(event, "test")
    store.insert(heartbeat(), "test")

    response = snapshot_client.get(f"/api/v1/events/{event.id}/snapshot", headers=auth)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG
    [request] = kit.requests
    assert str(request.url) == "https://172.22.100.21:8971/api/events/17.5-ab/snapshot.jpg"
    assert request.headers["X-Proxy-Secret"] == SECRET


def test_no_heartbeat_yet_is_404_and_nothing_is_fetched(snapshot_client, auth, kit):
    store = snapshot_client.app.state.runtime.store
    event = detection()
    store.insert(event, "test")

    response = snapshot_client.get(f"/api/v1/events/{event.id}/snapshot", headers=auth)

    assert response.status_code == 404
    assert kit.requests == []


def test_non_camera_event_is_404(snapshot_client, auth):
    store = snapshot_client.app.state.runtime.store
    event = heartbeat()
    store.insert(event, "test")
    assert (
        snapshot_client.get(f"/api/v1/events/{event.id}/snapshot", headers=auth).status_code == 404
    )


@pytest.mark.parametrize(
    ("respond", "status", "code"),
    [
        (lambda r: httpx.Response(404), 404, "not_found"),
        (lambda r: httpx.Response(401), 502, "node_refused_secret"),
        (
            lambda r: httpx.Response(
                200,
                content=b"x" * 2048,
                headers={"content-type": "image/jpeg"},
            ),
            502,
            "snapshot_too_large",
        ),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), 504, "node_timeout"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), 502, "node_unreachable"),
    ],
)
def test_kit_failures_map_to_error_codes(snapshot_client, auth, kit, respond, status, code):
    kit.respond = staticmethod(respond)
    store = snapshot_client.app.state.runtime.store
    event = detection()
    store.insert(event, "test")
    store.insert(heartbeat(), "test")

    response = snapshot_client.get(f"/api/v1/events/{event.id}/snapshot", headers=auth)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
