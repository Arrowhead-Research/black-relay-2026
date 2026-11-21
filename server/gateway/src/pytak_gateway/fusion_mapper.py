"""What Fusion's events mean for a site: they become the gateway's existing state changes.

A Fusion event or node status is turned into the same two things a node platform sends directly:
a detection (the node tripped) or a health report (the node is up or down). Both then go through
GatewayState, so every rule that already protects the state holds for Fusion too: a tripped node
stays tripped until an operator clears it, health never ends a trip, a replay changes nothing.

Fusion and the registry share one vocabulary: an event's (site_id, subsystem, node_id) is looked up
as it is among the registered nodes (a subsystem's "*" node takes what names no node). An event for a
node nobody registered is remembered, so an operator can find and register it, and never guessed at.

An RF alert's emitter (a MAC address) is kept with the trip it causes, and `rf.position` says where that
emitter is, in feet from the site's middle point: both go out in the trip's CoT, for the phone to show.

What is not here yet: camera objects and cue acks, and physical zone status. Those are tracks and zones, not
node status, and are counted as `ignored`.
"""

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import cot, trip_words
from .db import AllowedEmitterRow, DiscoveredRow, EmitterRow, HeldRow, NodeRow, UnplacedStateRow
from .fusion_client import Event, NodeStatus
from .models import ANY_NODE, FEET_MAX, DetectionEvent, HealthReport, NodeState, Subsystem, emitter_id
from .registry import find_node
from .state import GatewayState

log = logging.getLogger(__name__)

# A detection with no severity word of its own (trip_words.SEVERITIES) gets this middling one.
SEVERITY = 5

# Physical event types that trip the node. The contract only names `motion_cleared`, and the panel's own
# arming state is not part of it, so what counts as an intrusion is the operator's call. A zone reporting
# `tamper` always trips, whatever its event type.
DEFAULT_PHYSICAL_TRIPS = frozenset({"tamper"})

# A physical zone also trips its node when it moves out of its resting state, by what Fusion's panel bridge
# reports (seen on a real bench panel): zone kind is contact, motion, glass_break or other; `state` is
# closed/open for a contact and idle/active-ish for the rest. Resting states never trip; `zone_added` and
# `zone_removed` are the bridge listing its zones, not a change in one. Smoke ("other") is not in the default.
DEFAULT_ZONE_KINDS = frozenset({"contact", "motion", "glass_break"})
RESTING_STATES = frozenset({"closed", "idle", ""})
NOT_A_CHANGE = frozenset({"zone_added", "zone_removed"})

# Nodes nobody has registered yet are remembered, for the operator to find and register (registry.list_unregistered),
# and their detections are held a while so registering the node can still raise them. Both are bounded: a feed that
# invents site names must not be able to fill the disk.
DEFAULT_HOLD = timedelta(minutes=15)
HELD_PER_NODE = 100
DISCOVERED_MAX = 1000
DISCOVERED_TTL = timedelta(days=7)


# An RF alert names no node, but says in `location.zone` which RF node's zone heard the emitter: "zone-wbt-1" is
# node "wbt-1" (seen on Fusion's central broker, rf.alert.v0). An alert with no such zone stays the subsystem's "*".
RF_ZONE = re.compile(r"^zone-([A-Za-z0-9._-]{1,128})$")
NODE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# An RF alert by this rule says an RF node has stopped reporting (`context.node_id`): the node is offline,
# not tripped. Fusion has sent both spellings of the rule's id; its rule_type is always node_offline.
RF_NODE_OFFLINE = "node_offline"


# Where Fusion last put an emitter is only where it is for a while: a trip takes a position heard within this
# long, and older ones are dropped. They are capped as well: a feed that invents emitters must not fill the disk.
EMITTER_POSITION_TTL = timedelta(minutes=10)
EMITTERS_MAX = 5000


def emitter_of(data: dict) -> str | None:
    """The emitter an RF alert or position names, `emitter.id`, if it is a MAC address: lower case, with
    colons. Anything else is no emitter. Fusion's own text never passes: what is returned matched the pattern."""
    emitter = data.get("emitter")
    raw = emitter.get("id") if isinstance(emitter, dict) else None
    return emitter_id(raw)


def position_of(data: dict) -> tuple[float, float] | None:
    """Where an `rf.position` puts its emitter: `position.x_ft` and `position.y_ft`, feet from the site's middle
    point. None unless both are plain numbers a site could hold."""
    position = data.get("position")
    if not isinstance(position, dict):
        return None
    feet = []
    for key in ("x_ft", "y_ft"):
        value = position.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or abs(value) > FEET_MAX:
            return None
        feet.append(float(value))
    return feet[0], feet[1]


def attributed_node(ev: Event) -> str | None:
    """The node an event is about: its own node_id, or for an RF alert that names none, the node it is
    about (a node_offline alert's `context.node_id`) or the node its zone is."""
    if ev.node_id is not None or ev.subsystem != "rf":
        return ev.node_id
    context = ev.data.get("context")
    if ev.data.get("rule_type") == RF_NODE_OFFLINE and isinstance(context, dict):
        node = context.get("node_id")
        return node if isinstance(node, str) and NODE_ID.match(node) else None
    location = ev.data.get("location")
    zone = location.get("zone") if isinstance(location, dict) else None
    match = RF_ZONE.match(zone) if isinstance(zone, str) else None
    return match.group(1) if match else None


@dataclass(frozen=True)
class Policy:
    physical_trips: frozenset[str] = DEFAULT_PHYSICAL_TRIPS
    zone_kinds: frozenset[str] = DEFAULT_ZONE_KINDS  # zone kinds that trip the node when they leave their resting state
    hold: timedelta = DEFAULT_HOLD  # how long an unregistered node's detections are kept; zero keeps none

    @classmethod
    def from_env(cls) -> "Policy":
        """FUSION_PHYSICAL_TRIP_TYPES: comma-separated physical event types that trip a node.
        FUSION_PHYSICAL_TRIP_KINDS: comma-separated zone kinds that trip it on leaving their resting state.
        FUSION_HOLD_MINUTES: how long detections from unregistered nodes are held for replay (default 15)."""
        trips = os.getenv("FUSION_PHYSICAL_TRIP_TYPES")
        trip_kinds = os.getenv("FUSION_PHYSICAL_TRIP_KINDS")
        try:
            hold = timedelta(minutes=int(os.getenv("FUSION_HOLD_MINUTES", "") or DEFAULT_HOLD.total_seconds() // 60))
        except ValueError:
            raise ValueError("FUSION_HOLD_MINUTES must be a whole number of minutes") from None
        if hold < timedelta(0):
            raise ValueError("FUSION_HOLD_MINUTES cannot be negative")
        return cls(
            physical_trips=DEFAULT_PHYSICAL_TRIPS
            if trips is None
            else frozenset(t.strip() for t in trips.split(",") if t.strip()),
            zone_kinds=DEFAULT_ZONE_KINDS
            if trip_kinds is None
            else frozenset(k.strip() for k in trip_kinds.split(",") if k.strip()),
            hold=hold,
        )


def status_is_up(subsystem: str, data: dict) -> bool:
    """Is the node up, from the data of its status event (node status, or the panel's status)?

    Camera: the camera and its link must both be online. `detector_ok: false` can only mean inference is
    not yet confirmed, and `recording_ok` is a degradation, so neither makes the camera down.
    RF and physical: the node's `state` says; anything but "offline" (online, degraded) is up.
    A subsystem we do not know is taken as up: we cannot say it is not.
    """
    if subsystem == "camera":
        return data.get("camera_online") is not False and data.get("link_online") is not False
    if subsystem in ("rf", "physical"):
        return str(data.get("state", "")).lower() != "offline"
    return True


def _zone_left_rest(data: dict, policy: Policy) -> bool:
    """A zone of a type that matters has moved out of its resting state, as a live observation: a door opened,
    motion, glass breaking. Not a zone the bridge is only listing, and not a zone the bridge calls stale."""
    zone = data.get("zone")
    if not isinstance(zone, dict) or zone.get("kind") not in policy.zone_kinds:
        return False
    if data.get("event_type") in NOT_A_CHANGE or data.get("stale") is True:
        return False
    return str(data.get("state", "")).lower() not in RESTING_STATES


def classify(ev: Event, policy: Policy) -> tuple[str, bool] | None:
    """("trip", True) when the event trips its node, ("health", up) when it says whether the node is up,
    None when it is not about a node's status (yet)."""
    data = ev.data
    match ev.type:
        case "camera.detection":
            return ("trip", True) if data.get("event") == "object_start" else None
        case "rf.alert":
            return ("health", False) if data.get("rule_type") == RF_NODE_OFFLINE else ("trip", True)
        case "physical.event":
            if data.get("synthetic") is True:  # inferred by the bridge after a reconnect: not an observation
                return None
            if data.get("tamper") is True or data.get("event_type") in policy.physical_trips:
                return ("trip", True)
            if _zone_left_rest(data, policy):
                return ("trip", True)
            return None
        case "camera.node_status" | "rf.node_status" | "physical.panel_status":
            return ("health", status_is_up(ev.subsystem, data))
    return None


class FusionMapper:
    def __init__(self, state: GatewayState, policy: Policy | None = None):
        self.state = state
        self.policy = policy or Policy()
        self.stats: Counter[str] = Counter()
        self._unregistered_seen: set[tuple[str, str, str | None]] = set()
        self._discovery_full = False

    def apply(self, session: Session, events: list[Event]) -> list[bytes]:
        """Applies a page of events, in cursor order, in the caller's transaction. Returns the CoT to send
        once it has committed. One event that cannot be mapped is logged and skipped, not the whole page."""
        now = datetime.now(timezone.utc)
        out: list[bytes] = []
        for ev in events:
            try:
                out += self._event(session, ev, now)
            except SQLAlchemyError:
                raise  # the transaction is no good: the page must be tried again
            except Exception:  # noqa: BLE001
                self.stats["error"] += 1
                log.exception("fusion event %s (%s) could not be mapped", ev.id, ev.type)
        self._prune(session, now)
        return out

    def _event(self, session: Session, ev: Event, now: datetime) -> list[bytes]:
        """An `rf.position` first says where its emitter is, for the trips that emitter causes; then every
        event is taken as what it says of a node."""
        moved = self._position(session, ev, now) if ev.type == "rf.position" else []
        return moved + self._node_event(session, ev, now)

    def _position(self, session: Session, ev: Event, now: datetime) -> list[bytes]:
        """Remembers where Fusion put an emitter at a site, and moves the open trips it caused there. An
        older position than the one held changes nothing. Past EMITTERS_MAX emitters new ones are not added."""
        emitter, where = emitter_of(ev.data), position_of(ev.data)
        if emitter is None or where is None:
            self.stats["position_unreadable"] += 1
            return []
        row = session.get(EmitterRow, (ev.site_id, emitter))
        if row is None:
            if session.scalar(select(func.count()).select_from(EmitterRow)) >= EMITTERS_MAX:
                self.stats["position_dropped"] += 1
                return []
            row = EmitterRow(site_id=ev.site_id, emitter=emitter, x_ft=where[0], y_ft=where[1], heard_at=ev.timestamp)
            session.add(row)
        elif ev.timestamp < row.heard_at:
            return []  # out of order
        row.x_ft, row.y_ft, row.heard_at = where[0], where[1], ev.timestamp
        self.stats["position"] += 1
        return [cot.node_event(s, now) for s in self.state.emitter_heard(ev.site_id, emitter, *where, session)]

    @staticmethod
    def _allowed(session: Session, ev: Event) -> bool:
        """Whether this is an RF alert from an emitter on its site's allow list. An alert that names no emitter
        (or one that is not a MAC address) is never allowed: there is nothing to match, so it trips."""
        if ev.type != "rf.alert":
            return False
        emitter = emitter_of(ev.data)
        return emitter is not None and session.get(AllowedEmitterRow, (ev.site_id, emitter)) is not None

    def _emitter(self, session: Session, site_id: str, emitter: str | None, at: datetime) -> dict:
        """What a trip says of the emitter that caused it: its id, and its position if Fusion gave one within
        EMITTER_POSITION_TTL of the alert."""
        if emitter is None:
            return {}
        row = session.get(EmitterRow, (site_id, emitter))
        if row is None or abs(at - row.heard_at) > EMITTER_POSITION_TTL:
            return {"emitter": emitter}
        return {"emitter": emitter, "x_ft": row.x_ft, "y_ft": row.y_ft}

    def _node_event(self, session: Session, ev: Event, now: datetime) -> list[bytes]:
        node_id = attributed_node(ev)
        if node_id != ev.node_id:
            ev = ev.model_copy(update={"node_id": node_id})
        effect = classify(ev, self.policy)
        if effect and effect[0] == "trip" and self._allowed(session, ev):
            # A friendly emitter an admin allowed at this site: its alert trips nothing and is not held. What
            # it says of the node being there still counts.
            self.stats["allowed"] += 1
            effect = None
        node = find_node(session, ev.site_id, ev.subsystem, ev.node_id)
        if node is None:
            # Nobody registered this node (or its site, or its subsystem). Remember that it exists, so an
            # operator can find it, and keep a detection from it, so registering it can still raise that.
            self._unregistered(session, ev.site_id, ev.subsystem, ev.node_id, now, ev.type, events=1)
            if effect and effect[0] == "trip":
                self._hold(session, ev, now)
                # At a registered site it is a node the admin has not placed: its trip still shows, in the pane.
                return self._unplaced_trip(session, ev, now)
            return []
        self._seen(node, now)
        if effect is None:
            self.stats["ignored"] += 1
            return []
        if effect[0] == "trip":
            detection = DetectionEvent(
                event_id=ev.id[:128],
                type=ev.type[:64],
                time=ev.timestamp,
                severity=trip_words.severity(ev.data, SEVERITY),
                subsystem=node.subsystem,
                node_id=node.node_id,
                detail=trip_words.describe(ev.type, ev.data),
                **self._emitter(
                    session, node.site_id, emitter_of(ev.data) if ev.type == "rf.alert" else None, ev.timestamp
                ),
            )
            new = self.state.apply_event(node.site_id, detection, now, session)
            self.stats["tripped" if new else "stale_trip"] += 1
            return cot.trip_events(new, now) if new else []
        return self._health(session, node, effect[1], ev.timestamp)

    def reconcile(self, session: Session, nodes: list[NodeStatus], now: datetime) -> list[bytes]:
        """Brings node health in line with `/nodes`, the latest status of every node and how fresh it is.

        This is what notices silence: a node that has stopped reporting is `stale` there, and events alone
        never say so. It also heals whatever the event log lost to retention, since nodes outlive it. The
        reports are dated `now`, not the node's own time: they say how things are as of this query, and
        must be newer than whatever an earlier cycle (or an outage) recorded. A tripped node is never
        changed by health, so none of this can end an alert."""
        out: list[bytes] = []
        for status in nodes:
            node = find_node(session, status.site_id, status.subsystem, status.node_id)
            if node is None:
                self._unregistered(session, status.site_id, status.subsystem, status.node_id, now, None, events=0)
                out += self._unplaced_health(session, status, now)
                continue
            self._seen(node, now)
            up = status_is_up(status.subsystem, status.data) and not status.stale
            out += self._health(session, node, up, now)
        return out

    def mark_unreachable(self, session: Session, now: datetime) -> list[bytes]:
        """Fusion has been out of reach for a while: what it last said is no longer evidence. Every node
        Fusion reports on is offline until `/nodes` says otherwise (a tripped one stays tripped). Nodes
        that only ever report to the gateway directly are not Fusion's to speak for, and are left alone."""
        out: list[bytes] = []
        for node in session.scalars(select(NodeRow).where(NodeRow.fusion_seen_at.is_not(None))).all():
            out += self._health(session, node, False, now)
        # A node shown unplaced is only ever Fusion's: offline too (a tripped one stays tripped).
        for row in session.scalars(select(UnplacedStateRow)).all():
            new = self.state.apply_unplaced_health(
                row.site_id,
                HealthReport(subsystem=Subsystem(row.subsystem), node_id=row.node_id, up=False, time=now),
                session,
            )
            if new is not None:
                out.append(cot.node_event(new, now))
        return out

    def _unplaced_trip(self, session: Session, ev: Event, now: datetime) -> list[bytes]:
        """A detection from a node at a registered site that the admin has not placed (GatewayState
        .apply_unplaced_event): its node CoT, placed="false", for the pane. Nothing at a site nobody registered."""
        try:
            subsystem = Subsystem(ev.subsystem)
        except ValueError:
            return []
        detection = DetectionEvent(
            event_id=ev.id[:128],
            type=ev.type[:64],
            time=ev.timestamp,
            severity=trip_words.severity(ev.data, SEVERITY),
            subsystem=subsystem,
            node_id=ev.node_id if ev.node_id is not None else ANY_NODE,
            detail=trip_words.describe(ev.type, ev.data),
        )
        new = self.state.apply_unplaced_event(ev.site_id, detection, now, session)
        if new is None:
            return []
        self.stats["unplaced_trip"] += 1
        return [cot.node_event(new, now)]

    def _unplaced_health(self, session: Session, status: NodeStatus, now: datetime) -> list[bytes]:
        """Health for an unplaced node the pane shows (GatewayState.apply_unplaced_health)."""
        try:
            subsystem = Subsystem(status.subsystem)
        except ValueError:
            return []
        up = status_is_up(status.subsystem, status.data) and not status.stale
        node_id = status.node_id if status.node_id is not None else ANY_NODE
        new = self.state.apply_unplaced_health(
            status.site_id, HealthReport(subsystem=subsystem, node_id=node_id, up=up, time=now), session
        )
        return [cot.node_event(new, now)] if new else []

    def _seen(self, node: NodeRow, now: datetime) -> None:
        """Notes that Fusion reports on this node (at most once an hour: it is only to tell Fusion's nodes
        from the ones that report to the gateway directly)."""
        if node.fusion_seen_at is None or now - node.fusion_seen_at > timedelta(hours=1):
            node.fusion_seen_at = now

    def _health(self, session: Session, node: NodeRow, up: bool, at: datetime) -> list[bytes]:
        new: NodeState | None = self.state.apply_health(
            node.site_id, HealthReport(subsystem=node.subsystem, node_id=node.node_id, up=up, time=at), session
        )
        self.stats["health_change" if new else "health_same"] += 1
        return [cot.node_event(new, datetime.now(timezone.utc))] if new else []

    def _unregistered(
        self,
        session: Session,
        site: str,
        subsystem: str,
        node: str | None,
        now: datetime,
        type_: str | None,
        events: int,
    ) -> None:
        """Writes down that this node exists and nobody has registered it: when first and last seen, how many
        events, and the type of the last. A node with no id is "*". Past DISCOVERED_MAX distinct nodes new ones
        are not added (and that is said once)."""
        self.stats["unregistered"] += 1
        key = (site, subsystem, node)
        if key not in self._unregistered_seen:
            self._unregistered_seen.add(key)
            log.warning(
                "fusion: site %r %s node %r is not registered; register it to act on its events", site, subsystem, node
            )
        node_id = node if node is not None else ANY_NODE
        row = session.get(DiscoveredRow, (site, subsystem, node_id))
        if row is None:
            if session.scalar(select(func.count()).select_from(DiscoveredRow)) >= DISCOVERED_MAX:
                if not self._discovery_full:
                    self._discovery_full = True
                    log.error("fusion: %d unregistered nodes are already remembered; not adding more", DISCOVERED_MAX)
                return
            row = DiscoveredRow(site_id=site, subsystem=subsystem, node_id=node_id, first_seen=now, event_count=0)
            session.add(row)
        row.last_seen = now
        row.event_count += events
        row.last_type = type_ or row.last_type

    def _hold(self, session: Session, ev: Event, now: datetime) -> None:
        """Keeps a detection from an unregistered node, if it is recent enough and not held already."""
        if self.policy.hold <= timedelta(0) or ev.timestamp < now - self.policy.hold:
            return
        if session.get(HeldRow, ev.id) is not None:
            return  # delivered again
        node = ev.node_id if ev.node_id is not None else ANY_NODE
        same = HeldRow.site_id == ev.site_id, HeldRow.subsystem == ev.subsystem, HeldRow.node_id == node
        if session.scalar(select(func.count()).select_from(HeldRow).where(*same)) >= HELD_PER_NODE:
            oldest = session.scalar(select(HeldRow.event_id).where(*same).order_by(HeldRow.event_timestamp).limit(1))
            session.execute(delete(HeldRow).where(HeldRow.event_id == oldest))
        session.add(
            HeldRow(
                event_id=ev.id,
                site_id=ev.site_id,
                subsystem=ev.subsystem,
                node_id=node,
                type=ev.type,
                event_timestamp=ev.timestamp,
                severity=trip_words.severity(ev.data, SEVERITY),
                detail=trip_words.describe(ev.type, ev.data),
                emitter=emitter_of(ev.data) if ev.type == "rf.alert" else None,
            )
        )
        self.stats["held"] += 1

    def _prune(self, session: Session, now: datetime) -> None:
        session.execute(delete(HeldRow).where(HeldRow.event_timestamp < now - self.policy.hold))
        session.execute(delete(DiscoveredRow).where(DiscoveredRow.last_seen < now - DISCOVERED_TTL))
        session.execute(delete(EmitterRow).where(EmitterRow.heard_at < now - EMITTER_POSITION_TTL))
        self._discovery_full = False

    def replay(self, session: Session, now: datetime) -> list[bytes]:
        """Raises what was held for nodes that are registered now. Only the latest
        detection for each node is applied (one trip is enough: it stays until an operator clears it), through
        the same rules as a live one, so a clear or an earlier replay is respected. What was applied is no longer
        held. Returns the CoT to send once the caller's transaction has committed."""
        held = session.scalars(
            select(HeldRow).where(HeldRow.event_timestamp >= now - self.policy.hold).order_by(HeldRow.event_timestamp)
        ).all()
        latest: dict[tuple[str, str, str], tuple[NodeRow, HeldRow]] = {}
        used: list[str] = []
        for h in held:
            node = find_node(session, h.site_id, h.subsystem, h.node_id)
            if node is not None:
                latest[(node.site_id, node.subsystem, node.node_id)] = (node, h)
                used.append(h.event_id)
        out: list[bytes] = []
        for node, h in latest.values():
            detection = DetectionEvent(
                event_id=h.event_id[:128],
                type=h.type[:64],
                time=h.event_timestamp,
                severity=SEVERITY if h.severity is None else h.severity,
                subsystem=Subsystem(node.subsystem),
                node_id=node.node_id,
                detail=h.detail,
                **self._emitter(session, node.site_id, h.emitter, h.event_timestamp),
            )
            new = self.state.apply_event(node.site_id, detection, now, session)
            self.stats["replayed" if new else "stale_trip"] += 1
            if new:
                out += cot.trip_events(new, now)
        if used:
            session.execute(delete(HeldRow).where(HeldRow.event_id.in_(used)))
        return out
