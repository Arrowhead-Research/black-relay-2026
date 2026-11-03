"""Site configuration: zones, node placement, and per-node RSSI calibration.

Uses the same YAML layout as ``simulator/sites/``. Analytics reads only
``calibration_db`` for a node's RSSI correction; the simulator's
``rssi_offset_db`` is simulated ground truth and is deliberately ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

Point = tuple[float, float]


@dataclass(frozen=True)
class Zone:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, p: Point) -> bool:
        return self.x0 <= p[0] <= self.x1 and self.y0 <= p[1] <= self.y1

    def distance(self, p: Point) -> float:
        dx = max(self.x0 - p[0], 0.0, p[0] - self.x1)
        dy = max(self.y0 - p[1], 0.0, p[1] - self.y1)
        return math.hypot(dx, dy)


@dataclass(frozen=True)
class Node:
    node_id: str
    zone: str
    position: Point
    calibration_db: float = 0.0  # added to reported RSSI


@dataclass(frozen=True)
class Site:
    name: str
    zones: tuple[Zone, ...]
    nodes: dict[str, Node]

    @property
    def node_zones(self) -> frozenset[str]:
        """Zones that contain a node; the only zones strongest-node can report."""
        return frozenset(n.zone for n in self.nodes.values())

    def zone_for_point(self, p: Point) -> str:
        """Containing zone, else the nearest one."""
        for zone in self.zones:
            if zone.contains(p):
                return zone.name
        return min(self.zones, key=lambda z: z.distance(p)).name


def load_site(path: str | Path) -> Site:
    data = yaml.safe_load(Path(path).read_text())
    zones = tuple(Zone(z["name"], *(float(v) for v in z["rect"])) for z in data["zones"])
    names = {z.name for z in zones}
    nodes = {}
    for n in data["nodes"]:
        if n["zone"] not in names:
            raise ValueError(f"node {n['node_id']!r} references unknown zone {n['zone']!r}")
        if n["node_id"] in nodes:
            raise ValueError(f"duplicate node_id {n['node_id']!r}")
        x, y = n["position"]
        nodes[n["node_id"]] = Node(n["node_id"], n["zone"], (float(x), float(y)), float(n.get("calibration_db", 0.0)))
    return Site(data["name"], zones, nodes)
