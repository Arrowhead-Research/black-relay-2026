"""Marks, alert areas, rules and alert records (contract: schemas/alert.v0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerting import VISIT_GAP_S, AlertEngine, Areas, Marks, point_in_polygon  # noqa: E402
from test_publish import FakeDurable, FakeLive, validator  # noqa: E402

ALERT = validator("alert.v0")
ROOM = {"id": "a1", "name": "Inner", "points": [[0, 0], [10, 0], [10, 10], [0, 10]], "linger_s": 60}
DEV = {"type": "wifi_client", "band": "wifi", "nodes": {"wbt-1": {}, "wbt-2": {}, "wbt-3": {}}}


def pos(x, y, k=3):
    return {"x": x, "y": y, "nodes": k, "ids": ["wbt-1", "wbt-2", "wbt-3"][:k]}


def test_point_in_polygon():
    assert point_in_polygon(5, 5, ROOM["points"]) and not point_in_polygon(15, 5, ROOM["points"])


def test_area_entry_once_per_visit_then_linger():
    eng = AlertEngine()
    run = lambda t, p, marks=None: [a["rule_id"] for a in eng.evaluate("s", t, {"d1": DEV}, {"d1": p} if p else {},
                                                                       marks or {}, [ROOM], {}, set())]
    assert run(0, pos(5, 5)) == ["area_entry"]
    assert run(3, pos(11, 5)) == []                       # jitter out...
    assert run(6, pos(9, 5)) == []                        # ...and back in: same visit
    assert run(61, pos(5, 5)) == ["linger"]
    assert run(64, pos(5, 5)) == []
    assert run(64 + VISIT_GAP_S + 1, pos(5, 5)) == ["area_entry"]   # a new visit after the gap
    assert run(2000, pos(5, 5, k=2)) == []               # two nodes: no area alerts


def test_friendly_and_node_radios_are_never_alerted_priority_always():
    eng = AlertEngine()
    marks = {"d1": {"kind": "friendly"}, "d2": {"kind": "priority"}}
    devs = {"d1": DEV, "d2": DEV, "radio": DEV}
    ps = {k: pos(5, 5) for k in devs}
    out = eng.evaluate("s", 0, devs, ps, marks, [ROOM], {}, {"radio"})
    assert sorted((a["rule_id"], a["device"], a["severity"]) for a in out) == [
        ("area_entry", "d2", "high"), ("priority_heard", "d2", "high")]
    assert eng.evaluate("s", 5, devs, ps, marks, [ROOM], {}, {"radio"}) == []      # still heard: no repeat


def test_node_offline_only():
    eng = AlertEngine()
    st = lambda s: {"wbt-1": {"state": s, "sources": {"wifi": "ok" if s == "online" else "stopped"}}}
    assert eng.evaluate("s", 0, {}, {}, {}, [], st("online"), set()) == []
    assert [a["rule_id"] for a in eng.evaluate("s", 1, {}, {}, {}, [], st("offline"), set())] == ["node_offline"]
    assert eng.evaluate("s", 2, {}, {}, {}, [], st("online"), set()) == []          # no "back online" alert
    assert eng.evaluate("s", 3, {}, {}, {}, [], st("degraded"), set()) == []        # degraded is not offline


def test_device_linger_alarm():
    eng = AlertEngine()
    devs = {"d1": DEV}
    marks = {"d1": {"kind": None, "linger_s": 300}}
    run = lambda t, m=marks: [(a["rule_id"], a["severity"], a["context"].get("dwell_s")) for a in eng.evaluate("s", t, devs, {}, m, [], {}, set())]
    assert run(0) == [] and run(200) == []
    assert run(301) == [("linger", "medium", 301)]
    assert run(400) == []                                          # once per visit
    assert run(400 + VISIT_GAP_S + 1) == [] and run(400 + VISIT_GAP_S + 302) == [("linger", "medium", 301)]   # new visit
    eng2 = AlertEngine()
    prio = {"d1": {"kind": "priority", "linger_s": 60}}
    out = [(a["rule_id"], a["severity"]) for t in (0, 61) for a in eng2.evaluate("s", t, devs, {}, prio, [], {}, set())]
    assert out == [("priority_heard", "high"), ("linger", "high")]
    eng3 = AlertEngine()                                           # an explicit linger alarm applies to a friendly device too
    fr = {"d1": {"kind": "friendly", "linger_s": 60}}
    assert [a["rule_id"] for t in (0, 61) for a in eng3.evaluate("s", t, devs, {"d1": pos(5, 5)}, fr, [ROOM], {}, set())] == ["linger"]


def test_marks_team_and_site_scope(tmp_path):
    m = Marks(tmp_path / "marks.json")
    m.set("s1", tmp_path / "s1.json", {"device": "AA:BB:CC:00:00:01", "kind": "friendly", "scope": "team", "label": "Bryan's phone"}, "operator")
    m.set("s1", tmp_path / "s1.json", {"device": "aa:bb:cc:00:00:02", "kind": "priority"}, "operator")
    assert m.merged("s1", tmp_path / "s1.json")["aa:bb:cc:00:00:01"]["scope"] == "team"
    assert "aa:bb:cc:00:00:02" not in m.merged("s2", tmp_path / "s2.json")          # site scope stays with its site
    assert "aa:bb:cc:00:00:01" in Marks(tmp_path / "marks.json").merged("s2", None)   # team scope persists
    with pytest.raises(ValueError):
        m.set("s1", None, {"device": "x", "kind": "enemy"}, None)
    m.set("s1", tmp_path / "s1.json", {"device": "aa:bb:cc:00:00:02", "remove": True}, None)
    assert "aa:bb:cc:00:00:02" not in m.merged("s1", tmp_path / "s1.json")
    # a linger alarm alone, then with a mark, then the mark cleared (the alarm stays), then the alarm cleared
    p = tmp_path / "s1.json"
    m.set("s1", p, {"device": "cc:00:00:00:00:03", "linger_s": 900}, None)
    assert m.merged("s1", p)["cc:00:00:00:00:03"]["linger_s"] == 900 and m.merged("s1", p)["cc:00:00:00:00:03"]["kind"] is None
    m.set("s1", p, {"device": "cc:00:00:00:00:03", "kind": "priority"}, None)
    assert m.merged("s1", p)["cc:00:00:00:00:03"]["kind"] == "priority" and m.merged("s1", p)["cc:00:00:00:00:03"]["linger_s"] == 900
    m.set("s1", p, {"device": "cc:00:00:00:00:03", "kind": None}, None)
    assert m.merged("s1", p)["cc:00:00:00:00:03"]["kind"] is None
    m.set("s1", p, {"device": "cc:00:00:00:00:03", "linger_s": None}, None)
    assert "cc:00:00:00:00:03" not in m.merged("s1", p)
    with pytest.raises(ValueError):
        m.set("s1", p, {"device": "cc:00:00:00:00:03", "linger_s": 5}, None)


def test_areas_validation(tmp_path):
    a = Areas()
    out = a.add("s", tmp_path / "areas.json", {"name": "Lot", "points": [[0, 0], [5, 0], [5, 5]]}, None)
    assert out[0]["linger_s"] == 300 and json.loads((tmp_path / "areas.json").read_text())[0]["name"] == "Lot"
    with pytest.raises(ValueError):
        a.add("s", None, {"name": "x", "points": [[0, 0], [1, 1]]}, None)
    assert a.delete("s", tmp_path / "areas.json", [out[0]["id"]], None) == []


def test_console_alerts_end_to_end(tmp_path):
    """Marks and areas over HTTP; a poll raises an area alert that is stored, shown, acknowledged and published."""
    from test_server import LAYOUT, FakePoller, _get, _post, _serve
    from server import App, load_layout
    from publish import Publisher
    from sites import SiteManager
    from server import CalibrationStore
    sites = SiteManager(tmp_path / "data", load_layout(LAYOUT), CalibrationStore)
    durable = FakeDurable()
    app = App(FakePoller(), None, window_s=60, poll_s=3, sites=sites, publisher=Publisher(FakeLive(), durable),
              data_dir=tmp_path / "data")
    httpd, base = _serve(app)
    try:
        app.poll_once()
        p = _get(base, "/api/state")["devices"][0]["position"]
        sq = [[p["x"] - 5, p["y"] - 5], [p["x"] + 5, p["y"] - 5], [p["x"] + 5, p["y"] + 5], [p["x"] - 5, p["y"] + 5]]
        _post(base, "/api/areas/add", {"name": "Inner", "points": sq})
        _post(base, "/api/marks", {"device": "aa:bb:cc:00:00:09", "kind": "priority", "label": "suspect"})
        app.poll_once()
        st = _get(base, "/api/state")
        assert st["marks"]["aa:bb:cc:00:00:09"]["kind"] == "priority"
        assert sorted(a["rule_id"] for a in st["alerts"]["recent"]) == ["area_entry", "priority_heard"]
        assert st["alerts"]["unacked"] == 2
        alerts = [pl for t, pl in durable.sent if t == "detection/rf/alerts"]
        assert len(alerts) == 2
        for pl in alerts:
            ALERT.validate(pl)
        entry = next(pl for pl in alerts if pl["rule_id"] == "area_entry")
        assert entry["location"]["zone"] == "Inner" and entry["emitter"]["mark"] == "priority" and entry["severity"] == "high"
        assert _post(base, "/api/alerts/ack", {})["alerts"]["unacked"] == 0
        # stored per site and reloaded after a restart
        app2 = App(FakePoller(), None, window_s=60, poll_s=3, sites=sites, data_dir=tmp_path / "data")
        view = app2.alert_log.view(*app2.site_paths()[:1], app2.history_dir())
        assert len(view["recent"]) == 2 and view["unacked"] == 0
    finally:
        httpd.shutdown()


def test_rf_analytics_alerts_match_the_same_schema():
    from rfanalytics.alerts import Message
    from rfanalytics.formats import to_wire
    body = {"rule_id": "loiter-any-zone", "rule_type": "loiter", "severity": "medium",
            "emitter": {"id": "aa:bb:cc:00:00:01", "type": "wifi_client", "randomized_address": False},
            "location": {"zone": "north", "confidence": 0.6}, "context": {"dwell_s": 840}}
    ALERT.validate(to_wire(Message("alert", 1000.0, body))[1])
    ALERT.validate(to_wire(Message("alert", 1000.0, {"rule_id": "nodes", "rule_type": "node_offline", "severity": "high",
                                                       "context": {"node_id": "wbt-2", "last_heard": None}}))[1])
