"""End-to-end checks against rfsim scenarios and their ground truth.

These pin the analytics quality the design commits to (DESIGN.md 14, phase 3)
so regressions are caught as the logic evolves.
"""

from __future__ import annotations

from functools import cache

import pytest

from conftest import DEV_SITE, SIM_ROOT
from rfanalytics.analyzer import Analyzer
from rfanalytics.friendly import FriendlyEntry, FriendlyList
from rfanalytics.observation import parse_observation
from rfanalytics.score import score
from rfanalytics.site import load_site


@cache
def run_scenario(name: str):
    from rfsim import load_scenario, simulate

    result = simulate(load_scenario(SIM_ROOT / "scenarios" / f"{name}.yaml"))
    truth = result.truth

    # What operators could realistically register: stable IDs and the site SSID.
    friendly = FriendlyList([FriendlyEntry("safehouse AP", ssid="RELAY-OPS")])
    for actor in truth["actors"]:
        if actor["friendly"]:
            for d in actor["devices"]:
                if d["static_id"]:
                    friendly.add(FriendlyEntry(actor["actor_id"], source="test", emitter_id=d["static_id"]))

    site = load_site(DEV_SITE)
    analyzer = Analyzer(site, friendly)
    events = []
    for o in result.observations:
        events.extend(analyzer.ingest(parse_observation(o.to_dict())))
    events.extend(analyzer.flush())
    dicts = [e.to_dict() for e in events]
    return truth, dicts, score(dicts, truth, site.node_zones)


def test_loiterer_localization_meets_phase3_target():
    _, _, report = run_scenario("loiterer")
    assert report["localization"]["subject"]["accuracy"] >= 0.80
    assert all(v["detected"] for v in report["subject_visits"])


def test_repeat_vehicle_every_visit_detected_and_located():
    _, _, report = run_scenario("repeat-vehicle-co-travelers")
    assert [v["detected"] for v in report["subject_visits"]] == [True] * 4
    assert report["localization"]["subject"]["accuracy"] >= 0.85


def test_repeat_vehicle_tpms_visits_match_real_visits():
    truth, events, _ = run_scenario("repeat-vehicle-co-travelers")
    actor = next(a for a in truth["actors"] if a["actor_id"] == "subject-vehicle")
    tpms_ids = {d["static_id"] for d in actor["devices"] if d["profile"] == "tpms"}
    starts = [e for e in events if e["type"] == "visit_start" and e["emitter_id"] in tpms_ids]
    per_sensor = {}
    for e in starts:
        per_sensor.setdefault(e["emitter_id"], []).append(e["timestamp"])
    # Parking is bridged, so no sensor reports more visits than actually happened.
    assert all(len(v) <= 3 for v in per_sensor.values())
    # Across the four sensors, each real visit is seen.
    hours = {ts[11:13] for ts in (t for v in per_sensor.values() for t in v)}
    assert hours == {"08", "13", "21"}


@pytest.mark.parametrize("name", ["baseline-friendly", "passersby", "loiterer", "repeat-vehicle-co-travelers"])
def test_stable_friendly_ids_never_leak(name):
    _, _, report = run_scenario(name)
    assert report["friendly_leakage"]["stable"] == 0


def test_passersby_never_dwell():
    """Through-traffic must not look like loitering in any zone."""
    truth, events, _ = run_scenario("passersby")
    background = {
        e["emitter_id"]
        for a in truth["actors"] if a["role"] == "background"
        for d in a["devices"] for e in d["emitter_ids"]
    }
    for e in events:
        if e["type"] == "visit_end" and e["emitter_id"] in background:
            assert e["duration_s"] < 120, f"{e['emitter_id']} dwelled {e['duration_s']} s"
