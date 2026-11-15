"""SQLite event log + persisted cache. Single writer (normalizer); readers open mode=ro."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .. import model as M

SCHEMA = Path(__file__).resolve().parents[3] / "schema.sql"
_FALLBACK_SCHEMA = None


def _schema_text() -> str:
    p = os.environ.get("BR2_SCHEMA") or str(SCHEMA)
    if os.path.exists(p):
        return open(p).read()
    from importlib import resources
    return resources.files("br2").joinpath("schema.sql").read_text()


def row_to_event(r: sqlite3.Row | tuple, with_zone: bool = False) -> dict:
    seq, id_, ts, typ, subj, fj, tj, src, syn = r[:9]
    ev = _row_event(seq, id_, ts, typ, subj, fj, tj, src, syn)
    if with_zone:                       # team output only: the REST/SSE/MQTT event shape is unchanged
        try:
            z = json.loads(r[9]) if len(r) > 9 and r[9] else None
        except ValueError:
            z = None
        ev["zone"] = z if isinstance(z, dict) else None
    return ev


def _row_event(seq, id_, ts, typ, subj, fj, tj, src, syn) -> dict:
    return {"seq": seq, "id": id_, "ts": ts, "type": typ, "subject": subj,
            "from": json.loads(fj) if fj is not None else None,
            "to": json.loads(tj) if tj is not None else None, "source": src, "synthetic": bool(syn)}


COLS = "seq,id,ts,type,subject,from_json,to_json,source,synthetic"


SANE_MIN_ISO = "2024-01-01T00:00:00.000Z"     # anything earlier is a not-yet-set (RTC-less boot) clock
SANE_MIN_EPOCH = 1704067200.0
MAX_ROWS = 5_000_000                          # hard safety valve for a clock stuck far in the future


class Store:
    """Writer side."""

    def __init__(self, path: str, retention_days: float = 30, wall=time.time):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.executescript(_schema_text())
        if "zone_json" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN zone_json TEXT")     # database from before zone-at-event-time
        self.db.execute("PRAGMA synchronous=FULL")          # per-connection setting: assert it explicitly
        self.db.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('db_id', ?)", (uuid.uuid4().hex,))
        for suf in ("", "-wal", "-shm"):                      # outbound (shared group) opens the WAL db read-only
            try:
                os.chmod(path + suf, 0o660)
            except OSError:
                pass
        self.retention_days = retention_days
        self.wall = wall

    # ---- meta
    def meta(self) -> dict[str, str]:
        return {k: v for k, v in self.db.execute("SELECT k,v FROM meta")}

    def load_engine_meta(self) -> dict:
        try:
            return json.loads(self.meta().get("engine", "{}"))
        except ValueError:
            return {}

    def last_seq(self) -> int:
        r = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(r[0]) if r else 0

    def load_cache(self) -> tuple[list[dict], list[dict]]:
        z = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM zone_state")]
        p = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM partition_state")]
        return z, p

    def commit(self, events: list[dict], zones: dict, removed_zones: list[str],
               parts: dict, removed_parts: list[str], meta: dict | None = None) -> list[dict]:
        """Atomically append events (assigning seq), persist cache + engine meta. Returns events with seq/id.
        Either everything is durable (synchronous=FULL) or nothing is: callers keep their in-memory state in step."""
        out: list[dict] = []
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            nxt = self.last_seq()
            for e in events:
                nxt += 1
                # seq is assigned explicitly (== AUTOINCREMENT's next value) so that the id column is known
                zj = json.dumps(e["zone"], separators=(",", ":")) if e.get("zone") else None
                db.execute("INSERT INTO events(seq,id,ts,type,subject,from_json,to_json,source,synthetic,zone_json)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (nxt, f"evt-{nxt}", e["ts"], e["type"], e["subject"],
                            json.dumps(e["from"]), json.dumps(e["to"]), e["source"], 1 if e["synthetic"] else 0, zj))
                out.append({"seq": nxt, "id": f"evt-{nxt}", "ts": e["ts"], "type": e["type"],
                            "subject": e["subject"], "from": e["from"], "to": e["to"],
                            "source": e["source"], "synthetic": bool(e["synthetic"])})
            for zid, rec in zones.items():
                db.execute("INSERT OR REPLACE INTO zone_state VALUES(?,?)", (zid, json.dumps(rec)))
            for zid in removed_zones:
                db.execute("DELETE FROM zone_state WHERE id=?", (zid,))
            for pid, rec in parts.items():
                db.execute("INSERT OR REPLACE INTO partition_state VALUES(?,?)", (pid, json.dumps(rec)))
            for pid in removed_parts:
                db.execute("DELETE FROM partition_state WHERE id=?", (pid,))
            if meta:
                cur = self.load_engine_meta()
                cur.update(meta)
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('engine', ?)", (json.dumps(cur),))
            if events:
                self._note_clock(nxt, [e["ts"] for e in events])
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return out

    def _note_clock(self, last_seq: int, stamps: list[str]) -> None:
        """Bookkeeping for retention under a bad clock. Rows written while the clock was not set (RTC-less boot:
        1970) carry meaningless timestamps; we remember up to which seq, and from which (sane) moment on they count
        as having been written. Runs inside the commit transaction."""
        wall = self.wall()
        wall_ok = wall >= SANE_MIN_EPOCH
        insane = (not wall_ok) or any(t < SANE_MIN_ISO for t in stamps)
        m = self.meta()
        if insane:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('untrusted_upto', ?)", (str(last_seq),))
            if wall_ok:
                self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('untrusted_sane_iso', ?)", (M.iso(wall),))
            else:
                self.db.execute("DELETE FROM meta WHERE k='untrusted_sane_iso'")
        elif int(m.get("untrusted_upto", 0)) > 0 and "untrusted_sane_iso" not in m:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('untrusted_sane_iso', ?)", (M.iso(wall),))

    def prune(self, now: float | None = None) -> int:
        """Retention. Deletes ONLY a seq prefix: every row below the first row that is still inside the window.
        (A per-row `ts < cutoff` delete would leave holes when the clock stepped, and would destroy rows stamped
        before an RTC-less Pi got its time.) The effective age of an untrusted row is measured from the moment the
        clock first became sane. Returns rows deleted. Never moves the seq counter (AUTOINCREMENT)."""
        now = self.wall() if now is None else now
        if now < SANE_MIN_EPOCH:
            return 0                                          # our own clock is not set: do not prune on it
        cutoff = M.iso(now - self.retention_days * 86400)
        m = self.meta()
        u = int(m.get("untrusted_upto", 0))
        sane = m.get("untrusted_sane_iso")
        if u > 0 and not sane:
            sane = M.iso(now)
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('untrusted_sane_iso', ?)", (sane,))
        head = self.last_seq()
        r = self.db.execute("SELECT MIN(seq) FROM events WHERE (CASE WHEN seq<=? THEN ? ELSE ts END) >= ?",
                            (u, sane or "", cutoff)).fetchone()
        boundary = r[0] if r and r[0] is not None else head + 1
        n = self.db.execute("DELETE FROM events WHERE seq < ?", (boundary,)).rowcount
        cnt = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if cnt > MAX_ROWS:
            n += self.db.execute("DELETE FROM events WHERE seq <= ?", (head - MAX_ROWS,)).rowcount
        return n

    def close(self) -> None:
        self.db.close()


class Reader:
    """Read-only side (outbound). Opens the same WAL database with mode=ro + query_only."""

    def __init__(self, path: str):
        self.path = path
        self.db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection:
        if self.db is None:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False,
                                      timeout=5)
            self.db.execute("PRAGMA query_only=1")
        return self.db

    def last_seq(self) -> int:
        try:
            r = self._conn().execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        except sqlite3.Error:
            self.db = None
            return 0
        return int(r[0]) if r else 0

    def events_after(self, after: int, limit: int = 100, with_zone: bool = False) -> list[dict]:
        """`with_zone` adds each row's zone name/kind/partition/stale as of the event ("zone": dict | None)."""
        try:
            try:
                rows = self._conn().execute(f"SELECT {COLS},zone_json FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                            (after, limit)).fetchall()
            except sqlite3.OperationalError as e:
                if "zone_json" not in str(e):
                    raise
                rows = self._conn().execute(f"SELECT {COLS} FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                                            (after, limit)).fetchall()      # normalizer has not migrated the db yet
        except sqlite3.Error:
            self.db = None
            return []
        return [row_to_event(r, with_zone) for r in rows]

    def oldest_seq(self) -> int | None:
        try:
            r = self._conn().execute("SELECT MIN(seq) FROM events").fetchone()
        except sqlite3.Error:
            self.db = None
            return None
        return r[0] if r else None

    def floor(self) -> int:
        """Lowest seq still stored (head+1 when the log is empty). Pruning only ever advances it."""
        o = self.oldest_seq()
        return o if o is not None else self.last_seq() + 1

    def db_id(self) -> str | None:
        try:
            r = self._conn().execute("SELECT v FROM meta WHERE k='db_id'").fetchone()
        except sqlite3.Error:
            self.db = None
            return None
        return r[0] if r else None
