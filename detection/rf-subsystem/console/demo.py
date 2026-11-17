"""Synthetic emitters for rf-dfviz (DFVIZ_DEMO=1).

Produces Kismet-shaped device JSON (with per-node ``signal_rrd`` second
histories) for a few emitters with known positions, so Bluetooth, TPMS and
the smoothing can be checked when the real radios are quiet. Each device
carries ``dfviz.truth`` = [x, y] ft, which the page draws as a hollow marker.

RSSI follows the same log-distance model as the page, plus Gaussian noise
and occasional multipath dropouts, so the estimate jitters realistically.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

FT_PER_M = 3.28084
SENSITIVITY_DBM = -97


@dataclass
class Emitter:
    mac: str
    phy: str
    kind: str
    name: str
    p0: float                     # RSSI at 1 m
    n: float                      # path-loss exponent
    sigma: float                  # noise, dB
    position: Callable[[float], tuple[float, float] | None]  # t -> ft, or None when absent
    transmits: Callable[[float], bool]                       # t (whole seconds) -> sends this second?
    extra: dict[str, Any] = field(default_factory=dict)


def _loop(waypoints: list[tuple[float, float]], speed_ft_s: float) -> Callable[[float], tuple[float, float]]:
    legs = list(zip(waypoints, waypoints[1:] + waypoints[:1]))
    lengths = [math.dist(a, b) for a, b in legs]
    total = sum(lengths)

    def pos(t: float) -> tuple[float, float]:
        d = (t * speed_ft_s) % total
        for (a, b), length in zip(legs, lengths):
            if d <= length:
                f = d / length
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            d -= length
        return waypoints[0]

    return pos


def default_emitters(nodes: dict[str, list[float]]) -> list[Emitter]:
    xs = [p[0] for p in nodes.values()]
    ys = [p[1] for p in nodes.values()]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    walk = [(x0 + 0.25 * w, y0 + 0.15 * h), (x0 + 0.75 * w, y0 + 0.15 * h),
            (x0 + 0.75 * w, y0 + 0.85 * h), (x0 + 0.25 * w, y0 + 0.85 * h)]
    road_y = y1 + 0.4 * h
    period, speed = 120.0, 25.0  # a car every 2 minutes at ~17 mph
    span_from, span_to = x0 - 2 * w, x1 + 2 * w

    def car(dx: float, dy: float) -> Callable[[float], tuple[float, float] | None]:
        def pos(t: float):
            x = span_from + (t % period) * speed
            return (x + dx, road_y + dy) if x <= span_to else None
        return pos

    emitters = [
        Emitter("a4:de:00:00:00:01", "Bluetooth", "BTLE", "demo walker (BLE)", -59, 2.5, 6.0,
                _loop(walk, 2.5), lambda t: True),
        Emitter("a4:de:00:00:00:02", "IEEE802.11", "Wi-Fi Client", "demo laptop (Wi-Fi, still)", -40, 2.7, 5.0,
                lambda t: (cx + 0.1 * w, cy + 0.1 * h), lambda t: int(t) % 2 == 0),
        Emitter("a4:de:00:00:00:03", "Bluetooth", "BR/EDR", "demo headset (BT classic, still)", -55, 2.5, 6.0,
                lambda t: (x0 - 0.3 * w, y0 + 0.3 * h), lambda t: int(t) % 3 == 0),
    ]
    # A beacon that stands at one known spot for 45 s, then hops to the next:
    # for practising the live calibration workflow (its truth is the spot).
    hops = [(x0 + 0.5 * w, y0 + 0.25 * h), (x0 + 0.1 * w, y0 + 0.5 * h), (x0 + 0.9 * w, y0 + 0.6 * h),
            (x0 + 0.5 * w, y0 + 0.9 * h), (x0 - 0.4 * w, y0 + 0.2 * h), (x1 + 0.4 * w, y0 + 0.8 * h)]
    emitters.append(Emitter("a4:de:00:00:00:04", "Bluetooth", "BTLE", "demo calibration beacon (BLE, hops every 45 s)",
                            -59, 2.5, 6.0, lambda t: hops[int(t // 45) % len(hops)], lambda t: True))
    for i, (dx, dy) in enumerate([(-4, -3), (4, -3), (-4, 3), (4, 3)], start=1):
        emitters.append(Emitter(
            f"00:00:00:00:00:0{i}", "RTL433", "sensor", f"demo car TPMS {i}", -45, 2.2, 7.0,
            car(dx, dy), (lambda k: lambda t: int(t) % 4 == k % 4)(i),
            extra={"rtl433.device": {"rtl433.device.id": 0xDE000000 + i, "rtl433.device.model": "Demo-TPMS"}},
        ))
    return emitters


class DemoSource:
    """Same interface as KismetPoller.fetch_devices."""

    def __init__(self, layout: dict[str, Any], seed: int = 7, emitters: list[Emitter] | None = None) -> None:
        self.nodes = layout["nodes"]
        self.emitters = emitters or default_emitters(self.nodes)
        self.rng = random.Random(seed)
        # (mac, node) -> {second: rssi}
        self.history: dict[tuple[str, str], dict[int, int]] = {}
        self.generated_to: int | None = None

    def _generate(self, now: int) -> None:
        start = now - 60 if self.generated_to is None else self.generated_to + 1
        for t in range(max(start, now - 60), now + 1):
            for e in self.emitters:
                pos = e.position(t)
                if pos is None or not e.transmits(t):
                    continue
                for node, (nx, ny) in self.nodes.items():
                    d_m = max(math.dist(pos, (nx, ny)) / FT_PER_M, 0.3)
                    rssi = e.p0 - 10 * e.n * math.log10(d_m) + self.rng.gauss(0, e.sigma)
                    if self.rng.random() < 0.05:
                        rssi -= self.rng.uniform(10, 20)  # occasional multipath fade
                    if rssi >= SENSITIVITY_DBM:
                        self.history.setdefault((e.mac, node), {})[t] = int(round(min(rssi, -5)))
        self.generated_to = now
        for key, h in self.history.items():
            for sec in [s for s in h if s <= now - 60]:
                del h[sec]

    def fetch_devices(self, since: float | None = None) -> list[dict[str, Any]]:
        import time

        now = int(time.time())
        self._generate(now)
        out = []
        for e in self.emitters:
            seenby = []
            for node in self.nodes:
                h = self.history.get((e.mac, node)) or {}
                if not h:
                    continue
                last = max(h)
                vec = [0] * 60
                for sec, v in h.items():
                    vec[sec % 60] = v
                seenby.append({
                    "kismet.common.seenby.last_time": last,
                    "kismet.common.seenby.source": {"kismet.datasource.name": f"{node}:demo"},
                    "kismet.common.seenby.signal": {
                        "kismet.common.signal.type": "dbm",
                        "kismet.common.signal.last_signal": h[last],
                        "kismet.common.signal.signal_rrd": {
                            "kismet.common.rrd.last_time": last,
                            "kismet.common.rrd.minute_vec": vec,
                            "kismet.common.rrd.blank_val": 0,
                        },
                    },
                })
            if not seenby:
                continue
            pos = e.position(now)
            out.append({
                "kismet.device.base.macaddr": e.mac.upper(),
                "kismet.device.base.phyname": e.phy,
                "kismet.device.base.type": e.kind,
                "kismet.device.base.name": e.name,
                "kismet.device.base.manuf": "Demo",
                "kismet.device.base.last_time": max(sb["kismet.common.seenby.last_time"] for sb in seenby),
                "kismet.device.base.seenby": seenby,
                "dfviz.truth": [round(pos[0], 2), round(pos[1], 2)] if pos else None,
                **e.extra,
            })
        return out
