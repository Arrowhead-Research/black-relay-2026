"""The gateway's database: SQLAlchemy models, the engine, and the migration runner.

One SQLite file (REGISTRY_DB) holds everything the gateway has to remember: the registry (sites,
nodes) and the state built on it (what each node last said). The http and mqtt
services, and the http monitor, are separate processes sharing that file, so nothing is cached in
memory: every read goes to SQLite (WAL mode, microsecond point lookups at this volume).

Every transaction is BEGIN IMMEDIATE. A read-then-write (apply an event only if it is newer than
what is held) then runs under the write lock from its first statement, so two processes cannot both
read the same "held" and both write; the second waits (busy timeout) instead of failing mid-way.

The schema is owned by Alembic (migrations/): the models below describe what the latest revision
makes, and a new table or column needs a new revision there. `migrate` runs at start-up.
"""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

BUSY_TIMEOUT_MS = 5000


class UTCDateTime(TypeDecorator):
    """A timezone-aware datetime, kept as UTC (SQLite has no zone) and handed back as UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("a stored time must carry a timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class SiteRow(Base):
    """A site: a place (a safehouse), by Fusion's id."""

    __tablename__ = "sites"

    site_id: Mapped[str] = mapped_column(String, primary_key=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    boundary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The ATAK mission package this site's CoT belongs to: its name, and an Argon2id hash of its password.
    # This row is the only place a site and a mission package are linked. Every site has one: a site cannot
    # be made without both (the HTTP route always sets them).
    mission_package_name: Mapped[str] = mapped_column(String, nullable=False)
    mission_package_hash: Mapped[str] = mapped_column(String, nullable=False)


class NodeRow(Base):
    """A registered node: one device of a subsystem at a site, addressed as Fusion addresses it.
    `node_id` "*" is the subsystem's events that name no node (RF positions and device alerts have a null
    node id in Fusion), so those have somewhere to land."""

    __tablename__ = "nodes"

    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True)
    subsystem: Mapped[str] = mapped_column(String, primary_key=True)  # models.Subsystem
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    # None: the admin gave no number, so no circle (Gateway Changes Spec, change 3). RF carries accuracy_m instead.
    range_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    stream: Mapped[str | None] = mapped_column(String, nullable=True)
    # Where a camera or the panel looks: the target goes there when it trips (change 8). Both or neither.
    watch_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    watch_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When Fusion last reported on this node, if it ever has. It tells the nodes Fusion speaks for (which go
    # offline when Fusion is lost) from those that only report to the gateway directly.
    fusion_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Always loaded with the node, so a node cannot be read without its site's mission package (registry.node_of).
    site: Mapped[SiteRow] = relationship(lazy="joined")


class NodeStateRow(Base):
    """What the gateway last knew about one node (state.py). A node with no row is unknown.

    The threat columns are the circle drawn for the detection that tripped it; `threat_expires_at`
    is when that circle comes off the map and `threat_delete_rounds` is how many more re-sends still
    carry its delete (a phone that missed the only delete would keep a stale circle). They live here,
    not in memory, so a restart neither forgets a circle nor stops deleting one.
    """

    __tablename__ = "node_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["site_id", "subsystem", "node_id"],
            ["nodes.site_id", "nodes.subsystem", "nodes.node_id"],
            ondelete="CASCADE",
        ),
    )

    site_id: Mapped[str] = mapped_column(String, primary_key=True)
    subsystem: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)  # models.Status
    updated: Mapped[datetime] = mapped_column(UTCDateTime)
    tripped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    threat_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    threat_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    threat_ce: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)  # trip_words: the gateway's own words
    # RF: the emitter that caused the trip (a MAC address), and where Fusion last put it, in feet from the
    # site's middle point. Kept with the trip so a re-send, a restart and a later "which trips did this
    # emitter cause" all read the same thing.
    emitter: Mapped[str | None] = mapped_column(String, nullable=True)
    emitter_x_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    emitter_y_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    threat_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    threat_delete_rounds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class UnplacedStateRow(Base):
    """What the gateway last knew about a node Fusion reports at a registered site that the admin has not placed:
    it has no position or range, so it is shown in the pane only, never on the map. Kept apart from
    `fusion_discovered`, which is pruned: a trip here stays until it is cleared, as any trip does. Goes with its
    site; when the node is placed, its state moves to node_state (GatewayState.adopt_unplaced)."""

    __tablename__ = "unplaced_state"

    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True)
    subsystem: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)  # models.Status
    updated: Mapped[datetime] = mapped_column(UTCDateTime)
    tripped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)


class DiscoveredRow(Base):
    """A fusion (site, subsystem, node) that sent something while no node was registered for it: the drop-down
    of things an operator can register. `node_id` is "*" for events that name no node. Bounded: it is
    capped and old rows are pruned (fusion_mapper.py)."""

    __tablename__ = "fusion_discovered"

    site_id: Mapped[str] = mapped_column(String, primary_key=True)
    subsystem: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_type: Mapped[str | None] = mapped_column(String, nullable=True)


class HeldRow(Base):
    """A detection or alert that arrived for a node nobody had registered yet, kept for a short window so
    registering the node can still raise it (fusion_mapper.FusionMapper.replay)."""

    __tablename__ = "fusion_held"
    __table_args__ = (Index("ix_fusion_held_node", "site_id", "subsystem", "node_id"),)

    event_id: Mapped[str] = mapped_column(String, primary_key=True)  # fusion's id: a re-delivery is not held twice
    site_id: Mapped[str] = mapped_column(String)
    subsystem: Mapped[str] = mapped_column(String)
    node_id: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    event_timestamp: Mapped[datetime] = mapped_column(UTCDateTime)  # when it happened, not when we got it
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    emitter: Mapped[str | None] = mapped_column(String, nullable=True)  # RF: the emitter the alert named


class EmitterRow(Base):
    """Where Fusion last put an RF emitter at a site (`rf.position`): feet from the site's middle point. A trip
    that emitter causes carries this, so a phone can show where it was heard. Bounded: capped, and a position
    not refreshed for a while is dropped, since an old position is not where the emitter is (fusion_mapper.py)."""

    __tablename__ = "fusion_emitters"

    site_id: Mapped[str] = mapped_column(String, primary_key=True)
    emitter: Mapped[str] = mapped_column(String, primary_key=True)
    x_ft: Mapped[float] = mapped_column(Float)
    y_ft: Mapped[float] = mapped_column(Float)
    heard_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AllowedEmitterRow(Base):
    """An emitter an admin has allowed at a site: a friendly device (the house's own router, a team radio). An
    RF alert it causes there trips nothing (fusion_mapper.py). One list per site; it goes with the site.
    `name` is the admin's own word for it, shown only to an admin and never sent in CoT."""

    __tablename__ = "allowed_emitters"

    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id", ondelete="CASCADE"), primary_key=True)
    emitter: Mapped[str] = mapped_column(String, primary_key=True)  # models.EMITTER_ID
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FusionSourceRow(Base):
    """Where http-monitor is up to in one stream it polls from fusion (fusion_client.source_name).

    `cursor` is fusion's ingestion-order cursor: the last page we applied. It is saved in the same
    transaction as that page's state changes, so a crash re-reads a page (changes are idempotent) and
    never skips one. A cursor belongs to one fusion database history, so there is one row per stream
    and filter combination, never shared.
    """

    __tablename__ = "fusion_sources"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None: not polled yet
    last_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_poll_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)  # None: the last poll went well


class GapRow(Base):
    """History fusion deleted (retention) before we read it: events in (from_cursor, resume_after] are lost.
    Resuming past it was the gateway's choice (fusion_client.CursorExpired), so it is recorded."""

    __tablename__ = "fusion_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String)
    from_cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resume_after: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime)


def _enable_wal(cur: sqlite3.Cursor) -> None:
    """WAL mode lasts in the file, so only the first process on a new database has anything to switch. That
    switch takes a lock and, unlike a transaction, does not wait for the busy timeout when it cannot get it:
    two processes starting together on a new file would fail with "database is locked". So look first, and
    when the switch is refused try again until the timeout has gone by."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            if str(cur.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                cur.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def make_engine(path: str | Path = "registry.db") -> Engine:
    """The SQLite engine: WAL, foreign keys, a busy timeout, and BEGIN IMMEDIATE on every transaction."""
    path = str(path)
    if path == ":memory:":
        # One connection, shared: every new connection to :memory: would be a new, empty database.
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(URL.create("sqlite", database=path), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi, _record):
        dbapi.isolation_level = None  # the driver starts no transactions of its own: `begin` below does
        cur = dbapi.cursor()
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        _enable_wal(cur)
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def migrate(engine: Engine) -> None:
    """Bring the database to the latest revision. Safe to call from every process at start-up: the
    first holds the write lock while it migrates, the rest wait and find nothing left to do."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
