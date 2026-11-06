"""SQLite: each node's newest heartbeat, and recent events.

Not the system of record. A node's local storage is (DESIGN.md 4.1). Node rows survive an API
restart, because "newest heartbeat" must, or an old replay after a restart would move a node
back to an old address.
"""

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    timestamp_us INTEGER NOT NULL,
    received_at REAL NOT NULL,
    node_address TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    durable INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    object_id TEXT,
    timestamp_us INTEGER NOT NULL,
    received_at REAL NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_class ON events (durable, seq);
CREATE INDEX IF NOT EXISTS events_by_time ON events (timestamp_us, seq);
"""

NEW = "new"
OLD = "old"
FULL = "full"


class Store:
    def __init__(self, path: Path, retain_updates: int, retain_durable: int, check_every: int):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._retain = {0: retain_updates, 1: retain_durable}
        self._check_every = check_every
        self._since_check = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_heartbeat(
        self,
        node_id: str,
        timestamp_us: int,
        received_at: float,
        address: str | None,
        message: str,
        max_nodes: int,
    ) -> str:
        """Keep this heartbeat if it is the node's newest. An equal or older one is ignored
        whole, and a newest one with no address leaves the known address alone."""
        with self._lock:
            known = self._db.execute(
                "SELECT timestamp_us FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if known is None:
                count = self._db.execute("SELECT count(*) FROM nodes").fetchone()[0]
                if count >= max_nodes:
                    return FULL
                self._db.execute(
                    "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                    (node_id, timestamp_us, received_at, address, message),
                )
                return NEW
            if timestamp_us <= known["timestamp_us"]:
                return OLD
            self._db.execute(
                "UPDATE nodes SET timestamp_us = ?, received_at = ?, message = ?, "
                "node_address = COALESCE(?, node_address) WHERE node_id = ?",
                (timestamp_us, received_at, message, address, node_id),
            )
            return NEW

    def nodes(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()

    def node(self, node_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()

    def node_addresses(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT node_id, node_address FROM nodes WHERE node_address IS NOT NULL"
            ).fetchall()
        return {row["node_id"]: row["node_address"] for row in rows}

    def add_event(
        self,
        event_id: str,
        kind: str,
        durable: bool,
        node_id: str,
        object_id: str | None,
        timestamp_us: int,
        received_at: float,
        message: str,
    ) -> bool:
        """False if this ``event_id`` is already held: delivery is at-least-once."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO events (event_id, kind, durable, node_id, object_id, "
                "timestamp_us, received_at, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    kind,
                    int(durable),
                    node_id,
                    object_id,
                    timestamp_us,
                    received_at,
                    message,
                ),
            )
            if cursor.rowcount == 0:
                return False
            self._since_check += 1
            if self._since_check >= self._check_every:
                self._since_check = 0
                self._prune()
            return True

    def _prune(self) -> None:
        for durable, keep in self._retain.items():
            self._db.execute(
                "DELETE FROM events WHERE durable = ? AND seq <= "
                "(SELECT seq FROM events WHERE durable = ? ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                (durable, durable, keep),
            )

    def events_after(self, seq: int, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
            ).fetchall()

    def events_from_time(self, timestamp_us: int, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM events WHERE timestamp_us >= ? ORDER BY seq LIMIT ?",
                (timestamp_us, limit),
            ).fetchall()

    def events_latest(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return rows[::-1]

    def latest_seq(self) -> int:
        with self._lock:
            return self._db.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]

    def track_of_event(self, node_id: str, event_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT object_id FROM events WHERE node_id = ? AND event_id = ?",
                (node_id, event_id),
            ).fetchone()
        return row["object_id"] if row is not None else None
