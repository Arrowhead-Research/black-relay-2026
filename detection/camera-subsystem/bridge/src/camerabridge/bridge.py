"""The edge bridge service: wire Tasks 2-6 into one running node.

:class:`Bridge` is the camera subsystem's service layer. It is the only place the
independently-testable pieces meet:

* **Frigate detections** — a subscription, on the node-local broker, to Frigate's
  ``frigate/events`` topic.
  Each payload is parsed (:func:`frigate.parse_frigate_event`), the open track is
  recorded in the :class:`cues.CueMatcher`, the ``camera.detection.v0`` envelope
  is minted (:func:`formats.detection_event`), and it is delivered through the
  durable :class:`outbox.Outbox`.
* **RF cues** — a subscription, on the central broker, to
  ``detection/rf/cues/camera`` (``formats.TOPIC_RF_CUES_IN``). Each cue is parsed (:func:`cues.parse_cue`),
  matched against open tracks, and acknowledged (:func:`cues.handle_cue`). On a
  match the bridge remembers the ``rf_cue_id`` and stamps it onto that track's
  **next** detection's ``attributes.rf_cue_id`` (DESIGN.md 5.4).
* **Status heartbeats** — :meth:`node_state` evaluates Frigate's health topics
  (:class:`health.FrigateHealth`) and the central link, and :meth:`publish_status`
  lowers that :class:`status.NodeState` to ``camera.node-status.v0`` and delivers it
  durably.

Carried decisions baked in here (task-7 brief):

1. **Clock gating (DESIGN.md 7.5).** While the injected clock reports
   :class:`formats.ClockNotReady` (boot, before NTP sync), no timestamp from it is
   emitted. Durable events (``object_start``, ``object_end``, cue-acks) are held in a
   bounded in-memory FIFO with their monotonic capture time. The first entry point
   after the clock syncs stamps each as now minus its elapsed monotonic time and
   enqueues them in order, ahead of any new event. ``object_update`` and status are
   dropped while unsynced. Held events are lost on restart.
2. **Outbox lifecycle.** :meth:`handle_central_connect` calls ``outbox.replay`` on
   each central (re)connect; durable sends are flushed in steady state. The durable
   flag is ``event != "object_update"``: ``object_update`` is best-effort QoS 0,
   published directly and never persisted (so a long outage cannot backlog stale
   positions). While central is down nothing is handed to paho: durable messages
   wait in the outbox (paho would otherwise also queue them in memory, without
   limit, for the whole outage) and ``object_update`` is dropped.
3. **Snapshot URL** is minted here (the service knows the Frigate host); the
   parser leaves it ``None``.
4. **Robustness.** A malformed payload on the wire (bad JSON, or a well-formed
   JSON that the parser rejects) is skipped; the service keeps processing later
   events. The parsers never raise, and JSON decoding is guarded.

**Two MQTT connections** (DESIGN.md 4.1), both **injected** (:meth:`__init__`) so
tests drive fakes and the service drives paho:

* the **local** client talks to the node-local broker Frigate publishes to. It only
  subscribes to Frigate topics, and it never publishes.
* the **central** client talks to the fusion broker. It publishes ``camera.*``,
  subscribes to RF cues, and drives PUBACKs and outbox replay.

Each connects and reconnects on its own, so a central outage cannot lose a Frigate
event and a local-broker outage cannot stop the heartbeat. The bridge touches only a
tiny client surface: ``publish(topic, payload, qos) -> info`` (with ``info.mid``),
``subscribe(topic, qos)``, and the assignable paho VERSION2 callbacks.

**Thread safety.** paho runs ``on_message`` (→ :meth:`dispatch`) and
``on_publish`` (→ :meth:`on_publish_ack`) on its own network loop thread, while a
service's main thread calls :meth:`publish_status` and :meth:`expire_tracks` on a
timer. All of them mutate the shared outbox, matcher, and pending-cue state (and
the outbox file). Every public entry point therefore takes one re-entrant lock, so
all state changes serialize and no concurrent-mutation ``RuntimeError`` can kill
message processing. Internal helpers run under the caller's lock and never
re-acquire it beyond the re-entrant guarantee.

**paho callbacks and the lock.** paho calls some callbacks holding its internal
``_out_message_mutex``, and ``publish()`` takes that same mutex, so these callbacks
must never **wait** for the bridge lock, or they deadlock with a thread that holds
the lock and is inside ``publish()``:

* ``on_publish`` posts the mid to a thread-safe mailbox and applies it only if the
  lock is free.
* the central ``on_disconnect`` writes the connected flag without the lock.

A lock hold applies at most one batch of posted acks (one outbox rewrite), so a large
replay cannot trap whichever thread holds the lock. Every holder re-checks the mailbox
after it releases the lock (:meth:`_locked`), because a poster that failed to take the
lock may have posted the last ack of the send window. A delayed ack can at worst cause
a duplicate replay, which fusion dedupes.

**Fatal errors.** An ``OSError`` from the outbox (a full disk, for example) would kill
the paho network thread if it reached paho. The callbacks catch it, log it, and set
:attr:`fatal`; the service's main loop then exits nonzero so systemd restarts it.
"""

from __future__ import annotations

import functools
import json
import logging
import queue
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from paho.mqtt.enums import MQTTErrorCode

from . import formats
from .cues import CueMatcher, handle_cue, parse_cue
from .frigate import parse_frigate_event
from .health import FrigateHealth
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

# Default lifetime for a matched-but-not-yet-attached ``rf_cue_id`` (seconds).
# A pending cue is attached to its track's NEXT detection; if that detection never
# arrives, the pending entry is aged out on this horizon so it cannot leak over the
# node's uptime. Mirrors the matcher's default window so the two age together.
_DEFAULT_PENDING_TTL_S = 5.0

# Cap on durable events held for NTP sync; the oldest are dropped beyond it.
_DEFAULT_HELD_CAP = 10_000
_HELD_DROP_WARN_EVERY = 1_000

# Builds a held message before it can be stamped. Release always replaces the value.
_UNSTAMPED = formats.FixedClock("1970-01-01T00:00:00Z")


@dataclass
class _Held:
    """A durable message awaiting NTP sync, with its monotonic capture time."""

    topic: str
    message: dict
    captured_at: float


def _fatal_on_oserror(callback: Callable[..., None]) -> Callable[..., None]:
    """Keep an ``OSError`` out of paho, which would let it kill the network thread."""

    @functools.wraps(callback)
    def guarded(self: "Bridge", *args: Any, **kwargs: Any) -> None:
        try:
            callback(self, *args, **kwargs)
        except OSError:
            logger.exception("outbox I/O failed in an MQTT callback; the service must restart")
            self._fatal = True

    return guarded


def _discard_retained_messages(client: Any) -> None:
    """Make paho forget its unacked QoS-1 copies from the previous connection.

    paho keeps them and re-sends them after CONNACK, alongside the outbox replay. That
    would deliver each twice and stack more copies on every reconnect. The outbox is
    the one source of truth for what is unacked, so paho's copies go. paho has no
    public API for this, so a paho upgrade that renames these attributes raises here
    (and fails the real-broker tests) rather than silently duplicating.

    Runs in ``on_connect``, before paho walks this queue to re-send it.
    """
    with client._out_message_mutex:
        client._out_messages.clear()
        client._inflight_messages = 0


class Bridge:
    """Wire a Frigate subscription, an RF-cue subscription, and a status heartbeat.

    Construct with injected ``central_client`` and ``local_client``, a durable
    ``outbox``, a ``matcher``, a ``clock``, this node's ``node_id``, the Frigate events
    ``frigate_topic`` and ``frigate_base_url`` (for snapshot URLs). ``time_fn`` is
    injectable only so tests can align a track's observation time with a cue's
    ``ts_epoch``; production uses the wall clock (epoch seconds, NTP-corrected once
    synced — the matcher only compares epoch deltas within a window).
    ``monotonic_fn`` times held events; ``time_fn`` anchors them once the clock syncs.
    """

    def __init__(
        self,
        *,
        central_client: Any,
        local_client: Any,
        outbox: Outbox,
        matcher: CueMatcher,
        clock: formats.Clock,
        node_id: str,
        frigate_topic: str = "frigate/events",
        frigate_base_url: str = "",
        time_fn: Callable[[], float] = time.time,
        pending_ttl_s: float = _DEFAULT_PENDING_TTL_S,
        health: Optional[FrigateHealth] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        held_cap: int = _DEFAULT_HELD_CAP,
    ) -> None:
        self._central = central_client
        self._local = local_client
        # True only between a successful central CONNACK and its disconnect. Written
        # without the lock on disconnect (see module docstring).
        self._central_connected = False
        # PUBACK mids posted by paho, applied under the lock (see module docstring).
        self._acks: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._fatal = False
        self._outbox = outbox
        self._matcher = matcher
        self._clock = clock
        self._node_id = node_id
        self._frigate_topic = frigate_topic
        self._frigate_base_url = frigate_base_url.rstrip("/")
        self._time_fn = time_fn
        self._pending_ttl_s = pending_ttl_s
        # With no health source nothing can be proven, so every signal reports False.
        self._health = health or FrigateHealth(camera=None, stale_after_s=0.0)
        # object_id -> (rf_cue_id, observed_epoch) awaiting attachment to that
        # track's next detection. The epoch bounds the entry's lifetime so a match
        # whose track never produces a further detection cannot leak (expire_tracks).
        self._pending_cue: dict[str, tuple[str, float]] = {}
        self._monotonic_fn = monotonic_fn
        self._held_cap = held_cap
        self._held: deque[_Held] = deque()
        self._held_dropped = 0
        # Serializes all shared-state mutation across the paho loop thread and the
        # service's heartbeat thread (re-entrant: a locked public method may call
        # another helper that would otherwise re-lock on the same thread).
        self._lock = threading.RLock()

    # --- MQTT wiring --------------------------------------------------------

    def wire(self) -> None:
        """Register the paho VERSION2 callbacks on both injected clients.

        Subscriptions are (re)issued on each successful connect so a reconnect
        re-subscribes, mirroring paho's idiom.
        """
        self._central.on_connect = self._paho_central_on_connect
        self._central.on_disconnect = self._paho_central_on_disconnect
        self._central.on_message = self._paho_on_message
        self._central.on_publish = self._paho_on_publish
        self._local.on_connect = self._paho_local_on_connect
        self._local.on_disconnect = self._paho_local_on_disconnect
        self._local.on_message = self._paho_on_message

    # paho VERSION2 also fires on_connect for a REFUSED CONNACK (bad credentials, for
    # example), so the reason code decides whether a connection actually exists.
    @_fatal_on_oserror
    def _paho_central_on_connect(
        self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
    ) -> None:
        if reason_code.is_failure:
            logger.warning("central broker refused the connection: %s", reason_code)
            return
        self.handle_central_connect()

    def _paho_central_on_disconnect(self, *_args: Any, **_kwargs: Any) -> None:
        self.handle_central_disconnect()

    def _paho_local_on_connect(
        self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
    ) -> None:
        if reason_code.is_failure:
            logger.warning("local broker refused the connection: %s", reason_code)
            return
        self.handle_local_connect()

    def _paho_local_on_disconnect(self, *_args: Any, **_kwargs: Any) -> None:
        self.handle_local_disconnect()

    @_fatal_on_oserror
    def _paho_on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        # Stats age from receipt, so a retained copy of unknown age must not count.
        if message.retain and message.topic == self._health.stats_topic:
            return
        self.dispatch(message.topic, message.payload)

    @_fatal_on_oserror
    def _paho_on_publish(self, _client: Any, _userdata: Any, mid: Any, *_a: Any, **_k: Any) -> None:
        """Post a PUBACK without ever waiting for the lock (see module docstring)."""
        self._acks.put(mid)
        self._apply_posted_acks()

    # --- locking and acks ---------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the lock, then re-check the ack mailbox after releasing it.

        A poster whose try-acquire failed relies on this re-check. It applies one batch
        on this thread and returns; later acks arrive with the window it just opened.
        Used by top-level entry points only, so the release is a real release.
        """
        with self._lock:
            yield
        self._apply_posted_acks()

    def _apply_posted_acks(self) -> None:
        """Apply one batch of posted acks if the lock is free, without ever waiting."""
        if self._acks.empty() or not self._lock.acquire(blocking=False):
            return
        try:
            self._drain_acks()
        finally:
            self._lock.release()

    def _drain_acks(self) -> None:
        """Apply the acks posted so far as one batch, then fill the send window.

        Caller holds the lock. Collects first and applies once: acks that arrive during
        the rewrite wait for the next lock hold, so a hold costs at most one rewrite.
        """
        mids = []
        while True:
            try:
                mids.append(self._acks.get_nowait())
            except queue.Empty:
                break
        if mids:
            self._outbox.ack_many(mids)
            self._flush()

    # --- connection lifecycle ----------------------------------------------

    @property
    def central_connected(self) -> bool:
        """Whether the central client currently holds an accepted connection."""
        return self._central_connected

    @property
    def fatal(self) -> bool:
        """Whether a callback hit an unrecoverable I/O error (the service must restart)."""
        return self._fatal

    def handle_central_connect(self) -> None:
        """On central (re)connect: (re)subscribe to RF cues and replay the outbox.

        Replay re-sends un-acked durable entries with their original bytes, so events
        queued during an outage reach fusion with their ``event_id`` and ``timestamp``
        intact (carried decision 2). The cue topic is subscribed at QoS 1 so a reconnect
        blip cannot silently drop an inbound RF cue.
        """
        with self._locked():
            # Before replay forgets the old connection's mids (and while the link still
            # reads as down, so this applies acks without sending).
            self._drain_acks()
            _discard_retained_messages(self._central)
            self._central_connected = True
            self._central.subscribe(formats.TOPIC_RF_CUES_IN, 1)
            self._outbox.replay(self._publish)
            # After replay, which would otherwise re-send what this release flushes.
            self._release_held(self._clock_ready())

    def handle_central_disconnect(self) -> None:
        """Central link lost: durable messages wait in the outbox until replay.

        Must not take the lock (see module docstring). A bare flag write is safe: only
        the central network thread writes it, and a thread that reads a stale True just
        hands one message to paho, which the next connect discards and replays.
        """
        self._central_connected = False

    def handle_local_connect(self) -> None:
        """On local (re)connect: (re)subscribe to Frigate topics at QoS 1."""
        with self._lock:
            self._local.subscribe(self._frigate_topic, 1)
            for topic in self._health.topics:
                self._local.subscribe(topic, 1)

    def handle_local_disconnect(self) -> None:
        """Local link lost: cached Frigate health can no longer be verified.

        Waiting for the lock is safe here, unlike on central: nothing holding the lock
        ever waits on a mutex of the local client, which never publishes.
        """
        with self._lock:
            self._health.reset()

    def expire_tracks(self, now: Optional[float] = None) -> None:
        """Retire stale matcher tracks and aged pending cues to bound memory.

        Called on the status-heartbeat tick for a 24/7 node. Without it the
        matcher's track table grows one entry per distinct Frigate track id
        forever, and a matched cue whose track never produces a further detection
        never clears. ``now`` defaults to the bridge's own time source (epoch
        seconds), the same clock the matcher is fed, so tracks and pending cues age
        on one timeline.
        """
        if now is None:
            now = float(self._time_fn())
        with self._locked():
            self._matcher.expire(now)
            horizon = now - self._pending_ttl_s
            self._pending_cue = {
                object_id: entry
                for object_id, entry in self._pending_cue.items()
                if entry[1] >= horizon
            }

    # --- inbound dispatch ---------------------------------------------------

    def dispatch(self, topic: str, payload: Any) -> None:
        """Route one inbound MQTT message to its handler by topic.

        ``payload`` is raw wire bytes (or str/dict in tests). Malformed JSON is
        decoded defensively and skipped — the service never crashes on a bad
        payload (carried decision 4). An unknown topic is ignored.
        """
        with self._locked():
            # Checked before JSON decoding: "online" and "ON" are not JSON.
            if self._health.handles(topic):
                self._health.update(topic, payload)
                return
        data = self._decode_json(payload)
        if data is None:
            return
        with self._locked():
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
        boot window). With the clock unsynced a durable detection is held and an
        ``object_update`` is dropped. A pending ``rf_cue_id`` is attached to this
        detection and consumed only if the detection will actually be delivered.
        """
        # The one clock read for this event: a flip after it cannot let the event
        # overtake held ones, because release and stamping both use this value.
        synced = self._clock_ready()
        event = parse_frigate_event(payload)
        if event is None:
            return
        self._release_held(synced)

        # Record ALL zones the track currently occupies (Frigate zones overlap), so
        # an RF cue for ANY of them can match — not just zones[0]. A track outside
        # every defined zone is recorded under a sentinel no real cue zone matches.
        zones = list(event.zones) if event.zones else [_NO_ZONE]
        self._matcher.observe_track(event.object_id, zones, float(self._time_fn()))

        durable = event.event != "object_update"
        if not durable and not (synced and self._central_connected):
            # The pending cue stays for the next detection that is really delivered.
            logger.debug("dropping object_update for %s", event.object_id)
            return

        attributes: dict[str, Any] = {
            "bbox": list(event.bbox),
            "area_px": event.area_px,
            "stationary": event.stationary,
            # snapshot_url is minted service-side (decision 3) — the parser leaves None.
            "snapshot_url": self._snapshot_url(event.object_id),
        }
        pending = self._pending_cue.get(event.object_id)
        if pending is not None:
            attributes["rf_cue_id"] = pending[0]

        message = formats.detection_event(
            node_id=self._node_id,
            event=event.event,
            object_id=event.object_id,
            label=event.label,
            confidence=event.confidence,
            zones=list(event.zones),
            attributes=attributes,
            clock=self._clock if synced else _UNSTAMPED,
        )

        # Consume the pending cue now that a detection is actually emitted (this is
        # the "next detection" the match was held for); an object_end also clears it.
        self._pending_cue.pop(event.object_id, None)

        if not synced:
            self._hold(formats.TOPIC_DETECTIONS, message)
            return

        self._outbox.enqueue(formats.TOPIC_DETECTIONS, message, durable=durable)
        if durable:
            self._flush()
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
        the matched track's next detection. With the clock unsynced the ack is held,
        with the match decided now: the track may be gone by the time the clock syncs.
        """
        # The one clock read for this cue (see _handle_frigate).
        synced = self._clock_ready()
        cue = parse_cue(payload)
        if cue is None:
            return
        self._release_held(synced)

        ack = handle_cue(cue, self._matcher, self._node_id, self._clock if synced else _UNSTAMPED)

        if ack.get("matched") and ack.get("object_id") is not None:
            self._pending_cue[ack["object_id"]] = (ack["rf_cue_id"], float(self._time_fn()))

        if not synced:
            self._hold(formats.TOPIC_CUE_ACKS, ack)
            return

        self._outbox.enqueue(formats.TOPIC_CUE_ACKS, ack, durable=True)
        self._flush()

    # --- hold until NTP sync (DESIGN.md 7.5) --------------------------------

    def _clock_ready(self) -> bool:
        """Return whether the injected clock can stamp a trusted timestamp."""
        try:
            self._clock.now_rfc3339()
        except formats.ClockNotReady:
            return False
        return True

    def _hold(self, topic: str, message: dict) -> None:
        """Hold a durable message for the clock to sync, dropping the oldest at the cap."""
        self._held.append(_Held(topic, message, float(self._monotonic_fn())))
        if len(self._held) > self._held_cap:
            self._held.popleft()
            self._held_dropped += 1
            if self._held_dropped % _HELD_DROP_WARN_EVERY == 1:
                logger.warning(
                    "clock not NTP-synced and %d events held: dropping the oldest "
                    "(%d dropped so far)",
                    self._held_cap,
                    self._held_dropped,
                )

    def _release_held(self, synced: bool) -> None:
        """If ``synced``, stamp every held event and enqueue them in order.

        ``synced`` is the caller's one clock read, so the caller's own event is handled
        consistently with this release. Each is stamped now minus the monotonic time elapsed since its capture, so a
        node that booted with no uplink still reports when each event happened.
        """
        if not self._held or not synced:
            return
        wall_now = float(self._time_fn())
        mono_now = float(self._monotonic_fn())
        batch = [
            (
                held.topic,
                formats.with_timestamp(
                    held.message,
                    formats.rfc3339_from_epoch(wall_now - (mono_now - held.captured_at)),
                ),
            )
            for held in self._held
        ]
        self._held.clear()
        logger.info(
            "clock synced: releasing %d held events (%d dropped at the cap)",
            len(batch),
            self._held_dropped,
        )
        self._held_dropped = 0
        self._outbox.enqueue_many(batch)
        self._flush()

    # --- status heartbeat ---------------------------------------------------

    def node_state(self) -> NodeState:
        """Evaluate current health: Frigate's topics plus the central connection."""
        with self._lock:
            return self._health.evaluate(link_online=self._central_connected)

    def publish_status(self, state: NodeState) -> None:
        """Build and durably deliver one ``camera.node-status.v0`` heartbeat.

        Dropped silently while the clock is not NTP-synced (boot window). Also the
        regular trigger that releases held events once the clock has synced.
        """
        with self._locked():
            # The one clock read for this tick (see _handle_frigate).
            synced = self._clock_ready()
            self._release_held(synced)
            if not synced:
                logger.debug("clock not NTP-synced; dropping status heartbeat")
                return
            message = build_status(state, self._node_id, self._clock)
            self._outbox.enqueue(formats.TOPIC_STATUS, message, durable=True)
            self._flush()

    # --- outbound publish ---------------------------------------------------

    def _flush(self) -> None:
        """Send queued durable entries if central is up, up to the send window.

        While central is down the entries simply stay in the outbox; the next
        :meth:`handle_central_connect` replays them.
        """
        if self._central_connected:
            self._outbox.flush(self._publish)

    def _publish(self, topic: str, payload: bytes, qos: int):
        """Publish via the central client; return the ``mid``, or ``None`` on failure.

        This is the ``publish(topic, payload_bytes, qos) -> mid`` callback the outbox
        drives for durable flush/replay. On a failure the entry stays unsent. In
        particular paho reports a mid collision as ``MQTT_ERR_QUEUE_SIZE`` with the
        colliding mid, whose PUBACK belongs to some other message.
        """
        info = self._central.publish(topic, payload, qos)
        if info.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            logger.warning("central publish on %s failed: rc=%s", topic, info.rc)
            return None
        return info.mid

    @staticmethod
    def _serialize(message: dict) -> bytes:
        """Serialize a message dict to compact JSON bytes (QoS-0 direct publish)."""
        return json.dumps(message, separators=_SEP).encode("utf-8")
