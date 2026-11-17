"""End-to-end checks against rfsim scenarios and their ground truth.

These pin the analytics quality the design commits to (DESIGN.md 14, phase 3)
so regressions are caught as the logic evolves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import cache
from pathlib import Path

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


# -- alerting -----------------------------------------------------------------

RULES = Path(__file__).resolve().parents[1] / "config" / "rules.example.yaml"


@cache
def run_alerts(name: str):
    from rfsim import load_scenario, simulate

    from rfanalytics.alerts import AlertEngine, load_rules

    result = simulate(load_scenario(SIM_ROOT / "scenarios" / f"{name}.yaml"))
    truth = result.truth
    friendly = FriendlyList([FriendlyEntry("safehouse AP", ssid="RELAY-OPS")])
    for actor in truth["actors"]:
        if actor["friendly"]:
            for d in actor["devices"]:
                if d["static_id"]:
                    friendly.add(FriendlyEntry(actor["actor_id"], source="test", emitter_id=d["static_id"]))
    site = load_site(DEV_SITE)
    rules, tz = load_rules(RULES)
    analyzer, engine = Analyzer(site, friendly), AlertEngine(rules, node_ids=sorted(site.nodes), tz=tz)
    messages = []
    for o in result.observations:
        parsed = parse_observation(o.to_dict())
        messages += engine.observe(parsed)
        for e in analyzer.ingest(parsed):
            messages += engine.handle(e)
        messages += engine.tick(parsed.t)
    for e in analyzer.flush():
        messages += engine.handle(e)
    owner = {
        e["emitter_id"]: a["actor_id"] if a["role"] != "background" else "background"
        for a in truth["actors"] for d in a["devices"] for e in d["emitter_ids"]
    }
    return messages, owner


def alert_owners(messages, owner, rule_id):
    return [owner.get(m.body["emitter"]["id"]) for m in messages
            if m.kind == "alert" and m.body["rule_id"] == rule_id]


@pytest.mark.parametrize("name", ["passersby", "baseline-friendly"])
def test_quiet_scenarios_raise_no_alerts(name):
    messages, _ = run_alerts(name)
    assert [m.body["rule_id"] for m in messages if m.kind == "alert"] == []


def test_loiterer_raises_loiter_alert_for_subject_only():
    messages, owner = run_alerts("loiterer")
    owners = alert_owners(messages, owner, "loiter-exterior")
    assert owners and set(owners) == {"subject-1"}
    assert {m.body["location"]["zone"] for m in messages if m.kind == "alert"} == {"north-entrance"}


def test_repeat_vehicle_alerts_and_cues():
    messages, owner = run_alerts("repeat-vehicle-co-travelers")
    repeat = [m for m in messages if m.kind == "alert" and m.body["rule_id"] == "repeat-visitor"]
    assert repeat and {owner[m.body["emitter"]["id"]] for m in repeat} == {"subject-vehicle"}
    assert all(m.body["context"]["visits"] == 3 for m in repeat)
    assert set(alert_owners(messages, owner, "loiter-exterior")) == {"subject-vehicle"}

    cue_times = {m.t for m in messages if m.kind == "camera_cue"
                 and owner.get(m.body["emitter"]["id"]) == "subject-vehicle"}
    arrivals = [datetime(2026, 11, 2, h, m, tzinfo=timezone.utc).timestamp() for h, m in ((8, 10), (13, 35), (21, 45))]
    for arrival in arrivals:
        assert any(0 <= t - arrival <= 60 for t in cue_times), f"no cue within 60 s of {arrival}"
