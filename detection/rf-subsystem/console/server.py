"""WBT Console: live RF picture of a site from the WBT nodes.

Polls Kismet for each device's per-node RSSI (``seenby`` signal), keeps a
short rolling window per (device, node), and serves the console page and its
API. Everything learned at a building (node positions, walls, calibration,
history) is kept per *site* (``sites.py``) so the system can move between
buildings and wipe what it learned. Sign-in and roles: ``auth.py``.

    GET  /                  the console page (static/)
    GET  /api/session       {enabled, role}
    POST /api/login         {password} -> session cookie; POST /api/logout
    GET  /api/state         active site, node layout, per-band model defaults,
                            per-device smoothed per-node RSSI, node-to-node
                            calibration, saved calibration fits, walls, sources
    GET  /api/samples?id=<emitter>&since=<unix s>
                            raw per-second RSSI per node for one device
    GET  /api/sites                          all sites (summary)
    POST /api/sites                          {name}: create a site and switch to it
    POST /api/sites/<id>/activate            switch the console to this site
    POST /api/sites/<id>/rename              {name}
    POST /api/sites/<id>/retention           {days}             (admin)
    POST /api/sites/<id>/clear               delete its history (admin)
    POST /api/sites/<id>/wipe                {confirm: name}    (admin; not the active site)
    GET  /api/sites/<id>/export              zip of everything stored for it (admin)
    POST /api/site/nodes                     {node, x, y} place/move a node on the
                                             active site, or {node, remove: true}
    GET  /api/calibration                    {points, fits, version}
    POST /api/calibration/record             {device, x, y, t0, seconds, ...}:
                        record a spot from Kismet's packet log (every packet,
                        median per node); waits for the log to cover the window
    POST /api/calibration/points             add one spot
    POST /api/calibration/points/delete      {ids}: delete spots (journaled)
    POST /api/calibration/points/restore     {ids}: restore deleted spots
    POST /api/calibration/points/<id>/reassign  {device}: rebuild a spot for
                        another device from Kismet's packet log (same window)
    GET  /api/calibration/deleted            deleted spots that can be restored
    POST /api/calibration/fits/<band>        {fit} (null removes it)
    POST /api/walls/add                      {walls: [{x1, y1, x2, y2, draft?}]}
    POST /api/walls/delete                   {ids}
    POST /api/walls/accept                   clear draft flags
    POST /api/walls/clear                    remove all walls
                        Calibration and walls belong to the active site. One
                        journaled operation per request; a page never sends a
                        whole list, so a page with a stale list cannot wipe
                        what another page drew.

Every POST needs ``Content-Type: application/json`` (a cross-site form cannot
send that) and the operator role; site clear/wipe/retention/export need admin.

Smoothing: each node's per-second RSSI history (Kismet ``signal_rrd``) is
kept per (device, node) for a per-band window; outliers beyond 3 MAD of the
median are dropped and the rest is averaged with exponential time weights.
Localization (and its temporal filter) runs in the browser for now.

Environment (CONSOLE_*; the older DFVIZ_* names still work):
    KISMET_URL       default http://kismet-server:2501
    KISMET_USER / KISMET_PASSWORD
    CONSOLE_DATA     data directory (sites, session key), default ./data
    CONSOLE_LAYOUT   team template: node hardware (self_emitters) and the node
                     layout for the first site; falls back to layout.example.yaml
    CONSOLE_SITE     site to start on when none was chosen yet (default "default")
    CONSOLE_OPERATOR_PASSWORD / CONSOLE_ADMIN_PASSWORD   sign-in (unset: off)
    CONSOLE_WINDOW_S rolling RSSI window, default 60
    CONSOLE_POLL_S   Kismet poll interval, default 3
    CONSOLE_PORT     default 8080
    CONSOLE_KISMET_LOGS  directory of Kismet .kismet logs (calibration spots)
    CONSOLE_DEMO     "1": add synthetic emitters with known positions (BLE
                     walker, passing car with TPMS, ...); "only": demo alone
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

from rfanalytics.kismet import KismetIngestError, KismetPoller, describe_device, seenby_history

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from auth import COOKIE, Auth  # noqa: E402
from sites import SiteManager  # noqa: E402


def env(name: str, default: Any = None) -> Any:
    """CONSOLE_<name>, else the older DFVIZ_<name>, else default."""
    return os.environ.get(f"CONSOLE_{name}", os.environ.get(f"DFVIZ_{name}", default))
FT_PER_M = 3.28084

BANDS = {"wifi_ap": "wifi", "wifi_client": "wifi", "ble": "bt", "bt_classic": "bt", "tpms": "tpms"}
# Per band: history window, smoothing time constant, and when a node's entry
# is too old to use. TPMS sensors transmit about once a minute while moving,
# so they keep evidence longer.
BAND_TIMING = {
    "wifi": {"window_s": 60.0, "tau_s": 12.0, "stale_s": 30.0},
    "bt": {"window_s": 60.0, "tau_s": 12.0, "stale_s": 30.0},
    "tpms": {"window_s": 180.0, "tau_s": 60.0, "stale_s": 120.0},
}
# wall_db: loss per wall crossed. Starting guesses (interior drywall at 2.4/5 GHz
# is typically 3-6 dB); live calibration fits it.
DEFAULT_MODELS = {
    "wifi": {"ref_rssi_1m": -40.0, "path_loss_exp": 2.7, "sigma_db": 6.0, "wall_db": 4.0},
    "bt": {"ref_rssi_1m": -59.0, "path_loss_exp": 2.5, "sigma_db": 6.0, "wall_db": 4.0},
    "tpms": {"ref_rssi_1m": -45.0, "path_loss_exp": 2.2, "sigma_db": 7.0, "wall_db": 3.0},
}
MAX_WALLS = 500


def smooth_rssi(samples: list[tuple[float, int]], now: float, tau_s: float) -> dict[str, float]:
    """Outlier-rejected, exponentially time-weighted mean of (t, rssi) samples."""
    values = [r for _, r in samples]
    med = statistics.median(values)
    spread = 1.4826 * statistics.median([abs(v - med) for v in values])  # robust sigma
    limit = max(3 * spread, 4.0)
    kept = [(t, r) for t, r in samples if abs(r - med) <= limit] or samples
    wsum = rsum = 0.0
    for t, r in kept:
        w = math.exp(-max(0.0, now - t) / tau_s)
        wsum += w
        rsum += w * r
    return {
        "rssi": round(rsum / wsum if wsum > 0 else med, 2),
        "raw": float(samples[-1][1]),
        "spread": round(spread, 2),
        "n": len(samples),
        "outliers": len(samples) - len(kept),
        "last": samples[-1][0],
    }


def load_layout(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    nodes = {str(k): [float(v[0]), float(v[1])] for k, v in (data.get("nodes") or {}).items()}
    if len(nodes) < 2:
        raise ValueError(f"{path}: layout needs at least two nodes")
    raw_models = data.get("models") or {}
    if "model" in data:  # older single-model layout files apply to Wi-Fi
        raw_models = {"wifi": data["model"], **raw_models}
    models = {band: {**DEFAULT_MODELS[band], **(raw_models.get(band) or {})} for band in DEFAULT_MODELS}
    return {
        "name": data.get("name", "site"),
        "units": "ft",
        "nodes": nodes,
        "outline": data.get("outline") or _bounding_box(nodes),
        "self_emitters": {str(k): str(v).lower() for k, v in (data.get("self_emitters") or {}).items()},
        # How well the node positions are known (1 sd, ft); used to weight calibration readings.
        "node_sd_ft": float(data.get("node_sd_ft", 2.0)),
        "models": models,
    }


def _bounding_box(nodes: dict[str, list[float]]) -> list[list[float]]:
    xs = [p[0] for p in nodes.values()]
    ys = [p[1] for p in nodes.values()]
    return [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]


@dataclass
class _Meta:
    emitter_type: str
    attributes: dict[str, Any]
    last_seen: float = 0.0
    truth: list[float] | None = None  # demo emitters only


@dataclass
class Tracker:
    """Rolling per-(device, node) RSSI history from Kismet device snapshots."""

    window_s: float = 180.0  # how far back Kismet is asked for devices (longest band window)
    samples: dict[tuple[str, str], deque] = field(default_factory=dict)
    meta: dict[str, _Meta] = field(default_factory=dict)

    @staticmethod
    def _timing(emitter_type: str) -> dict[str, float]:
        return BAND_TIMING[BANDS.get(emitter_type, "wifi")]

    def update(self, devices: list[dict[str, Any]], now: float) -> None:
        for dev in devices:
            described = describe_device(dev)
            if described is None:
                continue
            emitter_type, emitter_id, attrs = described
            window = self._timing(emitter_type)["window_s"]
            m = self.meta.get(emitter_id)
            if m is None:
                m = self.meta[emitter_id] = _Meta(emitter_type, attrs)
            m.attributes = attrs or m.attributes
            if "dfviz.truth" in dev:
                m.truth = dev["dfviz.truth"]
            for node_id, history in seenby_history(dev):
                q = self.samples.setdefault((emitter_id, node_id), deque())
                last = q[-1][0] if q else float("-inf")
                for t, rssi in history:
                    if t > last and now - t <= window:
                        q.append((t, rssi))
                        last = t
                        m.last_seen = max(m.last_seen, t)
        self.prune(now)

    def prune(self, now: float) -> None:
        for key in list(self.samples):
            m = self.meta.get(key[0])
            window = self._timing(m.emitter_type)["window_s"] if m else max(t["window_s"] for t in BAND_TIMING.values())
            q = self.samples[key]
            while q and q[0][0] < now - window:
                q.popleft()
            if not q:
                del self.samples[key]
        live = {eid for eid, _ in self.samples}
        for eid in list(self.meta):
            if eid not in live:
                del self.meta[eid]

    def samples_since(self, emitter_id: str, since: float) -> dict[str, list[list[float]]]:
        """Raw (time, rssi) samples per node for one device, newer than ``since``."""
        return {node: [[t, r] for t, r in q if t >= since]
                for (eid, node), q in self.samples.items() if eid == emitter_id}

    def node_rssi(self, emitter_id: str, now: float) -> dict[str, dict[str, float]]:
        m = self.meta.get(emitter_id)
        if m is None:
            return {}
        timing = self._timing(m.emitter_type)
        out = {}
        for (eid, node), q in self.samples.items():
            if eid == emitter_id and q and now - q[-1][0] <= timing["stale_s"]:
                out[node] = smooth_rssi(list(q), now, timing["tau_s"])
        return out

    def snapshot(self, now: float) -> list[dict[str, Any]]:
        by_device: dict[str, dict[str, dict[str, float]]] = {}
        for (eid, node), q in self.samples.items():
            timing = self._timing(self.meta[eid].emitter_type)
            if q and now - q[-1][0] <= timing["stale_s"]:
                by_device.setdefault(eid, {})[node] = smooth_rssi(list(q), now, timing["tau_s"])
        out = []
        for eid, nodes in by_device.items():
            m = self.meta[eid]
            d = {
                "id": eid,
                "type": m.emitter_type,
                "band": BANDS.get(m.emitter_type, "wifi"),
                "name": m.attributes.get("ssid") or m.attributes.get("name") or m.attributes.get("tpms_protocol"),
                "manufacturer": m.attributes.get("manufacturer"),
                "randomized": bool(m.attributes.get("randomized_address")),
                "last_seen": m.last_seen,
                "nodes": nodes,
            }
            if m.truth is not None:
                d["truth"] = m.truth
            out.append(d)
        out.sort(key=lambda d: (-len(d["nodes"]), -max(n["rssi"] for n in d["nodes"].values())))
        return out


def calibration_samples(tracker: Tracker, layout: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """RSSI of each node's own radio as heard by the other nodes, at known distances."""
    nodes = layout["nodes"]
    out = []
    for tx, mac in layout["self_emitters"].items():
        if tx not in nodes:
            continue
        for rx, rec in tracker.node_rssi(mac, now).items():
            if rx == tx or rx not in nodes:
                continue
            d_ft = math.dist(nodes[tx], nodes[rx])
            out.append({"tx": tx, "rx": rx, "distance_ft": round(d_ft, 2),
                        "distance_m": round(d_ft / FT_PER_M, 3), "rssi": rec["rssi"], "n": rec["n"]})
    return out


MAX_CAL_BYTES = 512 * 1024
MAX_CAL_POINTS = 1000


class CalibrationStore:
    """Calibration spots and fits. Changes are made one item at a time on the
    server and appended to a journal (``<file>.journal.jsonl``), so nothing a
    page does can silently drop spots another page recorded, and deleted spots
    stay restorable."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.journal = self.path.with_name(self.path.name + ".journal.jsonl")
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"points": [], "fits": {}, "walls": []}
        self.version = 0
        if self.path.exists():
            try:
                self.data = self.validate(json.loads(self.path.read_text()))
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"console: ignoring unreadable {self.path}: {exc}", file=sys.stderr)
        for pt in self.data["points"]:
            pt.setdefault("id", self._new_id())
        for w in self.data["walls"]:
            w.setdefault("id", self._new_id())
        # Change counter survives restarts (pages compare it to discard stale state).
        if self.journal.exists():
            with open(self.journal) as j:
                self.version = sum(1 for _ in j)

    @staticmethod
    def _new_id() -> str:
        import uuid
        return uuid.uuid4().hex[:12]

    @staticmethod
    def validate_point(pt: Any) -> dict[str, Any]:
        if not isinstance(pt, dict) or not isinstance(pt.get("rssi"), dict):
            raise ValueError("each point needs an rssi object")
        if not all(isinstance(pt.get(k), (int, float)) for k in ("x", "y")):
            raise ValueError("each point needs numeric x and y")
        if pt.get("band") not in BAND_TIMING:
            raise ValueError(f"point band must be one of {sorted(BAND_TIMING)}")
        if not all(isinstance(v, (int, float)) and -130 <= v <= 0 for v in pt["rssi"].values()):
            raise ValueError("rssi values must be dBm numbers")
        if len(json.dumps(pt)) > 8192:
            raise ValueError("point too large")
        if "pos_sd_ft" in pt and not (isinstance(pt["pos_sd_ft"], (int, float)) and 0 <= pt["pos_sd_ft"] <= 50):
            raise ValueError("pos_sd_ft must be 0..50 ft")
        return pt

    @classmethod
    def validate(cls, doc: Any) -> dict[str, Any]:
        if not isinstance(doc, dict):
            raise ValueError("calibration must be an object")
        points, fits = doc.get("points", []), doc.get("fits", {})
        if not isinstance(points, list) or len(points) > MAX_CAL_POINTS:
            raise ValueError(f"points must be a list of at most {MAX_CAL_POINTS}")
        for pt in points:
            cls.validate_point(pt)
        if not isinstance(fits, dict) or not set(fits) <= set(BAND_TIMING):
            raise ValueError(f"fits keys must be bands {sorted(BAND_TIMING)}")
        return {"points": points, "fits": fits, "walls": cls.validate_walls(doc.get("walls", []))}

    @staticmethod
    def validate_walls(walls: Any) -> list[dict[str, Any]]:
        if not isinstance(walls, list) or len(walls) > MAX_WALLS:
            raise ValueError(f"walls must be a list of at most {MAX_WALLS}")
        out = []
        for w in walls:
            if not isinstance(w, dict) or not all(isinstance(w.get(k), (int, float)) and abs(w[k]) < 10000 for k in ("x1", "y1", "x2", "y2")):
                raise ValueError("each wall needs numeric x1, y1, x2, y2 (ft)")
            if (w["x1"], w["y1"]) == (w["x2"], w["y2"]):
                continue  # zero-length
            wall = {k: float(w[k]) for k in ("x1", "y1", "x2", "y2")}
            if w.get("draft"):
                wall["draft"] = True
            if isinstance(w.get("sd_ft"), (int, float)) and 0 <= w["sd_ft"] <= 20:
                wall["sd_ft"] = float(w["sd_ft"])  # how accurately the wall was drawn (1 sd, ft)
            if isinstance(w.get("id"), str) and 0 < len(w["id"]) <= 32:
                wall["id"] = w["id"]
            out.append(wall)
        return out

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"points": list(self.data["points"]), "fits": dict(self.data["fits"]),
                    "walls": list(self.data.get("walls", [])), "version": self.version}

    def _commit(self, entry: dict[str, Any]) -> None:
        """Persist the document and append the change to the journal (lock held)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a") as j:
            j.write(json.dumps({"t": time.time(), **entry}) + "\n")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)
        self.version += 1

    def add_point(self, pt: Any) -> dict[str, Any]:
        pt = self.validate_point(dict(pt))
        with self.lock:
            if len(self.data["points"]) >= MAX_CAL_POINTS:
                raise ValueError(f"at most {MAX_CAL_POINTS} points")
            if not pt.get("id") or any(q["id"] == pt["id"] for q in self.data["points"]):
                pt["id"] = self._new_id()
            pt["recorded_at"] = time.time()
            self.data["points"].append(pt)
            self._commit({"op": "add", "point": pt})
        return pt

    def delete_points(self, ids: list[str]) -> list[dict[str, Any]]:
        with self.lock:
            gone = [p for p in self.data["points"] if p["id"] in set(ids)]
            if gone:
                self.data["points"] = [p for p in self.data["points"] if p["id"] not in set(ids)]
                self._commit({"op": "delete", "points": gone})
        return gone

    def replace_point(self, point_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            for i, p in enumerate(self.data["points"]):
                if p["id"] == point_id:
                    new = self.validate_point({**p, **changes})
                    self.data["points"][i] = new
                    self._commit({"op": "update", "before": p, "after": new})
                    return new
        raise KeyError(point_id)

    def deleted(self) -> list[dict[str, Any]]:
        """Points deleted (per the journal) and not present now, newest first."""
        present = {p["id"] for p in self.data["points"]}
        out: dict[str, dict[str, Any]] = {}
        if self.journal.exists():
            for line in self.journal.read_text().splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("op") == "delete":
                    for p in e.get("points", []):
                        out[p["id"]] = {**p, "deleted_at": e["t"]}
        return sorted((p for i, p in out.items() if i not in present), key=lambda p: -p["deleted_at"])

    def restore(self, ids: list[str]) -> list[dict[str, Any]]:
        candidates = {p["id"]: p for p in self.deleted() if p["id"] in set(ids)}
        with self.lock:
            back = []
            for p in candidates.values():
                p = {k: v for k, v in p.items() if k != "deleted_at"}
                self.data["points"].append(p)
                back.append(p)
            if back:
                self._commit({"op": "restore", "points": back})
        return back

    def _walls_op(self, op: str, new: list[dict[str, Any]], **extra: Any) -> list[dict[str, Any]]:
        before = self.data.get("walls", [])
        self.data["walls"] = new
        self._commit({"op": "walls", "action": op, "before": before, "after": new, **extra})
        return new

    def add_walls(self, walls: Any) -> list[dict[str, Any]]:
        walls = self.validate_walls(walls)
        with self.lock:
            if len(self.data["walls"]) + len(walls) > MAX_WALLS:
                raise ValueError(f"at most {MAX_WALLS} walls")
            for w in walls:
                w["id"] = self._new_id()
            return self._walls_op("add", self.data["walls"] + walls)

    def delete_walls(self, ids: list[str]) -> list[dict[str, Any]]:
        with self.lock:
            keep = [w for w in self.data["walls"] if w["id"] not in set(ids)]
            if len(keep) == len(self.data["walls"]):
                return keep
            return self._walls_op("delete", keep, ids=list(ids))

    def accept_walls(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._walls_op("accept", [{k: v for k, v in w.items() if k != "draft"} for w in self.data["walls"]])

    def clear_walls(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._walls_op("clear", [])

    def set_fit(self, band: str, fit: Any) -> None:
        if band not in BAND_TIMING:
            raise ValueError(f"band must be one of {sorted(BAND_TIMING)}")
        if fit is not None and not isinstance(fit, dict):
            raise ValueError("fit must be an object or null")
        with self.lock:
            before = self.data["fits"].get(band)
            if fit is None:
                self.data["fits"].pop(band, None)
            else:
                self.data["fits"][band] = fit
            self._commit({"op": "fit", "band": band, "before": before, "after": fit})


class KismetLogError(Exception):
    pass


class KismetLog:
    """Reads per-node signal for one device and time window from Kismet's
    .kismet packet logs (SQLite), to record or rebuild a calibration spot.

    Reads take SQLite's normal shared lock (read-only), so they never see a
    half-written page while Kismet is writing. Unlocked ("immutable") reads did,
    and failed with "database disk image is malformed". When Kismet holds the
    write lock the read is retried; a read that still fails raises
    KismetLogError instead of silently returning fewer packets.
    """

    RETRIES = 20
    RETRY_S = 0.5

    def __init__(self, directory: str | Path | None) -> None:
        self.dir = Path(directory) if directory else None

    def _files(self) -> list[Path]:
        return sorted(self.dir.glob("*.kismet")) if self.dir and self.dir.is_dir() else []

    def _read(self, path: Path, fn: Any) -> Any:
        import sqlite3

        last: Exception | None = None
        for _ in range(self.RETRIES):
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
                try:
                    return fn(db)
                finally:
                    db.close()
            except sqlite3.DatabaseError as exc:   # "database is locked" while Kismet writes
                last = exc
                time.sleep(self.RETRY_S)
        raise KismetLogError(f"could not read {path.name}: {last}")

    @staticmethod
    def _has_data_signal(db: Any) -> bool:
        """Bluetooth capture logs no packets: each sighting is a ``data`` row
        (type linuxbthci) with its own ``devmac`` and ``signal``."""
        return {"devmac", "datasource", "signal", "ts_sec"} <= {r[1] for r in db.execute("pragma table_info(data)")}

    def newest(self) -> float:
        """Time of the newest record in the newest log (Kismet writes ~10 s behind)."""
        files = self._files()
        if not files:
            return 0.0

        def newest_in(db: Any) -> float:
            t = float(db.execute("select max(ts_sec) from packets").fetchone()[0] or 0)
            if self._has_data_signal(db):
                t = max(t, float(db.execute("select max(ts_sec) from data").fetchone()[0] or 0))
            return t

        return self._read(files[-1], newest_in)

    def wait_for(self, t: float, timeout: float = 45.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.newest() >= t:
                return True
            time.sleep(1.0)
        return False

    def spot(self, mac: str, start: float, seconds: float, min_packets: int = 3) -> dict[str, Any]:
        if not self.dir or not self.dir.is_dir():
            raise ValueError("Kismet logs are not available to this server (DFVIZ_KISMET_LOGS)")
        per: dict[str, list[int]] = {}

        def collect(db: Any) -> None:
            names = {u: n for u, n in db.execute("select uuid, name from datasources")}
            window = (mac.lower(), int(start), int(start + seconds))
            rows = db.execute(
                "select datasource, signal from packets where lower(sourcemac)=? and ts_sec between ? and ? and signal != 0",
                window).fetchall()
            if self._has_data_signal(db):   # Bluetooth sightings
                rows += db.execute(
                    "select datasource, signal from data where lower(devmac)=? and ts_sec between ? and ? and signal != 0",
                    window).fetchall()
            for src, sig in rows:
                node = str(names.get(src, "")).split(":", 1)[0].lower()
                if node:
                    per.setdefault(node, []).append(int(sig))

        for f in self._files():
            self._read(f, collect)
        rssi = {n: float(statistics.median(v)) for n, v in per.items() if len(v) >= min_packets}
        return {"rssi": rssi, "n": {n: len(per[n]) for n in rssi}}


SOURCE_SILENT_S = 300        # running but no new packets for this long: report it
SOURCE_POLL_S = 30
SOURCE_QUIET_OK = (":tpms",)  # TPMS sensors transmit rarely; silence there is normal


class SourceHealth:
    """Tracks Kismet capture sources and flags the ones that run but deliver
    nothing (e.g. a Bluetooth dongle that stopped scanning: Kismet shows it as
    running with no error)."""

    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}

    def update(self, sources: list[dict[str, Any]], now: float) -> None:
        for s in sources:
            key = s["uuid"] or s["name"]
            prev = self.sources.get(key)
            # Any change counts: Kismet resets counters when it restarts (a drop is activity, not silence).
            grew = prev is None or s["packets"] != prev["packets"]
            self.sources[key] = {**s, "first_seen": prev["first_seen"] if prev else now,
                                 "last_growth": now if grew else prev["last_growth"]}

    def report(self, now: float) -> list[dict[str, Any]]:
        out = []
        for s in sorted(self.sources.values(), key=lambda v: v["name"]):
            quiet_s = now - s["last_growth"] if s["running"] else None
            status = ("error" if s["error"] else "stopped") if not s["running"] else "ok"
            if (s["running"] and quiet_s is not None and quiet_s >= SOURCE_SILENT_S
                    and now - s["first_seen"] >= SOURCE_SILENT_S and not s["name"].endswith(SOURCE_QUIET_OK)):
                status = "silent"
            out.append({"name": s["name"], "status": status, "packets": s["packets"], "error": s["error"],
                        "quiet_s": None if quiet_s is None else round(quiet_s)})
        return out


PURGE_EVERY_S = 3600


class App:
    def __init__(self, poller: Any, layout: dict[str, Any] | None, window_s: float, poll_s: float,
                 extra_sources: list[Any] | None = None, cal_store: CalibrationStore | None = None,
                 kismet_log: KismetLog | None = None, sites: SiteManager | None = None,
                 auth: Auth | None = None) -> None:
        """With ``sites``, layout and calibration follow the active site;
        without it (tests, single-site use) the given layout and store are used."""
        self.poller = poller
        self.sites = sites
        self._layout = layout
        self._cal = cal_store if sites is None else None
        if sites is None and self._cal is None:
            self._cal = CalibrationStore(HERE / "calibration.json")
        self.auth = auth or Auth(None, None, None)
        self.kismet_log = kismet_log or KismetLog(None)
        self.extra_sources = extra_sources or []
        self.poll_s = poll_s
        self.tracker = Tracker(window_s)
        self.lock = threading.Lock()
        self.status: dict[str, Any] = {"ok": False, "error": "not polled yet", "last_poll": None, "devices_polled": 0}
        self.health = SourceHealth()
        self.last_source_poll = 0.0
        self.last_purge = 0.0

    @property
    def layout(self) -> dict[str, Any]:
        return self.sites.layout() if self.sites else self._layout

    @property
    def cal(self) -> CalibrationStore:
        return self.sites.active().cal if self.sites else self._cal

    def poll_once(self) -> None:
        devices: list[dict[str, Any]] = []
        error = None
        if self.poller is not None:
            try:
                devices = self.poller.fetch_devices(since=-self.tracker.window_s)
            except KismetIngestError as exc:
                error = str(exc)
        for source in self.extra_sources:
            devices += source.fetch_devices(since=-self.tracker.window_s)
        now = time.time()
        if self.poller is not None and hasattr(self.poller, "fetch_sources") and now - self.last_source_poll >= SOURCE_POLL_S:
            self.last_source_poll = now
            try:
                sources = self.poller.fetch_sources()
                with self.lock:
                    self.health.update(sources, now)
            except KismetIngestError:
                pass
        if self.sites and now - self.last_purge >= PURGE_EVERY_S:
            self.last_purge = now
            self.sites.purge(now)
        if error and not self.extra_sources:
            with self.lock:
                self.status.update(ok=False, error=error, last_poll=now)
            return
        with self.lock:
            self.tracker.update(devices, now)
            self.status.update(ok=error is None, error=error, last_poll=now, devices_polled=len(devices),
                               demo=bool(self.extra_sources))

    def run_poller(self) -> None:
        while True:
            started = time.time()
            self.poll_once()
            time.sleep(max(0.5, self.poll_s - (time.time() - started)))

    def state(self) -> dict[str, Any]:
        now = time.time()
        layout, cal = self.layout, self.cal
        site = None
        if self.sites:
            s = self.sites.active()
            site = {"id": s.id, "name": s.doc["name"], "retention_days": s.doc["retention_days"],
                    "version": self.sites.version}
        with self.lock:
            sources = self.health.report(now)
            return {
                "now": now,
                "site": site,
                "window_s": self.tracker.window_s,
                "timing": BAND_TIMING,
                "layout": layout,
                "kismet": dict(self.status),
                "devices": self.tracker.snapshot(now),
                "calibration": calibration_samples(self.tracker, layout, now),
                "calibration_fits": cal.data.get("fits", {}),
                "calibration_version": cal.version,
                "sources": sources,
                # Nodes Kismet knows (from capture source names), placed on the map or not.
                "source_nodes": sorted({x["name"].split(":", 1)[0] for x in sources if ":" in x["name"]}),
                "walls": cal.data.get("walls", []),
            }

    def samples(self, emitter_id: str, since: float) -> dict[str, Any]:
        with self.lock:
            return {"id": emitter_id, "now": time.time(), "nodes": self.tracker.samples_since(emitter_id, since)}


ADMIN_ACTIONS = ("retention", "clear", "wipe", "export")
LOGIN_FAIL_DELAY_S = 1.0


class Handler(SimpleHTTPRequestHandler):
    app: App

    def end_headers(self) -> None:
        # Always revalidate the page's own files, so a browser never keeps
        # running an old app.js/calib.js after the container is updated.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _json(self, obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---------- auth ----------
    def _role(self) -> str | None:
        from http.cookies import SimpleCookie

        token = None
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
            if COOKIE in jar:
                token = jar[COOKIE].value
        except Exception:  # noqa: BLE001 - a malformed cookie is just "not signed in"
            token = None
        return self.app.auth.verify(token)

    def _need(self, needed: str) -> str | None:
        """Role if it allows ``needed``; otherwise sends 401/403 and returns None."""
        role = self._role()
        if role is None:
            self._json({"error": "sign in required"}, 401)
            return None
        if not Auth.allows(role, needed):
            self._json({"error": f"needs the {needed} role"}, 403)
            return None
        return role

    def _cookie(self, value: str, max_age: int) -> dict[str, str]:
        return {"Set-Cookie": f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"}

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlsplit

        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        if url.path == "/api/session":
            return self._json({"enabled": self.app.auth.enabled, "role": self._role()})
        sites = self.app.sites
        if url.path.startswith("/api/sites/") and url.path.endswith("/export"):
            if self._need("admin") is None:
                return None
            site_id = url.path.split("/")[3]
            try:
                data = sites.export(site_id)
            except KeyError:
                return self._json({"error": "no such site"}, 404)
            stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="wbt-site-{site_id}-{stamp}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return None
        if self._need("operator") is None:
            return None
        if url.path == "/api/state":
            return self._json(self.app.state())
        if url.path == "/api/sites":
            if not sites:
                return self._json({"sites": [], "version": 0})
            return self._json({"sites": sites.list(), "version": sites.version})
        if url.path == "/api/calibration":
            return self._json(self.app.cal.snapshot())
        if url.path == "/api/calibration/deleted":
            return self._json({"points": self.app.cal.deleted()})
        if url.path == "/api/samples":
            q = parse_qs(url.query)
            try:
                since = float(q.get("since", ["0"])[0])
            except ValueError:
                return self._json({"error": "since must be a number"}, 400)
            emitter_id = (q.get("id") or [""])[0].lower()
            if not emitter_id:
                return self._json({"error": "id is required"}, 400)
            return self._json(self.app.samples(emitter_id, since))
        return self._json({"error": "not found"}, 404)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_CAL_BYTES:
            raise ValueError(f"body must be 1..{MAX_CAL_BYTES} bytes")
        return json.loads(self.rfile.read(length))

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"error": "Content-Type must be application/json"}, 415)
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        if path == "/api/login":
            role = self.app.auth.login(str(body.get("password", ""))) if self.app.auth.enabled else "admin"
            if role is None:
                time.sleep(LOGIN_FAIL_DELAY_S)
                return self._json({"error": "wrong password"}, 401)
            return self._json({"role": role}, headers=self._cookie(self.app.auth.issue(role), 12 * 3600))
        if path == "/api/logout":
            return self._json({"ok": True}, headers=self._cookie("", 0))
        needed = "admin" if path.startswith("/api/sites/") and path.rsplit("/", 1)[1] in ADMIN_ACTIONS else "operator"
        role = self._need(needed)
        if role is None:
            return None
        try:
            if path.startswith("/api/sites") or path == "/api/site/nodes":
                return self._sites_post(path, body, role)
            return self._cal_post(path, body)
        except KeyError as exc:
            return self._json({"error": f"not found: {exc}"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except KismetLogError as exc:
            return self._json({"error": f"Kismet log busy, try again ({exc})"}, 503)
        except Exception as exc:  # noqa: BLE001 - report instead of dropping the connection
            import traceback
            traceback.print_exc()
            return self._json({"error": f"server error: {exc}"}, 500)

    def _sites_post(self, path: str, body: dict[str, Any], role: str) -> None:
        sites = self.app.sites
        if sites is None:
            return self._json({"error": "this server runs without site workspaces"}, 404)
        if path == "/api/sites":
            site = sites.create(str(body.get("name", "")), activate=True, user=role)
            return self._json({"site": site, "version": sites.version})
        if path == "/api/site/nodes":
            node = str(body.get("node", ""))
            xy = None if body.get("remove") else [body.get("x"), body.get("y")]
            return self._json({"nodes": sites.set_node(node, xy, user=role), "version": sites.version})
        parts = path.split("/")   # /api/sites/<id>/<action>
        if len(parts) != 5:
            return self._json({"error": "not found"}, 404)
        site_id, action = parts[3], parts[4]
        if action == "activate":
            sites.activate(site_id, user=role)
        elif action == "rename":
            sites.rename(site_id, body.get("name", ""), user=role)
        elif action == "retention":
            sites.set_retention(site_id, body.get("days"), user=role)
        elif action == "clear":
            return self._json({"removed": sites.clear_history(site_id, user=role), "version": sites.version})
        elif action == "wipe":
            sites.wipe(site_id, str(body.get("confirm", "")))
        else:
            return self._json({"error": "not found"}, 404)
        return self._json({"sites": sites.list(), "version": sites.version})

    def _cal_post(self, path: str, body: dict[str, Any]) -> None:
        cal = self.app.cal
        if path == "/api/calibration/record":
            return self._record(body)
        if path == "/api/calibration/points":
            return self._json({"point": cal.add_point(body), "version": cal.version})
        if path == "/api/calibration/points/delete":
            return self._json({"deleted": cal.delete_points(list(body.get("ids", []))), "version": cal.version})
        if path == "/api/calibration/points/restore":
            return self._json({"restored": cal.restore(list(body.get("ids", []))), "version": cal.version})
        if path == "/api/walls":
            return self._json({"error": "this page is out of date: reload it (walls are now changed one operation at a time)"}, 410)
        if path == "/api/walls/add":
            return self._json({"walls": cal.add_walls(body.get("walls", [])), "version": cal.version})
        if path == "/api/walls/delete":
            return self._json({"walls": cal.delete_walls(list(body.get("ids", []))), "version": cal.version})
        if path == "/api/walls/accept":
            return self._json({"walls": cal.accept_walls(), "version": cal.version})
        if path == "/api/walls/clear":
            return self._json({"walls": cal.clear_walls(), "version": cal.version})
        if path.startswith("/api/calibration/fits/"):
            cal.set_fit(path.rsplit("/", 1)[1], body.get("fit"))
            return self._json({"fits": cal.data["fits"], "version": cal.version})
        if path.startswith("/api/calibration/points/") and path.endswith("/reassign"):
            point_id = path.split("/")[4]
            pt = next((p for p in cal.data["points"] if p["id"] == point_id), None)
            if pt is None:
                return self._json({"error": "no such spot"}, 404)
            device = str(body.get("device", "")).lower()
            if not device:
                return self._json({"error": "device is required"}, 400)
            rec = self.app.kismet_log.spot(device, pt["t"], float(body.get("seconds", pt.get("seconds", 15))))
            if len(rec["rssi"]) < 2:
                return self._json({"error": f"{device} was heard by {len(rec['rssi'])} node(s) with 3+ packets in that window"}, 422)
            changes = {"device": device, "rssi": rec["rssi"], "n": rec["n"], "source": "kismet-log",
                       "device_name": body.get("device_name"), "device_type": body.get("device_type"),
                       "band": body.get("band", pt["band"]), "warn": []}
            return self._json({"point": cal.replace_point(point_id, changes), "version": cal.version})
        return self._json({"error": "not found"}, 404)

    def _record(self, body: dict[str, Any]) -> None:
        """A calibration spot from the packet log: all packets, median per node.

        Kismet's per-second signal history (used for the live view) keeps only
        each second's peak and only the last minute, which on live data gave 3
        samples where the log had 8-17 packets per node, biased several dB high.
        """
        device = str(body.get("device", "")).lower()
        t0, seconds = float(body.get("t0", 0)), float(body.get("seconds", 15))
        if not device or t0 <= 0:
            return self._json({"error": "device and t0 are required"}, 400)
        klog = self.app.kismet_log
        if not klog.dir:
            return self._json({"error": "Kismet logs are not mounted (CONSOLE_KISMET_LOGS)"}, 503)
        if not klog.wait_for(t0 + seconds):
            return self._json({"error": "the Kismet log has not caught up with the recording window yet; try again"}, 503)
        cal = self.app.cal   # the site that was active when recording started
        rec = klog.spot(device, t0, seconds)
        if len(rec["rssi"]) < 2:
            heard = klog.spot(device, t0, seconds, min_packets=1)["n"]
            counts = ", ".join(f"{n} {c}" for n, c in sorted(heard.items())) or "none"
            return self._json({"error": f"only {len(rec['rssi'])} node(s) heard {body.get('device_name') or device} 3+ times "
                               f"in {seconds:.0f} s (sightings: {counts})", "rssi": rec["rssi"]}, 422)
        point = {k: body[k] for k in ("band", "device_name", "device_type", "x", "y", "warn", "pos_sd_ft") if k in body}
        point.update(device=device, t=t0, seconds=seconds, rssi=rec["rssi"], n=rec["n"], source="kismet-log")
        return self._json({"point": cal.add_point(point), "version": cal.version})

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.path.startswith("/api/"):
            sys.stderr.write("console: " + fmt % args + "\n")


def main() -> int:
    template_path = Path(env("LAYOUT", HERE / "site" / "layout.yaml"))
    if not template_path.is_file():
        print(f"console: no team layout at {template_path}; using layout.example.yaml", file=sys.stderr)
        template_path = HERE / "layout.example.yaml"
    template = load_layout(template_path)
    data_dir = Path(env("DATA", HERE / "data"))
    sites = SiteManager(data_dir, template, CalibrationStore, initial=env("SITE"))
    auth = Auth(os.environ.get("CONSOLE_OPERATOR_PASSWORD"), os.environ.get("CONSOLE_ADMIN_PASSWORD"),
                data_dir / ".session_key")
    if not auth.enabled:
        print("console: sign-in is OFF (set CONSOLE_OPERATOR_PASSWORD / CONSOLE_ADMIN_PASSWORD)", file=sys.stderr)
    demo = str(env("DEMO", "")).lower()
    poller = None if demo == "only" else KismetPoller(
        os.environ.get("KISMET_URL", "http://kismet-server:2501"),
        username=os.environ.get("KISMET_USER"),
        password=os.environ.get("KISMET_PASSWORD"),
    )
    extra = []
    if demo in ("1", "true", "yes", "only"):
        from demo import DemoSource

        extra.append(DemoSource(sites.layout()))
    window = max(float(env("WINDOW_S", 60)), max(t["window_s"] for t in BAND_TIMING.values()))
    klog = KismetLog(env("KISMET_LOGS"))
    app = App(poller, None, window, float(env("POLL_S", 3)), extra, None, klog, sites=sites, auth=auth)
    threading.Thread(target=app.run_poller, daemon=True).start()
    Handler.app = app
    port = int(env("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=str(HERE / "static")))
    print(f"console: serving on :{port}, site {sites.active_id!r}, polling "
          f"{poller.base_url if poller else '(no Kismet)'}{' + demo emitters' if extra else ''}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
