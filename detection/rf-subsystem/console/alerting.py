"""Marks, alert areas and alerts for the WBT Console.

- **Marks** (``Marks``): a device is *friendly* (team phones, laptops,
  vehicles; dimmed on the map, never alerted on) or *priority* (a device of
  interest; alerted wherever it is heard). Team-scope marks live in
  ``<data>/marks.json`` and follow the team to every site; site-scope marks
  live in the site's ``marks.json`` and are wiped with it. A site mark
  overrides a team mark. The nodes' own radios are always friendly.
- **Areas** (``Areas``): polygons on the site's floor plan (feet), e.g. an
  inner perimeter or the parking area, each with a linger time.
- **Rules** (``AlertEngine``), evaluated on every poll from server positions:

  | rule | fires when | severity |
  |---|---|---|
  | ``priority_heard`` | a priority device is heard (again after VISIT_GAP_S) | high |
  | ``area_entry`` | a non-friendly device placed by 3+ nodes enters an area (once per visit) | medium (high if priority) |
  | ``linger`` | it stays in the area longer than the area's linger time | medium (high if priority) |
  | ``node_offline`` / ``node_online`` | a node's capture stops / comes back | high / info |
  | ``node_health`` | the node agent reports a new problem (hot, undervoltage, unit down, adapter missing...) | medium |
  | ``new_device`` | a stable address (not randomized) first seen at this site after its learning period (baseline.py), heard by 2+ nodes | low |
  | ``repeat_visitor`` | a stable address starts its 3rd (or later) visit to this site | low |
  | ``arrived_with`` | a device (or vehicle) arrives with its known co-travelers (cotravel.py) | low (high if priority) |

  A visit ends after VISIT_GAP_S without the device in the area, so position
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
MAX_AREAS = 50
MAX_AREA_POINTS = 64
DEFAULT_LINGER_S = 300.0
VISIT_GAP_S = 600.0        # a device away (from the area, or unheard) this long starts a new visit
AREA_MIN_NODES = 3         # two nodes fix a curve, not a point: no area alerts from them
REPEAT_VISITS = 3          # repeat_visitor from this visit on
TPMS_GROUP_S = 30.0        # new TPMS sensors of one model within this time are one car
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
                kind = body.get("kind")
                if kind not in MARK_KINDS:
                    raise ValueError(f"kind must be one of {MARK_KINDS}")
                label = str(body.get("label") or "").strip()[:60] or None
                store[device] = {"kind": kind, "label": label, "t": round(time.time(), 1), "user": user}
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
    """Rules over server positions; keeps visit state per site."""

    def __init__(self) -> None:
        self.site: str | None = None
        self.visits: dict[tuple[str, str], dict[str, float]] = {}   # (device, area id) -> since, last, flags
        self.heard: dict[str, float] = {}                            # priority device -> last heard
        self.nodes: dict[str, str] = {}
        self.new_tpms: dict[str, float] = {}                         # TPMS model -> last new_device alert
        self.node_problems: dict[str, set[str]] = {}
    def evaluate(self, site: str, now: float, devices: dict[str, dict[str, Any]], positions: dict[str, dict[str, Any]],
                 marks: dict[str, dict[str, Any]], areas: list[dict[str, Any]], node_states: dict[str, dict[str, Any]],
                 friendly_ids: set[str], events: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        if site != self.site:              # another site: nothing carries over
            self.site, self.visits, self.heard, self.nodes = site, {}, {}, {}
        out: list[dict[str, Any]] = []

        def alert(rule: str, severity: str, dev_id: str | None, context: dict[str, Any], area: dict[str, Any] | None = None):
            out.append({"rule_id": rule, "rule_type": rule, "severity": severity, "device": dev_id,
                        "area": area and {"id": area["id"], "name": area["name"]}, "context": context, "t": now})

        for dev_id, dev in devices.items():
            mark = (marks.get(dev_id) or {}).get("kind")
            if dev_id in friendly_ids or mark == "friendly":
                continue
            if mark == "priority":
                last = self.heard.get(dev_id)
                if last is None or now - last > VISIT_GAP_S:
                    alert("priority_heard", "high", dev_id, {"nodes": sorted(dev["nodes"])})
                self.heard[dev_id] = now
            p = positions.get(dev_id)
            if not p or p["nodes"] < AREA_MIN_NODES:
                continue
            for area in areas:
                if not point_in_polygon(p["x"], p["y"], area["points"]):
                    continue
                key = (dev_id, area["id"])
                v = self.visits.get(key)
                sev = "high" if mark == "priority" else "medium"
                if v is None or now - v["last"] > VISIT_GAP_S:
                    v = self.visits[key] = {"since": now, "last": now, "lingered": 0.0}
                    alert("area_entry", sev, dev_id, {}, area)
                v["last"] = now
                if not v["lingered"] and now - v["since"] >= area["linger_s"]:
                    v["lingered"] = 1.0
                    alert("linger", sev, dev_id, {"dwell_s": round(now - v["since"])}, area)
        for key in [k for k, v in self.visits.items() if now - v["last"] > VISIT_GAP_S]:
            del self.visits[key]

        for ev in events:                  # baseline: new at this site, or back again
            dev = devices.get(ev["device"])
            if not dev or dev.get("randomized") or ev["device"] in friendly_ids \
                    or (marks.get(ev["device"]) or {}).get("kind") == "friendly":
                continue
            if ev["kind"] == "new" and dev.get("type") == "tpms":
                # a new car's 4 sensors arrive together: one alert per car (same model within TPMS_GROUP_S)
                model = dev.get("name") or "tpms"
                prev = next((x for x in out if x["rule_id"] == "new_device" and x["context"].get("model") == model), None)
                if prev is not None:
                    prev["context"]["sensors"].append(ev["device"])
                elif now - self.new_tpms.get(model, -1e18) > TPMS_GROUP_S:
                    alert("new_device", "low", ev["device"], {"model": model, "sensors": [ev["device"]]})
                self.new_tpms[model] = now
            elif ev["kind"] == "new" and len(dev["nodes"]) >= 2:
                alert("new_device", "low", ev["device"], {"nodes": sorted(dev["nodes"])})
            elif ev["kind"] == "visit" and ev["visits"] >= REPEAT_VISITS:
                alert("repeat_visitor", "low", ev["device"], {"visits": ev["visits"], "away_s": ev.get("away_s")})

        for node, st in node_states.items():
            prev = self.nodes.get(node)
            if st["state"] == "offline" and prev not in (None, "offline"):
                alert("node_offline", "high", None, {"node_id": node, "sources": st["sources"]})
            elif st["state"] != "offline" and prev == "offline":
                alert("node_online", "info", None, {"node_id": node, "sources": st["sources"]})
            self.nodes[node] = st["state"]
            probs = set((st.get("health") or {}).get("problems") or [])
            new = sorted(probs - self.node_problems.get(node, set()))
            if new and node in self.node_problems:        # not on the first report after a start
                alert("node_health", "medium", None, {"node_id": node, "problems": new})
            self.node_problems[node] = probs
        return out


def arrivals(now: float, events: list[dict[str, Any]], devices: dict[str, dict[str, Any]], marks: dict[str, dict[str, Any]],
             friendly_ids: set[str], cot: dict[str, Any], open_starts: dict[str, float], last: dict[str, float],
             window: float = 120.0) -> list[dict[str, Any]]:
    """``arrived_with``: a visit starts and a known co-traveler's visit started within ``window``.
    ``cot``: {"entity_of": device -> vehicle id, "partners": entity -> set, "members": entity -> devices};
    ``last``: group -> last alert time (deduplication, kept by the caller)."""
    out = []
    ent_of, partners, members = cot.get("entity_of", {}), cot.get("partners", {}), cot.get("members", {})
    for ev in events:
        dev_id = ev["device"]
        ent = ent_of.get(dev_id, dev_id)
        mine = partners.get(ent)
        if not mine or dev_id in friendly_ids or (marks.get(dev_id) or {}).get("kind") == "friendly":
            continue
        with_ = sorted(p for p in mine if any(abs(open_starts.get(m, -1e18) - now) <= window for m in members.get(p, {p})))
        if not with_:
            continue
        group = "|".join(sorted([ent, *with_]))
        if now - last.get(group, -1e18) <= VISIT_GAP_S:
            continue
        last[group] = now
        prio = any((marks.get(m) or {}).get("kind") == "priority" for e in [ent, *with_] for m in members.get(e, {e}))
        out.append({"rule_id": "arrived_with", "rule_type": "arrived_with", "severity": "high" if prio else "low",
                    "device": dev_id, "area": None, "context": {"entity": ent, "with": with_}, "t": now})
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
