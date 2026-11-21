"""src/pytak_gateway/cot_publisher.py

Turns a change of node state into CoT and sends it to TAK Server.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import cot
from .models import ClearRequest, DetectionEvent, HealthReport, Node, NodeState
from .registry import Registry
from .state import GatewayState

RESEND_SECONDS = 10
log = logging.getLogger(__name__)

Sender = Callable[..., Awaitable[None]]  # async (*events: bytes) -> None
Unlister = Callable[[str, list[str]], Awaitable[None]]  # async (mission package, uids) -> None


class CotPublisher:
    """Sends the CoT that follows from a state change, and re-sends it all on a timer."""

    def __init__(self, registry: Registry, sender: Sender, unlist: Unlister | None = None):
        """
        Args:
            registry: the sites and nodes
            sender: puts CoT on the wire
            unlist: takes map items out of a mission package (AtakMissionPackages.remove_items), so a phone
                that syncs the package later does not get back an item that has come off the map. None: nothing
                is taken out (the http monitor, which never takes an item off the map)
        """
        self.registry = registry
        self.sender = sender
        self.unlist = unlist
        self.state = GatewayState(registry)

    async def _unlist(self, items: list[tuple[str, str | None]]) -> None:
        """Take each (uid, mission package) out of its package. Only logged when it fails: the item is already
        off the map, and a phone that syncs the package later would only see it until its stale time."""
        if self.unlist is None:
            return
        by_mission: dict[str, list[str]] = {}
        for uid, mission in items:
            if mission:
                by_mission.setdefault(mission, []).append(uid)
        for mission, uids in by_mission.items():
            try:
                await self.unlist(mission, uids)
            except Exception as e:  # noqa: BLE001 - MissionPackageError, or anything else: never fail the caller
                log.warning("could not take %d items out of mission package %r: %s", len(uids), mission, e)

    @property
    def threat_expiry(self) -> dict[str, datetime]:
        return self.state.threat_expiry()

    @property
    def threat_deletes(self) -> dict[str, int]:
        return self.state.threat_deletes()

    async def handle_event(self, site_id: str, ev: DetectionEvent, now: datetime | None = None) -> NodeState | None:
        """A detection trips its node: send the node and its threat area. None when ignored."""
        now = now or datetime.now(timezone.utc)
        new = self.state.apply_event(site_id, ev, now)
        if new is None:
            # Not a placed node: one at a registered site that the admin has not placed is still shown, in the pane.
            new = self.state.apply_unplaced_event(site_id, ev, now)
            if new is not None:
                await self.sender(cot.node_event(new, now))
            return new
        await self.sender(*cot.trip_events(new, now))
        return new

    async def handle_health(self, site_id: str, h: HealthReport) -> NodeState | None:
        """Down is offline; up clears offline only. None when nothing changed."""
        new = self.state.apply_health(site_id, h) or self.state.apply_unplaced_health(site_id, h)
        if new is None:
            return None
        await self.sender(cot.node_event(new, datetime.now(timezone.utc)))
        return new

    async def forget(self, nodes: list[Node], site_id: str | None = None, mission: str | None = None) -> None:
        """Deregistered nodes (and their site, when it is deleted) come off the map. The registry
        is already updated (and with it their state and clocks), so a failed send is only logged: the
        next resend will not bring them back. When only nodes go (an update), they also come out of their
        mission package. When the site goes (`site_id`), its mission package has already been removed, and
        `mission` is the name it had."""
        deletes = []
        for node in nodes:
            deletes += [(cot.node_uid(node), node.mission_package), (cot.threat_uid(node), node.mission_package)]
        if site_id:
            deletes += [(cot.site_uid(site_id), mission), (cot.boundary_uid(site_id), mission)]
        if not deletes:
            return
        now = datetime.now(timezone.utc)
        try:
            await self.sender(*(cot.delete_event(uid, now) for uid, _ in deletes))
        except (OSError, RuntimeError) as e:
            log.warning("could not send removals to TAK: %s", e)
        if not site_id:
            await self._unlist(deletes)

    async def forget_boundary(self, site_id: str, mission: str | None) -> None:
        """An update took the site's boundary away: off the map, and out of its mission package."""
        uid = cot.boundary_uid(site_id)
        try:
            await self.sender(cot.delete_event(uid, datetime.now(timezone.utc)))
        except (OSError, RuntimeError) as e:
            log.warning("could not send the boundary's removal to TAK: %s", e)
        await self._unlist([(uid, mission)])

    async def handle_clear(self, site_id: str, c: ClearRequest) -> NodeState | None:
        """A clear ends the trip (ADR-104): send the node ok and take its threat area down. The
        delete is repeated on the next re-sends like any other. None when nothing was cleared."""
        new = self.state.clear(site_id, c)
        if new is None:
            return None
        if not new.placed:  # an unplaced node has no threat area to take down
            await self.sender(cot.node_event(new, datetime.now(timezone.utc)))
            return new
        uid = cot.threat_uid(new.node)
        now = datetime.now(timezone.utc)
        await self.sender(cot.node_event(new, now), cot.delete_event(uid, now))
        self.state.spend_delete_rounds([uid])
        await self._unlist([(uid, new.node.mission_package)])
        return new

    async def handle_allow(self, cleared: list[NodeState]) -> None:
        """An admin allowed an emitter, and the trips it had caused are ended (state.clear_emitter, already
        committed): send each node ok and take its threat area down, as a clear does. A failed send is only
        logged: the next re-send carries the nodes, and the deletes ride on it."""
        if not cleared:
            return
        now = datetime.now(timezone.utc)
        uids = [cot.threat_uid(s.node) for s in cleared]
        events = [cot.node_event(s, now) for s in cleared] + [cot.delete_event(uid, now) for uid in uids]
        try:
            await self.sender(*events)
        except (OSError, RuntimeError) as e:
            log.warning("could not send the trips an allowed emitter ended to TAK: %s", e)
            return
        self.state.spend_delete_rounds(uids)
        await self._unlist([(cot.threat_uid(s.node), s.node.mission_package) for s in cleared])

    async def resend_all(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        missions = self.registry.mission_packages()
        events = [
            cot.site_event(site, lat, lon, now, alert=self.state.site_alert(site), mission=missions.get(site))
            for site, lat, lon in self.registry.sites()
        ]
        events += [
            cot.boundary_event(site, points, now, mission=missions.get(site))
            for site, points in self.registry.boundaries().items()
        ]
        nodes = self.state.snapshot()
        events += [cot.node_event(s, now) for s in nodes]
        events += [cot.node_event(s, now) for s in self.state.unplaced_snapshot()]
        deleting = self.state.expire_threats(now)  # before the active ones: it moves the expired out
        mission_of = {cot.threat_uid(s.node): s.node.mission_package for s in nodes}  # each threat's own site's
        events += [cot.delete_event(uid, now) for uid in deleting]
        events += [cot.threat_event(s.node, s.threat, s.updated, now) for s in self.state.active_threats(now)]
        if events:
            await self.sender(*events)
        self.state.spend_delete_rounds(deleting)
        # Taken out on each of its few delete rounds: TAK answers 200 for one already out, and a round whose
        # call failed is made good by the next.
        await self._unlist([(uid, mission_of.get(uid)) for uid in deleting])

    async def resend_loop(self, every: float = RESEND_SECONDS) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.resend_all()
            except (OSError, RuntimeError) as e:  # TAK Server down or the VPN dropped: log, keep looping
                log.warning("re-send failed: %s", e)


async def log_sender(*events: bytes) -> None:
    """Stands in for TAK Server when COT_URL is unset: each CoT goes to the log instead."""
    for ev in events:
        log.info("CoT (not sent, no COT_URL): %s", ev.decode("utf-8"))
