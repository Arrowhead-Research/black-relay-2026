"""Server-side localization: per-node RSSI -> estimated position of each device.

A port of the page's localization (static/app.js ``buildGrid``/``costSurface``/
``filtered``/``localize`` and static/walls.js ``wallsExpected``), so positions
can be stored, published and alerted on without a browser open. Verified
against the page's own code: tests/parity/ (golden values produced by running
app.js and walls.js under node).

Model (log-distance path loss): predicted RSSI at node k for a transmitter at
p is P0 - 10 n log10(d_k(p) in m) - W * walls_k(p). For every grid cell the
observed per-node RSSI is compared with the prediction:
    cost(p) = sum(e^2) - sum(e)^2 / (K + sigma^2/tau^2),  e_k = rssi_k - predicted_k(p)
(the device's unknown transmit-power offset profiled out under a Gaussian
prior of spread tau). Each device's cost surface is averaged over time
(exponentially, only when new readings arrive); the position is the
likelihood-weighted mean within LOCAL_MEAN_FT of the best cell.

Per band the model is the saved calibration fit (n, p0, sigma, wall,
txSpread, per-node offsets), else the layout defaults: the same choice the
page makes (``calUseFits``).
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

FT_PER_M = 3.28084
LOCAL_MEAN_FT = 4.0        # position = weighted mean of the likelihood within this radius of the best cell
MIN_NODES = 2
TAU_S = 15.0               # cost-surface smoothing time constant, scaled per type:
TAU_SCALE = {"wifi_ap": 2.0, "wifi_client": 1.0, "ble": 1.0, "bt_classic": 1.0, "tpms": 0.25}
WALL_SAMPLES = 16          # plausible positions per wall (its drawing accuracy, sd_ft)
WALL_SD_DEFAULT = 2.0


# ---------- walls (same numbers as walls.js on every page) ----------

def _i32(x: int) -> int:
    return ((x + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _imul(a: int, b: int) -> int:
    return _i32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def _mulberry32(seed: int):
    state = [_i32(seed)]

    def rnd() -> float:
        s = state[0] = _i32(state[0] + 0x6D2B79F5)
        t = _imul(s ^ ((s & 0xFFFFFFFF) >> 15), 1 | s)
        t = _i32(_i32(t + _imul(t ^ ((t & 0xFFFFFFFF) >> 7), 61 | t)) ^ t)
        return ((t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296

    return rnd


def _js_num(v: float) -> str:
    """JavaScript's Number-to-string for the coordinates used in wall keys."""
    v = float(v)
    if v == 0:
        return "0"
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    return repr(v)


_SAMPLE_CACHE: dict[str, tuple[list[tuple[float, float, float, float]], tuple[float, float, float, float]]] = {}


def wall_samples(w: dict[str, Any]) -> tuple[list[tuple[float, float, float, float]], tuple[float, float, float, float]]:
    sd = float(w["sd_ft"]) if w.get("sd_ft") is not None else WALL_SD_DEFAULT
    key = ",".join(_js_num(v) for v in (w["x1"], w["y1"], w["x2"], w["y2"], sd))
    if key in _SAMPLE_CACHE:
        return _SAMPLE_CACHE[key]
    h = _i32(2166136261)
    for ch in key:
        h = _imul(h ^ ord(ch), 16777619)
    rnd = _mulberry32(h)

    def gauss() -> float:
        u = rnd()
        return math.sqrt(-2 * math.log(1 - u)) * math.cos(2 * math.pi * rnd())

    x1, y1, x2, y2 = (float(w[k]) for k in ("x1", "y1", "x2", "y2"))
    L = math.hypot(x2 - x1, y2 - y1)
    out = []
    if L > 0:
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        for _ in range(WALL_SAMPLES):
            s1, s2, e1, e2 = gauss() * sd, gauss() * sd, gauss() * sd, gauss() * sd
            out.append((x1 - uy * s1 - ux * e1, y1 + ux * s1 - uy * e1, x2 - uy * s2 + ux * e2, y2 + ux * s2 + uy * e2))
    bbox = (min(x1, x2) - 4 * sd, min(y1, y2) - 4 * sd, max(x1, x2) + 4 * sd, max(y1, y2) + 4 * sd)
    _SAMPLE_CACHE[key] = (out, bbox)
    return out, bbox


def _seg_cross(ax, ay, bx, by, cx, cy, dx, dy):
    """Do segments a-b and c-d properly cross? (Element-wise; touching at an end does not count.)"""
    def o(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1, d2 = o(cx, cy, dx, dy, ax, ay), o(cx, cy, dx, dy, bx, by)
    d3, d4 = o(ax, ay, bx, by, cx, cy), o(ax, ay, bx, by, dx, dy)
    return (((d1 > 0) & (d2 < 0)) | ((d1 < 0) & (d2 > 0))) & (((d3 > 0) & (d4 < 0)) | ((d3 < 0) & (d4 > 0)))


def walls_expected(ax: float, ay: Any, bx: Any, by: Any, walls: list[dict[str, Any]]) -> Any:
    """Expected (fractional) number of walls crossed by the straight path a-b
    (b may be arrays: one path per element)."""
    shape = np.broadcast(np.asarray(bx), np.asarray(by)).shape
    bx, by = (np.broadcast_to(np.asarray(v, dtype=float), shape).ravel() for v in (bx, by))
    n = np.zeros(bx.shape)
    lx, hx = np.minimum(ax, bx), np.maximum(ax, bx)
    ly, hy = np.minimum(ay, by), np.maximum(ay, by)
    for w in walls:
        sd = float(w["sd_ft"]) if w.get("sd_ft") is not None else WALL_SD_DEFAULT
        if sd <= 0:
            n = n + _seg_cross(ax, ay, bx, by, float(w["x1"]), float(w["y1"]), float(w["x2"]), float(w["y2"]))
            continue
        smp, bb = wall_samples(w)
        near = ~((hx < bb[0]) | (lx > bb[2]) | (hy < bb[1]) | (ly > bb[3]))
        if not near.any():
            continue
        idx = np.nonzero(near)
        sbx, sby = bx[idx], by[idx]
        c = np.zeros(len(sbx))
        for q in smp:
            c = c + _seg_cross(ax, ay, sbx, sby, *q)
        n = n.copy()
        n[idx] += c / WALL_SAMPLES
    return n.reshape(shape)


# ---------- grid and models ----------

class Grid:
    """Cells around the nodes; per node log10(distance in m) and expected walls crossed."""

    def __init__(self, nodes: dict[str, list[float]], walls: list[dict[str, Any]]) -> None:
        xs = [p[0] for p in nodes.values()]
        ys = [p[1] for p in nodes.values()]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 10)
        m = max(12, span * 0.5)
        self.x0, self.x1 = min(xs) - m, max(xs) + m
        self.y0, self.y1 = min(ys) - m, max(ys) + m
        self.cell = max(0.25, max(self.x1 - self.x0, self.y1 - self.y0) / 180)
        self.nx = math.ceil((self.x1 - self.x0) / self.cell)
        self.ny = math.ceil((self.y1 - self.y0) / self.cell)
        cx = self.x0 + (np.arange(self.nx) + 0.5) * self.cell
        cy = self.y0 + (np.arange(self.ny) + 0.5) * self.cell
        X, Y = np.meshgrid(cx, cy)                       # row-major: index j * nx + i
        self.X, self.Y = X.ravel(), Y.ravel()
        self.logd = {k: np.log10(np.maximum(np.hypot(self.X - px, self.Y - py) / FT_PER_M, 0.3)).astype(np.float32)
                     for k, (px, py) in nodes.items()}
        self.walls = ({k: walls_expected(px, py, self.X, self.Y, walls).astype(np.float32) for k, (px, py) in nodes.items()}
                      if walls else {})

    def bounds(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1, "cell": self.cell, "nx": self.nx, "ny": self.ny}


def band_models(layout: dict[str, Any], fits: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Live model per band: saved calibration fit, else the layout defaults."""
    out = {}
    for band, m in layout["models"].items():
        f = (fits or {}).get(band)
        out[band] = ({"n": f["n"], "p0": f["p0"], "sigma": f["sigma"], "wall": f.get("wall") or 0,
                      "txSpread": f.get("txSpread") or 8, "offsets": f.get("offsets") or {}, "calibrated": True,
                      "loo_median_ft": f.get("loo_median_ft")}
                     if f else
                     {"n": m["path_loss_exp"], "p0": m["ref_rssi_1m"], "sigma": m["sigma_db"], "wall": m.get("wall_db") or 0,
                      "txSpread": 8, "offsets": {}, "calibrated": False, "loo_median_ft": None})
    return out


# ---------- localization ----------

class Locator:
    """Keeps the grid for the current node layout and walls, and each device's
    time-averaged cost surface."""

    def __init__(self) -> None:
        self.grid: Grid | None = None
        self.geom_key: str | None = None
        self.model_key: str | None = None
        self.models: dict[str, dict[str, Any]] = {}
        self.version = 0                               # bumps when geometry or models change: restarts smoothing
        self.filt: dict[str, dict[str, Any]] = {}

    def configure(self, nodes: dict[str, list[float]], walls: list[dict[str, Any]], models: dict[str, dict[str, Any]]) -> None:
        geom = json.dumps([nodes, [{k: w.get(k) for k in ("x1", "y1", "x2", "y2", "sd_ft")} for w in walls]], sort_keys=True)
        if geom != self.geom_key:
            self.grid = Grid(nodes, walls) if len(nodes) >= 2 else None
            self.geom_key = geom
            self.version += 1
        mk = json.dumps(models, sort_keys=True)
        if mk != self.model_key:
            self.models, self.model_key = models, mk
            self.version += 1

    def _model(self, band: str) -> dict[str, Any]:
        return self.models.get(band) or self.models["wifi"]

    def _cost(self, dev: dict[str, Any]) -> dict[str, Any] | None:
        g, M = self.grid, self._model(dev["band"])
        ids = [k for k in dev["nodes"] if k in g.logd]
        if len(ids) < max(1, MIN_NODES):
            return None
        K, ten_n = len(ids), 10 * M["n"]
        div = K + (M["sigma"] ** 2) / (M["txSpread"] ** 2)
        off = M["offsets"]
        r = [dev["nodes"][k]["rssi"] - M["p0"] - (off.get(k) or 0) for k in ids]
        W = M["wall"] or 0
        Wk = [g.walls.get(k) if W else None for k in ids]
        L = [g.logd[k] for k in ids]
        s = np.zeros(g.nx * g.ny)
        s2 = np.zeros(g.nx * g.ny)
        for k in range(K):
            e = r[k] + ten_n * L[k].astype(float)
            if Wk[k] is not None:
                e = e + W * Wk[k].astype(float)
            s += e
            s2 += e * e
        return {"cost": (s2 - (s * s) / div).astype(np.float32), "ids": ids, "K": K, "r": r, "L": L,
                "ten_n": ten_n, "div": div, "W": W, "Wk": Wk}

    def _filtered(self, dev: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        key = max(n["last"] for n in dev["nodes"].values())
        f = self.filt.get(dev["id"])
        tau = TAU_S * TAU_SCALE.get(dev["type"], 1.0)
        if f is None or f["version"] != self.version or tau <= 0 or len(f["cost"]) != len(raw["cost"]):
            nf = {"cost": raw["cost"].copy(), "key": key, "version": self.version, "updated": True}
            self.filt[dev["id"]] = nf
            return nf
        if key <= f["key"]:
            f["updated"] = False
            return f
        a = 1 - math.exp(-min(key - f["key"], 10 * tau) / tau)
        old = f["cost"].astype(float)
        f["cost"] = (old + a * (raw["cost"].astype(float) - old)).astype(np.float32)
        f["key"], f["updated"] = key, True
        return f

    def _proximity(self, dev: dict[str, Any], nodes: dict[str, list[float]]) -> dict[str, Any] | None:
        M = self._model(dev["band"])
        node, rec = max(dev["nodes"].items(), key=lambda kv: kv[1]["rssi"])
        if node not in nodes:
            return None
        d_ft = 10 ** ((M["p0"] - rec["rssi"]) / (10 * M["n"])) * FT_PER_M
        return {"kind": "proximity", "node": node, "dFt": d_ft, "x": nodes[node][0], "y": nodes[node][1],
                "K": 1, "ids": [node], "offset": 0.0}

    def locate(self, dev: dict[str, Any], nodes: dict[str, list[float]]) -> dict[str, Any] | None:
        if self.grid is None:
            return None
        if len(dev["nodes"]) == 1 and MIN_NODES <= 1:
            return self._proximity(dev, nodes)
        raw = self._cost(dev)
        if raw is None:
            return None
        if raw["K"] < 2:
            return self._proximity(dev, nodes)
        g, M = self.grid, self._model(dev["band"])
        f = self._filtered(dev, raw)
        cost = f["cost"]
        best = int(np.argmin(cost))
        min_c = float(cost[best])
        inv = 1 / (2 * M["sigma"] * M["sigma"])
        bi, bj = best % g.nx, best // g.nx
        bx, by = g.x0 + (bi + 0.5) * g.cell, g.y0 + (bj + 0.5) * g.cell
        ri = math.ceil(LOCAL_MEAN_FT / g.cell)
        j0, j1 = max(0, bj - ri), min(g.ny - 1, bj + ri)
        i0, i1 = max(0, bi - ri), min(g.nx - 1, bi + ri)
        win = cost.reshape(g.ny, g.nx)[j0:j1 + 1, i0:i1 + 1].astype(float)
        xs = g.x0 + (np.arange(i0, i1 + 1) + 0.5) * g.cell
        ys = g.y0 + (np.arange(j0, j1 + 1) + 0.5) * g.cell
        XX, YY = np.meshgrid(xs, ys)
        inside = (XX - bx) ** 2 + (YY - by) ** 2 <= LOCAL_MEAN_FT * LOCAL_MEAN_FT
        v = np.where(inside, np.exp(-(win - min_c) * inv), 0.0)
        ls = v.sum()
        ex, ey = float((v * XX).sum() / ls), float((v * YY).sum() / ls)
        # Offset (unknown transmit power) and residual from the current readings at the estimate.
        N = g.nx * g.ny
        ec = min(N - 1, max(0, math.floor((ey - g.y0) / g.cell) * g.nx + math.floor((ex - g.x0) / g.cell)))
        s = s2 = 0.0
        for k in range(raw["K"]):
            e = raw["r"][k] + raw["ten_n"] * float(raw["L"][k][ec])
            if raw["Wk"][k] is not None:
                e += raw["W"] * float(raw["Wk"][k][ec])
            s += e
            s2 += e * e
        return {"kind": "fix", "x": ex, "y": ey, "K": raw["K"], "ids": raw["ids"], "offset": s / raw["div"],
                "res": math.sqrt(max(0.0, (s2 - (s * s) / raw["div"]) / raw["K"])), "updated": f["updated"]}

    def locate_all(self, devices: list[dict[str, Any]], nodes: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
        out = {}
        for dev in devices:
            est = self.locate(dev, nodes)
            if est is not None:
                out[dev["id"]] = est
        live = {d["id"] for d in devices}
        for eid in list(self.filt):
            if eid not in live:
                del self.filt[eid]
        return out


# ---------- geo-reference: site feet -> latitude/longitude ----------

EARTH_R_M = 6371008.8
M_PER_FT = 0.3048
GEO_WARN_RMS_M = 3.0       # node GPS points disagree with the floor plan by more than this: warn
GEO_WARN_SCALE = 0.1       # distances between GPS points vs the floor plan differ by more than 10 %: warn


def _enu(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """East/north metres of (lat, lon) from (lat0, lon0) (local tangent plane; fine at building scale)."""
    k = math.pi / 180 * EARTH_R_M
    return (lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def georeference(nodes: dict[str, list[float]], geo: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fit the floor plan (feet; the page's y axis points down) to node GPS points.

    Two or more nodes with GPS: rotation + offset by least squares (no scaling:
    the floor plan is measured). One node with GPS: also needs ``heading_deg``,
    the compass bearing of the page's "up" direction. Returns the transform with
    its quality (rms_m, scale) and any warnings, or None if not enough is known.
    """
    geo = geo or {}
    pts = [(k, v) for k, v in (geo.get("points") or {}).items() if k in nodes]
    heading = geo.get("heading_deg")
    if not pts or (len(pts) == 1 and heading is None):
        return None
    lat0 = sum(v[0] for _, v in pts) / len(pts)
    lon0 = sum(v[1] for _, v in pts) / len(pts)
    local = [(nodes[k][0] * M_PER_FT, -nodes[k][1] * M_PER_FT) for k, _ in pts]   # y up, metres
    enu = [_enu(lat0, lon0, v[0], v[1]) for _, v in pts]
    lc = (sum(p[0] for p in local) / len(local), sum(p[1] for p in local) / len(local))
    ec = (sum(p[0] for p in enu) / len(enu), sum(p[1] for p in enu) / len(enu))
    warnings = []
    scale = None
    if len(pts) >= 2:
        sxx = sxy = 0.0
        for (px, py), (qx, qy) in zip(local, enu):
            px, py, qx, qy = px - lc[0], py - lc[1], qx - ec[0], qy - ec[1]
            sxx += px * qx + py * qy
            sxy += px * qy - py * qx
        phi = math.atan2(sxy, sxx)
        dl = sum(math.hypot(p[0] - lc[0], p[1] - lc[1]) for p in local)
        de = sum(math.hypot(q[0] - ec[0], q[1] - ec[1]) for q in enu)
        scale = de / dl if dl > 0 else None
        if scale is not None and abs(scale - 1) > GEO_WARN_SCALE:
            warnings.append(f"node GPS distances are {scale:.2f}x the floor plan's: check the coordinates and node positions")
        method = "points"
    else:
        phi = -math.radians(float(heading))
        method = "heading"
    c, s = math.cos(phi), math.sin(phi)
    tf = {"lat0": lat0, "lon0": lon0, "c": c, "s": s, "lc": lc, "ec": ec}
    resid = [math.hypot(*(_apply(tf, *p)[i] - q[i] for i in (0, 1))) for p, q in zip(local, enu)]
    rms = math.sqrt(sum(r * r for r in resid) / len(resid))
    if rms > GEO_WARN_RMS_M:
        warnings.append(f"node GPS points disagree with the floor plan by {rms:.1f} m (rms)")
    heading_deg = (-math.degrees(phi)) % 360
    return {**tf, "method": method, "nodes": [k for k, _ in pts], "rms_m": round(rms, 2),
            "scale": None if scale is None else round(scale, 3), "heading_deg": round(heading_deg, 1), "warnings": warnings}


def _apply(tf: dict[str, Any], x_m: float, y_m: float) -> tuple[float, float]:
    """Local metres (y up) -> east/north metres from the transform's origin."""
    x, y = x_m - tf["lc"][0], y_m - tf["lc"][1]
    return tf["ec"][0] + tf["c"] * x - tf["s"] * y, tf["ec"][1] + tf["s"] * x + tf["c"] * y


def to_latlon(tf: dict[str, Any], x_ft: float, y_ft: float) -> tuple[float, float]:
    e, n = _apply(tf, x_ft * M_PER_FT, -y_ft * M_PER_FT)
    k = math.pi / 180 * EARTH_R_M
    return tf["lat0"] + n / k, tf["lon0"] + e / (k * math.cos(math.radians(tf["lat0"])))
