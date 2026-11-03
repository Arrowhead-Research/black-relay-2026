"""Score analytics events against rfsim ground truth.

Reports three things:

* **Localization accuracy:** the estimated zone is sampled every few seconds
  and compared with the true zone of the owning actor. Samples where the
  actor is in a zone without a node are counted as *unreachable* rather
  than wrong, because zone-level estimation cannot place them there (see
  ``localization.py``).
* **Friendly leakage:** emitters belonging to friendly actors that were not
  excluded, split into stable and randomized addresses.
* **Subject visit detection:** whether every visit by a non-friendly actor
  with role ``subject`` produced at least one confirmed analytics visit.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _t(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


@dataclass
class _Owner:
    actor_id: str
    role: str
    friendly: bool
    randomized: bool
    track: list[tuple[float, float, str]]  # (start, end, zone), sorted
    visits: list[tuple[float, float]]


def _index_truth(truth: dict[str, Any]) -> dict[str, _Owner]:
    owners = {}
    for actor in truth["actors"]:
        track = sorted(
            (_t(seg["start"]), _t(seg["end"]), seg["zone"])
            for visit in actor["visits"]
            for seg in visit["zone_track"]
        )
        visits = [(_t(v["start"]), _t(v["end"])) for v in actor["visits"]]
        for device in actor["devices"]:
            for e in device["emitter_ids"]:
                owners[e["emitter_id"]] = _Owner(
                    actor["actor_id"], actor["role"], actor["friendly"], device["randomized"], track, visits
                )
    return owners


def _true_zone(owner: _Owner, t: float, step: float) -> str | None:
    starts = [s for s, _, _ in owner.track]
    i = bisect_right(starts, t) - 1
    if i >= 0:
        start, end, zone = owner.track[i]
        # zone_track is sampled every `step` seconds; tolerate the sampling gap.
        if start <= t <= end + step:
            return zone
    return None


def score(events: list[dict[str, Any]], truth: dict[str, Any], node_zones: set[str] | frozenset[str],
          sample_s: float = 5.0) -> dict[str, Any]:
    owners = _index_truth(truth)
    by_emitter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        by_emitter[ev["emitter_id"]].append(ev)

    loc = defaultdict(lambda: {"correct": 0, "wrong": 0, "unreachable": 0})
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unknown_emitters = 0
    leaked: dict[str, set[str]] = {"stable": set(), "randomized": set()}
    detected_actor_times: dict[str, list[float]] = defaultdict(list)

    for emitter_id, evs in by_emitter.items():
        owner = owners.get(emitter_id)
        if owner is None:
            unknown_emitters += 1
            continue
        if owner.friendly:
            leaked["randomized" if owner.randomized else "stable"].add(emitter_id)

        evs.sort(key=lambda e: _t(e["timestamp"]))
        zone, since = None, 0.0
        for ev in evs:
            t = _t(ev["timestamp"])
            if ev["type"] == "visit_start":
                detected_actor_times[owner.actor_id].append(t)
            if zone is not None and ev["type"] in ("zone_estimate", "visit_end"):
                _sample(loc[owner.role], confusion, owner, zone, since, t, sample_s, node_zones)
            if ev["type"] == "zone_estimate":
                zone, since = ev["zone"], t
            elif ev["type"] == "visit_end":
                zone = None

    localization = {}
    for role, c in sorted(loc.items()):
        scored = c["correct"] + c["wrong"]
        localization[role] = {**c, "accuracy": round(c["correct"] / scored, 3) if scored else None}

    subject_visits = []
    for actor in truth["actors"]:
        if actor["friendly"] or actor["role"] != "subject":
            continue
        for v in actor["visits"]:
            start, end = _t(v["start"]), _t(v["end"])
            hit = any(start - 60 <= t <= end + 60 for t in detected_actor_times.get(actor["actor_id"], []))
            subject_visits.append({"actor_id": actor["actor_id"], "start": v["start"], "detected": hit})

    return {
        "scenario": truth.get("scenario"),
        "localization": localization,
        "confusion": {true: dict(est) for true, est in sorted(confusion.items())},
        "friendly_leakage": {k: len(v) for k, v in leaked.items()},
        "subject_visits": subject_visits,
        "emitters_without_truth": unknown_emitters,
    }


def _sample(counter, confusion, owner: _Owner, zone: str, t0: float, t1: float, step: float, node_zones) -> None:
    t = t0
    while t < t1:
        truth_zone = _true_zone(owner, t, step)
        if truth_zone is not None:
            if truth_zone not in node_zones:
                counter["unreachable"] += 1
            elif truth_zone == zone:
                counter["correct"] += 1
            else:
                counter["wrong"] += 1
            confusion[truth_zone][zone] += 1
        t += step
