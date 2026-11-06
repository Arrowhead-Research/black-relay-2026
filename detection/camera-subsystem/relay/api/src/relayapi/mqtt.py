"""The broker side: validate each message against its v0 schema, then keep it.

Everything a node sends is untrusted bytes. Nothing a message holds may stop this thread: paho
runs these callbacks on its network thread, and an exception there ends it.

A QoS 1 message is acknowledged only once it is on disk, or once it is certain it never will be
(a bad message, which being sent again cannot improve). The node deletes it from its outbox on
that acknowledgement, so a full disk must not produce one.
"""

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from jsonschema import Draft202012Validator

from relayapi.nodes import AddressPolicy, node_id_ok, timestamp_us, too_deep
from relayapi.settings import Settings
from relayapi.store import NEW_EVICTED, OLD, Store

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
# A connection shorter than this counts as flapping.
_STABLE_S = 60.0
_MAX_RECONNECT_DELAY_S = 30


class _Drop(Exception):
    def __init__(self, reason: str):
        self.reason = reason


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
        policy: AddressPolicy,
        clock: Callable[[], float],
        on_address: Callable[[], None] = lambda: None,
    ):
        self._settings = settings
        self._store = store
        self._policy = policy
        self._clock = clock
        self.on_address = on_address
        self._validators = load_schemas(settings.schema_dir)
        self._dropped: Counter[str] = Counter()
        self._store_failures: Counter[str] = Counter()
        self._lock = threading.Lock()

    def dropped(self) -> dict[str, int]:
        with self._lock:
            return dict(self._dropped)

    def store_failures(self) -> dict[str, int]:
        with self._lock:
            return {
                "store_full": self._store_failures["full"],
                "store_errors": self._store_failures["other"],
            }

    def _count(self, reason: str) -> None:
        with self._lock:
            self._dropped[reason] += 1

    def handle(self, topic: str, payload: bytes) -> bool:
        """Never raises. True means finished with, so acknowledge it. False means it could not
        be stored, so do not. The reason for a drop is counted, and the content never logged."""
        try:
            self._handle(topic, payload)
        except _Drop as drop:
            self._count(drop.reason)
        except sqlite3.Error as error:
            kind = "full" if "full" in str(error) else "other"
            with self._lock:
                self._store_failures[kind] += 1
            logger.warning("could not store a message on %s: %s", topic, type(error).__name__)
            return False
        except Exception:
            self._count("internal_error")
            logger.exception("a message on %s could not be kept", topic)
        return True

    def _handle(self, topic: str, payload: bytes) -> None:
        validator = self._validators.get(topic)
        if validator is None:
            raise _Drop("unknown_topic")
        message = self._decode(payload)
        if too_deep(message, self._settings.max_depth):
            raise _Drop("too_deep")
        try:
            valid = validator.is_valid(message)
        except _JSON_FAILURES:
            valid = False
        if not valid:
            raise _Drop("schema")
        if not node_id_ok(message["node_id"]):
            raise _Drop("bad_node_id")
        if not node_id_ok(message.get("camera_id", message["node_id"])):
            raise _Drop("bad_node_id")
        try:
            stamped = timestamp_us(message["timestamp"])
        except ValueError:
            raise _Drop("bad_timestamp") from None
        try:
            # ASCII-only, so that a lone surrogate cannot fail later on the way into SQLite.
            # allow_nan=False is what refuses NaN and the infinity 1e999 parses to.
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
            return json.loads(payload)
        except _JSON_FAILURES:
            raise _Drop("not_json") from None

    def _heartbeat(self, message: dict, stamped: int, text: str) -> None:
        now = self._clock()
        if stamped > (now + self._settings.future_tolerance_s) * 1_000_000:
            raise _Drop("future_heartbeat")
        attributes = message["attributes"]
        address = None
        if "node_address" in attributes and attributes["node_address"] is not None:
            address = self._policy.check(attributes["node_address"])
            if address is None:
                self._count("refused_address")
        elif "node_address" in attributes:
            self._count("refused_address")
        outcome = self._store.add_heartbeat(
            message["node_id"], stamped, now, address, text, self._settings.max_nodes
        )
        if outcome == OLD:
            raise _Drop("out_of_order_heartbeat")
        if outcome == NEW_EVICTED:
            self._count("evicted_node")
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
    # restarting, and delivers them when it is back. manual_ack: see this module's docstring.
    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.mqtt_client_id,
        clean_session=False,
        manual_ack=True,
    )


class Consumer:
    """Owns the broker connection. With the broker down, nothing else here is affected, and
    paho's thread keeps retrying."""

    def __init__(self, settings: Settings, ingest: Ingest, client_factory=make_client):
        self._settings = settings
        self._ingest = ingest
        self._client = client_factory(settings)
        self.connected = False
        self.reconnects = 0
        self._connected_at: float | None = None
        self._delay = 1
        # Messages not yet on disk, and not acknowledged. The broker's in-flight limit bounds
        # this, as it sends no more until it has acknowledgements.
        self._pending: list[tuple[str, bytes, int, int]] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._retrier = threading.Thread(target=self._retry_loop, daemon=True)

    def start(self) -> None:
        client = self._client
        client.username_pw_set(self._settings.api_broker_user, self._settings.api_broker_password)
        client.reconnect_delay_set(min_delay=self._delay, max_delay=_MAX_RECONNECT_DELAY_S)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(self._settings.broker_host, self._settings.broker_port, keepalive=30)
        client.loop_start()
        self._retrier.start()

    def stop(self) -> None:
        self._stopping.set()
        self._retrier.join(timeout=5)
        self._client.disconnect()
        self._client.loop_stop()

    def unacked(self) -> int:
        with self._lock:
            return len(self._pending)

    def retry_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for item in pending:
            topic, payload, mid, qos = item
            if self._ingest.handle(topic, payload):
                self._client.ack(mid, qos)
            else:
                with self._lock:
                    self._pending.append(item)

    def _retry_loop(self) -> None:
        while not self._stopping.wait(self._settings.store_retry_s):
            try:
                self.retry_pending()
            except Exception:
                logger.exception("retrying unstored messages failed")

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.connected = False
            logger.warning("the broker refused the connection: %s", reason_code)
            return
        self.connected = True
        self._connected_at = time.monotonic()
        client.subscribe([(topic, 1) for topic in _SCHEMAS])
        logger.info("connected to the broker")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        self.reconnects += 1
        # The broker sends these again on the new connection, under new packet ids.
        with self._lock:
            self._pending.clear()
        lasted = None if self._connected_at is None else time.monotonic() - self._connected_at
        self._connected_at = None
        # paho's own backoff restarts with every successful login, so a session taken over once
        # a second would reconnect once a second for ever.
        if lasted is not None and lasted >= _STABLE_S:
            self._delay = 1
        else:
            self._delay = min(self._delay * 2, _MAX_RECONNECT_DELAY_S)
        client.reconnect_delay_set(min_delay=self._delay, max_delay=_MAX_RECONNECT_DELAY_S)
        logger.warning("disconnected from the broker; retrying in %d s", self._delay)

    def _on_message(self, client, userdata, message) -> None:
        if self._ingest.handle(message.topic, message.payload):
            client.ack(message.mid, message.qos)
        elif message.qos > 0:
            with self._lock:
                self._pending.append((message.topic, message.payload, message.mid, message.qos))
