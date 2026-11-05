"""Integration smoke tests for ``camerabridge.bridge`` — the service wiring.

The bridge is the one component that joins Tasks 2-6 into a running service: a
Frigate MQTT subscription → :func:`frigate.parse_frigate_event` →
:func:`formats.detection_event` → durable :class:`outbox.Outbox` → publish; an RF
cue subscription → :func:`cues.handle_cue` → cue-ack + ``rf_cue_id`` correlation;
and a periodic status heartbeat. The MQTT client is **injected**, so these tests
drive a fake client and never touch a broker.

Review-Focus invariants proven here (carried decisions 1-4):

* a malformed Frigate payload on the wire is skipped and the service keeps
  processing later events (no crash);
* with the clock not-ready, a valid Frigate event produces **no** publish (no
  untrusted timestamp); durable events are held and stamped when the clock syncs;
* a Frigate ``new`` → a schema-valid ``camera.detection.v0`` with
  ``event:object_start`` on ``TOPIC_DETECTIONS`` at QoS 1;
* an ``object_update`` → published best-effort at QoS 0;
* a matching RF cue → a ``cue-ack`` ``matched:true`` **and** the next detection
  for that track carries ``attributes.rf_cue_id``.

Every emitted envelope is validated against its Task-1 JSON Schema so the wiring
can never drift from the contract.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator

from camerabridge import bridge as bridge_module
from camerabridge import formats
from camerabridge.bridge import Bridge
from camerabridge.cues import CueMatcher
from camerabridge.outbox import Outbox
from camerabridge.status import NodeState

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FIXED_TS = "2026-11-04T19:40:00Z"
FIXED_EPOCH = 1000.0


class FakeClock:
    """A ready clock that always returns a fixed RFC-3339 ``Z`` timestamp."""

    def __init__(self, ts: str = FIXED_TS) -> None:
        self._ts = ts

    def now_rfc3339(self) -> str:
        return self._ts


class _FakeInfo:
    """Mirrors paho's ``MQTTMessageInfo``: the broker-assigned ``mid`` and result code."""

    def __init__(self, mid: int, rc: int = 0) -> None:
        self.mid = mid
        self.rc = rc


class FakeMqttClient:
    """A minimal injected MQTT client: records publishes and subscriptions.

    ``publish`` returns a fresh monotonic ``mid`` like paho, so the outbox can map
    it back for ack. The bridge assigns ``on_connect``/``on_message``/
    ``on_publish`` callbacks; tests drive the bridge's public seams directly.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self._next_mid = 1
        # paho's retained-message state, which the bridge discards on connect.
        self._out_message_mutex = threading.RLock()
        self._out_messages: dict[int, object] = {}
        self._inflight_messages = 0
        self.on_connect = None
        self.on_message = None
        self.on_publish = None

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        mid = self._next_mid
        self._next_mid += 1
        return _FakeInfo(mid)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))


def load_schema(name: str) -> dict:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def validate(schema_file: str, message: dict) -> None:
    validator = Draft202012Validator(load_schema(schema_file))
    errors = sorted(validator.iter_errors(message), key=str)
    assert errors == [], [e.message for e in errors]


def frigate_payload(
    type_: str,
    object_id: str = "trk-1",
    zones=("perimeter-north",),
    label: str = "person",
    score: float = 0.81,
) -> dict:
    """A raw ``frigate/events`` payload mirroring the fixture's before/after shape."""
    after = {
        "id": object_id,
        "camera": "cam-01",
        "label": label,
        "score": score,
        "box": [120, 80, 64, 180],
        "area": 11520,
        "stationary": False,
        "current_zones": list(zones),
    }
    return {"type": type_, "before": dict(after), "after": after}


def make_bridge(
    client,
    *,
    tmp_path,
    clock=None,
    matcher=None,
    outbox=None,
    time_fn=lambda: FIXED_EPOCH,
    local_client=None,
    connected=True,
    **bridge_kwargs,
) -> Bridge:
    """Build a bridge whose ``client`` is the central connection.

    ``connected=True`` (the default) performs the central connect, so tests of the
    steady-state publish paths see a live link. Outage tests pass ``connected=False``.
    """
    clock = clock or FakeClock()
    matcher = matcher or CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    outbox = outbox or Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    bridge = Bridge(
        central_client=client,
        local_client=local_client or FakeMqttClient(),
        outbox=outbox,
        matcher=matcher,
        clock=clock,
        node_id="cam-01",
        frigate_topic="frigate/events",
        frigate_base_url="http://cam-01.local:5000",
        time_fn=time_fn,
        **bridge_kwargs,
    )
    if connected:
        bridge.handle_central_connect()
    return bridge


def ack_all(bridge, client) -> None:
    """Play the broker: PUBACK every QoS-1 publish until the send window stops moving."""
    acked = 0
    while acked < len(client.published):
        mid = acked + 1  # FakeMqttClient hands out mids 1, 2, 3, ... in publish order
        if client.published[acked][2] == 1:
            bridge._paho_on_publish(client, None, mid, None, None)
        acked += 1


def decode_published(client) -> list[tuple[str, dict, int]]:
    out: list[tuple[str, dict, int]] = []
    for topic, payload, qos in client.published:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        out.append((topic, json.loads(payload), qos))
    return out


# --- Frigate detection path -------------------------------------------------


def test_frigate_new_publishes_valid_object_start(tmp_path):
    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)

    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())

    pubs = decode_published(client)
    dets = [(t, m, q) for t, m, q in pubs if t == formats.TOPIC_DETECTIONS]
    assert len(dets) == 1
    topic, msg, qos = dets[0]
    assert msg["schema"] == "camera.detection.v0"
    assert msg["event"] == "object_start"
    assert msg["object_id"] == "trk-1"
    assert msg["camera_id"] == msg["node_id"] == "cam-01"
    assert qos == 1  # object_start is durable (QoS 1)
    validate("detection.v0.schema.json", msg)


def test_object_update_published_at_qos0(tmp_path):
    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)

    bridge.dispatch("frigate/events", json.dumps(frigate_payload("update", "trk-1")).encode())

    pubs = decode_published(client)
    dets = [(t, m, q) for t, m, q in pubs if t == formats.TOPIC_DETECTIONS]
    assert len(dets) == 1
    _topic, msg, qos = dets[0]
    assert msg["event"] == "object_update"
    assert qos == 0  # object_update is best-effort (QoS 0), not persisted
    validate("detection.v0.schema.json", msg)


def test_malformed_payload_skipped_then_service_recovers(tmp_path):
    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)

    # Invalid JSON on the wire — must not raise, must not publish.
    bridge.dispatch("frigate/events", b"not-json{{{")
    # Valid JSON but not a well-formed Frigate event — parser returns None, skip.
    bridge.dispatch("frigate/events", json.dumps({"type": "bogus"}).encode())
    assert client.published == []

    # The service keeps processing: a later valid event still publishes.
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-9")).encode())
    pubs = decode_published(client)
    assert any(t == formats.TOPIC_DETECTIONS and m["object_id"] == "trk-9" for t, m, _q in pubs)


def test_clock_not_ready_holds_detection(tmp_path):
    client = FakeMqttClient()
    clock = formats.SystemClock()  # un-synced: now_rfc3339 raises ClockNotReady

    bridge = make_bridge(client, tmp_path=tmp_path, clock=clock)
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())

    # No untrusted timestamp is emitted: nothing publishes, and it is held in memory.
    assert client.published == []
    assert not (tmp_path / "outbox.jsonl").exists()

    clock.mark_synced()
    bridge.publish_status(NodeState(True, True, True, True))
    dets = [m for t, m, _q in decode_published(client) if t == formats.TOPIC_DETECTIONS]
    assert [m["object_id"] for m in dets] == ["trk-1"]
    validate("detection.v0.schema.json", dets[0])


# --- RF cue correlation path ------------------------------------------------


def test_matching_cue_acks_and_tags_next_detection(tmp_path):
    client = FakeMqttClient()
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    bridge = make_bridge(client, tmp_path=tmp_path, matcher=matcher, time_fn=lambda: FIXED_EPOCH)

    # 1) A detection establishes an open track in the covered zone.
    bridge.dispatch(
        "frigate/events",
        json.dumps(frigate_payload("new", "trk-1", ("perimeter-north",))).encode(),
    )

    # 2) A matching RF cue (same zone, within the ±window of the track's time).
    cue = {
        "schema": "rf.camera-cue.v0",
        "rf_cue_id": "rf-cue-7",
        "zone": "perimeter-north",
        "ts_epoch": FIXED_EPOCH,
        "source_node": "rf-node-07",
        "tpms_id": "tpms-1",
    }
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())

    pubs = decode_published(client)
    acks = [(m, q) for t, m, q in pubs if t == formats.TOPIC_CUE_ACKS]
    assert len(acks) == 1
    ack, ack_qos = acks[0]
    assert ack["matched"] is True
    assert ack["object_id"] == "trk-1"
    assert ack["rf_cue_id"] == "rf-cue-7"
    assert ack_qos == 1  # cue-ack is durable (QoS 1)
    validate("cue-ack.v0.schema.json", ack)

    # 3) The NEXT detection for trk-1 carries attributes.rf_cue_id.
    bridge.dispatch(
        "frigate/events",
        json.dumps(frigate_payload("update", "trk-1", ("perimeter-north",))).encode(),
    )
    pubs2 = decode_published(client)
    dets = [m for t, m, _q in pubs2 if t == formats.TOPIC_DETECTIONS and m["object_id"] == "trk-1"]
    # The first (object_start, before the cue) had no rf_cue_id; the one after does.
    assert "rf_cue_id" not in dets[0]["attributes"]
    assert dets[-1]["attributes"].get("rf_cue_id") == "rf-cue-7"


def test_unmatched_cue_acks_matched_false(tmp_path):
    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)

    cue = {"rf_cue_id": "rf-cue-9", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())

    pubs = decode_published(client)
    acks = [m for t, m, _q in pubs if t == formats.TOPIC_CUE_ACKS]
    assert len(acks) == 1
    assert acks[0]["matched"] is False
    assert acks[0]["object_id"] is None
    validate("cue-ack.v0.schema.json", acks[0])


def test_malformed_cue_skipped(tmp_path):
    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, b"{not valid json")
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps({"zone": "perimeter-north"}).encode())
    assert client.published == []


def test_clock_not_ready_holds_cue_ack(tmp_path):
    client = FakeMqttClient()
    clock = formats.SystemClock()
    bridge = make_bridge(client, tmp_path=tmp_path, clock=clock)
    cue = {"rf_cue_id": "rf-cue-9", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    assert client.published == []

    clock.mark_synced()
    bridge.publish_status(NodeState(True, True, True, True))
    acks = [m for t, m, _q in decode_published(client) if t == formats.TOPIC_CUE_ACKS]
    assert [m["rf_cue_id"] for m in acks] == ["rf-cue-9"]
    validate("cue-ack.v0.schema.json", acks[0])


# --- bounded state over uptime (expire) -------------------------------------


def test_expire_tracks_retires_stale_matcher_track(tmp_path):
    client = FakeMqttClient()
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    bridge = make_bridge(client, tmp_path=tmp_path, matcher=matcher, time_fn=lambda: FIXED_EPOCH)

    # A detection records an open track.
    bridge.dispatch(
        "frigate/events", json.dumps(frigate_payload("new", "trk-1", ("perimeter-north",))).encode()
    )
    assert len(matcher._tracks) == 1

    # Much later: expire retires the stale track so the table does not grow forever.
    bridge.expire_tracks(now=FIXED_EPOCH + 10_000)
    assert matcher._tracks == {}

    # A cue that would have matched the now-retired track acks matched:false.
    cue = {"rf_cue_id": "rf-cue-1", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH + 10_000}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    acks = [m for t, m, _q in decode_published(client) if t == formats.TOPIC_CUE_ACKS]
    assert acks[-1]["matched"] is False


def test_pending_cue_cleared_on_object_end(tmp_path):
    client = FakeMqttClient()
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    bridge = make_bridge(client, tmp_path=tmp_path, matcher=matcher, time_fn=lambda: FIXED_EPOCH)

    bridge.dispatch(
        "frigate/events", json.dumps(frigate_payload("new", "trk-1", ("perimeter-north",))).encode()
    )
    cue = {"rf_cue_id": "rf-cue-7", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    assert bridge._pending_cue  # a match is pending attachment

    # The track ends: the object_end carries the cue, and the pending state clears.
    bridge.dispatch(
        "frigate/events", json.dumps(frigate_payload("end", "trk-1", ("perimeter-north",))).encode()
    )
    assert bridge._pending_cue == {}
    # Any later detection for that id does NOT re-attach the (now consumed) cue.
    bridge.dispatch(
        "frigate/events",
        json.dumps(frigate_payload("update", "trk-1", ("perimeter-north",))).encode(),
    )
    dets = [m for t, m, _q in decode_published(client) if t == formats.TOPIC_DETECTIONS]
    assert "rf_cue_id" not in dets[-1]["attributes"]


def test_pending_cue_aged_out_when_track_never_reappears(tmp_path):
    client = FakeMqttClient()
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    bridge = make_bridge(client, tmp_path=tmp_path, matcher=matcher, time_fn=lambda: FIXED_EPOCH)

    bridge.dispatch(
        "frigate/events", json.dumps(frigate_payload("new", "trk-1", ("perimeter-north",))).encode()
    )
    cue = {"rf_cue_id": "rf-cue-7", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    assert bridge._pending_cue  # pending, but the track never produces a next detection

    # Heartbeat-driven expiry ages out the orphaned pending cue (no unbounded leak).
    bridge.expire_tracks(now=FIXED_EPOCH + 10_000)
    assert bridge._pending_cue == {}


# --- connect / replay + status ----------------------------------------------


def test_handle_connect_subscribes_and_replays_outbox(tmp_path):
    client = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    queued = formats.detection_event(
        node_id="cam-01",
        event="object_start",
        object_id="trk-queued",
        label="person",
        confidence=0.9,
        zones=["perimeter-north"],
        attributes={},
        clock=FakeClock(),
    )
    outbox.enqueue(formats.TOPIC_DETECTIONS, queued, durable=True)

    local = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path, outbox=outbox, local_client=local, connected=False)
    bridge.handle_central_connect()
    bridge.handle_local_connect()

    # Each inbound topic is subscribed at QoS 1 (M2) on its own connection, so a
    # reconnect blip cannot drop an inbound object_start/object_end or RF cue, and
    # Frigate topics never touch the central broker.
    assert dict(client.subscriptions) == {formats.TOPIC_RF_CUES_IN: 1}
    assert dict(local.subscriptions)["frigate/events"] == 1
    assert formats.TOPIC_RF_CUES_IN not in dict(local.subscriptions)
    # The durable message queued during the "outage" is replayed on connect.
    pubs = decode_published(client)
    assert any(m["object_id"] == "trk-queued" for _t, m, _q in pubs)


def test_on_publish_acks_outbox_entry(tmp_path):
    client = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    bridge = make_bridge(client, tmp_path=tmp_path, outbox=outbox)

    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    # The broker ack for that publish removes the entry from the outbox file.
    published_mid = client.published[0]  # the publish happened
    assert published_mid is not None
    # The PUBACK for mid=1 (the first/only durable publish), via the real callback.
    bridge.wire()
    client.on_publish(client, None, 1, None, None)
    path = tmp_path / "outbox.jsonl"
    remaining = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert remaining == []


def test_publish_status_emits_durable_heartbeat(tmp_path):
    from camerabridge.status import NodeState

    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)
    bridge.publish_status(
        NodeState(camera_online=True, link_online=True, recording_ok=True, detector_ok=True)
    )
    pubs = decode_published(client)
    statuses = [(m, q) for t, m, q in pubs if t == formats.TOPIC_STATUS]
    assert len(statuses) == 1
    msg, qos = statuses[0]
    assert qos == 1
    validate("node-status.v0.schema.json", msg)


def test_publish_status_dropped_when_clock_not_ready(tmp_path):
    from camerabridge.status import NodeState

    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path, clock=formats.SystemClock())
    bridge.publish_status(
        NodeState(camera_online=True, link_online=True, recording_ok=True, detector_ok=True)
    )
    assert client.published == []


# --- multi-zone cue matching (I2) -------------------------------------------


def test_multi_zone_track_matches_cue_for_any_occupied_zone(tmp_path):
    client = FakeMqttClient()
    matcher = CueMatcher(covered_zones={"perimeter-north", "driveway"}, window_s=5.0)
    bridge = make_bridge(client, tmp_path=tmp_path, matcher=matcher, time_fn=lambda: FIXED_EPOCH)

    # A track occupying BOTH zones at once (Frigate zones overlap).
    bridge.dispatch(
        "frigate/events",
        json.dumps(frigate_payload("new", "trk-1", ("perimeter-north", "driveway"))).encode(),
    )
    # A cue for the SECOND zone must still match (not just zones[0]).
    cue = {"rf_cue_id": "rf-cue-2", "zone": "driveway", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())

    acks = [m for t, m, _q in decode_published(client) if t == formats.TOPIC_CUE_ACKS]
    assert acks[-1]["matched"] is True
    assert acks[-1]["object_id"] == "trk-1"
    assert acks[-1]["zone"] == "driveway"


# --- concurrency: shared state is serialized (C1) ---------------------------


def test_concurrent_dispatch_and_heartbeat_no_race(tmp_path):
    import threading

    from camerabridge.status import NodeState

    client = FakeMqttClient()
    bridge = make_bridge(client, tmp_path=tmp_path)
    errors: list[BaseException] = []
    rounds = 400

    def feed():
        try:
            for i in range(rounds):
                bridge.dispatch(
                    "frigate/events",
                    json.dumps(frigate_payload("new", f"trk-{i % 11}", ("perimeter-north",))).encode(),
                )
                bridge.dispatch(
                    formats.TOPIC_RF_CUES_IN,
                    json.dumps(
                        {"rf_cue_id": f"c{i}", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
                    ).encode(),
                )
        except BaseException as exc:  # noqa: BLE001 - surface any race to the assert
            errors.append(exc)

    def beat():
        try:
            for i in range(rounds):
                bridge.publish_status(NodeState(True, True, True, True))
                bridge.expire_tracks(now=FIXED_EPOCH + i)
                bridge._paho_on_publish(client, None, i + 1, None, None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=feed), threading.Thread(target=beat)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No "dictionary changed size during iteration" (or any other) race.
    assert errors == [], errors
    # State stayed consistent and usable afterwards.
    bridge.expire_tracks(now=FIXED_EPOCH + 10_000)
    assert bridge._pending_cue == {}


# --- broker-down at boot does not crash (I1) --------------------------------


class _BrokerDownClient:
    """A fake whose synchronous ``connect`` would raise — proves we don't call it.

    ``connect_async`` records the attempt (paho's non-raising, retry-on-loop path).
    """

    def __init__(self):
        self.connect_async_calls = []
        self.loop_started = False
        self.reconnect_delay = None

    def connect(self, *a, **k):  # pragma: no cover - must never be called
        raise ConnectionRefusedError("broker is down at boot")

    def connect_async(self, host, port, *a, **k):
        self.connect_async_calls.append((host, port))

    def reconnect_delay_set(self, *, min_delay, max_delay):
        self.reconnect_delay = (min_delay, max_delay)

    def loop_start(self):
        self.loop_started = True


def test_start_client_survives_broker_down_boot():
    from camerabridge import cli

    client = _BrokerDownClient()
    # Must not raise even though a synchronous connect would have.
    cli.start_client(client, "10.0.0.2", 1883)
    assert client.connect_async_calls == [("10.0.0.2", 1883)]
    assert client.loop_started is True
    assert client.reconnect_delay is not None


# --- node_id validation (M1) ------------------------------------------------


@pytest.mark.parametrize(
    "bad_node_id",
    [
        "CAM_01",  # uppercase + underscore
        "cam_01",  # underscore
        "-cam-01",  # leading hyphen
        "cam 01",  # space
        "cam-01\n",  # trailing newline: Python `$` would pass, fullmatch rejects
        "",  # empty
        42,  # not a string
    ],
)
def test_make_bridge_rejects_invalid_node_id(tmp_path, bad_node_id):
    from camerabridge import cli

    cfg = {
        "node_id": bad_node_id,
        "covered_zones": ["perimeter-north"],
        "outbox": {"path": str(tmp_path / "outbox.jsonl")},
        "frigate": {"topic": "frigate/events", "base_url": "http://x"},
    }
    with pytest.raises(ValueError, match="node_id"):
        cli.make_bridge(
            cfg, central_client=FakeMqttClient(), local_client=FakeMqttClient(), clock=FakeClock()
        )


def test_make_bridge_accepts_valid_node_id(tmp_path):
    from camerabridge import cli

    cfg = {
        "node_id": "cam-01",
        "covered_zones": ["perimeter-north"],
        "outbox": {"path": str(tmp_path / "outbox.jsonl")},
        "frigate": {"topic": "frigate/events", "base_url": "http://x"},
    }
    bridge = cli.make_bridge(
        cfg, central_client=FakeMqttClient(), local_client=FakeMqttClient(), clock=FakeClock()
    )
    assert isinstance(bridge, Bridge)


# --- CLI config plumbing ----------------------------------------------------


def test_load_config_reads_example_yaml():
    from camerabridge import cli

    cfg = cli.load_config(str(CONFIG_DIR / "bridge.example.yaml"))
    assert cfg["node_id"]
    assert "perimeter-north" in cfg["covered_zones"]
    assert cfg["broker"]["host"]
    assert cfg["frigate"]["base_url"]


def test_make_bridge_from_config_builds_bridge(tmp_path):
    from camerabridge import cli

    cfg = cli.load_config(str(CONFIG_DIR / "bridge.example.yaml"))
    cfg["outbox"]["path"] = str(tmp_path / "outbox.jsonl")  # keep the test off /var
    client = FakeMqttClient()
    bridge = cli.make_bridge(
        cfg, central_client=client, local_client=FakeMqttClient(), clock=FakeClock()
    )
    assert isinstance(bridge, Bridge)
    bridge.handle_central_connect()
    # Covered zones from config reach the matcher: a cue in a covered zone with a
    # matching track correlates.
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    assert any(t == formats.TOPIC_DETECTIONS for t, _p, _q in client.published)


# --- two MQTT connections: local (Frigate) + central (fusion) ----------------


class _ConnackRC:
    """Stands in for paho's ``ReasonCode`` on the VERSION2 callback signature."""

    def __init__(self, is_failure: bool) -> None:
        self.is_failure = is_failure


def outbox_lines(tmp_path) -> list[dict]:
    path = tmp_path / "outbox.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_central_down_frigate_event_persisted_and_replayed_byte_identical(tmp_path):
    central, local = FakeMqttClient(), FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, local_client=local, connected=False)
    bridge.wire()

    # Central never came up; Frigate's object_start arrives on the LOCAL connection.
    message = type("M", (), {"topic": "frigate/events", "retain": False})()
    message.payload = json.dumps(frigate_payload("new", "trk-outage")).encode()
    local.on_message(local, None, message)

    assert central.published == []  # nothing handed to paho while central is down
    lines = outbox_lines(tmp_path)
    assert len(lines) == 1
    persisted = lines[0]["bytes"].encode("utf-8")
    assert json.loads(persisted)["object_id"] == "trk-outage"

    # Central comes up: the entry is replayed with its exact original bytes.
    central.on_connect(central, None, None, _ConnackRC(False), None)
    assert central.published == [(formats.TOPIC_DETECTIONS, persisted, 1)]

    # The PUBACK empties the outbox.
    central.on_publish(central, None, 1, _ConnackRC(False), None)
    assert outbox_lines(tmp_path) == []


def test_object_update_not_published_while_central_down(tmp_path):
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, connected=False)
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("update", "trk-1")).encode())
    assert central.published == []
    assert outbox_lines(tmp_path) == []  # QoS 0 updates are never persisted


def test_central_disconnect_stops_publishing_until_reconnect(tmp_path):
    central, local = FakeMqttClient(), FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, local_client=local, connected=False)
    bridge.wire()
    central.on_connect(central, None, None, _ConnackRC(False), None)
    assert bridge.central_connected is True

    central.on_disconnect(central, None, None, _ConnackRC(True), None)
    assert bridge.central_connected is False
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-2")).encode())
    assert central.published == []
    assert len(outbox_lines(tmp_path)) == 1


def test_refused_central_connack_is_not_a_connection(tmp_path):
    # paho VERSION2 also fires on_connect for a refused CONNACK (e.g. a bad password).
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, connected=False)
    bridge.wire()
    central.on_connect(central, None, None, _ConnackRC(True), None)
    assert bridge.central_connected is False
    assert central.subscriptions == []


def test_refused_local_connack_does_not_subscribe(tmp_path):
    local = FakeMqttClient()
    bridge = make_bridge(FakeMqttClient(), tmp_path=tmp_path, local_client=local)
    bridge.wire()
    local.on_connect(local, None, None, _ConnackRC(True), None)
    assert local.subscriptions == []
    local.on_connect(local, None, None, _ConnackRC(False), None)
    assert ("frigate/events", 1) in local.subscriptions


def test_local_connection_does_not_drive_acks_or_replay(tmp_path):
    central, local = FakeMqttClient(), FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, local_client=local, connected=False)
    bridge.wire()
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-3")).encode())

    local.on_connect(local, None, None, _ConnackRC(False), None)
    assert central.published == []  # local connect must not replay to central
    assert local.published == []  # and nothing is ever published on the local broker
    assert local.on_publish is None
    assert bridge.central_connected is False


def test_on_publish_callback_does_not_block_on_bridge_lock(tmp_path):
    """paho holds its _out_message_mutex while calling on_publish, and publish() takes
    that mutex. If on_publish blocked on the bridge lock, a thread that holds the lock
    and is inside publish() would deadlock with the network thread."""
    import threading

    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.wire()
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-4")).encode())
    assert len(outbox_lines(tmp_path)) == 1

    release = threading.Event()
    holding = threading.Event()

    def hold_lock():
        with bridge._lock:
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    holding.wait(5)
    try:
        callback = threading.Thread(
            target=central.on_publish, args=(central, None, 1, _ConnackRC(False), None), daemon=True
        )
        callback.start()
        callback.join(1.0)
        assert not callback.is_alive(), "on_publish blocked on the bridge lock"
    finally:
        release.set()
        holder.join()

    # The ack was deferred, not lost: the next lock-holding entry point applies it.
    assert len(outbox_lines(tmp_path)) == 1
    bridge.expire_tracks()
    assert outbox_lines(tmp_path) == []


class _NetClient:
    """Records the async connect the CLI performs (no socket)."""

    def __init__(self) -> None:
        self.connect_async_calls: list[tuple[str, int]] = []
        self.loop_started = False
        self.loop_stopped = False
        self.thread_dies = False
        self._out_message_mutex = threading.RLock()
        self._out_messages: dict[int, object] = {}
        self._inflight_messages = 0

    @property
    def _thread(self):
        """Stands in for the thread paho's loop_start() creates."""
        alive = self.loop_started and not self.thread_dies
        return type("T", (), {"is_alive": lambda _self: alive})()

    def reconnect_delay_set(self, *, min_delay, max_delay):
        self.delay = (min_delay, max_delay)

    def connect_async(self, host, port):
        self.connect_async_calls.append((host, port))

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=0):
        return _FakeInfo(1)

    def subscribe(self, topic, qos=0):
        pass


def _run_main_one_tick(monkeypatch, tmp_path, cfg_overrides):
    """Run cli.main for one heartbeat with fake clients; return (central, local)."""
    import yaml

    from camerabridge import cli

    cfg = cli.load_config(str(CONFIG_DIR / "bridge.example.yaml"))
    cfg["outbox"]["path"] = str(tmp_path / "outbox.jsonl")
    cfg.update(cfg_overrides)
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    central, local = _NetClient(), _NetClient()
    monkeypatch.setattr(cli, "_build_central_client", lambda _cfg: central)
    monkeypatch.setattr(cli, "_build_local_client", lambda _cfg: local)
    monkeypatch.setattr(cli, "_ntp_synchronized", lambda: True)

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    assert cli.main(["run", "--config", str(config_path)]) == 0
    return central, local


def test_cli_dials_central_and_local_brokers_independently(monkeypatch, tmp_path):
    central, local = _run_main_one_tick(
        monkeypatch,
        tmp_path,
        {
            "broker": {"host": "10.0.0.2", "port": 1884},
            "frigate": {"topic": "frigate/events", "base_url": "http://x", "camera": "cam-01"},
        },
    )
    assert central.connect_async_calls == [("10.0.0.2", 1884)]
    # frigate.mqtt_host / mqtt_port default to the node-local broker.
    assert local.connect_async_calls == [("127.0.0.1", 1883)]
    assert central.loop_started and local.loop_started
    assert central.loop_stopped and local.loop_stopped


def test_cli_requires_a_central_broker_host(monkeypatch, tmp_path):
    # Defaulting the central host would silently publish fusion traffic to the local
    # broker and report the uplink as healthy.
    with pytest.raises(ValueError, match="broker.host"):
        _run_main_one_tick(monkeypatch, tmp_path, {"broker": {"port": 1883}})


# --- real health in camera.node-status.v0 ------------------------------------


class _Mono:
    """An injected monotonic clock."""

    def __init__(self, now: float = 500.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


WALL = 1_800_000_000.0


def frigate_stats(camera: str = "cam-01", *, last_updated: float = WALL) -> bytes:
    return json.dumps(
        {
            "cameras": {camera: {"camera_fps": 5.0, "detection_enabled": True}},
            "detectors": {"hailo": {"inference_speed": 9.5, "detection_start": 0.0, "pid": 42}},
            "processes": {"recording": {"pid": 77}},
            "service": {"uptime": 3600, "last_updated": last_updated},
        }
    ).encode()


HEALTHY_STATS = frigate_stats()


def healthy_link_file(tmp_path) -> Path:
    path = tmp_path / "state.json"
    report = {
        "state": "adopted",
        "updated": WALL,
        "detector_error": False,
        "log_read_at": WALL,
        "record_writing": True,
    }
    path.write_text(json.dumps(report))
    return path


def health_bridge(tmp_path, mono, central=None, local=None, connected=True):
    from camerabridge.health import FrigateHealth

    return make_bridge(
        central or FakeMqttClient(),
        tmp_path=tmp_path,
        local_client=local,
        connected=connected,
        health=FrigateHealth(
            camera="cam-01",
            stale_after_s=90.0,
            monotonic_fn=mono,
            wall_fn=lambda: WALL,
            link_state_path=healthy_link_file(tmp_path),
        ),
    )


def feed_frigate_health(bridge) -> None:
    # Raw wire payloads: "online" and "ON" are not JSON and must still get through.
    bridge.dispatch("frigate/available", b"online")
    bridge.dispatch("frigate/cam-01/recordings/state", b"ON")
    bridge.dispatch("frigate/stats", HEALTHY_STATS)


def test_local_connect_subscribes_to_frigate_health_topics(tmp_path):
    local = FakeMqttClient()
    bridge = health_bridge(tmp_path, _Mono(), local=local)
    bridge.handle_local_connect()
    subs = {topic for topic, _qos in local.subscriptions}
    assert subs == {
        "frigate/events",
        "frigate/available",
        "frigate/stats",
        "frigate/cam-01/recordings/state",
    }


def test_node_state_healthy_and_heartbeat_validates(tmp_path):
    central = FakeMqttClient()
    bridge = health_bridge(tmp_path, _Mono(), central=central)
    feed_frigate_health(bridge)

    state = bridge.node_state()
    assert (state.camera_online, state.detector_ok, state.recording_ok, state.link_online) == (
        True,
        True,
        True,
        True,
    )
    bridge.publish_status(state)
    statuses = [m for t, m, _q in decode_published(central) if t == formats.TOPIC_STATUS]
    assert statuses[0]["camera_online"] is True
    validate("node-status.v0.schema.json", statuses[0])


def test_node_state_no_data_is_all_false(tmp_path):
    bridge = health_bridge(tmp_path, _Mono(), connected=False)
    state = bridge.node_state()
    assert (state.camera_online, state.detector_ok, state.recording_ok, state.link_online) == (
        False,
        False,
        False,
        False,
    )


def test_link_online_follows_central_connection(tmp_path):
    bridge = health_bridge(tmp_path, _Mono(), connected=False)
    assert bridge.node_state().link_online is False
    bridge.handle_central_connect()
    assert bridge.node_state().link_online is True
    bridge.handle_central_disconnect()
    assert bridge.node_state().link_online is False


def test_local_disconnect_invalidates_retained_frigate_state(tmp_path):
    # available/recordings are only re-published on change, so a dead local broker
    # would otherwise leave recording_ok frozen at True.
    local = FakeMqttClient()
    bridge = health_bridge(tmp_path, _Mono(), local=local)
    bridge.wire()
    feed_frigate_health(bridge)
    assert bridge.node_state().recording_ok is True

    local.on_disconnect(local, None, None, _ConnackRC(True), None)
    state = bridge.node_state()
    assert (state.camera_online, state.detector_ok, state.recording_ok) == (False, False, False)
    assert state.link_online is True  # central is unaffected


def test_retained_stats_message_is_not_treated_as_fresh(tmp_path):
    # Staleness is measured from receipt, so an old retained message must not count.
    local = FakeMqttClient()
    bridge = health_bridge(tmp_path, _Mono(), local=local)
    bridge.wire()
    message = type("M", (), {"topic": "frigate/stats", "payload": HEALTHY_STATS, "retain": True})()
    bridge.dispatch("frigate/available", b"online")
    local.on_message(local, None, message)
    assert bridge.node_state().camera_online is False


def test_heartbeat_tick_reports_stale_stats_false(tmp_path):
    mono = _Mono()
    bridge = health_bridge(tmp_path, mono)
    feed_frigate_health(bridge)
    mono.now += 90.001
    assert bridge.node_state().camera_online is False


def test_stale_limit_is_three_status_intervals(tmp_path):
    from camerabridge import cli

    cfg = {
        "node_id": "cam-01",
        "outbox": {"path": str(tmp_path / "outbox.jsonl")},
        "status_interval_s": 10,
        "frigate": {"topic": "site1/events", "camera": "front"},
    }
    mono = _Mono()
    bridge = cli.make_bridge(
        cfg,
        central_client=FakeMqttClient(),
        local_client=FakeMqttClient(),
        clock=FakeClock(),
        monotonic_fn=mono,
    )
    # The health topics follow the prefix of the configured events topic.
    bridge.dispatch("site1/available", b"online")
    bridge.dispatch("site1/front/recordings/state", b"ON")
    bridge.dispatch(
        "site1/stats",
        frigate_stats("front", last_updated=time.time()),
    )
    mono.now += 30.0
    assert bridge.node_state().camera_online is True
    mono.now += 0.001
    assert bridge.node_state().camera_online is False


def test_cli_heartbeat_reports_measured_health_not_optimistic_defaults(monkeypatch, tmp_path):
    published: list = []
    from camerabridge import cli

    monkeypatch.setattr(
        _NetClient,
        "publish",
        lambda self, topic, payload, qos=0: (published.append((topic, payload)), _FakeInfo(1))[1],
    )
    # Mark central up as soon as the client "connects", with no Frigate data at all.
    real_make = cli.make_bridge

    def make_and_connect(*a, **k):
        bridge = real_make(*a, **k)
        def start_and_connect(self):
            self.loop_started = True
            bridge.handle_central_connect()

        monkeypatch.setattr(_NetClient, "loop_start", start_and_connect)
        return bridge

    monkeypatch.setattr(cli, "make_bridge", make_and_connect)
    _run_main_one_tick(monkeypatch, tmp_path, {})
    statuses = [json.loads(p) for t, p in published if t == formats.TOPIC_STATUS]
    assert statuses, "no heartbeat published"
    assert statuses[0]["camera_online"] is False
    assert statuses[0]["detector_ok"] is False
    assert statuses[0]["recording_ok"] is False
    assert statuses[0]["link_online"] is True


# --- hold durable events until NTP sync (DESIGN.md 7.5) ----------------------

SYNC_WALL = 1_800_000_000.0  # the trusted wall clock once NTP has synced
HEARTBEAT = NodeState(True, True, True, True)


def held_bridge(tmp_path, *, central=None, connected=True, **kwargs):
    """A bridge on an un-synced SystemClock with injected wall and monotonic time."""
    clock = formats.SystemClock()
    mono = _Mono(100.0)
    wall = {"now": 5.0}  # the untrusted boot-time wall clock
    central = central or FakeMqttClient()
    bridge = make_bridge(
        central,
        tmp_path=tmp_path,
        clock=clock,
        connected=connected,
        time_fn=lambda: wall["now"],
        monotonic_fn=mono,
        **kwargs,
    )
    return bridge, central, clock, mono, wall


def sync(clock, wall) -> None:
    wall["now"] = SYNC_WALL
    clock.mark_synced()


def feed(bridge, type_, object_id) -> None:
    bridge.dispatch("frigate/events", json.dumps(frigate_payload(type_, object_id)).encode())


def rfc3339(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_held_event_stamped_now_minus_elapsed_monotonic(tmp_path):
    bridge, central, clock, mono, wall = held_bridge(tmp_path)

    feed(bridge, "new", "trk-early")  # captured at monotonic 100
    mono.now = 130.0
    feed(bridge, "end", "trk-early")  # captured at monotonic 130
    mono.now = 160.0

    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)

    dets = [m for t, m, _q in decode_published(central) if t == formats.TOPIC_DETECTIONS]
    assert [(m["event"], m["timestamp"]) for m in dets] == [
        ("object_start", rfc3339(SYNC_WALL - 60.0)),
        ("object_end", rfc3339(SYNC_WALL - 30.0)),
    ]


def test_held_events_release_in_capture_order_before_later_events(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    bridge.dispatch(
        formats.TOPIC_RF_CUES_IN,
        json.dumps({"rf_cue_id": "c-1", "zone": "perimeter-north", "ts_epoch": 0.0}).encode(),
    )
    feed(bridge, "end", "trk-a")

    sync(clock, wall)
    # The first thing after sync is a NEW event: it must not jump the held ones.
    feed(bridge, "new", "trk-b")

    ids = [m.get("object_id") if t == formats.TOPIC_DETECTIONS else m["rf_cue_id"]
           for t, m, _q in decode_published(central)]
    assert ids == ["trk-a", "c-1", "trk-a", "trk-b"]


def test_no_placeholder_timestamp_reaches_the_wire_or_disk(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)
    for line in outbox_lines(tmp_path):
        assert not json.loads(line["bytes"])["timestamp"].startswith("1970")
    published = decode_published(central)
    assert published and not any(m["timestamp"].startswith("1970") for _t, m, _q in published)
    assert published[0][1]["timestamp"] == rfc3339(SYNC_WALL)


def test_nothing_released_while_clock_still_unsynced(tmp_path):
    bridge, central, _clock, _mono, _wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    bridge.publish_status(HEARTBEAT)
    feed(bridge, "new", "trk-b")
    assert central.published == []
    assert not (tmp_path / "outbox.jsonl").exists()


def test_object_update_and_status_still_dropped_while_unsynced(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "update", "trk-a")
    bridge.publish_status(HEARTBEAT)
    sync(clock, wall)
    feed(bridge, "new", "trk-b")  # flushes anything held
    messages = decode_published(central)
    assert [m["object_id"] for _t, m, _q in messages] == ["trk-b"]


def test_held_release_with_central_down_lands_in_outbox_for_replay(tmp_path):
    bridge, central, clock, mono, wall = held_bridge(tmp_path, connected=False)
    feed(bridge, "new", "trk-a")
    mono.now += 10.0
    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)

    assert central.published == []
    lines = outbox_lines(tmp_path)
    assert [line["topic"] for line in lines] == [formats.TOPIC_DETECTIONS, formats.TOPIC_STATUS]
    persisted = lines[0]["bytes"].encode("utf-8")
    assert json.loads(persisted)["timestamp"] == rfc3339(SYNC_WALL - 10.0)

    bridge.handle_central_connect()
    assert central.published[0] == (formats.TOPIC_DETECTIONS, persisted, 1)


def test_central_connect_after_sync_releases_held_without_duplicates(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path, connected=False)
    feed(bridge, "new", "trk-a")
    sync(clock, wall)
    bridge.handle_central_connect()
    assert [m["object_id"] for _t, m, _q in decode_published(central)] == ["trk-a"]


def test_held_cap_drops_oldest_with_warning(tmp_path, caplog):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path, held_cap=2)
    with caplog.at_level("WARNING", logger="camerabridge.bridge"):
        for name in ("trk-1", "trk-2", "trk-3"):
            feed(bridge, "new", name)
    assert any("oldest" in r.getMessage() for r in caplog.records)

    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)
    dets = [m["object_id"] for t, m, _q in decode_published(central) if t == formats.TOPIC_DETECTIONS]
    assert dets == ["trk-2", "trk-3"]


def test_default_held_cap_is_10000():
    assert bridge_module._DEFAULT_HELD_CAP == 10_000


def test_event_id_minted_at_capture_is_kept(tmp_path, monkeypatch):
    ids = iter(["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"])
    monkeypatch.setattr(formats, "new_event_id", lambda: next(ids))
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)
    assert decode_published(central)[0][1]["event_id"] == "11111111-1111-4111-8111-111111111111"


def _matched_cue(bridge, wall, cue_id="rf-7") -> None:
    cue = {"rf_cue_id": cue_id, "zone": "perimeter-north", "ts_epoch": wall["now"]}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())


def test_presync_matched_cue_ack_holds_match_and_tags_next_held_detection(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    _matched_cue(bridge, wall)
    assert "trk-a" in bridge._pending_cue

    feed(bridge, "end", "trk-a")  # carries the cue, and consumes it
    assert bridge._pending_cue == {}

    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)
    messages = decode_published(central)
    acks = [m for t, m, _q in messages if t == formats.TOPIC_CUE_ACKS]
    dets = [m for t, m, _q in messages if t == formats.TOPIC_DETECTIONS]
    # The match decided at capture survives, even though the track is gone by release.
    assert (acks[0]["matched"], acks[0]["object_id"]) == (True, "trk-a")
    assert "rf_cue_id" not in dets[0]["attributes"]
    assert dets[1]["attributes"]["rf_cue_id"] == "rf-7"


def test_presync_dropped_update_keeps_pending_cue_for_next_detection(tmp_path):
    bridge, central, clock, _mono, wall = held_bridge(tmp_path)
    feed(bridge, "new", "trk-a")
    _matched_cue(bridge, wall)

    feed(bridge, "update", "trk-a")  # dropped, so it cannot have delivered the cue
    assert "trk-a" in bridge._pending_cue

    feed(bridge, "end", "trk-a")
    assert bridge._pending_cue == {}
    sync(clock, wall)
    bridge.publish_status(HEARTBEAT)
    dets = [m for t, m, _q in decode_published(central) if t == formats.TOPIC_DETECTIONS]
    assert dets[-1]["attributes"]["rf_cue_id"] == "rf-7"


def test_presync_unmatched_cue_leaves_no_pending(tmp_path):
    bridge, _central, _clock, _mono, wall = held_bridge(tmp_path)
    _matched_cue(bridge, wall)
    assert bridge._pending_cue == {}


def test_concurrent_presync_capture_and_sync_keeps_every_event_once(tmp_path):
    import threading

    bridge, central, clock, _mono, wall = held_bridge(tmp_path, held_cap=10_000)
    names = [f"trk-{i}" for i in range(300)]

    def capture():
        for name in names:
            feed(bridge, "new", name)

    def heartbeat():
        for i in range(100):
            if i == 50:
                sync(clock, wall)
            bridge.publish_status(HEARTBEAT)

    threads = [threading.Thread(target=capture), threading.Thread(target=heartbeat)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bridge.publish_status(HEARTBEAT)
    ack_all(bridge, central)

    dets = [m["object_id"] for t, m, _q in decode_published(central) if t == formats.TOPIC_DETECTIONS]
    assert dets == names  # all delivered, once, in capture order


# --- C1: on_disconnect must not wait for the bridge lock ---------------------


class _MutexClient(FakeMqttClient):
    """publish() waits on a mutex, as paho's publish() waits on _out_message_mutex."""

    def __init__(self) -> None:
        super().__init__()
        self.mutex = threading.Lock()
        self.in_publish = threading.Event()

    def publish(self, topic, payload, qos=0):
        self.in_publish.set()
        with self.mutex:
            return super().publish(topic, payload, qos)


def test_central_on_disconnect_does_not_deadlock_with_publish(tmp_path):
    """paho's _handle_connack holds _out_message_mutex, calls loop_write, and a socket
    error fires on_disconnect, while the local thread holds the bridge lock inside
    central.publish(), waiting for that mutex."""
    central = _MutexClient()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.wire()

    central.mutex.acquire()  # the network thread is inside _handle_connack
    local_thread = threading.Thread(
        target=bridge.dispatch,
        args=("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode()),
        daemon=True,
    )
    local_thread.start()
    assert central.in_publish.wait(5)  # local thread: holds the lock, waits for the mutex

    disconnect = threading.Thread(
        target=central.on_disconnect, args=(central, None, None, _ConnackRC(True), None), daemon=True
    )
    disconnect.start()
    disconnect.join(1.0)
    alive = disconnect.is_alive()
    central.mutex.release()
    local_thread.join(5)
    assert not alive, "on_disconnect waited for the bridge lock"
    assert bridge.central_connected is False


# --- C2: one rewrite per lock hold, however fast acks arrive -----------------


def _seed(outbox, count):
    outbox.enqueue_many(
        [(formats.TOPIC_DETECTIONS, {"event_id": f"e{i}", "object_id": f"s{i}"}) for i in range(count)]
    )


def _count_rewrites(monkeypatch, outbox, during=None) -> list:
    calls: list[int] = []
    real = outbox._rewrite

    def counting():
        calls.append(1)
        if during is not None:
            during(len(calls))
        real()

    monkeypatch.setattr(outbox, "_rewrite", counting)
    return calls


def test_posted_acks_are_applied_with_one_rewrite(tmp_path, monkeypatch):
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 20)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox)
    rewrites = _count_rewrites(monkeypatch, outbox)

    for mid in range(1, 21):
        bridge._acks.put(mid)
    bridge.expire_tracks()

    assert len(rewrites) == 1
    assert outbox_lines(tmp_path) == []


def test_acks_arriving_during_a_rewrite_do_not_extend_the_lock_hold(tmp_path, monkeypatch):
    # PUBACKs arrive faster than one rewrite + fsync. A drain that loops until the
    # mailbox is empty holds the lock until the whole backlog is gone.
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 60)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox)
    next_mid = iter(range(2, 61))

    def another_ack_arrives(_count):
        mid = next(next_mid, None)
        if mid is not None:
            bridge._acks.put(mid)

    rewrites = _count_rewrites(monkeypatch, outbox, during=another_ack_arrives)
    bridge._acks.put(1)
    with bridge._lock:
        bridge._drain_acks()
    assert len(rewrites) == 1


def test_ack_posted_during_a_lock_hold_is_applied_when_the_holder_releases(tmp_path):
    # The poster's try-acquire fails while another thread holds the lock, and this may
    # be the last ack of the window, so the holder has to pick it up on release.
    class AckedDuringPublish(FakeMqttClient):
        def publish(self, topic, payload, qos=0):
            info = super().publish(topic, payload, qos)
            poster = threading.Thread(target=bridge._paho_on_publish, args=(self, None, info.mid))
            poster.start()
            poster.join()
            return info

    central = AckedDuringPublish()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    assert outbox_lines(tmp_path) == []


# --- I1: send window, publish result codes, no stacked copies ----------------

MQTT_ERR_QUEUE_SIZE = 15


def test_reconnect_sends_a_window_and_acks_advance_it(tmp_path):
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 50)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox, connected=False)
    bridge.wire()

    central.on_connect(central, None, None, _ConnackRC(False), None)
    assert len(central.published) == 20

    central.on_publish(central, None, 1, None, None)
    central.on_publish(central, None, 2, None, None)
    assert len(central.published) == 22  # acks open the window with no other trigger

    ack_all(bridge, central)
    sent = [json.loads(p)["object_id"] for _t, p, _q in central.published]
    assert sent == [f"s{i}" for i in range(50)]  # each once, in order
    assert outbox_lines(tmp_path) == []


def test_connect_discards_paho_retained_copies_before_replay(tmp_path):
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 3)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox)
    bridge.wire()
    assert len(central.published) == 3

    # The link dropped with those three unacked: paho keeps copies and would re-send
    # them on CONNACK alongside the replay.
    central.on_disconnect(central, None, None, _ConnackRC(True), None)
    central._out_messages.update({1: "copy", 2: "copy", 3: "copy"})
    central._inflight_messages = 3

    central.on_connect(central, None, None, _ConnackRC(False), None)
    assert central._out_messages == {}
    assert central._inflight_messages == 0
    assert len(central.published) == 6  # the replay is now the only copy


def test_refused_connack_keeps_paho_state(tmp_path):
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, connected=False)
    bridge.wire()
    central._out_messages[1] = "copy"
    central.on_connect(central, None, None, _ConnackRC(True), None)
    assert central._out_messages == {1: "copy"}


def test_rejected_publish_is_not_marked_sent_and_colliding_puback_keeps_entry(tmp_path):
    class RejectingClient(FakeMqttClient):
        reject = True

        def publish(self, topic, payload, qos=0):
            if self.reject:
                # paho returns the colliding mid along with MQTT_ERR_QUEUE_SIZE.
                self.published.append((topic, payload, qos))
                return _FakeInfo(7, rc=MQTT_ERR_QUEUE_SIZE)
            return super().publish(topic, payload, qos)

    central = RejectingClient()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.wire()
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    assert len(central.published) == 1

    # A PUBACK for the colliding mid belongs to some other message.
    central.on_publish(central, None, 7, None, None)
    assert len(outbox_lines(tmp_path)) == 1

    # The entry was not marked sent, so a later flush retries it.
    central.reject = False
    central.published.clear()
    bridge.publish_status(HEARTBEAT)
    assert central.published[0][0] == formats.TOPIC_DETECTIONS


def test_rejected_publish_stops_the_flush_to_preserve_order(tmp_path):
    class RejectOnce(FakeMqttClient):
        rejected = False

        def publish(self, topic, payload, qos=0):
            if not self.rejected:
                self.rejected = True
                return _FakeInfo(0, rc=MQTT_ERR_QUEUE_SIZE)
            return super().publish(topic, payload, qos)

    central = RejectOnce()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 3)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox, connected=False)
    bridge.handle_central_connect()
    assert central.published == []  # nothing overtook the rejected head entry


# --- M1: acks posted before a reconnect count ---------------------------------


def test_acks_posted_before_reconnect_are_applied_before_replay(tmp_path):
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.wire()
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    assert len(central.published) == 1

    bridge._acks.put(1)  # acked, but not yet applied when the link drops
    central.on_disconnect(central, None, None, _ConnackRC(True), None)
    central.on_connect(central, None, None, _ConnackRC(False), None)

    assert len(central.published) == 1  # not re-sent: replay would have forgotten mid 1
    assert outbox_lines(tmp_path) == []


# --- I3: updates dropped while central is down keep the pending cue ----------


def test_update_dropped_while_central_down_keeps_pending_cue(tmp_path):
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path)
    bridge.wire()
    feed(bridge, "new", "trk-1")
    cue = {"rf_cue_id": "rf-9", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())

    central.on_disconnect(central, None, None, _ConnackRC(True), None)
    feed(bridge, "update", "trk-1")  # dropped, so it cannot have delivered the cue
    assert "trk-1" in bridge._pending_cue

    feed(bridge, "end", "trk-1")
    central.on_connect(central, None, None, _ConnackRC(False), None)
    ends = [
        m
        for t, m, _q in decode_published(central)
        if t == formats.TOPIC_DETECTIONS and m["event"] == "object_end"
    ]
    assert ends[-1]["attributes"]["rf_cue_id"] == "rf-9"


# --- I4: a clock flip mid-handler must not let an event overtake held ones ---


class _FlipClock:
    """Not ready for the first ``not_ready_reads`` reads, ready ever after."""

    def __init__(self, not_ready_reads: int) -> None:
        self.reads_left = not_ready_reads
        self.ready_ts = FIXED_TS

    def now_rfc3339(self) -> str:
        if self.reads_left > 0:
            self.reads_left -= 1
            raise formats.ClockNotReady("not yet")
        return self.ready_ts


def _flip_bridge(tmp_path):
    clock = _FlipClock(not_ready_reads=10_000)
    central = FakeMqttClient()
    bridge = make_bridge(central, tmp_path=tmp_path, clock=clock, monotonic_fn=_Mono())
    feed(bridge, "new", "trk-held")
    assert central.published == []
    return bridge, central, clock


def _tick_until_ready(bridge, clock) -> None:
    """Heartbeat past any remaining not-ready reads, then once more to release."""
    while clock.reads_left > 0:
        bridge.publish_status(HEARTBEAT)
    bridge.publish_status(HEARTBEAT)


# The flip lands after the handler's 1st or 2nd clock read, so a handler that reads the
# clock separately for release and for stamping sees the two disagree on one of these.
@pytest.mark.parametrize("not_ready_reads", [1, 2, 3])
def test_clock_flip_mid_frigate_handler_cannot_overtake_held(tmp_path, not_ready_reads):
    bridge, central, clock = _flip_bridge(tmp_path)
    clock.reads_left = not_ready_reads
    feed(bridge, "new", "trk-new")
    _tick_until_ready(bridge, clock)
    ids = [m["object_id"] for t, m, _q in decode_published(central) if t == formats.TOPIC_DETECTIONS]
    assert ids == ["trk-held", "trk-new"]


@pytest.mark.parametrize("not_ready_reads", [1, 2, 3])
def test_clock_flip_mid_cue_handler_cannot_overtake_held(tmp_path, not_ready_reads):
    bridge, central, clock = _flip_bridge(tmp_path)
    clock.reads_left = not_ready_reads
    cue = {"rf_cue_id": "rf-1", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    _tick_until_ready(bridge, clock)
    topics = [t for t, _m, _q in decode_published(central) if t != formats.TOPIC_STATUS]
    assert topics == [formats.TOPIC_DETECTIONS, formats.TOPIC_CUE_ACKS]


def test_clock_flip_mid_status_does_not_overtake_or_stamp(tmp_path):
    bridge, central, clock = _flip_bridge(tmp_path)
    clock.reads_left = 1
    bridge.publish_status(HEARTBEAT)  # this tick read "not ready": no release, no stamp
    assert central.published == []
    bridge.publish_status(HEARTBEAT)
    topics = [t for t, _m, _q in decode_published(central)]
    assert topics == [formats.TOPIC_DETECTIONS, formats.TOPIC_STATUS]


# --- I5: OSError in a callback sets a fatal flag; main exits nonzero ---------


def _disk_full(*_a, **_k):
    raise OSError(28, "No space left on device")


def test_oserror_in_on_message_sets_fatal_instead_of_raising(tmp_path, monkeypatch):
    central, local = FakeMqttClient(), FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox, local_client=local)
    bridge.wire()
    assert bridge.fatal is False
    monkeypatch.setattr(outbox, "_append", _disk_full)
    message = type("M", (), {"topic": "frigate/events", "retain": False})()
    message.payload = json.dumps(frigate_payload("new", "trk-1")).encode()
    local.on_message(local, None, message)  # must not raise into paho
    assert bridge.fatal is True


def test_oserror_in_on_publish_sets_fatal_instead_of_raising(tmp_path, monkeypatch):
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    _seed(outbox, 1)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox)
    bridge.wire()
    monkeypatch.setattr(outbox, "_rewrite", _disk_full)
    central.on_publish(central, None, 1, None, None)
    assert bridge.fatal is True


def test_oserror_in_on_connect_sets_fatal_instead_of_raising(tmp_path, monkeypatch):
    # A connect releases held events into the outbox, which writes to disk.
    clock = formats.SystemClock()
    central = FakeMqttClient()
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    bridge = make_bridge(central, tmp_path=tmp_path, outbox=outbox, clock=clock, connected=False)
    bridge.wire()
    feed(bridge, "new", "trk-1")
    clock.mark_synced()
    monkeypatch.setattr(outbox, "_append", _disk_full)
    central.on_connect(central, None, None, _ConnackRC(False), None)
    assert bridge.fatal is True


def _run_main_rc(monkeypatch, tmp_path, *, setup=None):
    """Run cli.main with fake clients; ``setup(bridge, central, local)`` runs before it."""
    import yaml

    from camerabridge import cli

    cfg = cli.load_config(str(CONFIG_DIR / "bridge.example.yaml"))
    cfg["outbox"]["path"] = str(tmp_path / "outbox.jsonl")
    config_path = tmp_path / "bridge.yaml"
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    central, local = _NetClient(), _NetClient()
    monkeypatch.setattr(cli, "_build_central_client", lambda _cfg: central)
    monkeypatch.setattr(cli, "_build_local_client", lambda _cfg: local)
    monkeypatch.setattr(cli, "_ntp_synchronized", lambda: True)
    real_make = cli.make_bridge

    def make(*a, **k):
        bridge = real_make(*a, **k)
        if setup:
            setup(bridge, central, local)
        return bridge

    monkeypatch.setattr(cli, "make_bridge", make)
    ticks = {"count": 0}

    def sleep(_seconds):
        ticks["count"] += 1
        if ticks["count"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)
    return cli.main(["run", "--config", str(config_path)])


def test_main_exits_zero_on_keyboard_interrupt_when_healthy(monkeypatch, tmp_path):
    assert _run_main_rc(monkeypatch, tmp_path) == 0


def test_main_exits_nonzero_when_bridge_is_fatal(monkeypatch, tmp_path):
    def setup(bridge, _central, _local):
        bridge._fatal = True

    assert _run_main_rc(monkeypatch, tmp_path, setup=setup) == 1


@pytest.mark.parametrize("which", ["central", "local"])
def test_main_exits_nonzero_when_a_client_thread_dies(monkeypatch, tmp_path, which):
    def setup(_bridge, central, local):
        (central if which == "central" else local).thread_dies = True

    assert _run_main_rc(monkeypatch, tmp_path, setup=setup) == 1


def test_main_exits_nonzero_on_oserror_in_the_heartbeat(monkeypatch, tmp_path):
    def setup(bridge, _central, _local):
        monkeypatch.setattr(bridge, "publish_status", _disk_full)

    assert _run_main_rc(monkeypatch, tmp_path, setup=setup) == 1
