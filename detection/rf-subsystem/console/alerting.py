"""Marks and alerts for the WBT Console.

- **Marks** (``Marks``): a device is *friendly* (team phones, laptops,
  vehicles; dimmed on the map) or *priority* (a device of interest), and/or
  has a linger alarm (``linger_s``). Team-scope
  marks live in ``<data>/marks.json`` and follow the team to every site;
  site-scope marks live in the site's ``marks.json`` and are wiped with it. A
  site mark overrides a team mark. The nodes' own radios are always friendly.
- **Areas** (``Areas``): polygons on the site's floor plan (feet), each with a
  linger time.
- **Rules** (``AlertEngine``), evaluated on every poll:

  | rule | fires when | severity |
  |---|---|---|
  | ``priority_heard`` | a priority-marked device is heard (again after VISIT_GAP_S unheard) | high |
  | ``area_entry`` | a non-friendly device placed by 3+ nodes enters an alert area (once per visit) | medium (high if priority) |
  | ``linger`` | it stays in the area longer than the area's linger time; or a device with its own linger alarm (``linger_s`` on its mark) is heard that long; once per visit | medium (high if priority) |
  | ``node_offline`` | a node's capture stops (all its sources down, or its agent silent with no sources) | high |

  A visit ends after VISIT_GAP_S away (from the area, or unheard), so position
  jitter at an area's edge does not repeat alerts.

- **Alerts** (``AlertLog``) are kept per site in ``history/<date>-alerts.jsonl``
  (purged with the site's history), shown in the page, acknowledged by
  operators, and published as rf.alert.v0 (``publish.py``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

MARK_KINDS = ("friendly", "priority")
MARK_SCOPES = ("team", "site")
VISIT_GAP_S = 600.0        # a priority device unheard this long alerts again when it reappears
MAX_AREAS = 50
MAX_AREA_POINTS = 64
DEFAULT_LINGER_S = 300.0
AREA_MIN_NODES = 3         # two nodes fix a curve, not a point: no area alerts from them
RECENT_ALERTS = 300


def _read_json(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path | None, doc: Any, journal: dict[str, Any] | None = None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if journal is not None:
        with open(path.with_suffix(".journal.jsonl"), "a") as j:
            j.write(json.dumps({"t": time.time(), **journal}) + "\n")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)


class Marks:
    """Friendly/priority marks: team scope (all sites) and site scope."""

    def __init__(self, team_path: Path | None) -> None:
        self.team_path = team_path
        self.lock = threading.Lock()
        self.team: dict[str, dict[str, Any]] = _read_json(team_path, {})
        self.sites: dict[str, dict[str, dict[str, Any]]] = {}     # site key -> marks

    def _site(self, key: str, path: Path | None) -> dict[str, dict[str, Any]]:
        if key not in self.sites:
            self.sites[key] = _read_json(path, {})
        return self.sites[key]

    def merged(self, key: str, path: Path | None) -> dict[str, dict[str, Any]]:
        with self.lock:
            out = {d: {**m, "scope": "team"} for d, m in self.team.items()}
            out.update({d: {**m, "scope": "site"} for d, m in self._site(key, path).items()})
            return out

    def set(self, key: str, path: Path | None, body: dict[str, Any], user: str | None) -> dict[str, dict[str, Any]]:
        device = str(body.get("device", "")).lower().strip()
        if not device or len(device) > 64:
            raise ValueError("device is required")
        scope = body.get("scope", "site")
        if scope not in MARK_SCOPES:
            raise ValueError(f"scope must be one of {MARK_SCOPES}")
        with self.lock:
            store, spath = (self.team, self.team_path) if scope == "team" else (self._site(key, path), path)
            before = store.get(device)
            if body.get("remove"):
                store.pop(device, None)
            else:
                entry = dict(store.get(device) or {})
                if "kind" in body:          # friendly | priority | null (clear the mark, keep a linger alarm)
                    if body["kind"] is not None and body["kind"] not in MARK_KINDS:
                        raise ValueError(f"kind must be one of {MARK_KINDS} or null")
                    entry["kind"] = body["kind"]
                if "linger_s" in body:      # seconds, or null for no linger alarm
                    v = body["linger_s"]
                    if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool) and 60 <= v <= 86400):
                        raise ValueError("linger_s must be 60-86400 seconds or null")
                    entry["linger_s"] = None if v is None else float(v)
                if "label" in body:
                    entry["label"] = str(body.get("label") or "").strip()[:60] or None
                if not entry.get("kind") and not entry.get("linger_s"):
                    store.pop(device, None)
                else:
                    store[device] = {"kind": entry.get("kind"), "linger_s": entry.get("linger_s"), "label": entry.get("label"),
                                     "t": round(time.time(), 1), "user": user}
            _write_json(spath, store, {"op": "mark", "device": device, "before": before, "after": store.get(device), "user": user})
        return self.merged(key, path)


def point_in_polygon(x: float, y: float, pts: list[list[float]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class Areas:
    """Alert areas of each site (polygons in feet)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.docs: dict[str, list[dict[str, Any]]] = {}

    def get(self, key: str, path: Path | None) -> list[dict[str, Any]]:
        with self.lock:
            if key not in self.docs:
                self.docs[key] = _read_json(path, [])
            return self.docs[key]

    def add(self, key: str, path: Path | None, body: dict[str, Any], user: str | None) -> list[dict[str, Any]]:
        name = str(body.get("name", "")).strip()[:40]
        pts = body.get("points")
        if not name:
            raise ValueError("an area needs a name")
        if not (isinstance(pts, list) and 3 <= len(pts) <= MAX_AREA_POINTS and all(
                isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) and abs(v) < 10000 for v in p)
                for p in pts)):
            raise ValueError(f"an area needs 3-{MAX_AREA_POINTS} points [x, y] in feet")
        linger = body.get("linger_s", DEFAULT_LINGER_S)
        if not (isinstance(linger, (int, float)) and 30 <= linger <= 86400):
            raise ValueError("linger_s must be 30-86400")
        areas = self.get(key, path)
        with self.lock:
            if len(areas) >= MAX_AREAS:
                raise ValueError(f"at most {MAX_AREAS} areas")
            area = {"id": uuid.uuid4().hex[:10], "name": name, "points": [[round(float(x), 2), round(float(y), 2)] for x, y in pts],
                    "linger_s": float(linger)}
            areas.append(area)
            _write_json(path, areas, {"op": "add", "area": area, "user": user})
            return list(areas)

    def delete(self, key: str, path: Path | None, ids: list[str], user: str | None) -> list[dict[str, Any]]:
        areas = self.get(key, path)
        with self.lock:
            gone = [a for a in areas if a["id"] in set(ids)]
            areas[:] = [a for a in areas if a["id"] not in set(ids)]
            _write_json(path, areas, {"op": "delete", "areas": gone, "user": user})
            return list(areas)


class AlertEngine:
    """Priority device appears; area entry and linger; a device's own linger alarm; node offline."""

    def __init__(self) -> None:
        self.site: str | None = None
        self.heard: dict[str, float] = {}      # priority device -> last heard
        self.visits: dict[str, list[float]] = {}   # device with a linger alarm -> [visit start, last heard, alerted]
        self.area_visits: dict[tuple[str, str], list[float]] = {}   # (device, area) -> [since, last, lingered]
        self.nodes: dict[str, str] = {}        # node -> last state

    def evaluate(self, site: str, now: float, devices: dict[str, dict[str, Any]], positions: dict[str, dict[str, Any]],
                 marks: dict[str, dict[str, Any]], areas: list[dict[str, Any]], node_states: dict[str, dict[str, Any]],
                 friendly_ids: set[str]) -> list[dict[str, Any]]:
        if site != self.site:              # another site: nothing carries over
            self.site, self.heard, self.visits, self.area_visits, self.nodes = site, {}, {}, {}, {}
        out: list[dict[str, Any]] = []

        def alert(rule: str, dev_id: str | None, context: dict[str, Any], severity: str = "high",
                  area: dict[str, Any] | None = None) -> None:
            out.append({"rule_id": rule, "rule_type": rule, "severity": severity, "device": dev_id,
                        "area": area and {"id": area["id"], "name": area["name"]}, "context": context, "t": now})

        for dev_id, dev in devices.items():
            mark = marks.get(dev_id) or {}
            if dev_id in friendly_ids:         # the nodes' own radios
                continue
            friendly = mark.get("kind") == "friendly"     # silences area alerts; an explicit linger alarm still applies
            sev = "high" if mark.get("kind") == "priority" else "medium"
            p = positions.get(dev_id)
            if p and p["nodes"] >= AREA_MIN_NODES and not friendly:
                for area in areas:
                    if not point_in_polygon(p["x"], p["y"], area["points"]):
                        continue
                    key = (dev_id, area["id"])
                    v = self.area_visits.get(key)
                    if v is None or now - v[1] > VISIT_GAP_S:
                        v = self.area_visits[key] = [now, now, 0.0]
                        alert("area_entry", dev_id, {}, sev, area)
                    v[1] = now
                    if not v[2] and now - v[0] >= area["linger_s"]:
                        v[2] = 1.0
                        alert("linger", dev_id, {"dwell_s": round(now - v[0])}, sev, area)
            if mark.get("kind") == "priority":
                last = self.heard.get(dev_id)
                if last is None or now - last > VISIT_GAP_S:
                    alert("priority_heard", dev_id, {"nodes": sorted(dev["nodes"])})
                self.heard[dev_id] = now
            if mark.get("linger_s"):
                v = self.visits.get(dev_id)
                if v is None or now - v[1] > VISIT_GAP_S:
                    v = self.visits[dev_id] = [now, now, 0.0]
                v[1] = now
                if not v[2] and now - v[0] >= mark["linger_s"]:
                    v[2] = 1.0
                    alert("linger", dev_id, {"dwell_s": round(now - v[0]), "linger_s": mark["linger_s"]}, sev)
        for dev_id in [k for k, v in self.visits.items() if now - v[1] > VISIT_GAP_S]:
            del self.visits[dev_id]
        for key in [k for k, v in self.area_visits.items() if now - v[1] > VISIT_GAP_S]:
            del self.area_visits[key]

        for node, st in node_states.items():
            prev = self.nodes.get(node)
            if st["state"] == "offline" and prev not in (None, "offline"):
                alert("node_offline", None, {"node_id": node, "sources": st["sources"]})
            self.nodes[node] = st["state"]
        return out


class AlertLog:
    """Recent alerts of each site (memory + history/<date>-alerts.jsonl), with acknowledgements."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.recent: dict[str, deque] = {}
        self.acked: dict[str, set[str]] = {}

    def _load(self, key: str, hdir: Path | None) -> None:
        if key in self.recent:
            return
        self.recent[key], self.acked[key] = deque(maxlen=RECENT_ALERTS), set()
        if hdir is None or not hdir.exists():
            return
        for f in sorted(hdir.glob("*-alerts.jsonl"))[-2:]:
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if "ack" in rec:
                    self.acked[key].update(rec["ack"])
                else:
                    self.recent[key].append(rec)

    def _append(self, hdir: Path | None, rec: dict[str, Any], now: float) -> None:
        if hdir is None:
            return
        hdir.mkdir(parents=True, exist_ok=True)
        with open(hdir / f"{time.strftime('%Y-%m-%d', time.gmtime(now))}-alerts.jsonl", "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def add(self, key: str, hdir: Path | None, alerts: list[dict[str, Any]]) -> None:
        with self.lock:
            self._load(key, hdir)
            for a in alerts:
                self.recent[key].append(a)
                self._append(hdir, a, a["t"])

    def ack(self, key: str, hdir: Path | None, ids: list[str] | None, user: str | None) -> int:
        with self.lock:
            self._load(key, hdir)
            ids = [a["id"] for a in self.recent[key] if a["id"] not in self.acked[key]] if ids is None else \
                  [i for i in ids if i not in self.acked[key]]
            if ids:
                self.acked[key].update(ids)
                self._append(hdir, {"ack": ids, "user": user, "t": time.time()}, time.time())
            return len(ids)

    def view(self, key: str, hdir: Path | None, limit: int = 100) -> dict[str, Any]:
        with self.lock:
            self._load(key, hdir)
            recent = list(self.recent[key])[-limit:][::-1]
            acked = self.acked[key]
            return {"recent": [{**a, "acked": a["id"] in acked} for a in recent],
                    "unacked": sum(1 for a in self.recent[key] if a["id"] not in acked)}
