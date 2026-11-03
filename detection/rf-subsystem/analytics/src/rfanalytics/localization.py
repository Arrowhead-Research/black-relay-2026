"""Zone-level localization from relative RSSI (DESIGN.md 7.3.4, CEN-04).

Per (emitter, node), RSSI is kept in a sliding time window and reduced to a
median, which is robust to the shadowing spikes and dropouts typical of
passive capture. Two estimators are available:

* ``strongest``: zone of the node with the highest calibrated median.
  Confidence grows with the margin over the runner-up node.
* ``centroid``: RSSI-weighted centroid of node positions (weights are linear
  power), mapped to the containing or nearest zone. Confidence is the share
  of total weight held by the strongest node.

Both can only place emitters in or between node zones; zones without a node
(for example the road) are not reachable. That limitation is reported by the
scorer rather than hidden.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median

from rfanalytics.site import Site

METHODS = ("strongest", "centroid")


@dataclass(frozen=True)
class ZoneEstimate:
    zone: str
    confidence: float
    method: str
    nodes: tuple[str, ...]
    position: tuple[float, float] | None = None


class RssiWindow:
    """Sliding window of RSSI samples per node for one emitter."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._samples: dict[str, deque[tuple[float, int]]] = {}

    def add(self, node_id: str, t: float, rssi: int) -> None:
        self._samples.setdefault(node_id, deque()).append((t, rssi))

    def medians(self, now: float) -> dict[str, float]:
        cutoff = now - self.window_s
        out = {}
        for node_id, samples in list(self._samples.items()):
            while samples and samples[0][0] < cutoff:
                samples.popleft()
            if samples:
                out[node_id] = median(r for _, r in samples)
            else:
                del self._samples[node_id]
        return out


def estimate_zone(site: Site, medians: dict[str, float], method: str) -> ZoneEstimate | None:
    calibrated = {
        n: rssi + site.nodes[n].calibration_db for n, rssi in medians.items() if n in site.nodes
    }
    if not calibrated:
        return None
    ranked = sorted(calibrated, key=calibrated.__getitem__, reverse=True)
    nodes = tuple(ranked)

    if method == "strongest":
        best = ranked[0]
        if len(ranked) == 1:
            confidence = 0.3
        else:
            margin = calibrated[best] - calibrated[ranked[1]]
            confidence = min(1.0, 0.3 + margin / 10.0)
        return ZoneEstimate(site.nodes[best].zone, round(confidence, 2), method, nodes)

    if method == "centroid":
        weights = {n: 10 ** (rssi / 10.0) for n, rssi in calibrated.items()}
        total = sum(weights.values())
        x = sum(w * site.nodes[n].position[0] for n, w in weights.items()) / total
        y = sum(w * site.nodes[n].position[1] for n, w in weights.items()) / total
        confidence = max(weights.values()) / total
        return ZoneEstimate(site.zone_for_point((x, y)), round(confidence, 2), method, nodes, (round(x, 1), round(y, 1)))

    raise ValueError(f"unknown localization method {method!r}; choose from {METHODS}")
