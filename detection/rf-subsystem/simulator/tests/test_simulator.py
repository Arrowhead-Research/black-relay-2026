"""Behavioral and contract tests for the simulator.

These double as the first business-rule fixtures for rf-analytics: if the
simulator stops producing the properties asserted here, analytics tests built
on its scenarios become meaningless.
"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import cache
from pathlib import Path

import jsonschema
import pytest

from rfsim import load_scenario, simulate
from rfsim.cli import main as cli_main
from rfsim.devices import build_kit
from rfsim.scenario import Actor

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = sorted((ROOT / "scenarios").glob("*.yaml"))
SCHEMA = json.loads((ROOT.parent / "schemas" / "observation.v0.schema.json").read_text())


@cache
def run(name: str):
    return simulate(load_scenario(ROOT / "scenarios" / f"{name}.yaml"))


def actor_truth(result, actor_id: str) -> dict:
    return next(a for a in result.truth["actors"] if a["actor_id"] == actor_id)


def device_ids(result, actor_id: str, profile: str | None = None) -> set[str]:
    return {
        e["emitter_id"]
        for d in actor_truth(result, actor_id)["devices"]
        if profile is None or d["profile"] == profile
        for e in d["emitter_ids"]
    }


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_produces_schema_valid_observations(path):
    result = run(path.stem)
    assert result.observations, "scenario produced no observations"
    validator = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())
    for obs in result.observations:
        validator.validate(obs.to_dict())
    timestamps = [o.timestamp for o in result.observations]
    assert timestamps == sorted(timestamps)


def test_same_seed_is_deterministic():
    path = ROOT / "scenarios" / "loiterer.yaml"
    a = [o.to_dict() for o in simulate(load_scenario(path)).observations]
    b = [o.to_dict() for o in simulate(load_scenario(path)).observations]
    assert a == b


def test_different_seed_changes_output():
    path = ROOT / "scenarios" / "loiterer.yaml"
    a = simulate(load_scenario(path, seed=1)).observations
    b = simulate(load_scenario(path, seed=2)).observations
    assert [o.to_dict() for o in a] != [o.to_dict() for o in b]


def test_phone_wifi_address_rotates_but_tpms_does_not():
    result = run("baseline-friendly")
    team1_wifi = device_ids(result, "team-1", "phone_wifi")
    assert len(team1_wifi) >= 2, "30 min with 15 min rotation should show at least two addresses"
    assert all(int(mac[:2], 16) & 0x02 for mac in team1_wifi), "randomized MACs must be locally administered"

    for d in actor_truth(result, "team-vehicle")["devices"]:
        if d["profile"] == "tpms":
            assert len(d["emitter_ids"]) <= 1
            assert d["static_id"].startswith("tpms:")


def test_tpms_only_heard_while_moving():
    result = run("baseline-friendly")
    tpms_ids = device_ids(result, "team-vehicle", "tpms")
    assert tpms_ids, "team vehicle drives at 25:00 and should be heard"
    scenario = load_scenario(ROOT / "scenarios" / "baseline-friendly.yaml")
    start = scenario.start_time
    for obs in result.observations:
        if obs.emitter_id in tpms_ids:
            t = (obs.timestamp - start).total_seconds()
            assert 25 * 60 <= t <= 25 * 60 + 20, f"TPMS heard while parked at t={t}"


def test_friendly_emitter_ids_cover_team_but_not_neighbors():
    result = run("baseline-friendly")
    friendly = set(result.truth["friendly_emitter_ids"])
    assert device_ids(result, "team-1") <= friendly
    assert device_ids(result, "safehouse-ap") <= friendly
    assert not (device_ids(result, "neighbor-ap") & friendly)


def test_loiterer_ground_truth_dwell_and_zones():
    result = run("loiterer")
    track = actor_truth(result, "subject-1")["visits"][0]["zone_track"]
    dwell = defaultdict(float)
    for seg in track:
        dwell[seg["zone"]] += seg["dwell_s"]
    assert dwell["north-entrance"] >= 15 * 60
    assert 4 * 60 <= dwell["east-lot"] <= 6 * 60


def test_loiterer_strongest_node_tracks_subject():
    """Smoothed strongest-node localization should mostly agree with truth."""
    result = run("loiterer")
    ids = device_ids(result, "subject-1")
    scenario = load_scenario(ROOT / "scenarios" / "loiterer.yaml")
    t0 = scenario.start_time

    def strongest_node(window_start: float, window_end: float) -> str | None:
        per_node = defaultdict(list)
        for o in result.observations:
            t = (o.timestamp - t0).total_seconds()
            if o.emitter_id in ids and window_start <= t < window_end:
                per_node[o.node_id].append(o.rssi_dbm)
        if not per_node:
            return None
        return max(per_node, key=lambda n: sorted(per_node[n])[len(per_node[n]) // 2])

    # Middle of the north-entrance loiter and of the east-lot stop.
    assert strongest_node(14 * 60, 22 * 60) == "rf-node-01"
    assert strongest_node(27 * 60, 31 * 60) == "rf-node-02"


def test_repeat_vehicle_tpms_heard_on_every_visit():
    result = run("repeat-vehicle-co-travelers")
    tpms_ids = device_ids(result, "subject-vehicle", "tpms")
    assert len(tpms_ids) == 4
    visits = actor_truth(result, "subject-vehicle")["visits"]
    for visit in visits:
        heard = {
            o.emitter_id
            for o in result.observations
            if o.emitter_id in tpms_ids and visit["start"] <= o.to_dict()["timestamp"] <= visit["end"]
        }
        assert heard, f"no TPMS heard during visit starting {visit['start']}"


def test_background_actors_never_dwell():
    result = run("passersby")
    for actor in result.truth["actors"]:
        if actor["role"] != "background":
            continue
        for visit in actor["visits"]:
            for seg in visit["zone_track"]:
                assert seg["zone"] in ("north-road", "outside")


def test_actor_position_and_motion():
    actor = Actor("a", "subject", False, [[(0, (0, 0)), (10, (10, 0)), (20, (10, 0))]])
    assert actor.position(5) == (5, 0)
    assert actor.position(25) is None
    assert actor.is_moving(0) and actor.is_moving(5)
    assert not actor.is_moving(10) and not actor.is_moving(15)
    assert actor.motion_starts() == [0]


def test_unknown_kit_is_rejected():
    import random

    with pytest.raises(ValueError, match="unknown kit"):
        build_kit(random.Random(0), "a", "toaster", 0)


def test_cli_writes_jsonl_and_truth(tmp_path):
    out, truth = tmp_path / "obs.jsonl", tmp_path / "truth.json"
    rc = cli_main(["run", str(ROOT / "scenarios" / "baseline-friendly.yaml"), "-o", str(out), "-t", str(truth)])
    assert rc == 0
    lines = out.read_text().splitlines()
    assert lines and json.loads(lines[0])["schema"] == "rf.observation.v0"
    assert json.loads(truth.read_text())["scenario"] == "baseline-friendly"
