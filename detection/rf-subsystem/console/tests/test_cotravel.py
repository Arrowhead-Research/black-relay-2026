"""Vehicles and co-travelers (cotravel.py), the arrived_with rule, and the
simulator measurements (cotravel_eval.py) as regression thresholds."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alerting import AlertEngine, arrivals  # noqa: E402
from cotravel import analyze, poisson_sf, vehicles  # noqa: E402

DAY = 86400.0
TPMS = {f"tpms:0000000{i}": {"type": "tpms", "name": "Schrader", "randomized": False} for i in range(1, 5)}
META = {**TPMS, "00:11:22:00:00:01": {"type": "bt_classic", "name": "Car audio", "randomized": False},
        "00:11:22:00:00:09": {"type": "wifi_ap", "name": "neighbor", "randomized": False},
        "aa:11:22:00:00:02": {"type": "ble", "name": None, "randomized": True}}


def car_visits(starts):
    out = []
    for s in starts:
        for i, tid in enumerate(TPMS):
            out += [(tid, s + i, s + 8 + i), (tid, s + 700 + i, s + 708 + i)]    # bursts on arrival and departure
        out.append(("00:11:22:00:00:01", s + 2, s + 705))                        # head unit while parked
        out.append(("aa:11:22:00:00:02", s + 30, s + 600))                       # randomized phone: not followed
    return out


def test_poisson_tail():
    assert poisson_sf(0, 3) == 1 and poisson_sf(1, 0.5) == pytest.approx(1 - 2.718281828 ** -0.5)


def test_vehicle_from_one_burst_and_cotravelers_across_visits():
    visits = car_visits([8 * 3600, 13 * 3600, 21 * 3600]) + [("00:11:22:00:00:09", 0, DAY)]   # an AP always there
    res = analyze(visits, META, DAY)
    assert [v["sensors"] for v in res["vehicles"]] == [sorted(TPMS)]
    veh = res["vehicles"][0]["id"]
    assert res["groups"] == [sorted(["00:11:22:00:00:01", veh])]
    pair = res["pairs"][0]
    assert pair["together"] == 3 and pair["p"] < 0.01
    # one shared visit is not enough
    assert analyze(car_visits([8 * 3600]), META, DAY)["groups"] == []


def test_two_cars_of_the_same_make_together_are_split():
    a = {f"tpms:a000000{i}": {"type": "tpms", "name": "Schrader"} for i in range(4)}
    b = {f"tpms:b000000{i}": {"type": "tpms", "name": "Schrader"} for i in range(4)}
    visits = [(k, 100 + i, 110) for i, k in enumerate(list(a) + list(b))]           # passing together once
    visits += [(k, 5000 + i, 5010) for i, k in enumerate(a)]                         # car a alone later
    groups = [v["sensors"] for v in vehicles(visits, {**a, **b})]
    assert sorted(a) in groups and all(len(g) <= 5 for g in groups)


def test_arrived_with_rule():
    cot = {"entity_of": {t: "vehicle:1" for t in TPMS}, "members": {"vehicle:1": set(TPMS)},
           "partners": {"vehicle:1": {"00:11:22:00:00:01"}, "00:11:22:00:00:01": {"vehicle:1"}}}
    devs = {"00:11:22:00:00:01": {}, "tpms:00000001": {}}
    ev = [{"kind": "visit", "device": "00:11:22:00:00:01", "visits": 4}]
    last: dict = {}
    out = arrivals(1000, ev, devs, {}, set(), cot, {"tpms:00000002": 950}, last)
    assert [(a["rule_id"], a["context"]["with"], a["severity"]) for a in out] == [("arrived_with", ["vehicle:1"], "low")]
    assert arrivals(1010, ev, devs, {}, set(), cot, {"tpms:00000002": 950}, last) == []        # once per arrival
    assert arrivals(5000, ev, devs, {}, set(), cot, {"tpms:00000002": 950}, {}) == []          # partner not arriving now
    prio = arrivals(1000, ev, devs, {"tpms:00000003": {"kind": "priority"}}, set(), cot, {"tpms:00000002": 990}, {})
    assert prio[0]["severity"] == "high"


def test_new_car_is_one_alert():
    eng = AlertEngine()
    devs = {t: {"type": "tpms", "name": "Schrader", "nodes": {"wbt-1": {}}} for t in TPMS}
    out = eng.evaluate("s", 0, devs, {}, {}, [], {}, set(), [{"kind": "new", "device": t, "visits": 1} for t in TPMS])
    assert len(out) == 1 and sorted(out[0]["context"]["sensors"]) == sorted(TPMS)


@pytest.mark.parametrize("scenario,expect", [
    ("repeat-vehicle-co-travelers", {"vehicle_precision": 0.95, "vehicle_recall": 0.85, "pairs": 1, "pair_precision": 1.0}),
    ("passersby", {"vehicle_precision": 0.95, "vehicle_recall": 0.8, "pairs": 0}),
])
def test_simulator_scenarios(scenario, expect):
    pytest.importorskip("rfsim")
    from cotravel_eval import run
    got = run(scenario)
    assert got["vehicle_precision"] >= expect["vehicle_precision"] and got["vehicle_recall"] >= expect["vehicle_recall"]
    assert got["pairs"] == expect["pairs"]
    if expect["pairs"]:
        assert got["pair_precision"] == expect["pair_precision"]
        assert any("subject-vehicle" in g[0] and "subject-vehicle" in g[1] for g in got["groups"])
