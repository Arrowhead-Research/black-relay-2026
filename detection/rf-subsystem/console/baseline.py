"""Per-site baseline (pattern of life): which devices are normally here, and when.

``history/baseline.sqlite`` in the site's directory (cleared with the site's
history, wiped with the site):

    devices(id, type, band, name, manufacturer, randomized, first_seen,
            last_seen, visits, heard_s)
    presence(id, how, n)    how = UTC hour of the week (0 = Monday 00:00);
                            n = visits that included that hour
    visits(id, start, end)  every finished visit (co-travelers: cotravel.py)
    meta(key, value)        learning_until: devices first seen before this
                            are the site's background

A visit ends after VISIT_GAP_S unheard. ``observe`` returns events for the
alert rules: ``new`` (first time at this site, after the learning period)
and ``visit`` (a device's next visit, with its count).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

VISIT_GAP_S = 600.0
LEARN_S = 2 * 3600.0       # devices first seen in the site's first 2 hours are its background
FLUSH_S = 30.0
MAX_STEP_S = 10.0          # heard time added per poll at most (a gap means it was not heard)


def hour_of_week(t: float) -> int:
    g = time.gmtime(t)
    return g.tm_wday * 24 + g.tm_hour


class Baseline:
    def __init__(self, path: Path | None, learn_s: float = LEARN_S) -> None:
        self.path, self.learn_s = path, learn_s
        self.lock = threading.Lock()
        self.cache: dict[str, dict[str, Any]] = {}
        self.dirty: set[str] = set()
        self.presence: dict[tuple[str, int], int] = {}
        self.dirty_presence: set[tuple[str, int]] = set()
        self.last_hour: dict[str, int] = {}
        self.open: dict[str, list[float]] = {}      # device -> [visit start, last heard]
        self.closed: list[tuple[str, float, float]] = []
        self.last_flush = 0.0
        self.db: sqlite3.Connection | None = None
        self.learning_until: float | None = None
        self._open()

    def _open(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=False)
        self.db.executescript("""
            create table if not exists devices (id text primary key, type text, band text, name text, manufacturer text,
                randomized integer, first_seen real, last_seen real, visits integer, heard_s real);
            create table if not exists presence (id text, how integer, n integer, primary key (id, how));
            create table if not exists meta (key text primary key, value text);
            create table if not exists visits (id text, start real, end real);
            create index if not exists visits_start on visits (start);
        """)
        row = self.db.execute("select value from meta where key='learning_until'").fetchone()
        self.learning_until = float(row[0]) if row else None
        self.cache, self.presence = {}, {}

    def _ensure(self) -> None:
        """The site's history was cleared under us: start a fresh database."""
        if self.path is not None and not self.path.exists():
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                pass
            self.dirty, self.dirty_presence, self.last_hour, self.open, self.closed = set(), set(), {}, {}, []
            self._open()

    def _load(self, dev_id: str) -> dict[str, Any] | None:
        if dev_id in self.cache:
            return self.cache[dev_id]
        row = self.db.execute("select id, type, band, name, manufacturer, randomized, first_seen, last_seen, visits, heard_s "
                              "from devices where id=?", (dev_id,)).fetchone()
        if row is None:
            return None
        keys = ("id", "type", "band", "name", "manufacturer", "randomized", "first_seen", "last_seen", "visits", "heard_s")
        rec = dict(zip(keys, row))
        rec["randomized"] = bool(rec["randomized"])
        self.cache[dev_id] = rec
        return rec

    def observe(self, devices: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        events = []
        with self.lock:
            self._ensure()
            if self.learning_until is None:
                self.learning_until = now + self.learn_s
                self.db.execute("insert or replace into meta values ('learning_until', ?)", (str(self.learning_until),))
                self.db.commit()
            for d in devices:
                dev_id = d["id"]
                rec = self._load(dev_id)
                if rec is None:
                    rec = self.cache[dev_id] = {"id": dev_id, "type": d["type"], "band": d["band"], "name": d.get("name"),
                                                "manufacturer": d.get("manufacturer"), "randomized": bool(d.get("randomized")),
                                                "first_seen": now, "last_seen": now, "visits": 1, "heard_s": 0.0}
                    if now >= self.learning_until:
                        events.append({"kind": "new", "device": dev_id, "visits": 1})
                    self._mark_hour(dev_id, now, new_visit=True)
                    self.open[dev_id] = [now, now]
                else:
                    gap = now - rec["last_seen"]
                    if gap > VISIT_GAP_S:
                        rec["visits"] += 1
                        events.append({"kind": "visit", "device": dev_id, "visits": rec["visits"], "away_s": round(gap)})
                        self._mark_hour(dev_id, now, new_visit=True)
                        if dev_id in self.open:
                            self.closed.append((dev_id, *self.open[dev_id]))
                        self.open[dev_id] = [now, now]
                    else:
                        rec["heard_s"] += min(max(gap, 0.0), MAX_STEP_S)
                        self._mark_hour(dev_id, now)
                    rec["last_seen"] = now
                    rec["name"] = d.get("name") or rec["name"]
                    self.open.setdefault(dev_id, [now, now])[1] = now
                self.dirty.add(dev_id)
            if now - self.last_flush >= FLUSH_S:
                self._flush(now)
        return events

    def _mark_hour(self, dev_id: str, now: float, new_visit: bool = False) -> None:
        how = hour_of_week(now)
        if not new_visit and self.last_hour.get(dev_id) == how:
            return
        self.last_hour[dev_id] = how
        key = (dev_id, how)
        if key not in self.presence:
            row = self.db.execute("select n from presence where id=? and how=?", key).fetchone()
            self.presence[key] = row[0] if row else 0
        self.presence[key] += 1
        self.dirty_presence.add(key)

    def _flush(self, now: float) -> None:
        for dev_id in [k for k, v in self.open.items() if now - v[1] > VISIT_GAP_S]:
            self.closed.append((dev_id, *self.open.pop(dev_id)))
        if self.closed:
            self.db.executemany("insert into visits values (?,?,?)", self.closed)
            self.closed = []
        recs = [self.cache[i] for i in self.dirty]
        self.db.executemany(
            "insert or replace into devices values (?,?,?,?,?,?,?,?,?,?)",
            [(r["id"], r["type"], r["band"], r["name"], r["manufacturer"], int(r["randomized"]), r["first_seen"],
              r["last_seen"], r["visits"], round(r["heard_s"], 1)) for r in recs])
        self.db.executemany("insert or replace into presence values (?,?,?)",
                            [(k[0], k[1], self.presence[k]) for k in self.dirty_presence])
        self.db.commit()
        self.dirty, self.dirty_presence = set(), set()
        self.last_flush = now
        if len(self.cache) > 20000:      # keep memory bounded on busy sites
            self.cache = {}
            self.presence = {}

    def flush(self) -> None:
        with self.lock:
            self._flush(time.time())

    def get(self, dev_id: str) -> dict[str, Any] | None:
        with self.lock:
            self._ensure()
            rec = self._load(dev_id)
            if rec is None:
                return None
            pres = {how: n for (i, how), n in self.presence.items() if i == dev_id}
            for how, n in self.db.execute("select how, n from presence where id=?", (dev_id,)):
                pres.setdefault(how, n)
            return {**rec, "background": rec["first_seen"] < (self.learning_until or 0), "presence": dict(sorted(pres.items()))}

    def visits(self, since: float = 0.0) -> tuple[list[tuple[str, float, float]], dict[str, dict[str, Any]]]:
        """Finished and open visits since ``since``, and each device's type, band, name and randomized flag."""
        with self.lock:
            self._ensure()
            self._flush(time.time())
            rows = [tuple(r) for r in self.db.execute("select id, start, end from visits where end >= ? order by start", (since,))]
            rows += [(k, v[0], v[1]) for k, v in self.open.items() if v[1] >= since]
            ids = {r[0] for r in rows}
            meta = {}
            for i in ids:
                rec = self._load(i)
                if rec:
                    meta[i] = {"type": rec["type"], "band": rec["band"], "name": rec["name"], "randomized": rec["randomized"]}
            return rows, meta

    def summary(self) -> dict[str, Any]:
        with self.lock:
            self._ensure()
            self._flush(time.time())
            n, rnd = self.db.execute("select count(*), sum(randomized) from devices").fetchone()
            return {"devices": n or 0, "randomized": rnd or 0, "learning_until": self.learning_until}
