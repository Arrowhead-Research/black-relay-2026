"""The broker side: validate each message against its v0 schema, then keep it.

Everything a node sends is untrusted bytes. Nothing a message holds may stop this thread: paho
runs these callbacks on its network thread, and an exception there ends it.
"""

import json
import logging
import math
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from jsonschema import Draft202012Validator

from relayapi.nodes import NODE_ID_RE, node_address, timestamp_us
from relayapi.settings import Settings
from relayapi.store import FULL, OLD, Store

logger = logging.getLogger(__name__)

TOPIC_DETECTIONS = "detection/camera/detections"
TOPIC_CUE_ACKS = "detection/camera/cue-acks"
TOPIC_STATUS = "detection/camera/status/nodes"
_SCHEMAS = {
    TOPIC_DETECTIONS: "detection.v0.schema.json",
    TOPIC_CUE_ACKS: "cue-ack.v0.schema.json",
    TOPIC_STATUS: "node-status.v0.schema.json",
}
# What a malformed message can raise from JSON handling: bad text, an integer past Python's
# digit limit, a value too large, or nesting past the recursion limit.
_JSON_FAILURES = (ValueError, TypeError, OverflowError, RecursionError)


class _Drop(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def load_schemas(schema_dir: Path) -> dict[str, Draft202012Validator]:
    validators = {}
    for topic, filename in _SCHEMAS.items():
        schema = json.loads((Path(schema_dir) / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validators[topic] = Draft202012Validator(schema)
    return validators


class Ingest:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        clock: Callable[[], float],
        on_address: Callable[[], None] = lambda: None,
    ):
        self._settings = settings
        self._store = store
        self._clock = clock
        self.on_address = on_address
        self._validators = load_schemas(settings.schema_dir)
        self._dropped: Counter[str] = Counter()
        self._lock = threading.Lock()

    def dropped(self) -> dict[str, int]:
        with self._lock:
            return dict(self._dropped)

    def _count(self, reason: str) -> None:
        with self._lock:
            self._dropped[reason] += 1

    def handle(self, topic: str, payload: bytes) -> None:
        """Never raises. The reason for a drop is counted, and the content is never logged."""
        try:
            self._handle(topic, payload)
        except _Drop as drop:
            self._count(drop.reason)
        except Exception:
            self._count("internal_error")
            logger.exception("a message on %s could not be kept", topic)

    def _handle(self, topic: str, payload: bytes) -> None:
        validator = self._validators.get(topic)
        if validator is None:
            raise _Drop("unknown_topic")
        message = self._decode(payload)
        try:
            valid = validator.is_valid(message)
        except _JSON_FAILURES:
            valid = False
        if not valid:
            raise _Drop("schema")
        if not NODE_ID_RE.fullmatch(message["node_id"]):
            raise _Drop("bad_node_id")
        if not NODE_ID_RE.fullmatch(message.get("camera_id", message["node_id"])):
            raise _Drop("bad_node_id")
        try:
            stamped = timestamp_us(message["timestamp"])
        except ValueError:
            raise _Drop("bad_timestamp") from None
        try:
            # ASCII-only, so that a lone surrogate cannot fail later on the way into SQLite.
            text = json.dumps(message, separators=(",", ":"), allow_nan=False)
        except _JSON_FAILURES:
            raise _Drop("not_json") from None
        if topic == TOPIC_STATUS:
            self._heartbeat(message, stamped, text)
        else:
            self._event(topic, message, stamped, text)

    def _decode(self, payload: bytes) -> Any:
        if len(payload) > self._settings.max_payload_bytes:
            raise _Drop("oversize")
        try:
            return json.loads(payload, parse_constant=_refuse_constant, parse_float=_finite_float)
        except _JSON_FAILURES:
            raise _Drop("not_json") from None

    def _heartbeat(self, message: dict, stamped: int, text: str) -> None:
        now = self._clock()
        if stamped > (now + self._settings.future_tolerance_s) * 1_000_000:
            raise _Drop("future_heartbeat")
        raw_address = message["attributes"].get("node_address")
        address = node_address(raw_address)
        if raw_address is not None and address is None:
            self._count("bad_address")
        outcome = self._store.add_heartbeat(
            message["node_id"], stamped, now, address, text, self._settings.max_nodes
        )
        if outcome == OLD:
            raise _Drop("out_of_order_heartbeat")
        if outcome == FULL:
            raise _Drop("too_many_nodes")
        if address is not None:
            self.on_address()

    def _event(self, topic: str, message: dict, stamped: int, text: str) -> None:
        if topic == TOPIC_DETECTIONS:
            kind = "detection"
            durable = message["event"] != "object_update"
        else:
            kind = "cue_ack"
            durable = True
        added = self._store.add_event(
            message["event_id"],
            kind,
            durable,
            message["node_id"],
            message.get("object_id"),
            stamped,
            self._clock(),
            text,
        )
        if not added:
            raise _Drop("duplicate_event")


def make_client(settings: Settings) -> mqtt.Client:
    # A fixed client id and a kept session: the broker queues QoS 1 messages while the API is
    # restarting, and delivers them when it is back.
    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.mqtt_client_id,
        clean_session=False,
    )


class Consumer:
    """Owns the broker connection. With the broker down, nothing else here is affected, and
    paho's thread keeps retrying."""

    def __init__(self, settings: Settings, ingest: Ingest, client_factory=make_client):
        self._settings = settings
        self._ingest = ingest
        self._client = client_factory(settings)
        self.connected = False

    def start(self) -> None:
        client = self._client
        client.username_pw_set(self._settings.broker_user, self._settings.broker_password)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self._settings.broker_host, self._settings.broker_port, keepalive=30)
        client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.connected = False
            logger.warning("the broker refused the connection: %s", reason_code)
            return
        self.connected = True
        client.subscribe([(topic, 1) for topic in _SCHEMAS])
        logger.info("connected to the broker")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        logger.warning("disconnected from the broker; retrying")

    def _on_message(self, client, userdata, message) -> None:
        self._ingest.handle(message.topic, message.payload)
