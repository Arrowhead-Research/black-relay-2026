"""Measure vehicle grouping and co-traveler detection on simulator scenarios against ground truth.

    cd detection/rf-subsystem/analytics && uv run python ../console/tests/cotravel_eval.py [scenario ...]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from baseline import Baseline  # noqa: E402
from cotravel import analyze  # noqa: E402

SCENARIOS = HERE.parents[1] / "simulator" / "scenarios"
BAND = {"wifi_ap": "wifi", "wifi_client": "wifi", "ble": "bt", "bt_classic": "bt", "tpms": "tpms"}


def run(name: str, seed: int | None = None) -> dict:
    from rfsim.engine import simulate
    from rfsim.scenario import load_scenario
    res = simulate(load_scenario(SCENARIOS / f"{name}.yaml", seed=seed))
    bins: dict[int, dict[str, dict]] = defaultdict(dict)
    for o in res.observations:
        t = o.timestamp.timestamp()
        a = o.attributes or {}
        d = bins[int(t // 3)].setdefault(o.emitter_id, {
            "id": o.emitter_id, "type": o.emitter_type, "band": BAND.get(o.emitter_type, "wifi"),
            "name": a.get("tpms_protocol") or a.get("name") or a.get("ssid"), "randomized": bool(a.get("randomized_address")),
            "nodes": {}})
        d["nodes"][o.node_id] = {}
    base = Baseline(None, learn_s=0)
    for b in sorted(bins):
        base.observe(list(bins[b].values()), b * 3.0)
    base.flush()
    visits, meta = base.visits()
    span = res.observations[-1].timestamp.timestamp() - res.observations[0].timestamp.timestamp()
    result = analyze(visits, meta, span)
    return score(result, res.truth, meta)


def score(result: dict, truth: dict, meta: dict) -> dict:
    owner, tpms_of = {}, defaultdict(set)
    for actor in truth["actors"]:
        for dev in actor["devices"]:
            for e in dev["emitter_ids"]:
                owner[e["emitter_id"]] = actor["actor_id"]
                if dev["emitter_type"] == "tpms" and e["emitter_id"] in meta:
                    tpms_of[actor["actor_id"]].add(e["emitter_id"])
    truth_veh = {a: s for a, s in tpms_of.items() if len(s) >= 2}
    pred = result["vehicles"]
    pure = [v for v in pred if len({owner.get(s) for s in v["sensors"]}) == 1]
    recovered = [a for a, s in truth_veh.items() if any(set(v["sensors"]) == s for v in pure)]
    veh_actor = {v["id"]: owner.get(v["sensors"][0]) for v in pred}
    ent_actor = lambda e: veh_actor.get(e) or owner.get(e)  # noqa: E731
    pairs = result["pairs"]
    good = [p for p in pairs if ent_actor(p["a"]) and ent_actor(p["a"]) == ent_actor(p["b"])]
    return {
        "vehicles_true": len(truth_veh), "vehicles_predicted": len(pred), "vehicles_pure": len(pure),
        "vehicles_recovered": len(recovered),
        "vehicle_precision": round(len(pure) / len(pred), 3) if pred else None,
        "vehicle_recall": round(len(recovered) / len(truth_veh), 3) if truth_veh else None,
        "pairs": len(pairs), "pairs_correct": len(good),
        "pair_precision": round(len(good) / len(pairs), 3) if pairs else None,
        "groups": [[f"{e} ({ent_actor(e)})" for e in g] for g in result["groups"]],
    }


if __name__ == "__main__":
    import json
    for name in sys.argv[1:] or ["repeat-vehicle-co-travelers", "passersby"]:
        print(name, json.dumps(run(name), indent=1))
