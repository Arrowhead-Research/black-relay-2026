"""src/pytak_gateway/state.py

What the gateway last knew about each node, and the rules for changing it.

`GatewayState` decides; it sends nothing. A detection trips a node, a health report takes it offline or
back, a clear ends a trip, and each of those returns the node's new state, or None when the change is
ignored (stale, a replay, nothing new). The state lives in the database (db.NodeStateRow), not in memory,
so it survives a restart and is shared by every process on the registry file.

Turning a new state into CoT and sending it is cot_publisher.py's job. The Fusion path (fusion_mapper.py)
uses this class directly. A message never says which site it is for: the caller passes the site, which it
got from the URL (HTTP), the topic (MQTT) or Fusion's own event (the http monitor). A node is
(site, subsystem, node_id).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from . import cot
from .db import DiscoveredRow, NodeRow, NodeStateRow, SiteRow, UnplacedStateRow
from .models import ClearRequest, DetectionEvent, HealthReport, Location, Node, NodeState, Status, Subsystem
from .registry import Registry, find_node, node_of, unregistered_filter

THREAT_LIFETIME = timedelta(minutes=5)  # with no newer detection, a threat area comes off the map
DELETE_ROUNDS = 3  # re-sends that carry a threat's delete: the first and two repeats, for a phone that missed one

# a node's state row is the node's: same three-part key
_STATE_OF = and_(
    NodeStateRow.site_id == NodeRow.site_id,
    NodeStateRow.subsystem == NodeRow.subsystem,
    NodeStateRow.node_id == NodeRow.node_id,
)


def _ms(t: datetime) -> datetime:
    """`updated` goes out in milliseconds (cot._iso_ms), so that is all a phone can send back."""
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


def _state(node: NodeRow, row: NodeStateRow) -> NodeState:
    threat = None
    if row.threat_lat is not None:
        threat = Location(lat=row.threat_lat, lon=row.threat_lon, ce=row.threat_ce)
    return NodeState(
        node=node_of(node),
        status=Status(row.status),
        updated=row.updated,
        threat=threat,
        severity=row.severity,
        event_type=row.event_type,
        detail=row.detail,
        emitter=row.emitter,
        x_ft=row.emitter_x_ft,
        y_ft=row.emitter_y_ft,
    )


def _unplaced_state(site: SiteRow, row: UnplacedStateRow) -> NodeState:
    """An unplaced node as it is sent: at its site's position, with no range (so no circle), and placed False."""
    node = Node(
        site_id=site.site_id,
        mission_package=site.mission_package_name,
        subsystem=Subsystem(row.subsystem),
        node_id=row.node_id,
        lat=site.lat,
        lon=site.lon,
        range_m=None,
    )
    return NodeState(
        node=node,
        status=Status(row.status),
        updated=row.updated,
        severity=row.severity,
        event_type=row.event_type,
        detail=row.detail,
        placed=False,
    )


def _set_threat(row: NodeStateRow, threat: Location | None) -> None:
    row.threat_lat, row.threat_lon, row.threat_ce = (
        (threat.lat, threat.lon, threat.ce) if threat else (None, None, None)
    )


class GatewayState:
    """The latest state per node, and the rule for applying events. The gateway re-sends all of
    it on a timer, which makes it the thing that has to be right about "latest"."""

    def __init__(self, registry: Registry):
        self.registry = registry

    def _held(self, s: Session, site_id: str, subsystem, node_id: str) -> tuple[NodeRow | None, NodeStateRow | None]:
        key = (site_id, getattr(subsystem, "value", subsystem), node_id)
        return s.get(NodeRow, key), s.get(NodeStateRow, key)

    @contextmanager
    def _txn(self, session: Session | None):
        """The caller's transaction when it has one open (http-monitor saves a page's changes together
        with its cursor), otherwise a transaction of its own."""
        if session is not None:
            yield session
        else:
            with self.registry.begin() as s:
                yield s

    def apply_event(
        self, site_id: str, ev: DetectionEvent, now: datetime | None = None, session: Session | None = None
    ) -> Optional[NodeState]:
        """A detection event means the node tripped. Returns the new state, or None if ignored.
        The threat area is put on a clock from `now`: it comes off the map THREAT_LIFETIME later."""
        now = now or datetime.now(timezone.utc)
        with self._txn(session) as s:
            node, held = self._held(s, site_id, ev.subsystem, ev.node_id)
            if node is None:
                return None  # unknown node: rejected upstream too
            if held and held.tripped_at is not None and ev.time <= held.tripped_at:
                return None  # out of order or duplicate
            # `updated` is what a phone compares, so it only ever moves forward, even when a health report
            # (source time later than this detection's) has already moved it.
            try:
                newer = held is None or ev.time > held.updated
                updated = ev.time if newer else held.updated + timedelta(milliseconds=1)
            except OverflowError:
                return None  # a node dated at the calendar's end
            # Only a detection that says where it is has a threat area. One that does not (every Fusion
            # detection) draws nothing round its node: the node's own position is not where the thing detected
            # is, and a circle as wide as the node's range says nothing of where to look. Where a trip is, the
            # phone draws: at the emitter for RF, at the node's watch point for a camera or the panel.
            threat = ev.location
            had_threat = held is not None and held.threat_lat is not None
            row = held or NodeStateRow(site_id=site_id, subsystem=node.subsystem, node_id=node.node_id)
            row.status, row.updated, row.tripped_at = Status.tripped.value, updated, ev.time
            _set_threat(row, threat)
            row.severity, row.event_type, row.detail = ev.severity, ev.type, ev.detail
            row.emitter, row.emitter_x_ft, row.emitter_y_ft = ev.emitter, ev.x_ft, ev.y_ft
            row.threat_expires_at = now + THREAT_LIFETIME if threat else None
            # With a new circle, a repeat of the old one's delete would take it down. With none, an older trip's
            # circle (one drawn before the gateway stopped drawing them) has to come off the map.
            row.threat_delete_rounds = DELETE_ROUNDS if had_threat and threat is None else 0
            s.add(row)
            return _state(node, row)

    def apply_health(self, site_id: str, h: HealthReport, session: Session | None = None) -> Optional[NodeState]:
        """Health says up or down. Down is offline. Neither changes a TRIPPED node: losing one can
        be the incident itself, so an outage must not end the alert (open question: who clears)."""
        with self._txn(session) as s:
            node, held = self._held(s, site_id, h.subsystem, h.node_id)
            if node is None:
                return None
            if held and (h.time <= held.updated or held.status == Status.tripped.value):
                return None
            if not h.up and held is not None and held.status == Status.offline.value:
                return None  # already offline
            if not h.up:
                new_status = Status.offline
            elif held is None or held.status == Status.offline.value:
                new_status = Status.ok
            else:
                return None  # already ok
            row = held or NodeStateRow(site_id=site_id, subsystem=node.subsystem, node_id=node.node_id)
            row.status, row.updated = new_status.value, h.time
            row.severity = row.event_type = row.detail = None
            row.emitter = row.emitter_x_ft = row.emitter_y_ft = None
            s.add(row)
            return _state(node, row)

    def clear(self, site_id: str, c: ClearRequest) -> Optional[NodeState]:
        """The one path that ends a trip (ADR-104): the node goes ok, with `updated` one
        millisecond past the trip. That is newer for every phone and older than any detection the
        node makes next, and no clock's reading: a clear's own time only has to be later than
        the trip. None, and nothing changes, unless the node is tripped, the clear is newer than
        the trip, and the trip it names (when it names one) is the one held. Its threat area's
        delete starts its DELETE_ROUNDS."""
        with self.registry.begin() as s:
            node, held = self._held(s, site_id, c.subsystem, c.node_id)
            if node is None:
                return self._clear_unplaced(s, site_id, c)
            if held is None or held.status != Status.tripped.value or c.time <= _ms(held.updated):
                return None
            if c.tripped is not None and _ms(c.tripped) != _ms(held.updated):
                return None  # a newer detection, or a replay
            # `updated` goes out in milliseconds, and a phone takes only a strictly newer one.
            try:
                updated = _ms(held.updated) + timedelta(milliseconds=1)
            except OverflowError:
                return None  # a trip dated at the calendar's end
            self._end_trip(held, updated)
            return _state(node, held)

    # --- unplaced nodes: Fusion reports them at a registered site, the admin has not placed them ----------------

    def apply_unplaced_event(
        self, site_id: str, ev: DetectionEvent, now: datetime | None = None, session: Session | None = None
    ) -> Optional[NodeState]:
        """A detection from a node at a registered site that is not placed: it trips, by the same rules as a placed
        node's, with no threat area (there is nowhere to draw one). None when ignored: no such site, the node is
        placed after all, or the detection is stale."""
        with self._txn(session) as s:
            site = s.get(SiteRow, site_id)
            subsystem = getattr(ev.subsystem, "value", ev.subsystem)
            if site is None or find_node(s, site_id, subsystem, ev.node_id) is not None:
                return None
            held = s.get(UnplacedStateRow, (site_id, subsystem, ev.node_id))
            if held and held.tripped_at is not None and ev.time <= held.tripped_at:
                return None  # out of order or duplicate
            try:
                newer = held is None or ev.time > held.updated
                updated = ev.time if newer else held.updated + timedelta(milliseconds=1)
            except OverflowError:
                return None
            row = held or UnplacedStateRow(site_id=site_id, subsystem=subsystem, node_id=ev.node_id)
            row.status, row.updated, row.tripped_at = Status.tripped.value, updated, ev.time
            row.severity, row.event_type, row.detail = ev.severity, ev.type, ev.detail
            s.add(row)
            return _unplaced_state(site, row)

    def apply_unplaced_health(
        self, site_id: str, h: HealthReport, session: Session | None = None
    ) -> Optional[NodeState]:
        """Health for a node at a registered site that is not placed: the same rules as a placed node's. The
        first report of one the pane does not list yet lists it, ok or offline: the pane shows every sensor
        Fusion reports at the house, placed or not."""
        with self._txn(session) as s:
            subsystem = getattr(h.subsystem, "value", h.subsystem)
            held = s.get(UnplacedStateRow, (site_id, subsystem, h.node_id))
            if held is None:
                site = s.get(SiteRow, site_id)
                if site is None or find_node(s, site_id, subsystem, h.node_id) is not None:
                    return None
                held = UnplacedStateRow(site_id=site_id, subsystem=subsystem, node_id=h.node_id)
                held.status, held.updated = (Status.ok if h.up else Status.offline).value, h.time
                s.add(held)
                return _unplaced_state(site, held)
            if h.time <= held.updated or held.status == Status.tripped.value:
                return None
            if not h.up and held.status == Status.offline.value:
                return None  # already offline
            if not h.up:
                new_status = Status.offline
            elif held.status == Status.offline.value:
                new_status = Status.ok
            else:
                return None  # already ok
            held.status, held.updated = new_status.value, h.time
            held.severity = held.event_type = held.detail = None
            return _unplaced_state(held_site(s, held), held)

    def seed_unplaced(self, site_id: str, session: Session, now: datetime | None = None) -> None:
        """After a build or update, in its transaction: every node Fusion has reported at the site that is not
        placed is listed at once, ok as of now, so the pane shows the whole house without waiting for Fusion's
        next report (which then corrects it: offline, or tripped)."""
        now = now or datetime.now(timezone.utc)
        for d in session.scalars(
            select(DiscoveredRow).where(DiscoveredRow.site_id == site_id, *unregistered_filter())
        ).all():
            if session.get(UnplacedStateRow, (site_id, d.subsystem, d.node_id)) is None:
                session.add(
                    UnplacedStateRow(
                        site_id=site_id, subsystem=d.subsystem, node_id=d.node_id, status=Status.ok.value, updated=now
                    )
                )

    def unplaced_snapshot(self, site_id: str | None = None) -> list[NodeState]:
        """Every unplaced node's state (or one site's), for a re-send."""
        with self.registry.begin() as s:
            q = select(UnplacedStateRow, SiteRow).join(SiteRow, SiteRow.site_id == UnplacedStateRow.site_id)
            if site_id is not None:
                q = q.where(UnplacedStateRow.site_id == site_id)
            rows = s.execute(q.order_by(UnplacedStateRow.site_id, UnplacedStateRow.subsystem, UnplacedStateRow.node_id))
            return [_unplaced_state(site, row) for row, site in rows.all()]

    def adopt_unplaced(self, site_id: str, session: Session, now: datetime | None = None) -> list[NodeState]:
        """After a build or update, in its transaction: an unplaced node the admin has now placed takes its state
        with it, so a trip carries on, now on the map with a threat area round where it was placed. Returns the
        states that moved, for their CoT."""
        now = now or datetime.now(timezone.utc)
        out = []
        for row in session.scalars(select(UnplacedStateRow).where(UnplacedStateRow.site_id == site_id)).all():
            node, held = self._held(session, site_id, row.subsystem, row.node_id)
            if node is None:
                continue  # still not placed, or now taken by its subsystem's "*" node (covered_unplaced)
            state = held or NodeStateRow(site_id=site_id, subsystem=row.subsystem, node_id=row.node_id)
            state.status, state.updated, state.tripped_at = row.status, row.updated, row.tripped_at
            state.severity, state.event_type, state.detail = row.severity, row.event_type, row.detail
            state.threat_delete_rounds = 0
            # No circle round the node (as apply_event): an unplaced trip never said where it is.
            _set_threat(state, None)
            state.threat_expires_at = None
            session.add(state)
            session.delete(row)
            out.append(_state(node, state))
        return out

    def covered_unplaced(self, site_id: str, session: Session) -> list[Node]:
        """After a build or update, in its transaction: unplaced nodes the site now covers without placing them
        (their subsystem's "*" node is placed, so their events go to it) are no longer listed. Returns them, to
        take off phones."""
        gone = []
        for row in session.scalars(select(UnplacedStateRow).where(UnplacedStateRow.site_id == site_id)).all():
            node = find_node(session, site_id, row.subsystem, row.node_id)
            if node is not None and (node.subsystem, node.node_id) != (row.subsystem, row.node_id):
                gone.append(_unplaced_state(held_site(session, row), row).node)
                session.delete(row)
        return gone

    @staticmethod
    def _clear_unplaced(s: Session, site_id: str, c: ClearRequest) -> Optional[NodeState]:
        """As clear, for an unplaced node: it goes ok one millisecond past the trip. It has no threat area."""
        held = s.get(UnplacedStateRow, (site_id, getattr(c.subsystem, "value", c.subsystem), c.node_id))
        if held is None or held.status != Status.tripped.value or c.time <= _ms(held.updated):
            return None
        if c.tripped is not None and _ms(c.tripped) != _ms(held.updated):
            return None
        try:
            updated = _ms(held.updated) + timedelta(milliseconds=1)
        except OverflowError:
            return None
        held.status, held.updated = Status.ok.value, updated
        held.severity = held.event_type = held.detail = None
        return _unplaced_state(held_site(s, held), held)

    @staticmethod
    def _end_trip(held: NodeStateRow, updated: datetime) -> None:
        held.status, held.updated = Status.ok.value, updated
        _set_threat(held, None)
        held.severity = held.event_type = held.detail = held.threat_expires_at = None
        held.emitter = held.emitter_x_ft = held.emitter_y_ft = None
        held.threat_delete_rounds = DELETE_ROUNDS

    def clear_emitter(self, site_id: str, emitter: str, session: Session) -> list[NodeState]:
        """Ends every open trip this emitter caused at this site, as a clear ends one: an admin has just said
        the emitter is friendly. Trips other emitters caused, and other sites', are left. Returns the nodes
        cleared."""
        rows = session.execute(
            select(NodeRow, NodeStateRow)
            .join(NodeStateRow, _STATE_OF)
            .where(
                NodeStateRow.site_id == site_id,
                NodeStateRow.status == Status.tripped.value,
                NodeStateRow.emitter == emitter,
            )
        ).all()
        cleared = []
        for node, held in rows:
            try:
                updated = _ms(held.updated) + timedelta(milliseconds=1)
            except OverflowError:
                continue  # a trip dated at the calendar's end
            self._end_trip(held, updated)
            cleared.append(_state(node, held))
        return cleared

    def emitter_heard(self, site_id: str, emitter: str, x_ft: float, y_ft: float, session: Session) -> list[NodeState]:
        """Fusion has put this emitter somewhere new: every trip it caused at this site, and still open, takes
        the new position. Returns the nodes that changed, to be sent again. Nothing else about a trip moves:
        not its `updated`, so no phone takes this for a new trip."""
        rows = session.execute(
            select(NodeRow, NodeStateRow)
            .join(NodeStateRow, _STATE_OF)
            .where(
                NodeStateRow.site_id == site_id,
                NodeStateRow.status == Status.tripped.value,
                NodeStateRow.emitter == emitter,
            )
        ).all()
        changed = []
        for node, row in rows:
            if (row.emitter_x_ft, row.emitter_y_ft) != (x_ft, y_ft):
                row.emitter_x_ft, row.emitter_y_ft = x_ft, y_ft
                changed.append(_state(node, row))
        return changed

    def snapshot(self) -> list[NodeState]:
        with self.registry.begin() as s:
            rows = s.execute(
                select(NodeRow, NodeStateRow)
                .join(NodeStateRow, _STATE_OF)
                .order_by(NodeRow.site_id, NodeRow.subsystem, NodeRow.node_id)
            ).all()
            return [_state(node, row) for node, row in rows]

    @property
    def nodes(self) -> dict[str, NodeState]:
        """Every known node, keyed by its map uid (cot.node_uid)."""
        return {cot.node_uid(st.node): st for st in self.snapshot()}

    def site_alert(self, site_id: str) -> bool:
        """True when any node of this site is tripped: the site element's status."""
        with self.registry.begin() as s:
            return (
                s.scalar(
                    select(NodeStateRow.node_id)
                    .where(NodeStateRow.site_id == site_id, NodeStateRow.status == Status.tripped.value)
                    .limit(1)
                )
                is not None
            )

    def threat_expiry(self) -> dict[str, datetime]:
        """Threat uid -> when it comes off the map, for every threat still on a clock."""
        with self.registry.begin() as s:
            rows = s.execute(
                select(NodeRow, NodeStateRow)
                .join(NodeStateRow, _STATE_OF)
                .where(NodeStateRow.threat_expires_at.is_not(None))
            ).all()
            return {cot.threat_uid(node_of(node)): row.threat_expires_at for node, row in rows}

    def threat_deletes(self) -> dict[str, int]:
        """Threat uid -> re-sends its delete still rides on."""
        with self.registry.begin() as s:
            return self._pending_deletes(s)

    @staticmethod
    def _pending_deletes(s: Session) -> dict[str, int]:
        rows = s.execute(
            select(NodeRow, NodeStateRow).join(NodeStateRow, _STATE_OF).where(NodeStateRow.threat_delete_rounds > 0)
        ).all()
        return {cot.threat_uid(node_of(node)): row.threat_delete_rounds for node, row in rows}

    def active_threats(self, now: datetime) -> list[NodeState]:
        """Every threat still inside its lifetime, so a phone that connected after the detection
        draws the circle on the next re-send."""
        expiry = self.threat_expiry()
        return [
            st for st in self.snapshot() if st.threat is not None and expiry.get(cot.threat_uid(st.node), now) > now
        ]

    def expire_threats(self, now: datetime) -> list[str]:
        """The uids whose delete rides on this re-send: threats past their lifetime, each for
        DELETE_ROUNDS re-sends, since a phone that missed the only delete would keep a stale
        circle. The node stays tripped: who clears that is still an open question in the PRD."""
        with self.registry.begin() as s:
            expired = s.scalars(select(NodeStateRow).where(NodeStateRow.threat_expires_at <= now)).all()
            for row in expired:
                row.threat_expires_at = None
                row.threat_delete_rounds = DELETE_ROUNDS
            s.flush()
            return list(self._pending_deletes(s))

    def spend_delete_rounds(self, uids: list[str]) -> None:
        """Called only after the send went out: a re-send that failed does not use up a round."""
        with self.registry.begin() as s:
            rows = s.execute(
                select(NodeRow, NodeStateRow).join(NodeStateRow, _STATE_OF).where(NodeStateRow.threat_delete_rounds > 0)
            ).all()
            for node, row in rows:
                if cot.threat_uid(node_of(node)) in uids:
                    row.threat_delete_rounds -= 1


def held_site(s: Session, row: UnplacedStateRow) -> SiteRow:
    """The site an unplaced node's state belongs to (it goes with its site, so there is one)."""
    return s.get(SiteRow, row.site_id)
