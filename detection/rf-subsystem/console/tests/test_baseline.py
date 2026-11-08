"""Site baseline (pattern of life), its alert rules, and replaying a Kismet log."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from baseline import LEARN_S, VISIT_GAP_S, Baseline, hour_of_week  # noqa: E402


def d(dev_id, randomized=False, nodes=("wbt-1", "wbt-2")):
    return {"id": dev_id, "type": "wifi_client", "band": "wifi", "name": None, "randomized": randomized,
            "nodes": {n: {} for n in nodes}}


def test_new_and_visits_after_learning(tmp_path):
    b = Baseline(tmp_path / "history" / "baseline.sqlite")
    t0 = 1_800_000_000.0
    assert b.observe([d("bg")], t0) == []                          # learning period: the site's background
    assert b.observe([d("bg")], t0 + 3) == []
    later = t0 + LEARN_S + 10
    assert b.observe([d("bg"), d("new")], later) == [
        {"kind": "visit", "device": "bg", "visits": 2, "away_s": round(LEARN_S + 7)},
        {"kind": "new", "device": "new", "visits": 1}]
    assert b.observe([d("new")], later + VISIT_GAP_S + 1)[0]["visits"] == 2
    b.flush()
    rec = Baseline(tmp_path / "history" / "baseline.sqlite").get("bg")   # persisted
    assert rec["visits"] == 2 and rec["background"] and rec["heard_s"] == 3
    assert rec["presence"] == {hour_of_week(t0): 1, hour_of_week(later): 1}
    assert b.summary()["devices"] == 2


def test_cleared_history_starts_a_fresh_baseline(tmp_path):
    path = tmp_path / "history" / "baseline.sqlite"
    b = Baseline(path)
    b.observe([d("x")], 1_800_000_000.0)
    b.flush()
    path.unlink()                                                  # Site menu: Clear history
    assert b.get("x") is None and b.summary()["devices"] == 0


def test_replay_runs_the_pipeline_on_a_log(tmp_path):
    from replay import replay
    from server import CalibrationStore, load_layout
    from sites import SiteManager
    from test_server import AP, LAYOUT, _frame, dev
    template = load_layout(LAYOUT)
    sites = SiteManager(tmp_path / "data", template, CalibrationStore)
    site_dir = tmp_path / "data" / "sites" / sites.active().id
    nodes = sites.layout()["nodes"]
    (site_dir / "areas.json").write_text(json.dumps([{"id": "a1", "name": "Everywhere", "linger_s": 60,
                                                      "points": [[-500, -500], [500, -500], [500, 500], [-500, 500]]}]))
    logs = tmp_path / "logs"
    logs.mkdir()
    db = sqlite3.connect(logs / "Kismet-test.kismet")
    db.execute("create table datasources (uuid text, name text)")
    db.execute("create table packets (ts_sec int, ts_usec int, sourcemac text, datasource text, signal int, dlt int, packet blob)")
    db.execute("create table devices (devmac text, phyname text, device blob)")
    names = list(nodes)
    db.executemany("insert into datasources values (?, ?)", [(f"U{i}", f"{n}:wifi") for i, n in enumerate(names)])
    phone = "00:1a:11:00:00:77"
    db.execute("insert into devices values (?, 'IEEE802.11', ?)", (phone, json.dumps(dev(phone, 1000, {}, kind="Wi-Fi Client"))))
    rows = []
    for t in range(1000, 1120):
        for i, n in enumerate(names):
            rows.append((t, phone, f"U{i}", -50 - 6 * i, _frame("data_to_ds", AP, phone, AP)))
    db.executemany("insert into packets values (?, 0, ?, ?, ?, 127, ?)", rows)
    db.commit()
    db.close()
    res = replay(site_dir, tmp_path / "data", logs, 1010, 1110, template, learn_s=0)
    assert res["devices_placed"] == 1 and res["positions"] and res["polls"] == 34
    assert res["alerts_by_rule"] == {"area_entry": 1, "linger": 1}
    assert {p["id"] for p in res["positions"]} == {phone}
