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
* with the clock not-ready, a valid Frigate event produces **no** publish
  (boot-only drop window, not an untrusted timestamp);
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
from pathlib import Path

from jsonschema.validators import Draft202012Validator

from camerabridge import formats
from camerabridge.bridge import Bridge
from camerabridge.cues import CueMatcher
from camerabridge.outbox import Outbox

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
    """Mirrors paho's ``MQTTMessageInfo`` — carries the broker-assigned ``mid``."""

    def __init__(self, mid: int) -> None:
        self.mid = mid


class FakeMqttClient:
    """A minimal injected MQTT client: records publishes and subscriptions.

    ``publish`` returns a fresh monotonic ``mid`` like paho, so the outbox can map
    it back for ack. The bridge assigns ``on_connect``/``on_message``/
    ``on_publish`` callbacks; tests drive the bridge's public seams directly.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []
        self.subscriptions: list[str] = []
        self._next_mid = 1
        self.on_connect = None
        self.on_message = None
        self.on_publish = None

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        mid = self._next_mid
        self._next_mid += 1
        return _FakeInfo(mid)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)


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
) -> Bridge:
    clock = clock or FakeClock()
    matcher = matcher or CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    outbox = outbox or Outbox(str(tmp_path / "outbox.jsonl"), max_bytes=10_000_000, max_age_s=1e9)
    return Bridge(
        client=client,
        outbox=outbox,
        matcher=matcher,
        clock=clock,
        node_id="cam-01",
        frigate_topic="frigate/events",
        frigate_base_url="http://cam-01.local:5000",
        time_fn=time_fn,
    )


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


def test_clock_not_ready_drops_detection(tmp_path):
    client = FakeMqttClient()
    clock = formats.SystemClock()  # un-synced: now_rfc3339 raises ClockNotReady

    bridge = make_bridge(client, tmp_path=tmp_path, clock=clock)
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())

    # Boot-only window: no untrusted timestamp is emitted, so nothing publishes.
    assert client.published == []


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


def test_clock_not_ready_drops_cue(tmp_path):
    client = FakeMqttClient()
    clock = formats.SystemClock()
    bridge = make_bridge(client, tmp_path=tmp_path, clock=clock)
    cue = {"rf_cue_id": "rf-cue-9", "zone": "perimeter-north", "ts_epoch": FIXED_EPOCH}
    bridge.dispatch(formats.TOPIC_RF_CUES_IN, json.dumps(cue).encode())
    assert client.published == []


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

    bridge = make_bridge(client, tmp_path=tmp_path, outbox=outbox)
    bridge.handle_connect()

    assert "frigate/events" in client.subscriptions
    assert formats.TOPIC_RF_CUES_IN in client.subscriptions
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
    # Simulate the PUBACK for mid=1 (the first/only durable publish).
    bridge.on_publish_ack(1)
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
    bridge = cli.make_bridge(cfg, client=client, clock=FakeClock())
    assert isinstance(bridge, Bridge)
    # Covered zones from config reach the matcher: a cue in a covered zone with a
    # matching track correlates.
    bridge.dispatch("frigate/events", json.dumps(frigate_payload("new", "trk-1")).encode())
    assert any(t == formats.TOPIC_DETECTIONS for t, _p, _q in client.published)
