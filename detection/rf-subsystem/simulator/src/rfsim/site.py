"""Site geometry: zones and node placement in a local x/y grid (meters)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

Point = tuple[float, float]
OUTSIDE = "outside"


@dataclass(frozen=True)
class Zone:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, p: Point) -> bool:
        return self.x0 <= p[0] <= self.x1 and self.y0 <= p[1] <= self.y1


@dataclass(frozen=True)
class Node:
    node_id: str
    zone: str
    position: Point
    # Per-adapter RSSI bias, to exercise calibration in analytics (DESIGN.md 7.3.4).
    rssi_offset_db: float = 0.0


@dataclass(frozen=True)
class Site:
    name: str
    zones: tuple[Zone, ...]
    nodes: tuple[Node, ...]

    def zone_at(self, p: Point) -> str:
        """Name of the first zone containing p, or OUTSIDE."""
        for zone in self.zones:
            if zone.contains(p):
                return zone.name
        return OUTSIDE


def load_site(path: str | Path) -> Site:
    data = yaml.safe_load(Path(path).read_text())
    zones = []
    for z in data["zones"]:
        x0, y0, x1, y1 = (float(v) for v in z["rect"])
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"zone {z['name']!r}: rect must be [x0, y0, x1, y1] with x1>x0, y1>y0")
        zones.append(Zone(z["name"], x0, y0, x1, y1))
    zone_names = {z.name for z in zones}

    nodes = []
    for n in data["nodes"]:
        if n["zone"] not in zone_names:
            raise ValueError(f"node {n['node_id']!r} references unknown zone {n['zone']!r}")
        x, y = n["position"]
        nodes.append(Node(n["node_id"], n["zone"], (float(x), float(y)), float(n.get("rssi_offset_db", 0.0))))
    if len({n.node_id for n in nodes}) != len(nodes):
        raise ValueError("duplicate node_id in site")

    return Site(data["name"], tuple(zones), tuple(nodes))
