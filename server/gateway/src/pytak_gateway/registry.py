"""The registry: sites and their nodes, kept in SQLite (models in db.py).

An operator creates a site, then registers its nodes under it. A node is addressed the way Fusion
addresses it: (site_id, subsystem, node_id). Nothing is cached in memory: the http and mqtt services and
the http monitor are separate processes sharing one database file, so every read goes to SQLite.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from .db import AllowedEmitterRow, DiscoveredRow, NodeRow, NodeStateRow, SiteRow, make_engine, migrate
from .models import (
    ALLOWED_EMITTERS_MAX,
    ANY_NODE,
    BuildNode,
    BuildPackage,
    Node,
    NodeRegistration,
    Status,
    Subsystem,
)


class SiteExists(Exception):
    pass


class Refused(ValueError):
    """A build package the registry will not take, and nothing was saved. `loc` says which part, as
    FastAPI's own refusals do (["body", "nodes", 2, "node_id"]), so the caller is told one way."""

    def __init__(self, loc: list, msg: str):
        super().__init__(msg)
        self.loc, self.msg = loc, msg


class TrippedNodes(Exception):
    """An update that would remove tripped nodes, and nothing was saved: their alert would vanish for every
    phone. `nodes` are their node ids."""

    def __init__(self, nodes: list[str]):
        super().__init__(", ".join(nodes))
        self.nodes = nodes


class Registry:
    def __init__(self, path: str | Path = "registry.db"):
        self.engine = make_engine(path)
        migrate(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def load_from_env(cls) -> "Registry":
        """REGISTRY_DB, default registry.db in the working directory."""
        return cls(os.environ.get("REGISTRY_DB", "registry.db"))

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _txn(self, session: Session | None):
        """The caller's transaction when it has one open, otherwise a transaction of its own."""
        if session is not None:
            yield session
        else:
            with self.begin() as s:
                yield s

    def begin(self):
        """One transaction: `with registry.begin() as session:`. It commits on leaving the block, rolls
        back if the block raises, and holds the write lock throughout (db.py)."""
        return self._sessions.begin()

    # --- sites ------------------------------------------------------------------------------

    def build_site(
        self,
        pkg: BuildPackage,
        session: Session,
        mission_package: tuple[str, str],
    ) -> list[Node]:
        """The whole site at once, in the caller's transaction: the site, its boundary and its nodes. Returns
        the nodes. `mission_package` is the (name, password hash) the site is linked to. SiteExists: the id is
        taken. Refused: a node Fusion has not reported. Either way nothing is saved, as the caller's
        transaction rolls back."""
        if session.get(SiteRow, pkg.site_id) is not None:
            raise SiteExists(pkg.site_id)
        _check_reported(session, pkg)
        name, hashed = mission_package
        site = SiteRow(
            site_id=pkg.site_id,
            lat=pkg.lat,
            lon=pkg.lon,
            boundary=_boundary_json(pkg),
            mission_package_name=name,
            mission_package_hash=hashed,
        )
        session.add(site)
        session.flush()  # the site first: its nodes point at it
        return [_put_node(session, site, n) for n in pkg.nodes]

    def update_site(self, site_id: str, pkg: BuildPackage, session: Session) -> tuple[list[Node], list[Node]]:
        """The whole site again, in the caller's transaction. A node left out is removed.
        Returns (the nodes, the nodes removed). KeyError: no such site. Refused: a node Fusion has not
        reported. TrippedNodes: it would remove a tripped node."""
        site = session.get(SiteRow, site_id)
        if site is None:
            raise KeyError(site_id)
        _check_reported(session, pkg)
        wanted = {(n.subsystem.value, n.node_id) for n in pkg.nodes}
        leaving = [
            r
            for r in session.scalars(select(NodeRow).where(NodeRow.site_id == site_id))
            if (r.subsystem, r.node_id) not in wanted
        ]
        tripped = sorted(r.node_id for r in leaving if _status(session, r) == Status.tripped.value)
        if tripped:
            raise TrippedNodes(tripped)
        gone = [node_of(r) for r in leaving]
        for r in leaving:
            session.delete(r)  # its state cascades
        site.lat, site.lon, site.boundary = pkg.lat, pkg.lon, _boundary_json(pkg)
        session.flush()
        return [_put_node(session, site, n) for n in pkg.nodes], gone

    def boundary(self, site_id: str) -> list[tuple[float, float]] | None:
        """The site's traced boundary, or None when it has none (or there is no such site)."""
        with self.begin() as s:
            text = s.scalar(select(SiteRow.boundary).where(SiteRow.site_id == site_id))
        return [tuple(p) for p in json.loads(text)] if text else None

    def boundaries(self) -> dict[str, list[tuple[float, float]]]:
        """Every site's traced boundary, for those that have one: what each re-send draws."""
        with self.begin() as s:
            rows = s.execute(select(SiteRow.site_id, SiteRow.boundary).where(SiteRow.boundary.is_not(None))).all()
        return {r.site_id: [tuple(p) for p in json.loads(r.boundary)] for r in rows}

    def site_package(self, site_id: str) -> dict | None:
        """The site as its last build or update left it, in the build package's own shape (models.BuildPackage),
        with its mission package's name but never its password: what an admin on any phone edits. None: no such
        site."""
        with self.begin() as s:
            site = s.get(SiteRow, site_id)
            if site is None:
                return None
            rows = s.scalars(
                select(NodeRow).where(NodeRow.site_id == site_id).order_by(NodeRow.subsystem, NodeRow.node_id)
            )
            nodes = []
            for r in rows:
                node = {"subsystem": r.subsystem, "node_id": r.node_id, "lat": r.lat, "lon": r.lon}
                # As a build sends it: RF carries accuracy_m, the others range_m; null when blank.
                if r.subsystem == Subsystem.rf.value:
                    node["accuracy_m"] = r.accuracy_m
                else:
                    node["range_m"] = r.range_m
                if r.stream is not None:
                    node["stream"] = r.stream
                if r.watch_lat is not None and r.watch_lon is not None:
                    node["watch"] = [r.watch_lat, r.watch_lon]
                nodes.append(node)
            out = {"site_id": site.site_id, "lat": site.lat, "lon": site.lon}
            if site.boundary:
                out["boundary"] = json.loads(site.boundary)
            out["nodes"] = nodes
            out["mission_package_name"] = site.mission_package_name
        return out

    def mission_package_of(self, site_id: str) -> tuple[str, str]:
        """The (name, password hash) of the site's mission package. KeyError: no such site."""
        with self.begin() as s:
            row = s.execute(
                select(SiteRow.mission_package_name, SiteRow.mission_package_hash).where(SiteRow.site_id == site_id)
            ).first()
        if row is None:
            raise KeyError(site_id)
        return row.mission_package_name, row.mission_package_hash

    def mission_packages(self) -> dict[str, str]:
        """Every site's mission package name: what a site's own CoT is annotated with."""
        with self.begin() as s:
            return {
                r.site_id: r.mission_package_name
                for r in s.execute(select(SiteRow.site_id, SiteRow.mission_package_name))
            }

    # --- allowed emitters -------------------------------------------------------------------

    def allow_emitter(self, site_id: str, emitter: str, name: str | None, session: Session) -> bool:
        """Puts an emitter on the site's allow list, in the caller's transaction. Allowing one already there
        keeps it, and takes the new name if one is given. True when it was added. KeyError: no such site.
        Refused: the list is full."""
        if session.get(SiteRow, site_id) is None:
            raise KeyError(site_id)
        row = session.get(AllowedEmitterRow, (site_id, emitter))
        if row is not None:
            row.name = name if name is not None else row.name
            return False
        listed = session.scalar(
            select(func.count()).select_from(AllowedEmitterRow).where(AllowedEmitterRow.site_id == site_id)
        )
        if listed >= ALLOWED_EMITTERS_MAX:
            raise Refused(["body", "emitter"], f"the site already allows {ALLOWED_EMITTERS_MAX} emitters")
        session.add(AllowedEmitterRow(site_id=site_id, emitter=emitter, name=name, added_at=datetime.now(timezone.utc)))
        return True

    def allowed_emitters(self, site_id: str) -> list[dict]:
        """The site's allow list, oldest first. KeyError: no such site."""
        with self.begin() as s:
            if s.get(SiteRow, site_id) is None:
                raise KeyError(site_id)
            rows = s.scalars(
                select(AllowedEmitterRow)
                .where(AllowedEmitterRow.site_id == site_id)
                .order_by(AllowedEmitterRow.added_at, AllowedEmitterRow.emitter)
            ).all()
            return [{"emitter": r.emitter, "name": r.name, "added": _iso(r.added_at)} for r in rows]

    def disallow_emitter(self, site_id: str, emitter: str) -> bool:
        """Takes an emitter off the site's allow list. False when it was not on it (or there is no such site)."""
        with self.begin() as s:
            row = s.get(AllowedEmitterRow, (site_id, emitter))
            if row is None:
                return False
            s.delete(row)
            return True

    def delete_site(self, site_id: str) -> list[Node] | None:
        """Removes the site and its nodes. Returns the nodes removed; None if no site."""
        with self.begin() as s:
            if s.get(SiteRow, site_id) is None:
                return None
            gone = [node_of(r) for r in s.scalars(select(NodeRow).where(NodeRow.site_id == site_id))]
            s.execute(delete(SiteRow).where(SiteRow.site_id == site_id))  # nodes and their state cascade
        return gone

    def sites(self) -> list[tuple[str, float, float]]:
        with self.begin() as s:
            rows = s.execute(select(SiteRow.site_id, SiteRow.lat, SiteRow.lon).order_by(SiteRow.site_id)).all()
        return [(r.site_id, r.lat, r.lon) for r in rows]

    # --- nodes ------------------------------------------------------------------------------

    def resolve(self, site_id: str, subsystem: Subsystem | str, node_id: str) -> Node | None:
        """None means: not a node we know. The caller rejects the event; it never guesses."""
        with self.begin() as s:
            row = s.get(NodeRow, (site_id, Subsystem(subsystem).value, node_id))
            return node_of(row) if row else None

    def register_node(
        self, site_id: str, subsystem: Subsystem, node_id: str, reg: NodeRegistration, session: Session | None = None
    ) -> tuple[Node, bool]:
        """Add the node to the site, or update it if the site already has it. Returns (node, created).
        Position defaults to the site's. KeyError: no such site. ValueError: only a camera has a stream."""
        with self._txn(session) as s:
            site = s.get(SiteRow, site_id)
            if site is None:
                raise KeyError(site_id)
            node = Node(
                site_id=site_id,
                subsystem=subsystem,
                node_id=node_id,
                lat=site.lat if reg.lat is None else reg.lat,
                lon=site.lon if reg.lon is None else reg.lon,
                range_m=reg.range_m,
                stream=reg.stream,
            )
            row = s.get(NodeRow, (site_id, subsystem.value, node_id))
            created = row is None
            row = row or NodeRow(site_id=site_id, site=site, subsystem=subsystem.value, node_id=node_id)
            row.lat, row.lon, row.range_m, row.stream = node.lat, node.lon, node.range_m, node.stream
            s.add(row)
            _ok_from_now(s, row)
        return node, created

    # --- fusion: what has been seen and not registered ----------------------------------------

    def count_unregistered(self) -> int:
        """How many nodes Fusion has sent something for that nobody has registered."""
        with self.begin() as s:
            return s.scalar(select(func.count()).select_from(DiscoveredRow).where(*_unregistered())) or 0

    def list_unregistered(self) -> list[dict]:
        """What an operator can register: Fusion's sites, each with its subsystems and, under them, its nodes,
        for every node that has sent something and is not registered (a node a subsystem's "*" node takes is
        not listed). `registered` says whether the site itself exists."""
        with self.begin() as s:
            rows = s.scalars(
                select(DiscoveredRow)
                .where(*_unregistered())
                .order_by(DiscoveredRow.site_id, DiscoveredRow.subsystem, DiscoveredRow.node_id)
            ).all()
            known = set(s.scalars(select(SiteRow.site_id)))
            sites: dict[str, dict] = {}
            for r in rows:
                site = sites.setdefault(
                    r.site_id, {"site_id": r.site_id, "registered": r.site_id in known, "subsystems": {}}
                )
                site["subsystems"].setdefault(r.subsystem, []).append(
                    {
                        "node_id": r.node_id,
                        "first_seen": _iso(r.first_seen),
                        "last_seen": _iso(r.last_seen),
                        "events": r.event_count,
                        "last_type": r.last_type,
                    }
                )
        return [
            {**site, "subsystems": [{"subsystem": sub, "nodes": nodes} for sub, nodes in site["subsystems"].items()]}
            for site in sites.values()
        ]


def _boundary_json(pkg: BuildPackage) -> str | None:
    return json.dumps([list(p) for p in pkg.boundary]) if pkg.boundary else None


def _status(s: Session, row: NodeRow) -> str | None:
    state = s.get(NodeStateRow, (row.site_id, row.subsystem, row.node_id))
    return state.status if state else None


def _check_reported(s: Session, pkg: BuildPackage) -> None:
    """Refused, naming the first node that is neither registered at this site already nor reported by Fusion
    for it. A subsystem's "*" node is reported when Fusion has reported anything for that subsystem there."""
    for i, n in enumerate(pkg.nodes):
        sub = n.subsystem.value
        if s.get(NodeRow, (pkg.site_id, sub, n.node_id)) is not None:
            continue
        found = select(DiscoveredRow).where(DiscoveredRow.site_id == pkg.site_id, DiscoveredRow.subsystem == sub)
        if n.node_id != ANY_NODE:
            found = found.where(DiscoveredRow.node_id == n.node_id)
        if s.scalars(found.limit(1)).first() is None:
            raise Refused(
                ["body", "nodes", i, "node_id"], f"Fusion has not reported {sub}/{n.node_id} at {pkg.site_id}"
            )


def _put_node(s: Session, site: SiteRow, n: BuildNode) -> Node:
    """Adds or replaces one node as the package has it. What the gateway knows of it (its state) is kept."""
    row = s.get(NodeRow, (site.site_id, n.subsystem.value, n.node_id)) or NodeRow(
        site_id=site.site_id, site=site, subsystem=n.subsystem.value, node_id=n.node_id
    )
    row.lat, row.lon, row.range_m, row.accuracy_m, row.stream = n.lat, n.lon, n.range_m, n.accuracy_m, n.stream
    row.watch_lat, row.watch_lon = n.watch if n.watch else (None, None)
    s.add(row)
    _ok_from_now(s, row)
    return node_of(row)


def _iso(t) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def unregistered_filter() -> tuple:
    """SQL: a discovered node no registered node takes (see _unregistered)."""
    return _unregistered()


def _unregistered() -> tuple:
    """SQL: a discovered node has no node of its own and its subsystem has no "*" node either."""

    def registered(node):
        return exists().where(
            NodeRow.site_id == DiscoveredRow.site_id,
            NodeRow.subsystem == DiscoveredRow.subsystem,
            NodeRow.node_id == node,
        )

    return (~registered(DiscoveredRow.node_id), ~registered(ANY_NODE))


def find_node(s: Session, site_id: str, subsystem: str, node_id: str | None) -> NodeRow | None:
    """The registered node Fusion's (site, subsystem, node) is, in the caller's session. A node of its own
    wins; failing that the subsystem's ANY_NODE takes it (an event that names no node is only ever that);
    None when neither exists."""
    for node in (node_id, ANY_NODE) if node_id is not None else (ANY_NODE,):
        row = s.get(NodeRow, (site_id, subsystem, node))
        if row is not None:
            return row
    return None


def node_of(row: NodeRow) -> Node:
    watch = (row.watch_lat, row.watch_lon) if row.watch_lat is not None and row.watch_lon is not None else None
    # The mission package is read off the node's own site row, the one link between the two (db.py).
    return Node(
        site_id=row.site_id,
        mission_package=row.site.mission_package_name,
        subsystem=row.subsystem,
        node_id=row.node_id,
        lat=row.lat,
        lon=row.lon,
        range_m=row.range_m,
        accuracy_m=row.accuracy_m,
        stream=row.stream,
        watch=watch,
    )


def _ok_from_now(s: Session, row: NodeRow) -> None:
    """A node is ok from the moment it is registered, until it says otherwise: it then has a state, so it is sent
    (and is in its site's mission package) at once, not only after its first event. A node that already has a
    state keeps it. Dated now: a detection from before it still trips it (GatewayState.apply_event only refuses
    one older than a trip), and a health report from before it changes nothing, as it should."""
    _fusions(s, row)
    key = (row.site_id, row.subsystem, row.node_id)
    if s.get(NodeStateRow, key) is None:
        s.add(
            NodeStateRow(
                site_id=row.site_id,
                subsystem=row.subsystem,
                node_id=row.node_id,
                status=Status.ok.value,
                updated=datetime.now(timezone.utc),
                threat_delete_rounds=0,
            )
        )


def _fusions(s: Session, row: NodeRow) -> None:
    """A node registered from what Fusion reported (the drop-down) is Fusion's from the start: when Fusion goes out
    of reach it is marked offline with the rest (FusionMapper.mark_unreachable), not left reading ok. A subsystem's
    "*" node is Fusion's when Fusion reported anything in that subsystem at the site."""
    if row.fusion_seen_at is not None:
        return
    q = select(func.max(DiscoveredRow.last_seen)).where(
        DiscoveredRow.site_id == row.site_id, DiscoveredRow.subsystem == row.subsystem
    )
    if row.node_id != ANY_NODE:
        q = q.where(DiscoveredRow.node_id == row.node_id)
    seen = s.scalar(q)
    if seen is not None:
        row.fusion_seen_at = seen
