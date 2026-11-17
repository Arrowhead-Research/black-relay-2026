"""Single-writer SQLite store shared by HTTP threads and MQTT clients."""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from fusion_service.domain.errors import (
    CursorExpired,
    IdentityConflict,
    InvalidCursor,
    Rejected,
    StorageUnavailable,
)
from fusion_service.domain.events import Event
from fusion_service.domain.nodes import Node
from fusion_service.domain.time import parse_stamp
from fusion_service.domain.topics import TOPICS
from fusion_service.settings import Settings
from fusion_service.storage.retention import metadata_value, prune
from fusion_service.storage.schema import open_database


@dataclass
class EventPage:
    """One page of the cursor feed; the API layer wraps it in its response schema."""

    events: list[Event]
    next_cursor: int
    has_more: bool


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.last_error: str | None = None
        try:
            self.db = open_database(settings)
        except sqlite3.Error as error:
            raise StorageUnavailable(type(error).__name__) from error

    def close(self) -> None:
        with self.lock:
            self.db.close()

    @contextmanager
    def _storage_boundary(self, *, record: bool) -> Iterator[None]:
        """Translate sqlite3 failures into StorageUnavailable; optionally track readiness."""
        try:
            yield
        except StorageUnavailable as error:
            if record:
                self.last_error = error.reason
            raise
        except sqlite3.Error as error:
            if record:
                self.last_error = type(error).__name__
            raise StorageUnavailable(type(error).__name__) from error

    def maintain(self, now: datetime) -> None:
        with self.lock, self._storage_boundary(record=True):
            with self.db:
                prune(self.db, self.settings, now.timestamp())
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def insert(self, event: Event, namespace: str) -> bool:
        """Store a normalized event. False when it is a duplicate of one already stored."""
        canonical = json.dumps(
            event.source.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        body = event.model_dump_json()
        size = len(body.encode())
        if size > self.settings.max_event_bytes:
            raise Rejected("normalized_event_too_large")
        key = (namespace, event.source_schema, event.source_id)
        now = parse_stamp(event.received_at).timestamp()
        with self.lock, self._storage_boundary(record=True):
            with self.db:
                existing = self.db.execute(
                    "SELECT digest,expires FROM seen WHERE namespace=? AND schema=? AND source_id=?",
                    key,
                ).fetchone()
                if existing and existing["expires"] >= now:
                    if existing["digest"] != digest:
                        raise IdentityConflict("source_id_conflict")
                    return False
                prune(self.db, self.settings, now, incoming=size)
                if metadata_value(self.db, "event_bytes") + size > self.settings.max_event_bytes:
                    raise StorageUnavailable("retention_catching_up")
                if (
                    self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
                    >= self.settings.max_dedupe_entries
                ):
                    raise StorageUnavailable("dedupe_capacity_reached")
                self.db.execute(
                    "INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)",
                    (*key, digest, now + self.settings.dedupe_s),
                )
                self.db.execute(
                    "INSERT INTO events(received,site_id,subsystem,type,node_id,body,size) VALUES (?,?,?,?,?,?,?)",
                    (now, event.site_id, event.subsystem, event.type, event.node_id, body, size),
                )
                self.db.execute(
                    "UPDATE metadata SET value=value+? WHERE key='event_bytes'", (size,)
                )
                if TOPICS[event.source.topic].status:
                    self._update_node(event, now, body)
            self.last_error = None
            return True

    def _update_node(self, event: Event, received: float, body: str) -> None:
        self.db.execute(
            """INSERT INTO nodes VALUES (?,?,?,?,?,?)
            ON CONFLICT(site_id,subsystem,node_id) DO UPDATE SET
                occurred=excluded.occurred,received=excluded.received,body=excluded.body
            WHERE excluded.occurred >= nodes.occurred""",
            (
                event.site_id,
                event.subsystem,
                event.node_id,
                parse_stamp(event.timestamp).timestamp(),
                received,
                body,
            ),
        )
        self.db.execute(
            """DELETE FROM nodes WHERE rowid IN (
            SELECT rowid FROM nodes ORDER BY received DESC,rowid DESC LIMIT -1 OFFSET ?)""",
            (self.settings.max_nodes,),
        )

    def events(self, after: int | None, limit: int, **filters) -> EventPage:
        with self.lock, self._storage_boundary(record=False):
            floor = metadata_value(self.db, "floor_cursor")
            if after is None:
                after = floor
            if after < floor:
                raise CursorExpired(floor)
            high = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            if after > (high[0] if high else 0):
                raise InvalidCursor("cursor is ahead of this database")
            where, params = ["cursor> ?"], [after]
            for name in ("site_id", "subsystem", "type", "node_id"):
                if filters.get(name) is not None:
                    where.append(f"{name}=?")
                    params.append(filters[name])
            params.append(limit + 1)
            rows = self.db.execute(
                f"SELECT cursor,body FROM events WHERE {' AND '.join(where)} ORDER BY cursor LIMIT ?",
                params,
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = Event.model_validate_json(row["body"])
            item.cursor = row["cursor"]
            items.append(item)
        return EventPage(
            events=items,
            next_cursor=items[-1].cursor if items else after,
            has_more=len(rows) > limit,
        )

    def nodes(
        self, now: datetime, site_id: str | None = None, subsystem: str | None = None
    ) -> list[Node]:
        where, params = [], []
        if site_id is not None:
            where.append("site_id=?")
            params.append(site_id)
        if subsystem is not None:
            where.append("subsystem=?")
            params.append(subsystem)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self.lock, self._storage_boundary(record=False):
            rows = self.db.execute(
                f"SELECT * FROM nodes{clause} ORDER BY site_id,subsystem,node_id", params
            ).fetchall()
        result = []
        for row in rows:
            event = Event.model_validate_json(row["body"])
            result.append(
                Node.from_status(
                    event,
                    occurred=row["occurred"],
                    received=row["received"],
                    now=now.timestamp(),
                    stale_after_s=self.settings.stale_after_s(event.subsystem),
                    future_tolerance_s=self.settings.future_tolerance_s,
                )
            )
        return result

    def health(self) -> dict:
        with self.lock:
            try:
                pages = self.db.execute("PRAGMA page_count").fetchone()[0]
                page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
                return {
                    "ready": self.last_error is None,
                    "last_error": self.last_error,
                    "event_count": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "event_bytes": metadata_value(self.db, "event_bytes"),
                    "max_event_bytes": self.settings.max_event_bytes,
                    "database_bytes": pages * page_size,
                    "max_database_bytes": self.settings.max_database_bytes,
                    "dedupe_entries": self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0],
                    "floor_cursor": metadata_value(self.db, "floor_cursor"),
                }
            except sqlite3.Error:
                return {"ready": False, "last_error": "database_unavailable"}
