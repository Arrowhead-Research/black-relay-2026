"""Alert rule business rules, driven with hand-built events."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import T0, obs
from rfanalytics.alerts import AlertEngine
from rfanalytics.events import Event

BASE = T0.timestamp()
ID = "aa:00:00:00:00:01"


def ev(kind: str, t: float, emitter_id: str = ID, emitter_type: str = "bt_classic", **data) -> Event:
    return Event(kind, BASE + t, emitter_id, emitter_type, data)


def visit(engine, start: float, end: float, *, zone: str = "east", emitter_id: str = ID,
          emitter_type: str = "bt_classic", randomized: bool = False, step: float = 10.0):
    """Drive one visit: start, zone, periodic observations and ticks, end."""
    msgs = engine.handle(ev("visit_start", start, emitter_id, emitter_type, randomized_address=randomized))
    msgs += engine.handle(ev("zone_estimate", start, emitter_id, emitter_type, zone=zone, confidence=0.8))
    t = start
    while t <= end:
        msgs += engine.observe(obs(t, "n-east", -60, emitter_id=emitter_id, emitter_type=emitter_type))
        msgs += engine.tick(BASE + t)
        t += step
    msgs += engine.handle(ev("visit_end", end, emitter_id, emitter_type))
    return msgs


def alerts(msgs, rule_id=None):
    return [m for m in msgs if m.kind == "alert" and (rule_id is None or m.body["rule_id"] == rule_id)]


# -- loiter -----------------------------------------------------------------

def test_loiter_fires_once_after_min_dwell():
    engine = AlertEngine([{"id": "loiter", "type": "loiter", "min_dwell_s": 300}])
    msgs = visit(engine, 0, 900)
    fired = alerts(msgs)
    assert len(fired) == 1
    assert fired[0].t == pytest.approx(BASE + 300)
    assert fired[0].body["location"]["zone"] == "east"
    assert fired[0].body["context"]["dwell_s"] == 300


def test_loiter_respects_zone_filter_and_short_stays():
    engine = AlertEngine([{"id": "loiter", "type": "loiter", "min_dwell_s": 300, "zones": ["west"]}])
    assert alerts(visit(engine, 0, 900, zone="east")) == []
    engine = AlertEngine([{"id": "loiter", "type": "loiter", "min_dwell_s": 300}])
    assert alerts(visit(engine, 0, 200)) == []


def test_loiter_does_not_count_silence():
    engine = AlertEngine([{"id": "loiter", "type": "loiter", "min_dwell_s": 300}])
    engine.handle(ev("visit_start", 0))
    engine.handle(ev("zone_estimate", 0, zone="east", confidence=0.8))
    engine.observe(obs(0, "n-east", -60, emitter_id=ID, emitter_type="bt_classic"))
    # Nothing heard since t=0; the clock advancing alone must not create dwell.
    assert alerts(engine.tick(BASE + 1000)) == []


# -- new device ---------------------------------------------------------------

NEW = {"id": "new", "type": "new_device", "learn_s": 0, "hours": {"start": "22:00", "end": "05:00"}}


def at(hh: int, mm: int = 0) -> float:
    return datetime(2026, 11, 2, hh, mm, tzinfo=timezone.utc).timestamp() - BASE


def test_new_device_only_in_hours_and_only_once():
    engine = AlertEngine([NEW])
    assert alerts(visit(engine, at(14), at(14, 5))) == [], "daytime"
    engine = AlertEngine([NEW])
    assert len(alerts(visit(engine, at(23), at(23, 5)))) == 1
    assert alerts(visit(engine, at(23, 30), at(23, 35))) == [], "seen before"


def test_new_device_learning_period_and_randomized():
    engine = AlertEngine([{**NEW, "learn_s": 3600}])
    engine.tick(BASE + at(22))  # engine starts at 22:00
    assert alerts(visit(engine, at(22, 30), at(22, 35))) == [], "still learning"
    engine = AlertEngine([NEW])
    assert alerts(visit(engine, at(23), at(23, 5), randomized=True)) == []


def test_new_device_min_duration_ignores_passersby():
    engine = AlertEngine([{**NEW, "min_duration_s": 120}])
    assert alerts(visit(engine, at(23), at(23) + 30)) == [], "passing through"
    fired = alerts(visit(engine, at(23, 10), at(23, 20), emitter_id="aa:00:00:00:00:02"))
    assert len(fired) == 1 and fired[0].body["context"]["present_s"] >= 120


# -- repeat visitor -----------------------------------------------------------

def test_repeat_visitor_fires_on_third_visit_within_window():
    engine = AlertEngine([{"id": "repeat", "type": "repeat_visitor", "min_visits": 3, "window_s": 86400}])
    msgs = []
    for hour in (1, 6, 12, 18):
        msgs += visit(engine, hour * 3600, hour * 3600 + 60)
    fired = alerts(msgs)
    assert len(fired) == 1, "fires on the 3rd visit, then cools down for the window"
    assert fired[0].body["context"]["visits"] == 3


def test_repeat_visitor_ignores_old_and_randomized_visits():
    engine = AlertEngine([{"id": "repeat", "type": "repeat_visitor", "min_visits": 3, "window_s": 3600}])
    msgs = []
    for hour in (0, 2, 4):  # more than the window apart
        msgs += visit(engine, hour * 3600, hour * 3600 + 60)
    for hour in (5, 5.2, 5.4):
        msgs += visit(engine, hour * 3600, hour * 3600 + 60, emitter_id="02:00:00:00:00:09", randomized=True)
    assert alerts(msgs) == []


# -- watchlist ---------------------------------------------------------------

def test_watchlist_matches_id_and_ssid():
    engine = AlertEngine([{"id": "watch", "type": "watchlist", "emitter_ids": [ID.upper()], "ssids": ["BAD-NET"]}])
    assert len(alerts(engine.handle(ev("visit_start", 0)))) == 1
    ap = ev("visit_start", 5, "bb:00:00:00:00:01", "wifi_ap", attributes={"ssid": "BAD-NET"})
    assert len(alerts(engine.handle(ap))) == 1
    other = ev("visit_start", 9, "bb:00:00:00:00:02", "wifi_ap", attributes={"ssid": "OK"})
    assert alerts(engine.handle(other)) == []


# -- node offline ------------------------------------------------------------

def test_node_offline_and_recovery():
    engine = AlertEngine([{"id": "nodes", "type": "node_offline", "silence_s": 60}], node_ids=["n-west", "n-east"])
    msgs = []
    for t in range(0, 200, 10):
        msgs += engine.observe(obs(t, "n-west", -60))
        if t < 50:
            msgs += engine.observe(obs(t, "n-east", -60))
        msgs += engine.tick(BASE + t)
    msgs += engine.observe(obs(200, "n-east", -60))
    status = [(m.body["node_id"], m.body["state"]) for m in msgs if m.kind == "node_status"]
    assert status == [("n-east", "offline"), ("n-east", "online")]
    assert len(alerts(msgs, "nodes")) == 1


# -- camera cue ---------------------------------------------------------------

def test_camera_cue_on_first_heard_with_site_cooldown():
    engine = AlertEngine([{"id": "cue", "type": "camera_cue", "emitter_types": ["tpms"], "cooldown_s": 60}])
    msgs = []
    for i, t in enumerate((0, 1, 2, 3, 90)):  # one car (4 sensors), then another
        msgs += engine.handle(ev("first_heard", t, f"tpms:0000000{i}", "tpms", node_id="n-east", zone="east"))
    cues = [m for m in msgs if m.kind == "camera_cue"]
    assert [round(m.t - BASE) for m in cues] == [0, 90]
    assert cues[0].body["zone"] == "east"


# -- configuration -----------------------------------------------------------

@pytest.mark.parametrize("spec, match", [
    ({"id": "x", "type": "teleport"}, "unknown type"),
    ({"id": "x", "type": "loiter", "severity": "urgent"}, "severity"),
    ({"id": "x", "type": "loiter", "min_dwel_s": 5}, "unknown keys"),
    ({"id": "x", "type": "watchlist"}, "needs emitter_ids or ssids"),
])
def test_bad_rule_config_rejected(spec, match):
    with pytest.raises(ValueError, match=match):
        AlertEngine([spec])


def test_disabled_rules_do_not_fire():
    engine = AlertEngine([{"id": "loiter", "type": "loiter", "min_dwell_s": 60, "enabled": False}])
    assert alerts(visit(engine, 0, 600)) == []
