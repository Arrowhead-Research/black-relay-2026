"""Bounded history: delete only an ingestion prefix so the floor is an exact loss boundary."""

import logging
import sqlite3
import threading
from typing import TYPE_CHECKING

from fusion_service.domain.errors import StorageUnavailable
from fusion_service.domain.time import Clock
from fusion_service.settings import Settings

if TYPE_CHECKING:
    from fusion_service.storage.store import Store

log = logging.getLogger(__name__)


def metadata_value(db: sqlite3.Connection, key: str) -> int:
    return db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]


def _delete_prefix(db: sqlite3.Connection, rows) -> None:
    if not rows:
        return
    end = rows[-1]["cursor"]
    db.execute("DELETE FROM events WHERE cursor<=?", (end,))
    db.execute("UPDATE metadata SET value=? WHERE key='floor_cursor'", (end,))
    db.execute(
        "UPDATE metadata SET value=value-? WHERE key='event_bytes'",
        (sum(row["size"] for row in rows),),
    )


def prune(db: sqlite3.Connection, settings: Settings, now: float, incoming: int = 0) -> None:
    """Expire old events, make room for `incoming` bytes, and expire dedupe entries.

    Runs inside the caller's transaction. Works in small batches so a long shutdown does not
    turn into one long maintenance pause.
    """
    for _ in range(10):
        rows = db.execute(
            "SELECT cursor,size,received FROM events ORDER BY cursor LIMIT 500",
        ).fetchall()
        if not rows:
            break
        expired = []
        for row in rows:
            if row["received"] >= now - settings.retention_s:
                break
            expired.append(row)
        excess = metadata_value(db, "event_bytes") + incoming - settings.max_event_bytes
        chosen, freed = [], 0
        for row in rows:
            if len(chosen) >= len(expired) and freed >= excess:
                break
            chosen.append(row)
            freed += row["size"]
        _delete_prefix(db, chosen)
        if len(chosen) < len(rows):
            break
    db.execute(
        "DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen WHERE expires<? LIMIT 5000)",
        (now,),
    )


class Maintenance:
    """Periodic retention on its own thread; start()/stop() mirror mqtt.Connection."""

    def __init__(self, store: "Store", clock: Clock, interval_s: float = 30):
        self.store, self.clock, self.interval_s = store, clock, interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="retention", daemon=True)

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            try:
                self.store.maintain(self.clock())
            except StorageUnavailable:
                log.warning("database maintenance unavailable")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join()
