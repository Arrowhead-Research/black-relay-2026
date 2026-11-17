"""Business rules of the analyzer, on small hand-built inputs."""

from __future__ import annotations

from conftest import obs
from rfanalytics.analyzer import Analyzer, AnalyzerConfig
from rfanalytics.friendly import FriendlyEntry, FriendlyList
from rfanalytics.localization import estimate_zone


def run(site, observations, **config):
    analyzer = Analyzer(site, config=AnalyzerConfig(**config))
    events = []
    for o in observations:
        events.extend(analyzer.ingest(o))
    events.extend(analyzer.flush())
    return analyzer, events


def types(events):
    return [e.type for e in events]


def test_single_transmission_is_discarded_as_noise(line_site):
    # One transmission heard by three nodes is still one transmission.
    analyzer, events = run(line_site, [obs(0, "n-west", -60), obs(0.1, "n-middle", -70), obs(0.2, "n-east", -80)])
    assert events == []
    assert analyzer.stats.visits_discarded == 1


def test_visit_confirmed_after_min_transmissions(line_site):
    _, events = run(line_site, [obs(0, "n-west", -60), obs(10, "n-west", -61)])
    assert types(events) == ["visit_start", "zone_estimate", "visit_end"]
    start, zone, end = events
    assert start.t == obs(0, "n-west", -60).t
    assert zone.data["zone"] == "west"
    assert end.data["reason"] == "end_of_input"
    assert end.data["transmissions"] == 2


def test_short_gaps_are_bridged_and_long_gaps_split(line_site):
    seq = [obs(t, "n-west", -60) for t in (0, 10, 200, 210)]  # 190 s gap < 300 s
    _, events = run(line_site, seq)
    assert types(events).count("visit_start") == 1

    seq = [obs(t, "n-west", -60) for t in (0, 10, 400, 410)]  # 390 s gap > 300 s
    _, events = run(line_site, seq)
    assert types(events).count("visit_start") == 2
    assert [e.data["reason"] for e in events if e.type == "visit_end"] == ["gap", "end_of_input"]


def test_tpms_gap_bridges_a_parked_vehicle(line_site):
    seq = [obs(t, "n-west", -80, emitter_id="tpms:00000001", emitter_type="tpms") for t in (0, 5, 1200, 1205)]
    _, events = run(line_site, seq)
    assert types(events).count("visit_start") == 1


def test_zone_change_needs_hysteresis(line_site):
    seq = [obs(t, "n-west", -50) for t in range(0, 30, 5)]
    seq += [obs(30, "n-east", -40)]  # one outlier toward the east...
    seq += [obs(t, "n-west", -50) for t in range(35, 60, 5)]  # ...then back to west
    _, events = run(line_site, seq, rssi_window_s=4)
    zones = [e.data["zone"] for e in events if e.type == "zone_estimate"]
    assert zones == ["west"], "a single outlier must not move the emitter"

    seq = [obs(t, "n-west", -50) for t in range(0, 30, 5)]
    seq += [obs(t, "n-east", -40) for t in range(30, 60, 5)]
    _, events = run(line_site, seq, rssi_window_s=4)
    assert [e.data["zone"] for e in events if e.type == "zone_estimate"] == ["west", "east"]


def test_dwell_is_split_by_zone(line_site):
    seq = [obs(t, "n-west", -50) for t in range(0, 120, 5)]
    seq += [obs(t, "n-east", -50) for t in range(120, 180, 5)]
    _, events = run(line_site, seq, rssi_window_s=4)
    dwell = events[-1].data["dwell_s"]
    assert set(dwell) == {"west", "east"}
    assert 115 <= dwell["west"] <= 130 and 45 <= dwell["east"] <= 60


def test_silent_emitter_zone_goes_stale(line_site):
    tpms = dict(emitter_id="tpms:00000001", emitter_type="tpms")
    seq = [obs(t, "n-west", -80, **tpms) for t in (0, 5)]           # heard arriving in the west
    seq += [obs(t, "n-middle", -40, emitter_id="02:00:00:00:00:09") for t in range(0, 1000, 20)]  # clock
    seq += [obs(t, "n-east", -80, **tpms) for t in (1000, 1005)]    # heard leaving from the east
    seq.sort(key=lambda o: o.t)
    _, events = run(line_site, seq)
    tpms_events = [e for e in events if e.emitter_id == "tpms:00000001"]
    zones = [e.data["zone"] for e in tpms_events if e.type == "zone_estimate"]
    assert zones == ["west", None, "east"]
    dwell = tpms_events[-1].data["dwell_s"]
    assert dwell.get("west", 0) < 10, "the silent parked period must not count as dwell"


def test_friendly_observations_produce_nothing(line_site):
    friendly = FriendlyList([FriendlyEntry("team", emitter_id="02:00:00:00:00:01")])
    analyzer = Analyzer(line_site, friendly)
    events = [e for t in range(0, 60, 5) for e in analyzer.ingest(obs(t, "n-west", -50))]
    events += analyzer.flush()
    assert events == []
    assert analyzer.stats.friendly_excluded == 12
    assert analyzer.stats.emitters == set()


def test_strongest_uses_calibration(line_site):
    from dataclasses import replace

    medians = {"n-west": -60, "n-east": -58}
    assert estimate_zone(line_site, medians, "strongest").zone == "east"
    nodes = dict(line_site.nodes)
    nodes["n-west"] = replace(nodes["n-west"], calibration_db=5)
    calibrated = replace(line_site, nodes=nodes)
    assert estimate_zone(calibrated, medians, "strongest").zone == "west"


def test_centroid_lands_between_equal_nodes(line_site):
    est = estimate_zone(line_site, {"n-west": -60, "n-east": -60}, "centroid")
    assert est.zone == "middle"
    assert est.position == (15.0, 5.0)
