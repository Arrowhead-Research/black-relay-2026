"""Vehicles and co-travelers from visits (baseline.py).

- **Vehicles:** TPMS sensors of the same model whose visits start (or end)
  within VEHICLE_SYNC_S of each other belong to one vehicle: sensors transmit
  while the car moves, so all four burst together on arrival and departure.
  One shared burst is enough; a group larger than VEHICLE_MAX (two cars of the
  same make passing together) is split, keeping only pairs seen together twice.
- **Co-travelers:** two stable devices (not randomized) *arrive together*
  (visit starts within WINDOW_S) or *leave together* (ends within WINDOW_S) on
  at least MIN_TOGETHER different visits, more often than chance: with A's
  visits spread over the observed span, the chance that one of B's visits
  starts or ends within WINDOW_S of one of A's is ~ 2 * 2 * WINDOW_S * nA / span;
  the pair counts if P(Poisson(expected) >= together) < P_MAX. Pairs join into
  groups (a vehicle counts once). Randomized addresses change every few
  minutes and cannot be followed across visits (identity linking: later).

``analyze`` is a pure function of visits, used live (``App.cotravel``) and to
measure precision/recall on the simulator (tests/test_cotravel.py).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from typing import Any

WINDOW_S = 120.0
MIN_TOGETHER = 2
P_MAX = 0.01
VEHICLE_SYNC_S = 30.0
VEHICLE_MAX = 5


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


class _Union:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, a: str) -> str:
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: str, b: str) -> None:
        self.p[self.find(a)] = self.find(b)

    def groups(self, items) -> list[list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for i in items:
            out[self.find(i)].append(i)
        return [sorted(g) for g in out.values()]


def _together(visits: list[tuple[str, float, float]], window: float) -> dict[tuple[str, str], set[tuple[int, int]]]:
    """Pairs of devices with visits starting or ending within ``window``: (a, b) -> {(visit of a, visit of b)}."""
    out: dict[tuple[str, str], set[tuple[int, int]]] = defaultdict(set)
    for key in (1, 2):                       # starts, then ends
        order = sorted(range(len(visits)), key=lambda i: visits[i][key])
        times = [visits[i][key] for i in order]
        for pos, i in enumerate(order):
            t = visits[i][key]
            hi = bisect.bisect_right(times, t + window)
            for j in order[pos + 1:hi]:
                a, b = visits[i][0], visits[j][0]
                if a == b:
                    continue
                pair, vis = ((a, b), (i, j)) if a < b else ((b, a), (j, i))
                out[pair].add(vis)
    return out


def _merge(visits: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """Merge each entity's visits that overlap (or are within VEHICLE_SYNC_S) into one."""
    out: list[tuple[str, float, float]] = []
    last: dict[str, int] = {}
    for d, s, e in sorted(visits, key=lambda v: (v[0], v[1])):
        i = last.get(d)
        if i is not None and s <= out[i][2] + VEHICLE_SYNC_S:
            out[i] = (d, out[i][1], max(out[i][2], e))
        else:
            last[d] = len(out)
            out.append((d, s, e))
    return out


def vehicles(visits: list[tuple[str, float, float]], meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    tpms = [v for v in visits if (meta.get(v[0]) or {}).get("type") == "tpms"]
    pairs = _together(tpms, VEHICLE_SYNC_S)
    model = lambda d: (meta.get(d) or {}).get("name")  # noqa: E731
    counts = {p: len({vi for vi, _ in vis}) for p, vis in pairs.items() if model(p[0]) == model(p[1])}
    out = []
    for need in (1, 2):
        u = _Union()
        for (a, b), n in counts.items():
            if n >= need:
                u.union(a, b)
        groups = u.groups({d for p in counts for d in p})
        if need == 1:
            small = [g for g in groups if 2 <= len(g) <= VEHICLE_MAX]
            big = {d for g in groups if len(g) > VEHICLE_MAX for d in g}
            out += small
            if not big:
                break
            counts = {p: n for p, n in counts.items() if p[0] in big and p[1] in big}
        else:
            out += [g for g in groups if 2 <= len(g) <= VEHICLE_MAX]
    return [{"id": f"vehicle:{g[0].split(':', 1)[-1]}", "sensors": g, "model": model(g[0])} for g in out]


def analyze(visits: list[tuple[str, float, float]], meta: dict[str, dict[str, Any]], span_s: float | None = None) -> dict[str, Any]:
    vehs = vehicles(visits, meta)
    veh_of = {s: v["id"] for v in vehs for s in v["sensors"]}
    stable = [v for v in visits if not (meta.get(v[0]) or {}).get("randomized")]
    # A vehicle is one traveler: its sensors' visits become the vehicle's, overlapping ones merged into one.
    as_entity = _merge([(veh_of.get(d, d), s, e) for d, s, e in stable])
    n_visits: dict[str, int] = defaultdict(int)
    for d, _, _ in as_entity:
        n_visits[d] += 1
    if span_s is None:
        span_s = max((v[2] for v in visits), default=0) - min((v[1] for v in visits), default=0)
    span_s = max(span_s, 1.0)
    pairs = []
    for (a, b), vis in _together(as_entity, WINDOW_S).items():
        together = min(len({i for i, _ in vis}), len({j for _, j in vis}))   # distinct visits on each side
        if together < MIN_TOGETHER:
            continue
        p_one = min(1.0, 2 * 2 * WINDOW_S * n_visits[a] / span_s)
        expected = n_visits[b] * p_one
        p = poisson_sf(together, expected)
        if p < P_MAX:
            pairs.append({"a": a, "b": b, "together": together, "expected": round(expected, 3), "p": p})
    u = _Union()
    for pr in pairs:
        u.union(pr["a"], pr["b"])
    groups = [g for g in u.groups({d for pr in pairs for d in (pr["a"], pr["b"])}) if len(g) >= 2]
    return {"vehicles": vehs, "pairs": sorted(pairs, key=lambda p: p["p"]), "groups": groups}


def partners(result: dict[str, Any]) -> dict[str, set[str]]:
    """Device or vehicle -> its co-travelers (vehicle sensors map to their vehicle's partners)."""
    out: dict[str, set[str]] = defaultdict(set)
    for g in result["groups"]:
        for d in g:
            out[d].update(x for x in g if x != d)
    for v in result["vehicles"]:
        for s in v["sensors"]:
            out[s] = out.get(v["id"], set())
    return dict(out)
