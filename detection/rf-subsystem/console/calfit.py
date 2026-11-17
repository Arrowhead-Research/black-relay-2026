"""Server-side calibration fit and model selection.

A port of the page's calibration maths (static/calib.js ``calFit``,
``calLocate``, ``calLeaveOneOut``, ``calBest``), so the model in use is chosen
on the server for every site, with no page open. Verified against the page
code: tests/parity/cal_golden.json (page code run under node by
make_cal_golden.mjs).

Per band, every candidate (node links on/off x per-node offsets on/off x wall
loss on/off x transmit-power spread 4/8/16 dB) is fitted by weighted least
squares on the recorded spots (plus node links) and scored by its
leave-one-out median error at the walked spots. Candidates with implausible
parameters are rejected; the best one is adopted if it beats the layout
defaults at the same spots (``best_model``). ``FitManager`` saves the result as
the band's fit whenever the inputs change.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np

from locate import FT_PER_M, LOCAL_MEAN_FT, Grid, _js_num, walls_expected

LN10 = math.log(10)
CAL_N_RANGE = (1.5, 5)
CAL_MAX_SIGMA = 12
CAL_MIN_LOO = 5              # spots needed in the error estimate before a fit replaces the defaults
CAL_TX_SPREADS = (4, 8, 16)  # transmit-power uncertainty candidates, dB


def fixed(x: float, digits: int) -> float:
    """JavaScript's +x.toFixed(digits) (half away from zero on the exact value)."""
    return float(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def median(v: list[float]) -> float:
    s = sorted(v)
    m = len(s) >> 1
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def solve(A: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    M = [list(r) + [b[i]] for i, r in enumerate(A)]
    for c in range(n):
        p = c
        for r in range(c + 1, n):
            if abs(M[r][c]) > abs(M[p][c]):
                p = r
        if abs(M[p][c]) < 1e-9:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r == c:
                continue
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


class Calibrator:
    def __init__(self, nodes: dict[str, list[float]], walls: list[dict[str, Any]], models: dict[str, dict[str, Any]],
                 node_sd_ft: float = 2.0, grid: Grid | None = None) -> None:
        self.nodes, self.walls, self.models, self.node_sd = nodes, walls, models, node_sd_ft
        self.grid = grid or Grid(nodes, walls)
        self._wall_cache: dict[tuple[float, float, str], float] = {}

    # ---------- fit ----------
    def _walls(self, x: float, y: float, k: str) -> float:
        key = (x, y, k)
        if key not in self._wall_cache:
            nx, ny = self.nodes[k]
            self._wall_cache[key] = float(walls_expected(x, y, nx, ny, self.walls)) if self.walls else 0.0
        return self._wall_cache[key]

    def pos_var(self, n: float, d_m: float, spot_sd: float) -> float:
        g = (10 * max(n, 0.5)) / (LN10 * max(d_m, 0.3))
        return g * g * ((spot_sd * spot_sd + self.node_sd * self.node_sd) / (FT_PER_M * FT_PER_M))

    def rows(self, spots: list[dict[str, Any]], cols: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for p in spots:
            for k, v in p["rssi"].items():
                if k not in self.nodes:
                    continue
                d_m = max(math.hypot(p["x"] - self.nodes[k][0], p["y"] - self.nodes[k][1]) / FT_PER_M, 0.3)
                x = [0.0] * cols["P"]
                x[0] = -10 * math.log10(d_m)
                if p["device"] in cols["dev"]:
                    x[cols["dev"][p["device"]]] = 1
                if k in cols["off"]:
                    x[cols["off"][k]] = 1
                if cols["wall"] is not None:
                    x[cols["wall"]] = -self._walls(p["x"], p["y"], k)
                sd = p["pos_sd_ft"] if p.get("pos_sd_ft") is not None else 2
                out.append({"x": x, "y": v, "dM": d_m, "sd": sd,
                            "crossings": -x[cols["wall"]] if cols["wall"] is not None else 0})
        return out

    @staticmethod
    def normal(rows: list[dict[str, Any]], cols: dict[str, Any], weights: list[float] | None):
        P = cols["P"]
        A = [[0.0] * P for _ in range(P)]
        b = [0.0] * P
        for i, r in enumerate(rows):
            w = weights[i] if weights else 1
            x = r["x"]
            for a in range(P):
                if not x[a]:
                    continue
                b[a] += w * x[a] * r["y"]
                for c in range(P):
                    A[a][c] += w * x[a] * x[c]
        off = list(cols["off"].values())
        for a in off:
            for c in off:
                A[a][c] += 100   # offsets average zero
        return A, b

    def fit(self, spots: list[dict[str, Any]], opts: dict[str, Any]) -> dict[str, Any] | None:
        with_offsets = bool(opts.get("offsets"))
        nodes = [k for k in dict.fromkeys(k for p in spots for k in p["rssi"]) if k in self.nodes]
        devices = list(dict.fromkeys(p["device"] for p in spots))
        cols: dict[str, Any] = {"dev": {}, "off": {}, "wall": None, "P": 1}
        for d in devices:
            cols["dev"][d] = cols["P"]
            cols["P"] += 1
        if with_offsets:
            for k in nodes:
                cols["off"][k] = cols["P"]
                cols["P"] += 1
        if opts.get("walls"):
            cols["wall"] = cols["P"]
            cols["P"] += 1
        rows = self.rows(spots, cols)
        if opts.get("walls"):   # W is only identifiable when paths cross different numbers of walls
            c = [r["crossings"] for r in rows]
            if not c:            # the page gets NaN here, which also means "no fit"
                return None
            m = sum(c) / len(c)
            if not any(v > 0.1 for v in c) or sum((v - m) ** 2 for v in c) / len(c) < 0.05:
                return None
        P = cols["P"]
        if len(rows) < P + 2:
            return None
        xs = [r["x"][0] for r in rows]
        mx = sum(xs) / len(xs)
        if sum((v - mx) ** 2 for v in xs) / len(xs) < 0.5:
            return None
        A, b = self.normal(rows, cols, None)
        theta = solve(A, b)
        if theta is None:
            return None
        weights, sigma0sq = None, None
        predict = lambda r, th: sum(v * th[j] for j, v in enumerate(r["x"]))  # noqa: E731
        for _ in range(3):
            pv = [self.pos_var(theta[0], r["dM"], r["sd"]) for r in rows]
            res = [r["y"] - predict(r, theta) for r in rows]
            acc = 0.0
            for i, e in enumerate(res):
                acc = acc + e * e - pv[i]
            sigma0sq = max(1, acc / max(1, len(rows) - P))
            weights = [1 / (sigma0sq + v) for v in pv]
            A, b = self.normal(rows, cols, weights)
            t = solve(A, b)
            if t is None:
                break
            theta = t
        sse = chi2 = 0.0
        for i, r in enumerate(rows):
            e = r["y"] - predict(r, theta)
            sse += e * e
            chi2 += (weights[i] if weights else 1) * e * e
        dof = max(1, len(rows) - P)
        scale = max(1, chi2 / dof)

        def se(idx: int) -> float | None:
            e = [0.0] * P
            e[idx] = 1
            col = solve(A, e)
            return math.sqrt(max(0.0, col[idx] * scale)) if col else None

        n_se = se(0)
        wall_se = se(cols["wall"]) if cols["wall"] is not None else None
        p0_by_device = {d: fixed(theta[cols["dev"][d]], 1) for d in devices}
        offsets, offset_se = {}, {}
        for k, i in cols["off"].items():
            offsets[k] = fixed(theta[i], 2)
            v = se(i)
            offset_se[k] = None if v is None else fixed(v, 2)
        return {
            "n": fixed(theta[0], 2), "nSe": None if n_se is None else fixed(n_se, 2),
            "p0": fixed(median(list(p0_by_device.values())), 1), "p0ByDevice": p0_by_device,
            "sigma": fixed(math.sqrt(sse / dof), 2),
            "sigma0": fixed(math.sqrt(sigma0sq or sse / dof), 2),
            "wall": fixed(theta[cols["wall"]], 2) if cols["wall"] is not None else 0,
            "wallSe": None if wall_se is None else fixed(wall_se, 2),
            "offsetsFitted": with_offsets, "wallsFitted": cols["wall"] is not None, "txSpread": opts.get("txSpread") or 8,
            "offsets": offsets, "offsetSe": offset_se, "measurements": len(rows), "devices": len(devices),
        }

    # ---------- locate one spot (single shot) ----------
    def locate(self, rssi: dict[str, float], fit: dict[str, Any], device: str | None) -> tuple[float, float] | None:
        g = self.grid
        ids = [k for k in rssi if k in g.logd]
        if len(ids) < 2:
            return None
        byd = fit.get("p0ByDevice") or {}
        p0 = byd[device] if byd.get(device) is not None else fit["p0"]
        K, ten_n = len(ids), 10 * fit["n"]
        spread = fit.get("txSpread") or 8
        div = K + (fit["sigma"] * fit["sigma"]) / (spread * spread)
        offs = fit.get("offsets") or {}
        W = fit.get("wall") or 0
        s = np.zeros(g.nx * g.ny)
        s2 = np.zeros(g.nx * g.ny)
        for k in ids:
            e = (rssi[k] - p0 - (offs.get(k) or 0)) + ten_n * g.logd[k].astype(float)
            if W and k in g.walls:
                e = e + W * g.walls[k].astype(float)
            s += e
            s2 += e * e
        v = s2 - (s * s) / div
        best = int(np.argmin(v))
        min_c = float(v[best])
        cost = v.astype(np.float32)
        inv = 1 / (2 * max(fit["sigma"], 1) ** 2)
        bi, bj = best % g.nx, best // g.nx
        ri = math.ceil(LOCAL_MEAN_FT / g.cell)
        j0, j1, i0, i1 = max(0, bj - ri), min(g.ny - 1, bj + ri), max(0, bi - ri), min(g.nx - 1, bi + ri)
        win = cost.reshape(g.ny, g.nx)[j0:j1 + 1, i0:i1 + 1].astype(float)
        w = np.exp(-(win - min_c) * inv)
        xs = g.x0 + (np.arange(i0, i1 + 1) + 0.5) * g.cell
        ys = g.y0 + (np.arange(j0, j1 + 1) + 0.5) * g.cell
        ls = w.sum()
        return float((w * xs[None, :]).sum() / ls), float((w * ys[:, None]).sum() / ls)

    def leave_one_out(self, spots: list[dict[str, Any]], opts: dict[str, Any], extra: list[dict[str, Any]]):
        out = []
        for i, p in enumerate(spots):
            fit = self.fit([q for j, q in enumerate(spots) if j != i] + extra, opts)
            if not fit:
                continue
            est = self.locate(p["rssi"], fit, p["device"])
            if est:
                out.append({"id": p["id"], "x": est[0], "y": est[1], "err": math.hypot(est[0] - p["x"], est[1] - p["y"])})
        return out

    # ---------- model selection ----------
    def links_as_spots(self, band: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if band != "wifi":
            return []
        return [{"id": f"link:{l['node']}", "kind": "link", "band": band, "device": l["device"],
                 "x": self.nodes[l["node"]][0], "y": self.nodes[l["node"]][1], "pos_sd_ft": self.node_sd, "rssi": l["rssi"]}
                for l in links if l["node"] in self.nodes]

    def best_model(self, band: str, points: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
        link_spots = self.links_as_spots(band, links)
        linked = {l["device"] for l in link_spots}
        spots = [p for p in points if p.get("band") == band and p.get("device") not in linked]
        dm = self.models[band]
        default = {"n": dm["path_loss_exp"], "p0": dm["ref_rssi_1m"], "sigma": dm["sigma_db"], "offsets": {}, "txSpread": 8,
                   "wall": (dm.get("wall_db") or 0) if self.walls else 0}
        def_errs = []
        for p in spots:
            e = self.locate(p["rssi"], default, p.get("device"))
            if e:
                def_errs.append(math.hypot(e[0] - p["x"], e[1] - p["y"]))
        def_errs.sort()
        candidates = []
        for use_links in ([False, True] if link_spots else [False]):
            for offsets in (False, True):
                for walls in ((False, True) if self.walls else (False,)):
                    for tx in CAL_TX_SPREADS:
                        opts = {"offsets": offsets, "walls": walls, "txSpread": tx, "links": use_links}
                        extra = link_spots if use_links else []
                        fit = self.fit(spots + extra, opts)
                        if not fit:
                            continue
                        fit["txSpread"] = tx
                        fit["nodeLinks"] = len(extra) if use_links else 0
                        loo = self.leave_one_out(spots, opts, extra)
                        errs = sorted(l["err"] for l in loo)
                        problems = []
                        if fit["n"] < CAL_N_RANGE[0] or fit["n"] > CAL_N_RANGE[1]:
                            problems.append(f"n = {_js_num(fit['n'])} outside {_js_num(CAL_N_RANGE[0])}–{_js_num(CAL_N_RANGE[1])}")
                        if fit["sigma"] > CAL_MAX_SIGMA:
                            problems.append(f"σ = {_js_num(fit['sigma'])} dB")
                        if walls and fit["wall"] < 0:
                            problems.append(f"negative wall loss ({_js_num(fit['wall'])} dB)")
                        candidates.append({"opts": opts, "fit": fit, "errs": errs, "problems": problems,
                                           "median": median(errs) if errs else None,
                                           "params": 1 + fit["devices"] + (len(self.nodes) if offsets else 0) + (1 if walls else 0)})
        ok = [c for c in candidates if not c["problems"] and c["median"] is not None and len(c["errs"]) >= CAL_MIN_LOO]
        ok.sort(key=lambda c: (c["median"], c["params"]))
        def_median = median(def_errs) if def_errs else None
        best = ok[0] if ok else None
        adopted = bool(best and def_median is not None and best["median"] < def_median)
        reason = ("no spots" if not spots else "too few readings" if not candidates else "too few spots" if not ok
                  else "fit not better" if not adopted else None)
        return {"band": band, "spots": spots, "links": link_spots, "candidates": candidates, "best": best,
                "adopted": adopted, "reason": reason, "defMedian": def_median, "default": default}


def fit_signature(band: str, points: list[dict[str, Any]], links: list[dict[str, Any]], nodes: dict[str, Any],
                  walls: list[dict[str, Any]], models: dict[str, Any], node_sd: float) -> str:
    spots = [[p.get("id"), p.get("device"), p.get("x"), p.get("y"), p.get("pos_sd_ft"), p.get("rssi")]
             for p in points if p.get("band") == band]
    key = [band, spots, [[l["node"], l["device"], l["rssi"]] for l in links] if band == "wifi" else [],
           nodes, [{k: w.get(k) for k in ("x1", "y1", "x2", "y2", "sd_ft")} for w in walls], models.get(band), node_sd]
    return "srv:" + hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def fit_to_save(r: dict[str, Any], sig: str) -> dict[str, Any] | None:
    """The saved fit for a band (what the page and the locator read), or None for the defaults."""
    if not r["adopted"]:
        return None
    best = r["best"]
    return {**best["fit"], "sig": sig, "t": time.time(), "spots": len(r["spots"]),
            "loo_median_ft": best["median"], "loo_worst_ft": best["errs"][-1], "loo_spots": len(best["errs"]),
            "default_median_ft": r["defMedian"], "candidates": len(r["candidates"]), "by": "server"}
