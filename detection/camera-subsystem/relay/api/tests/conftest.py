"""A relay API wired to fakes: an MQTT client, a node's Frigate and MediaMTX's control API."""

import asyncio
import json
import types
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from relayapi.app import create_app
from relayapi.settings import Settings

TOKEN = "correct-horse-battery-staple"
BROKER_USER = "relay"
BROKER_PASSWORD = "br0ker%2F/p#w@rd:x"
SCHEMAS = Path(__file__).resolve().parents[3] / "bridge" / "schemas"
NOW = "2026-11-05T12:00:10Z"
DETECTIONS = "detection/camera/detections"
CUE_ACKS = "detection/camera/cue-acks"
STATUS = "detection/camera/status/nodes"


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def heartbeat(
    node_id="cam-01",
    timestamp="2026-11-05T12:00:00Z",
    address="192.168.8.21",
    **fields,
):
    attributes = fields.pop("attributes", {})
    if address is not None:
        attributes = {**attributes, "node_address": address}
    return {
        "schema": "camera.node-status.v0",
        "event_id": str(uuid.uuid4()),
        "timestamp": timestamp,
        "node_id": node_id,
        "camera_online": True,
        "link_online": True,
        "recording_ok": True,
        "detector_ok": True,
        "attributes": attributes,
        **fields,
    }


def detection(event="object_start", event_id=None, timestamp="2026-11-05T12:00:00Z", **fields):
    return {
        "schema": "camera.detection.v0",
        "event_id": event_id or str(uuid.uuid4()),
        "timestamp": timestamp,
        "node_id": "cam-01",
        "camera_id": "cam-01",
        "event": event,
        "object_id": "1730749200.12-abc",
        "label": "person",
        "confidence": 0.87,
        "zones": ["perimeter-north"],
        "attributes": {},
        **fields,
    }


def cue_ack(event_id=None):
    return {
        "schema": "camera.cue-ack.v0",
        "event_id": event_id or str(uuid.uuid4()),
        "timestamp": "2026-11-05T12:00:00Z",
        "node_id": "cam-01",
        "rf_cue_id": "cue-1",
        "zone": "perimeter-north",
        "matched": False,
        "object_id": None,
        "attributes": {},
    }


class FakeMqtt:
    """The parts of paho's client the API drives."""

    def __init__(self):
        self.credentials = None
        self.subscriptions = []
        self.started = False
        self.stopped = False
        self.connect_target = None
        self.on_connect = self.on_disconnect = self.on_message = None

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.connect_target = (host, port)

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        pass

    def subscribe(self, topics):
        self.subscriptions.append(topics)
        return (0, 1)

    def connect(self):
        code = types.SimpleNamespace(is_failure=False)
        self.on_connect(self, None, types.SimpleNamespace(session_present=False), code, None)

    def drop(self):
        code = types.SimpleNamespace(is_failure=True)
        self.on_disconnect(self, None, None, code, None)

    def deliver(self, topic, payload):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=payload, qos=1))


class FakeMediaMtx:
    """The control API's in-memory path configuration."""

    def __init__(self):
        self.paths = {}
        self.calls = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("control API down", request=request)
        action, name = request.url.path.removeprefix("/v3/config/paths/").split("/", 1)
        if action == "get":
            if name in self.paths:
                return httpx.Response(200, json=self.paths[name])
            return httpx.Response(404, json={"error": "path not found"})
        if action == "add":
            if name in self.paths:
                return httpx.Response(400, json={"error": "path already exists"})
            self.paths[name] = json.loads(request.content)
            return httpx.Response(200)
        if action == "patch":
            if name not in self.paths:
                return httpx.Response(404, json={"error": "path not found"})
            self.paths[name].update(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(500)

    @property
    def writes(self):
        return [c for c in self.calls if c.method != "GET"]


class Chunks(httpx.AsyncByteStream):
    """A body that arrives as these chunks, like one read from a socket."""

    def __init__(self, chunks, delay=0.0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


class FakeNode:
    """A node's Frigate. Tests replace ``respond``."""

    def __init__(self):
        self.requests = []
        self.respond = lambda request: httpx.Response(200, content=b"frigate")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.respond(request)
        if asyncio.iscoroutine(result):
            result = await result
        if result.is_stream_consumed:
            # Built with content=, which real transports never give. Keeps its Content-Length.
            return httpx.Response(
                result.status_code, headers=result.headers, stream=Chunks([result.content])
            )
        return result


class Relay:
    def __init__(self, tmp_path, monkeypatch=None, **overrides):
        self.tmp_path = tmp_path
        self.mqtt = FakeMqtt()
        self.mediamtx = FakeMediaMtx()
        self.node = FakeNode()
        self.now = epoch(NOW)
        self.overrides = overrides
        self.app = None
        self.http = None

    def settings(self):
        values = dict(
            api_token=TOKEN,
            broker_host="broker.test",
            broker_port=1883,
            broker_user=BROKER_USER,
            broker_password=BROKER_PASSWORD,
            mediamtx_api_url="http://mediamtx.test:9997",
            mediamtx_api_user="relay-api",
            mediamtx_api_password="control-pass",
            db_path=self.tmp_path / "relay.sqlite3",
            schema_dir=SCHEMAS,
            path_sync_interval_s=3600.0,
        )
        values.update(self.overrides)
        return Settings(**values)

    def start(self):
        self.app = create_app(
            self.settings(),
            mqtt_client_factory=lambda settings: self.mqtt,
            node_transport=httpx.MockTransport(self.node),
            mediamtx_transport=httpx.MockTransport(self.mediamtx),
            clock=lambda: self.now,
        )
        self.http = TestClient(self.app, headers={"Authorization": f"Bearer {TOKEN}"})
        self.http.__enter__()
        self.mqtt.connect()
        return self

    def stop(self):
        if self.http is not None:
            self.http.__exit__(None, None, None)
            self.http = None

    def restart(self):
        self.stop()
        self.mqtt = FakeMqtt()
        return self.start()

    def sync_paths(self, full=True):
        self.http.portal.call(self.app.state.path_sync.sync_once, full)

    def dropped(self):
        return self.http.get("/nodes").json()["broker"]["dropped"]


@pytest.fixture
def make_relay(tmp_path):
    relays = []

    def make(**overrides):
        relay = Relay(tmp_path, **overrides).start()
        relays.append(relay)
        return relay

    yield make
    for relay in relays:
        relay.stop()


@pytest.fixture
def relay(make_relay):
    return make_relay()
