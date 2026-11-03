"""Streaming analyzer: friendly exclusion, visits, and zone tracking.

Processing is driven by observation time ("event time"), not wall-clock
time, so the same code handles live MQTT feeds and file replays and gives
identical results for identical input. Input must be roughly time-ordered;
records more than ``settle_s`` out of order are still accepted but may be
assigned to the next estimate.

Noise handling (DESIGN.md section 8, CEN-08):

* A visit is only *confirmed* (and reported) after ``min_transmissions``
  distinct transmissions, which suppresses spurious single decodes.
* Gaps shorter than the per-type visit gap are bridged, so missed packets do
  not split a visit.
* Zone estimates use windowed medians, and a new zone must win
  ``zone_hysteresis`` consecutive estimates before it replaces the current one.
* An emitter silent for longer than ``stale_after_s`` has a *stale* zone: a
  ``zone_estimate`` with ``zone: null`` is emitted and dwell stops accruing
  until it transmits again. Without this, a parked car's TPMS sensors (silent
  while stationary) would keep the zone they were last heard in.
* Observations of one transmission from several nodes are grouped: an
  emitter's zone is re-estimated only once its observations have *settled*
  (no new observation for ``settle_s``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from rfanalytics.events import Event, iso_utc
from rfanalytics.friendly import FriendlyList
from rfanalytics.localization import METHODS, RssiWindow, ZoneEstimate, estimate_zone
from rfanalytics.observation import Observation
from rfanalytics.site import Site

DEFAULT_VISIT_GAP_S = {
    "wifi_ap": 300.0,
    "wifi_client": 300.0,
    "bt_classic": 300.0,
    "ble": 300.0,
    # TPMS is silent while a vehicle is parked; bridge a typical stop.
    "tpms": 1800.0,
}


@dataclass
class AnalyzerConfig:
    method: str = "strongest"
    rssi_window_s: float = 60.0
    stale_after_s: float = 60.0  # silence after which an emitter's zone is unknown
    settle_s: float = 1.0
    zone_hysteresis: int = 2
    min_transmissions: int = 2
    same_transmission_s: float = 0.5  # observations closer than this are one transmission
    visit_gap_s: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_VISIT_GAP_S))
    expiry_check_interval_s: float = 5.0

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"unknown method {self.method!r}; choose from {METHODS}")
        if self.zone_hysteresis < 1 or self.min_transmissions < 1:
            raise ValueError("zone_hysteresis and min_transmissions must be >= 1")


@dataclass
class _Visit:
    emitter_id: str
    emitter_type: str
    window: RssiWindow
    start: float
    last_seen: float
    last_tx: float
    transmissions: int = 1
    observations: int = 1
    confirmed: bool = False
    randomized: bool = False
    nodes: set[str] = field(default_factory=set)
    fresh_since: float = 0.0
    zone: str | None = None
    zone_since: float = 0.0
    estimate: ZoneEstimate | None = None
    candidate: str | None = None
    candidate_count: int = 0
    dwell: Counter = field(default_factory=Counter)
    ever_located: bool = False


@dataclass
class AnalyzerStats:
    observations: int = 0
    friendly_excluded: int = 0
    visits_confirmed: int = 0
    visits_discarded: int = 0  # never reached min_transmissions
    emitters: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "friendly_excluded": self.friendly_excluded,
            "distinct_emitters": len(self.emitters),
            "visits_confirmed": self.visits_confirmed,
            "visits_discarded": self.visits_discarded,
        }


class Analyzer:
    def __init__(self, site: Site, friendly: FriendlyList | None = None, config: AnalyzerConfig | None = None) -> None:
        self.site = site
        self.friendly = friendly or FriendlyList()
        self.config = config or AnalyzerConfig()
        self.stats = AnalyzerStats()
        self._open: dict[str, _Visit] = {}
        self._dirty: set[str] = set()
        self._now = float("-inf")
        self._next_expiry_check = float("-inf")

    # -- public API ---------------------------------------------------------

    def ingest(self, obs: Observation) -> list[Event]:
        """Process one observation; returns any events it caused."""
        events: list[Event] = []
        self._now = max(self._now, obs.t)
        self._settle(events, self._now - self.config.settle_s)
        if self._now >= self._next_expiry_check:
            self._expire(events, self._now)
            self._next_expiry_check = self._now + self.config.expiry_check_interval_s

        self.stats.observations += 1
        if self.friendly.match(obs) is not None:
            self.stats.friendly_excluded += 1
            return events  # dropped before tracking or storage

        visit = self._open.get(obs.emitter_id)
        if visit is not None and obs.t - visit.last_seen > self._gap(visit.emitter_type):
            self._close(events, visit, "gap")
            visit = None
        if visit is None:
            visit = _Visit(
                emitter_id=obs.emitter_id,
                emitter_type=obs.emitter_type,
                window=RssiWindow(self.config.rssi_window_s),
                start=obs.t,
                last_seen=obs.t,
                last_tx=obs.t,
                randomized=obs.randomized,
                fresh_since=obs.t,
            )
            self._open[obs.emitter_id] = visit
            self.stats.emitters.add(obs.emitter_id)
        else:
            visit.observations += 1
            if obs.t - visit.last_seen > self.config.stale_after_s:
                # Periodic expiry may not have run since the emitter went quiet.
                if visit.zone is not None:
                    self._mark_stale(events, visit)
                visit.fresh_since = obs.t
            if obs.t - visit.last_tx > self.config.same_transmission_s:
                visit.transmissions += 1
                visit.last_tx = obs.t
            visit.last_seen = max(visit.last_seen, obs.t)

        visit.window.add(obs.node_id, obs.t, obs.rssi_dbm)
        visit.nodes.add(obs.node_id)
        self._dirty.add(obs.emitter_id)

        if not visit.confirmed and visit.transmissions >= self.config.min_transmissions:
            visit.confirmed = True
            self.stats.visits_confirmed += 1
            events.append(Event("visit_start", visit.start, visit.emitter_id, visit.emitter_type,
                                {"randomized_address": visit.randomized}))
            if visit.zone is not None:
                events.append(self._zone_event(visit, visit.zone_since))
        return events

    def flush(self) -> list[Event]:
        """Settle all pending estimates and close every open visit (end of input)."""
        events: list[Event] = []
        self._settle(events, float("inf"))
        for visit in list(self._open.values()):
            self._close(events, visit, "end_of_input")
        return events

    def open_visits(self) -> list[dict[str, Any]]:
        """Snapshot of confirmed open visits (for a live view)."""
        return [
            {
                "emitter_id": v.emitter_id,
                "emitter_type": v.emitter_type,
                "since": iso_utc(v.start),
                "last_seen": iso_utc(v.last_seen),
                "zone": v.zone,
                "confidence": v.estimate.confidence if v.estimate else None,
            }
            for v in self._open.values()
            if v.confirmed
        ]

    # -- internals ----------------------------------------------------------

    def _gap(self, emitter_type: str) -> float:
        return self.config.visit_gap_s.get(emitter_type, 300.0)

    def _settle(self, events: list[Event], horizon: float) -> None:
        for emitter_id in list(self._dirty):
            visit = self._open.get(emitter_id)
            if visit is None:
                self._dirty.discard(emitter_id)
                continue
            if visit.last_seen <= horizon:
                self._dirty.discard(emitter_id)
                self._update_zone(events, visit)

    def _update_zone(self, events: list[Event], visit: _Visit) -> None:
        est = estimate_zone(self.site, visit.window.medians(visit.last_seen), self.config.method)
        if est is None:
            return
        t = visit.last_seen
        if visit.zone is None:
            # First fix of the visit covers it from the start; a fix after a
            # stale period starts at the first fresh observation.
            since = visit.start if not visit.ever_located else visit.fresh_since
            visit.zone, visit.zone_since, visit.estimate = est.zone, since, est
            visit.ever_located = True
            if visit.confirmed:
                events.append(self._zone_event(visit, visit.zone_since))
            return
        if est.zone == visit.zone:
            visit.estimate = est
            visit.candidate, visit.candidate_count = None, 0
            return
        if est.zone == visit.candidate:
            visit.candidate_count += 1
        else:
            visit.candidate, visit.candidate_count = est.zone, 1
        if visit.candidate_count >= self.config.zone_hysteresis:
            visit.dwell[visit.zone] += t - visit.zone_since
            visit.zone, visit.zone_since, visit.estimate = est.zone, t, est
            visit.candidate, visit.candidate_count = None, 0
            if visit.confirmed:
                events.append(self._zone_event(visit, t))

    def _zone_event(self, visit: _Visit, t: float) -> Event:
        est = visit.estimate
        assert est is not None and visit.zone is not None
        data: dict[str, Any] = {
            "zone": visit.zone,
            "confidence": est.confidence,
            "method": est.method,
            "nodes": list(est.nodes),
        }
        if est.position is not None:
            data["position"] = list(est.position)
        return Event("zone_estimate", t, visit.emitter_id, visit.emitter_type, data)

    def _expire(self, events: list[Event], now: float) -> None:
        for visit in list(self._open.values()):
            silent = now - visit.last_seen
            if silent > self._gap(visit.emitter_type):
                self._close(events, visit, "gap")
            elif silent > self.config.stale_after_s and visit.zone is not None:
                self._mark_stale(events, visit)

    def _mark_stale(self, events: list[Event], visit: _Visit) -> None:
        visit.dwell[visit.zone] += visit.last_seen - visit.zone_since
        visit.zone, visit.estimate = None, None
        visit.candidate, visit.candidate_count = None, 0
        if visit.confirmed:
            events.append(Event("zone_estimate", visit.last_seen, visit.emitter_id, visit.emitter_type,
                                {"zone": None, "reason": "stale"}))

    def _close(self, events: list[Event], visit: _Visit, reason: str) -> None:
        if visit.emitter_id in self._dirty:
            self._dirty.discard(visit.emitter_id)
            self._update_zone(events, visit)
        del self._open[visit.emitter_id]
        if not visit.confirmed:
            self.stats.visits_discarded += 1
            return
        if visit.zone is not None:
            visit.dwell[visit.zone] += visit.last_seen - visit.zone_since
        events.append(Event("visit_end", visit.last_seen, visit.emitter_id, visit.emitter_type, {
            "reason": reason,
            "start": iso_utc(visit.start),
            "duration_s": round(visit.last_seen - visit.start, 1),
            "transmissions": visit.transmissions,
            "observations": visit.observations,
            "nodes": sorted(visit.nodes),
            "dwell_s": {z: round(s, 1) for z, s in visit.dwell.most_common()},
            "randomized_address": visit.randomized,
        }))
