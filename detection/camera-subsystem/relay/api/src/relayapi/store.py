"""SQLite: each node's newest heartbeat, and recent events.

Not the system of record. A node's local storage is (DESIGN.md 4.1). Node rows survive an API
restart, because "newest heartbeat" must, or an old replay after a restart would move a node
back to an old address.

``synchronous=FULL``: a commit is on disk when it returns, because a message is acknowledged to
the broker only after that, and the node then deletes it from its outbox.
"""

import fcntl
import os
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
CREATE INDEX IF NOT EXISTS nodes_by_receipt ON nodes (received_at);
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
NEW_EVICTED = "new_evicted"
OLD = "old"


class StoreLocked(Exception):
    """Another API already holds this database. Two would fight over one broker session."""


class Store:
    def __init__(
        self,
        path: Path,
        retain_updates: int,
        retain_durable: int,
        check_every: int,
        retain_bytes: int,
    ):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise StoreLocked(f"another API is using {path}") from None
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._retain = {0: retain_updates, 1: retain_durable}
        self._retain_bytes = retain_bytes
        self._check_every = check_every
        self._since_check = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            self._bytes = self._total_bytes()

    def close(self) -> None:
        with self._lock:
            self._db.close()
        os.close(self._lock_fd)

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
        whole, and a newest one with no address leaves the known address alone.

        A stored stamp counts for no more than when it really arrived, so a stamp ahead of the
        clock cannot make the truth from the same node look old. When the table is full, the
        node silent longest makes room, as a real node must always get in.
        """
        with self._lock:
            known = self._db.execute(
                "SELECT min(timestamp_us, CAST(received_at * 1000000 AS INTEGER)) AS newest "
                "FROM nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if known is None:
                count = self._db.execute("SELECT count(*) FROM nodes").fetchone()[0]
                evicted = count >= max_nodes
                if evicted:
                    self._db.execute(
                        "DELETE FROM nodes WHERE node_id IN "
                        "(SELECT node_id FROM nodes ORDER BY received_at, node_id LIMIT ?)",
                        (count - max_nodes + 1,),
                    )
                self._db.execute(
                    "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                    (node_id, timestamp_us, received_at, address, message),
                )
                return NEW_EVICTED if evicted else NEW
            if timestamp_us <= known["newest"]:
                return OLD
            self._db.execute(
                "UPDATE nodes SET timestamp_us = ?, received_at = ?, message = ?, "
                "node_address = COALESCE(?, node_address) WHERE node_id = ?",
                (timestamp_us, received_at, message, address, node_id),
            )
            return NEW

    def prune_nodes(self, unseen_since: float) -> int:
        with self._lock:
            return self._db.execute(
                "DELETE FROM nodes WHERE received_at < ?", (unseen_since,)
            ).rowcount

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
            self._bytes += len(message)
            self._since_check += 1
            if self._since_check >= self._check_every:
                self._since_check = 0
                self._prune_by_count()
            if self._bytes > self._retain_bytes:
                self._prune_by_bytes()
            return True

    def stored_bytes(self) -> int:
        with self._lock:
            return self._total_bytes()

    def _total_bytes(self) -> int:
        return self._db.execute("SELECT coalesce(sum(length(message)), 0) FROM events").fetchone()[
            0
        ]

    def _prune_by_count(self) -> None:
        for durable, keep in self._retain.items():
            cut = self._db.execute(
                "SELECT seq FROM events WHERE durable = ? ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (durable, keep),
            ).fetchone()
            if cut is None:
                continue
            # Only the rows going are read. Summing the whole table here would read every page of
            # it on each check, with the lock held.
            freed = self._db.execute(
                "SELECT sum(length(message)) FROM events WHERE durable = ? AND seq <= ?",
                (durable, cut[0]),
            ).fetchone()[0]
            self._db.execute(
                "DELETE FROM events WHERE durable = ? AND seq <= ?", (durable, cut[0])
            )
            self._bytes -= freed

    def _prune_by_bytes(self) -> None:
        # Updates go first, then the oldest of the rest.
        for durable in (0, 1):
            excess = self._bytes - self._retain_bytes
            if excess <= 0:
                return
            last = None
            freed = 0
            for row in self._db.execute(
                "SELECT seq, length(message) AS size FROM events WHERE durable = ? ORDER BY seq",
                (durable,),
            ):
                last = row["seq"]
                freed += row["size"]
                if freed >= excess:
                    break
            if last is not None:
                self._db.execute(
                    "DELETE FROM events WHERE durable = ? AND seq <= ?", (durable, last)
                )
                self._bytes -= freed

    def events_after(self, seq: int, limit: int, max_bytes: int | None = None) -> list[sqlite3.Row]:
        """Rows after ``seq``. With ``max_bytes``, stops once they reach that size, but always
        returns at least one row if there is one, or a large row could never be sent."""
        rows = []
        size = 0
        with self._lock:
            for row in self._db.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
            ):
                rows.append(row)
                size += len(row["message"])
                if max_bytes is not None and size >= max_bytes:
                    break
        return rows

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
