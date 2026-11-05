"""The edge bridge service: wire Tasks 2-6 into one running node.

:class:`Bridge` is the camera subsystem's service layer. It is the only place the
independently-testable pieces meet:

* **Frigate detections** — a subscription to Frigate's ``frigate/events`` topic.
  Each payload is parsed (:func:`frigate.parse_frigate_event`), the open track is
  recorded in the :class:`cues.CueMatcher`, the ``camera.detection.v0`` envelope
  is minted (:func:`formats.detection_event`), and it is delivered through the
  durable :class:`outbox.Outbox`.
* **RF cues** — a subscription to ``detection/rf/cues/camera``
  (``formats.TOPIC_RF_CUES_IN``). Each cue is parsed (:func:`cues.parse_cue`),
  matched against open tracks, and acknowledged (:func:`cues.handle_cue`). On a
  match the bridge remembers the ``rf_cue_id`` and stamps it onto that track's
  **next** detection's ``attributes.rf_cue_id`` (DESIGN.md 5.4).
* **Status heartbeats** — :meth:`publish_status` lowers a :class:`status.NodeState`
  to ``camera.node-status.v0`` and delivers it durably.

Carried decisions baked in here (task-7 brief):

1. **Clock gating.** While the injected clock reports
   :class:`formats.ClockNotReady` (boot, before NTP sync), the build raises and
   the bridge **drops** the message rather than emit an untrusted timestamp. The
   drop is caught around every envelope build; it never crashes the service.
2. **Outbox lifecycle.** :meth:`handle_connect` calls ``outbox.replay`` on each
   (re)connect; durable sends are flushed in steady state. The durable flag is
   ``event != "object_update"``: ``object_update`` is best-effort QoS 0, published
   directly and never persisted (so a long outage cannot backlog stale positions).
3. **Snapshot URL** is minted here (the service knows the Frigate host); the
   parser leaves it ``None``.
4. **Robustness.** A malformed payload on the wire (bad JSON, or a well-formed
   JSON that the parser rejects) is skipped; the service keeps processing later
   events. The parsers never raise, and JSON decoding is guarded.

The MQTT client is **injected** (:meth:`__init__`) so tests drive a fake and the
service drives paho. The bridge touches only a tiny client surface:
``publish(topic, payload, qos) -> info`` (with ``info.mid``), ``subscribe(topic)``,
and the assignable ``on_connect`` / ``on_message`` / ``on_publish`` callbacks.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from . import formats
from .cues import CueMatcher, handle_cue, parse_cue
from .frigate import parse_frigate_event
from .outbox import Outbox
from .status import NodeState, build_status

__all__ = ["Bridge"]

logger = logging.getLogger(__name__)

# Compact, deterministic JSON — matches the outbox's on-disk serialization so a
# directly-published QoS-0 payload is byte-shaped like a durable one.
_SEP = (",", ":")

# Zone recorded for a track Frigate reports outside every defined zone. It is not
# a real site zone, so an RF cue (which carries a real zone) can never spuriously
# match a zone-less track — the matcher simply never finds it. Keeps the matcher
# keyed by a concrete string instead of ``None``.
_NO_ZONE = "__no_zone__"


class Bridge:
    """Wire a Frigate subscription, an RF-cue subscription, and a status heartbeat.

    Construct with an injected MQTT ``client``, a durable ``outbox``, a
    ``matcher``, a ``clock``, this node's ``node_id``, the Frigate events
    ``frigate_topic`` and ``frigate_base_url`` (for snapshot URLs). ``time_fn`` is
    injectable only so tests can align a track's observation time with a cue's
    ``ts_epoch``; production uses the wall clock (epoch seconds, NTP-corrected once
    synced — the matcher only compares epoch deltas within a window).
    """

    def __init__(
        self,
        *,
        client: Any,
        outbox: Outbox,
        matcher: CueMatcher,
        clock: formats.Clock,
        node_id: str,
        frigate_topic: str = "frigate/events",
        frigate_base_url: str = "",
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._outbox = outbox
        self._matcher = matcher
        self._clock = clock
        self._node_id = node_id
        self._frigate_topic = frigate_topic
        self._frigate_base_url = frigate_base_url.rstrip("/")
        self._time_fn = time_fn
        # object_id -> rf_cue_id awaiting attachment to that track's next detection.
        self._pending_cue: dict[str, str] = {}

    # --- MQTT wiring --------------------------------------------------------

    def wire(self) -> None:
        """Register the paho-style callbacks on the injected client.

        Subscriptions are (re)issued inside :meth:`handle_connect` so a reconnect
        re-subscribes, mirroring paho's idiom.
        """
        self._client.on_connect = self._paho_on_connect
        self._client.on_message = self._paho_on_message
        self._client.on_publish = self._paho_on_publish

    def _paho_on_connect(self, *_args: Any, **_kwargs: Any) -> None:
        self.handle_connect()

    def _paho_on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        self.dispatch(message.topic, message.payload)

    def _paho_on_publish(self, _client: Any, _userdata: Any, mid: Any, *_a: Any, **_k: Any) -> None:
        self.on_publish_ack(mid)

    # --- connection lifecycle ----------------------------------------------

    def handle_connect(self) -> None:
        """On (re)connect: (re)subscribe to both inbound topics and replay the outbox.

        Replay re-sends every un-acked durable entry with its original bytes, so
        events queued during an outage reach fusion with their ``event_id`` and
        ``timestamp`` intact (carried decision 2).
        """
        self._client.subscribe(self._frigate_topic)
        self._client.subscribe(formats.TOPIC_RF_CUES_IN)
        self._outbox.replay(self._publish)

    def on_publish_ack(self, mid: Any) -> None:
        """Broker PUBACK for a QoS-1 publish: drop the acked entry from the outbox."""
        self._outbox.ack(mid)

    # --- inbound dispatch ---------------------------------------------------

    def dispatch(self, topic: str, payload: Any) -> None:
        """Route one inbound MQTT message to its handler by topic.

        ``payload`` is raw wire bytes (or str/dict in tests). Malformed JSON is
        decoded defensively and skipped — the service never crashes on a bad
        payload (carried decision 4). An unknown topic is ignored.
        """
        data = self._decode_json(payload)
        if data is None:
            return
        if topic == self._frigate_topic:
            self._handle_frigate(data)
        elif topic == formats.TOPIC_RF_CUES_IN:
            self._handle_cue(data)
        # Any other topic: not ours — ignore.

    @staticmethod
    def _decode_json(payload: Any) -> Optional[object]:
        """Decode an MQTT payload to a Python object, or ``None`` if malformed.

        Accepts an already-decoded ``dict`` (test convenience), ``bytes``/``str``
        JSON, and returns ``None`` for invalid JSON or undecodable bytes rather
        than raising.
        """
        if isinstance(payload, dict):
            return payload
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8")
            return json.loads(payload)
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    # --- Frigate detection path ---------------------------------------------

    def _handle_frigate(self, payload: object) -> None:
        """Parse a Frigate event, observe the track, build + deliver a detection.

        The track is observed unconditionally (keeps the matcher fresh even in the
        boot window). The envelope build is wrapped so a not-ready clock drops the
        detection instead of stamping an untrusted time. A pending ``rf_cue_id`` is
        attached to this detection and consumed only once the build succeeds.
        """
        event = parse_frigate_event(payload)
        if event is None:
            return

        zone = event.zones[0] if event.zones else _NO_ZONE
        self._matcher.observe_track(event.object_id, zone, float(self._time_fn()))

        attributes: dict[str, Any] = {
            "bbox": list(event.bbox),
            "area_px": event.area_px,
            "stationary": event.stationary,
            # snapshot_url is minted service-side (decision 3) — the parser leaves None.
            "snapshot_url": self._snapshot_url(event.object_id),
        }
        pending = self._pending_cue.get(event.object_id)
        if pending is not None:
            attributes["rf_cue_id"] = pending

        try:
            message = formats.detection_event(
                node_id=self._node_id,
                event=event.event,
                object_id=event.object_id,
                label=event.label,
                confidence=event.confidence,
                zones=list(event.zones),
                attributes=attributes,
                clock=self._clock,
            )
        except formats.ClockNotReady:
            logger.debug("clock not NTP-synced; dropping detection for %s", event.object_id)
            return

        # Consume the pending cue only now that a detection is actually emitted,
        # so a boot-window drop keeps it for the next (trusted) detection.
        self._pending_cue.pop(event.object_id, None)

        durable = event.event != "object_update"
        self._outbox.enqueue(formats.TOPIC_DETECTIONS, message, durable=durable)
        if durable:
            self._outbox.flush(self._publish)
        else:
            # object_update: best-effort QoS 0, published directly (never persisted).
            self._publish(formats.TOPIC_DETECTIONS, self._serialize(message), 0)

    def _snapshot_url(self, object_id: str) -> Optional[str]:
        """Mint the edge-Frigate snapshot URL for a track, or ``None`` if unset.

        The service knows its Frigate host (``frigate_base_url``); the parser
        cannot (DESIGN.md 5.6). With no base URL configured the field stays
        ``None`` rather than emitting a broken link.
        """
        if not self._frigate_base_url:
            return None
        return f"{self._frigate_base_url}/api/events/{object_id}/snapshot.jpg"

    # --- RF cue path --------------------------------------------------------

    def _handle_cue(self, payload: object) -> None:
        """Parse an RF cue, acknowledge it, and remember a match for correlation.

        Every cue the node acts on yields a cue-ack (matched or not) so fusion
        always sees the cue was received. On a match the ``rf_cue_id`` is held for
        the matched track's next detection. A not-ready clock drops the ack.
        """
        cue = parse_cue(payload)
        if cue is None:
            return

        try:
            ack = handle_cue(cue, self._matcher, self._node_id, self._clock)
        except formats.ClockNotReady:
            logger.debug("clock not NTP-synced; dropping cue-ack for %s", cue.rf_cue_id)
            return

        if ack.get("matched") and ack.get("object_id") is not None:
            self._pending_cue[ack["object_id"]] = ack["rf_cue_id"]

        self._outbox.enqueue(formats.TOPIC_CUE_ACKS, ack, durable=True)
        self._outbox.flush(self._publish)

    # --- status heartbeat ---------------------------------------------------

    def publish_status(self, state: NodeState) -> None:
        """Build and durably deliver one ``camera.node-status.v0`` heartbeat.

        Dropped silently while the clock is not NTP-synced (boot window).
        """
        try:
            message = build_status(state, self._node_id, self._clock)
        except formats.ClockNotReady:
            logger.debug("clock not NTP-synced; dropping status heartbeat")
            return
        self._outbox.enqueue(formats.TOPIC_STATUS, message, durable=True)
        self._outbox.flush(self._publish)

    # --- outbound publish ---------------------------------------------------

    def _publish(self, topic: str, payload: bytes, qos: int):
        """Publish via the injected client and return the broker ``mid`` for ack.

        This is the ``publish(topic, payload_bytes, qos) -> mid`` callback the
        outbox drives for durable flush/replay.
        """
        info = self._client.publish(topic, payload, qos)
        return getattr(info, "mid", None)

    @staticmethod
    def _serialize(message: dict) -> bytes:
        """Serialize a message dict to compact JSON bytes (QoS-0 direct publish)."""
        return json.dumps(message, separators=_SEP).encode("utf-8")
