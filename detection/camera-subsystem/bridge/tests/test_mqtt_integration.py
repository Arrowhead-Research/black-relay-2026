"""Real-paho integration tests against real ``mosquitto`` brokers on loopback.

One broker plays the node-local broker (Frigate's), a second plays the central
broker. The bridge is built by the same CLI wiring the service uses. Skipped, with a
visible reason, when the ``mosquitto`` binary is not installed.
"""

from __future__ import annotations

import json
import queue
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from camerabridge import cli, formats

MOSQUITTO = shutil.which("mosquitto") or shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin")

pytestmark = pytest.mark.skipif(
    MOSQUITTO is None, reason="mosquitto binary not found; real-broker test not run"
)

WAIT_S = 15.0


def wait_until(predicate, timeout=WAIT_S, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Broker:
    """A mosquitto process on a fixed loopback port that can be stopped and restarted.

    Persistence is on so a test subscriber's session survives a restart: the message
    the bridge replays after the restart is queued for it, whichever reconnects first.
    """

    def __init__(self, directory: Path) -> None:
        self.port = free_port()
        directory.mkdir()
        self._conf = directory / "mosquitto.conf"
        self._conf.write_text(
            f"listener {self.port} 127.0.0.1\n"
            "allow_anonymous true\n"
            "max_queued_messages 0\n"
            "persistence true\n"
            f"persistence_location {directory}/\n",
            encoding="utf-8",
        )
        self._proc = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [MOSQUITTO, "-c", str(self._conf)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def accepting() -> bool:
            with socket.socket() as sock:
                sock.settimeout(0.2)
                return sock.connect_ex(("127.0.0.1", self.port)) == 0

        wait_until(accepting, what=f"mosquitto on port {self.port}")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.send_signal(signal.SIGCONT)
            self._proc.terminate()
            self._proc.wait(10)
            self._proc = None

    def save_state(self) -> None:
        """Ask mosquitto to write its database now, so a later crash keeps sessions."""
        db = self._conf.parent / "mosquitto.db"
        before = db.stat().st_mtime_ns if db.exists() else 0
        self._proc.send_signal(signal.SIGUSR1)
        wait_until(lambda: db.exists() and db.stat().st_mtime_ns != before, what="mosquitto db")

    def freeze(self) -> None:
        """Stop the process: the socket stays open but nothing is read or acknowledged."""
        self._proc.send_signal(signal.SIGSTOP)

    def crash(self) -> None:
        self._proc.kill()
        self._proc.wait(10)
        self._proc = None


class Peer:
    """A plain paho client: plays Frigate on the local broker, fusion on the central."""

    def __init__(self, port: int, *, subscribe: str | None = None) -> None:
        self.received: queue.Queue = queue.Queue()
        self._ready = False
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"peer-{uuid.uuid4().hex[:8]}",
            clean_session=False if subscribe else True,
        )
        self._subscribe = subscribe

        def on_connect(client, _userdata, _flags, reason_code, _properties):
            if reason_code.is_failure:
                return
            if self._subscribe:
                client.subscribe(self._subscribe, 1)
            else:
                self._ready = True

        def on_subscribe(*_args):
            self._ready = True

        self._client.on_connect = on_connect
        self._client.on_subscribe = on_subscribe
        self._client.on_message = lambda _c, _u, m: self.received.put((m.topic, bytes(m.payload)))
        self._client.reconnect_delay_set(min_delay=1, max_delay=1)
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()
        wait_until(lambda: self._ready, what="test peer to connect")

    def publish(self, topic: str, payload, *, retain: bool = False, qos: int = 0) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain).wait_for_publish(WAIT_S)

    def next_on(self, topic: str) -> bytes:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            try:
                got_topic, payload = self.received.get(timeout=0.2)
            except queue.Empty:
                continue
            if got_topic == topic:
                return payload
        raise AssertionError(f"nothing received on {topic}")

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class Service:
    """The bridge with its two real paho clients, wired as ``cli.main`` wires them."""

    def __init__(
        self, tmp_path: Path, central_port: int, local_port: int, *, local_keepalive: int = 60
    ) -> None:
        self.outbox_path = tmp_path / "outbox.jsonl"
        cfg = {
            "node_id": "cam-01",
            "covered_zones": ["perimeter-north"],
            "outbox": {"path": str(self.outbox_path)},
            "status_interval_s": 30,
            "broker": {"host": "127.0.0.1", "port": central_port},
            "frigate": {
                "topic": "frigate/events",
                "camera": "cam-01",
                "mqtt_host": "127.0.0.1",
                "mqtt_port": local_port,
            },
        }
        clock = formats.SystemClock()
        clock.mark_synced()
        self.central = cli._build_central_client(cfg)
        self.local = cli._build_local_client(cfg)
        self.bridge = cli.make_bridge(
            cfg, central_client=self.central, local_client=self.local, clock=clock
        )
        self.bridge.wire()
        cli.start_client(self.central, "127.0.0.1", central_port, min_delay=1, max_delay=1)
        self.local_disconnects = 0
        on_local_disconnect = self.local.on_disconnect

        def count_local_disconnect(*args, **kwargs):
            self.local_disconnects += 1
            on_local_disconnect(*args, **kwargs)

        self.local.on_disconnect = count_local_disconnect
        self.local.reconnect_delay_set(min_delay=1, max_delay=1)
        self.local.connect_async("127.0.0.1", local_port, keepalive=local_keepalive)
        self.local.loop_start()

    def outbox_payloads(self) -> list[bytes]:
        if not self.outbox_path.exists():
            return []
        lines = self.outbox_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["bytes"].encode("utf-8") for line in lines if line.strip()]

    def close(self) -> None:
        for client in (self.central, self.local):
            client.loop_stop()
            client.disconnect()


@pytest.fixture
def rig(tmp_path):
    local = Broker(tmp_path / "local")
    central = Broker(tmp_path / "central")
    cleanups = [local.stop, central.stop]
    local.start()
    central.start()

    def service(**kwargs) -> Service:
        svc = Service(tmp_path, central.port, local.port, **kwargs)
        cleanups.insert(0, svc.close)
        return svc

    def peer(port: int, **kwargs) -> Peer:
        p = Peer(port, **kwargs)
        cleanups.insert(0, p.close)
        return p

    yield local, central, service, peer
    for cleanup in cleanups:
        cleanup()


def frigate_new(object_id: str) -> bytes:
    after = {
        "id": object_id,
        "camera": "cam-01",
        "label": "person",
        "score": 0.8,
        "box": [1, 2, 3, 4],
        "area": 100,
        "stationary": False,
        "current_zones": ["perimeter-north"],
    }
    return json.dumps({"type": "new", "before": after, "after": after}).encode()


def object_ids(payloads: list[bytes]) -> list[str]:
    return [json.loads(p)["object_id"] for p in payloads]


def drain(peer: Peer, topic: str, *, settle: float = 1.5) -> list[bytes]:
    """Everything received on ``topic``, once nothing new arrives for ``settle`` seconds."""
    got: list[bytes] = []
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        try:
            got_topic, payload = peer.received.get(timeout=0.1)
        except queue.Empty:
            continue
        if got_topic == topic:
            got.append(payload)
        deadline = time.monotonic() + settle
    return got


def connected_rig(rig):
    local, central, service, peer = rig
    fusion = peer(central.port, subscribe=formats.TOPIC_DETECTIONS)
    svc = service()
    frigate = peer(local.port)
    wait_until(lambda: svc.bridge.central_connected, what="central connection")
    wait_until(lambda: svc.local.is_connected(), what="local connection")
    time.sleep(0.2)  # let the local SUBACK land before Frigate publishes
    return local, central, fusion, svc, frigate


def test_events_survive_central_outage_and_replay_once_in_order_byte_identical(rig):
    local, central, fusion, svc, frigate = connected_rig(rig)

    central.stop()
    wait_until(lambda: not svc.bridge.central_connected, what="central disconnect")

    names = ["trk-0", "trk-1", "trk-2", "trk-3"]
    for name in names:
        frigate.publish("frigate/events", frigate_new(name))
    wait_until(lambda: len(svc.outbox_payloads()) == len(names), what="events to reach the outbox")
    persisted = svc.outbox_payloads()
    assert object_ids(persisted) == names
    assert svc.local.is_connected()  # the local side never noticed the outage
    # The outbox alone holds them: paho must not be carrying its own copies.
    assert len(svc.central._out_messages) == 0

    central.start()
    wait_until(lambda: svc.outbox_payloads() == [], what="PUBACKs to empty the outbox")
    assert drain(fusion, formats.TOPIC_DETECTIONS) == persisted  # exact bytes, order, count


def test_message_in_flight_at_disconnect_is_delivered_exactly_once(rig):
    _local, central, fusion, svc, frigate = connected_rig(rig)
    central.save_state()  # keep fusion's session through the crash below
    central.freeze()

    frigate.publish("frigate/events", frigate_new("trk-inflight"))
    wait_until(lambda: len(svc.central._out_messages) == 1, what="the publish to be in flight")
    persisted = svc.outbox_payloads()
    assert object_ids(persisted) == ["trk-inflight"]

    central.crash()
    wait_until(lambda: not svc.bridge.central_connected, what="central disconnect")
    assert object_ids(svc.outbox_payloads()) == ["trk-inflight"]  # unacked, so still held

    central.start()
    wait_until(lambda: svc.outbox_payloads() == [], what="the PUBACK to empty the outbox")
    # Not one copy from paho's retained queue plus one from the replay.
    assert drain(fusion, formats.TOPIC_DETECTIONS) == persisted
    assert len(svc.central._out_messages) == 0


def test_local_events_survive_a_large_replay(tmp_path, rig):
    """A large replay must not trap the local thread: with a 2 s keepalive, holding the
    lock past 3 s gets the local client dropped, and Frigate's events are lost."""
    from camerabridge.outbox import Outbox

    backlog = 3000
    local, central, service, peer = rig
    fusion = peer(central.port, subscribe=formats.TOPIC_DETECTIONS)
    seeded = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=64 * 2**20, max_age_s=1e9)
    clock = formats.FixedClock("2026-11-04T19:40:00Z")
    seeded.enqueue_many(
        [
            (
                formats.TOPIC_DETECTIONS,
                formats.detection_event(
                    node_id="cam-01",
                    event="object_start",
                    object_id=f"seed-{i}",
                    label="person",
                    confidence=0.8,
                    zones=["perimeter-north"],
                    attributes={},
                    clock=clock,
                ),
            )
            for i in range(backlog)
        ]
    )
    del seeded

    frigate = peer(local.port)
    central.stop()  # so the local side is up and subscribed before the replay begins
    svc = service(local_keepalive=2)
    wait_until(lambda: svc.local.is_connected(), what="local connection")
    time.sleep(0.3)
    central.start()
    wait_until(lambda: svc.bridge.central_connected, what="central connection")

    live = [f"live-{i}" for i in range(40)]
    for name in live:
        frigate.publish("frigate/events", frigate_new(name))
        time.sleep(0.05)

    wait_until(lambda: svc.outbox_payloads() == [], timeout=120, what="the backlog to drain")
    received = object_ids(drain(fusion, formats.TOPIC_DETECTIONS))
    assert svc.local_disconnects == 0
    assert received == [f"seed-{i}" for i in range(backlog)] + live


def test_paho_exposes_the_thread_and_retained_state_the_service_relies_on(rig):
    # The fatal-exit check and the replay rely on paho internals; a paho upgrade that
    # renames them must fail here.
    _local, _central, _fusion, svc, _frigate = connected_rig(rig)
    assert cli._loop_alive(svc.central) is True
    assert cli._loop_alive(svc.local) is True
    svc.local.loop_stop()
    assert cli._loop_alive(svc.local) is False
    svc.local.loop_start()


def test_frigate_health_tracked_and_central_stays_up_when_local_dies(rig):
    local, central, service, peer = rig
    svc = service()
    frigate = peer(local.port)
    wait_until(lambda: svc.bridge.central_connected, what="central connection")
    wait_until(lambda: svc.local.is_connected(), what="local connection")
    time.sleep(0.2)

    frigate.publish("frigate/available", "online", retain=True)
    frigate.publish("frigate/cam-01/recordings/state", "ON", retain=True)
    frigate.publish(
        "frigate/stats",
        json.dumps(
            {
                "cameras": {"cam-01": {"camera_fps": 5.0, "detection_enabled": True}},
                "detectors": {"hailo": {"inference_speed": 9.0, "pid": 42}},
                "processes": {"recording": {"pid": 77}},
                "service": {"uptime": 3600, "last_updated": time.time()},
            }
        ),
    )

    def healthy() -> bool:
        s = svc.bridge.node_state()
        return s.camera_online and s.detector_ok and s.recording_ok and s.link_online

    wait_until(healthy, what="all-healthy node state")

    fusion = peer(central.port, subscribe=formats.TOPIC_STATUS)
    local.stop()
    wait_until(lambda: not svc.local.is_connected(), what="local disconnect")

    # Losing the local broker means no Frigate data: fail safe, while the central
    # link is untouched and the heartbeat still goes out.
    wait_until(lambda: not svc.bridge.node_state().recording_ok, what="health to fail safe")
    state = svc.bridge.node_state()
    assert (state.camera_online, state.detector_ok, state.recording_ok) == (False, False, False)
    assert state.link_online is True

    svc.bridge.publish_status(state)
    heartbeat = json.loads(fusion.next_on(formats.TOPIC_STATUS))
    assert heartbeat["recording_ok"] is False
    assert heartbeat["link_online"] is True
