"""WBT Console backend: Kismet snapshots -> rolling per-node RSSI and calibration."""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from demo import DemoSource  # noqa: E402
from server import BAND_TIMING, App, CalibrationStore, KismetLog, Tracker, calibration_samples, load_layout, smooth_rssi  # noqa: E402

LAYOUT = Path(__file__).resolve().parents[1] / "layout.example.yaml"


def dev(mac: str, t: float, rssi_by_node: dict[str, int | None], kind: str = "Wi-Fi AP", ssid: str | None = None,
        phy: str = "IEEE802.11", history: dict[str, dict[int, int]] | None = None) -> dict:
    """Kismet device; ``history`` gives per-node {second: rssi} as a signal_rrd minute vector."""
    seenby = []
    for node, rssi in rssi_by_node.items():
        sig = ({"kismet.common.signal.type": "dbm", "kismet.common.signal.last_signal": rssi}
               if rssi is not None else {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0})
        if history and node in history:
            vec = [0] * 60
            for sec, v in history[node].items():
                vec[sec % 60] = v
            sig["kismet.common.signal.signal_rrd"] = {"kismet.common.rrd.last_time": max(history[node]),
                                                      "kismet.common.rrd.minute_vec": vec}
        seenby.append({"kismet.common.seenby.last_time": t,
                       "kismet.common.seenby.source": {"kismet.datasource.name": f"{node}:wifi"},
                       "kismet.common.seenby.signal": sig})
    d = {"kismet.device.base.macaddr": mac.upper(), "kismet.device.base.phyname": phy,
         "kismet.device.base.type": kind, "kismet.device.base.last_time": t, "kismet.device.base.seenby": seenby}
    if ssid:
        d["dot11.device"] = {"dot11.device.last_beaconed_ssid": ssid}
    return d


def test_example_layout_loads():
    layout = load_layout(LAYOUT)
    assert len(layout["nodes"]) >= 2 and set(layout["self_emitters"]) <= set(layout["nodes"])
    assert set(layout["models"]) == {"wifi", "bt", "tpms"}


def test_smooth_rssi_rejects_outliers_and_weights_recent():
    # Steady -50 with one 20 dB multipath fade: the fade is dropped.
    samples = [(float(t), -50) for t in range(10)] + [(10.0, -70)]
    out = smooth_rssi(samples, now=10, tau_s=12)
    assert out["rssi"] == -50 and out["outliers"] == 1 and out["raw"] == -70 and out["n"] == 11
    # A device that moved: recent samples dominate, but older ones still count.
    moved = [(float(t), -70) for t in range(0, 20)] + [(float(t), -50) for t in range(20, 40)]
    m = smooth_rssi(moved, now=40, tau_s=12)
    assert -58 < m["rssi"] < -50
    assert m["rssi"] > smooth_rssi(moved, now=40, tau_s=60)["rssi"]  # shorter tau tracks faster


def test_tracker_uses_per_second_history_and_band_windows():
    tr = Tracker()
    hist = {"wbt-1": {1000 + i: -50 + (i % 3) for i in range(30)}, "wbt-2": {1029: -80}}
    tr.update([dev("aa:bb:cc:00:00:01", 1029, {"wbt-1": -50, "wbt-2": -80}, ssid="lab", history=hist)], now=1030)
    d = tr.snapshot(now=1030)[0]
    assert d["band"] == "wifi" and d["name"] == "lab"
    assert d["nodes"]["wbt-1"]["n"] == 30          # thirty seconds of history from one poll
    assert -50 <= d["nodes"]["wbt-1"]["rssi"] <= -48
    # The same history again adds nothing new.
    tr.update([dev("aa:bb:cc:00:00:01", 1029, {"wbt-1": -50}, history=hist)], now=1031)
    assert tr.snapshot(now=1031)[0]["nodes"]["wbt-1"]["n"] == 30
    # A node not heard for longer than the band's stale time drops out; then everything ages out.
    assert tr.snapshot(now=1029 + BAND_TIMING["wifi"]["stale_s"] + 1) == []
    tr.prune(now=1029 + BAND_TIMING["wifi"]["window_s"] + 1)
    assert tr.samples == {} and tr.meta == {}


def test_tpms_keeps_sparse_evidence_longer_than_wifi():
    tr = Tracker()
    tpms = dev("00:00:00:00:00:00", 1000, {"wbt-1": -60, "wbt-2": -70}, kind="sensor", phy="RTL433")
    tpms["rtl433.device"] = {"rtl433.device.id": 0x1A2B3C4D, "rtl433.device.model": "Schrader"}
    wifi = dev("aa:bb:cc:00:00:02", 1000, {"wbt-1": -60, "wbt-2": -70})
    tr.update([tpms, wifi], now=1000)
    later = 1000 + BAND_TIMING["wifi"]["stale_s"] + 30   # past Wi-Fi staleness, within TPMS
    ids = {d["id"]: d for d in tr.snapshot(now=later)}
    assert set(ids) == {"tpms:1a2b3c4d"}
    assert ids["tpms:1a2b3c4d"]["band"] == "tpms" and ids["tpms:1a2b3c4d"]["name"] == "Schrader"


def test_snapshot_sorts_by_node_count_then_strength():
    tr = Tracker()
    tr.update([dev("aa:00:00:00:00:01", 10, {"wbt-1": -40}),
               dev("aa:00:00:00:00:02", 10, {"wbt-1": -70, "wbt-2": -60}),
               dev("aa:00:00:00:00:03", 10, {"wbt-1": -50, "wbt-2": -45})], now=10)
    assert [d["id"] for d in tr.snapshot(now=10)] == ["aa:00:00:00:00:03", "aa:00:00:00:00:02", "aa:00:00:00:00:01"]


def test_calibration_uses_node_radios_at_known_distances():
    layout = load_layout(LAYOUT)
    tr = Tracker()
    wbt1_radio = layout["self_emitters"]["wbt-1"]
    tr.update([dev(wbt1_radio, 10, {"wbt-1": -30, "wbt-2": -60, "wbt-4": -70}, kind="Wi-Fi Client")], now=10)
    cal = {(c["tx"], c["rx"]): c for c in calibration_samples(tr, layout, now=10)}
    assert set(cal) == {("wbt-1", "wbt-2"), ("wbt-1", "wbt-4")}  # own radio at the node is excluded
    assert cal[("wbt-1", "wbt-2")]["distance_ft"] == 20.0
    assert cal[("wbt-1", "wbt-4")]["distance_ft"] == pytest.approx(44.72, abs=0.01)
    assert cal[("wbt-1", "wbt-2")]["rssi"] == -60


class FakePoller:
    base_url = "http://fake"

    def __init__(self):
        self.calls = []

    def fetch_devices(self, since=None):
        import time
        self.calls.append(since)
        return [dev("aa:bb:cc:00:00:09", time.time(), {"wbt-1": -45, "wbt-2": -60, "wbt-3": -70})]


def test_api_state_end_to_end():
    poller = FakePoller()
    app = App(poller, load_layout(LAYOUT), window_s=60, poll_s=3)
    app.poll_once()
    assert poller.calls == [-60]  # only devices heard within the window
    server.Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler, directory=str(server.HERE / "static")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        state = json.load(urlopen(base + "/api/state"))
        assert state["kismet"]["ok"] is True
        assert state["layout"]["nodes"]["wbt-2"] == [20, 0]
        assert state["devices"][0]["nodes"]["wbt-1"]["rssi"] == -45
        resp = urlopen(base + "/")
        assert "WBT Console" in resp.read().decode()
        assert resp.headers["Cache-Control"] == "no-cache"  # never run stale page code after an update
    finally:
        httpd.shutdown()


def test_demo_source_flows_through_tracker_with_truth():
    layout = load_layout(LAYOUT)
    app = App(None, layout, window_s=180, poll_s=3, extra_sources=[DemoSource(layout, seed=1)])
    app.poll_once()
    state = app.state()
    by_band = {}
    for d in state["devices"]:
        by_band.setdefault(d["band"], []).append(d)
    assert {"wifi", "bt"} <= set(by_band)
    walker = next(d for d in by_band["bt"] if d["name"] == "demo walker (BLE)")
    assert len(walker["nodes"]) == 4 and len(walker["truth"]) == 2
    assert all(n["n"] > 20 for n in walker["nodes"].values())  # a minute of per-second history
    assert state["kismet"]["demo"] is True


def _serve(app):
    server.Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(server.Handler, directory=str(server.HERE / "static")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_samples_endpoint_returns_raw_per_node_history(tmp_path):
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3, cal_store=CalibrationStore(tmp_path / "cal.json"))
    import time
    now = int(time.time())
    hist = {"wbt-1": {now - 20 + i: -50 - i for i in range(20)}, "wbt-2": {now - 5: -70}}
    app.tracker.update([dev("aa:bb:cc:00:00:07", now, {"wbt-1": -50, "wbt-2": -70}, history=hist)], now=now)
    httpd, base = _serve(app)
    try:
        out = json.load(urlopen(f"{base}/api/samples?id=AA:BB:CC:00:00:07&since={now - 10}"))
        assert [t for t, _ in out["nodes"]["wbt-1"]] == list(range(now - 10, now))
        assert out["nodes"]["wbt-2"] == [[now - 5, -70]]
    finally:
        httpd.shutdown()


def _get(base, path):
    return json.load(urlopen(base + path))


def _post(base, path, body):
    from urllib.request import Request
    return json.load(urlopen(Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})))


SPOT = {"band": "wifi", "device": "aa:bb:cc:00:00:01", "device_name": "phone hotspot", "x": 3, "y": 4,
        "t": 1000, "rssi": {"wbt-1": -61.5, "wbt-2": -72}, "n": {"wbt-1": 12, "wbt-2": 9}}


def test_two_pages_cannot_overwrite_each_others_spots(tmp_path):
    """Regression: the page used to POST its whole spot list, so a page with an
    older list silently deleted spots another page had recorded."""
    path = tmp_path / "cal.json"
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3, cal_store=CalibrationStore(path))
    httpd, base = _serve(app)
    try:
        page_a = json.load(urlopen(base + "/api/calibration"))              # page A loads (empty)
        _post(base, "/api/calibration/points", {**SPOT, "device_name": "from B"})  # page B records
        _post(base, "/api/calibration/points", {**SPOT, "device_name": "from A", "x": 9})  # page A records later
        names = sorted(p["device_name"] for p in json.load(urlopen(base + "/api/calibration"))["points"])
        assert page_a["points"] == [] and names == ["from A", "from B"]
        # Whole-document replacement no longer exists.
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as exc:
            _post(base, "/api/calibration", {"points": []})
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
    assert len(CalibrationStore(path).data["points"]) == 2  # persisted


def test_delete_restore_fits_and_journal(tmp_path):
    from urllib.error import HTTPError

    path = tmp_path / "cal.json"
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3, cal_store=CalibrationStore(path))
    httpd, base = _serve(app)
    try:
        a = _post(base, "/api/calibration/points", SPOT)["point"]
        b = _post(base, "/api/calibration/points", {**SPOT, "x": 12})["point"]
        assert a["id"] != b["id"]
        _post(base, "/api/calibration/points/delete", {"ids": [a["id"]]})
        assert [p["id"] for p in json.load(urlopen(base + "/api/calibration"))["points"]] == [b["id"]]
        deleted = json.load(urlopen(base + "/api/calibration/deleted"))["points"]
        assert [p["id"] for p in deleted] == [a["id"]]
        _post(base, "/api/calibration/points/restore", {"ids": [a["id"]]})
        assert {p["id"] for p in json.load(urlopen(base + "/api/calibration"))["points"]} == {a["id"], b["id"]}
        assert json.load(urlopen(base + "/api/calibration/deleted"))["points"] == []
        fit = {"n": 2.4, "p0": -58, "sigma": 4.1, "offsets": {}}
        _post(base, "/api/calibration/fits/wifi", {"fit": fit})
        state = json.load(urlopen(base + "/api/state"))
        assert state["calibration_fits"] == {"wifi": fit} and state["calibration_version"] == 5
        _post(base, "/api/calibration/fits/wifi", {"fit": None})
        assert json.load(urlopen(base + "/api/state"))["calibration_fits"] == {}
        for path_, bad in (("/api/calibration/points", {**SPOT, "rssi": {"wbt-1": 5}}),
                           ("/api/calibration/points", {**SPOT, "band": "radar"}),
                           ("/api/calibration/fits/radar", {"fit": {}})):
            with pytest.raises(HTTPError) as exc:
                _post(base, path_, bad)
            assert exc.value.code == 400
    finally:
        httpd.shutdown()
    ops = [json.loads(line)["op"] for line in (tmp_path / "cal.json.journal.jsonl").read_text().splitlines()]
    assert ops == ["add", "add", "delete", "restore", "fit", "fit"]


def _fake_kismet_log(path, packets):
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("create table datasources (uuid text, name text)")
    db.execute("create table packets (ts_sec int, sourcemac text, datasource text, signal int)")
    db.executemany("insert into datasources values (?, ?)", [("U1", "wbt-1:wifi"), ("U2", "wbt-2:wifi"), ("U3", "wbt-3:wifi")])
    db.executemany("insert into packets values (?, ?, ?, ?)", packets)
    db.commit()
    db.close()


def test_reassign_rebuilds_spot_from_kismet_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    phone = "AA:BB:CC:00:00:99"
    packets = [(1000 + i, phone, "U1", -50 - (i % 3)) for i in range(15)]
    packets += [(1000 + i, phone, "U2", -70) for i in range(0, 15, 3)]
    packets += [(1000 + i, phone, "U3", -80) for i in range(2)]      # too few: dropped
    packets += [(1030, phone, "U1", -10)]                              # outside the window
    _fake_kismet_log(logs / "Kismet-test.kismet", packets)
    rec = KismetLog(logs).spot(phone, 1000, 15)
    assert rec == {"rssi": {"wbt-1": -51.0, "wbt-2": -70.0}, "n": {"wbt-1": 15, "wbt-2": 5}}

    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3,
              cal_store=CalibrationStore(tmp_path / "cal.json"), kismet_log=KismetLog(logs))
    httpd, base = _serve(app)
    try:
        spot = _post(base, "/api/calibration/points", {**SPOT, "device": "aa:bb:cc:00:00:55", "device_name": "router"})["point"]
        out = _post(base, f"/api/calibration/points/{spot['id']}/reassign", {"device": phone, "device_name": "phone"})["point"]
        assert out["device"] == phone.lower() and out["device_name"] == "phone" and out["source"] == "kismet-log"
        assert out["rssi"] == {"wbt-1": -51.0, "wbt-2": -70.0} and (out["x"], out["y"], out["t"]) == (3, 4, 1000)
    finally:
        httpd.shutdown()


def test_bluetooth_spot_reads_data_rows(tmp_path):
    """Bluetooth capture logs no packets: sightings are data rows (linuxbthci)."""
    import sqlite3
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "Kismet-test.kismet"
    _fake_kismet_log(path, [(3000, "11:22:33:44:55:66", "U1", -40)])   # unrelated Wi-Fi traffic
    tag = "C8:D0:83:C6:F3:D9"
    db = sqlite3.connect(path)
    db.execute("create table data (ts_sec int, devmac text, datasource text, type text, signal int)")
    db.executemany("insert into datasources values (?, ?)", [("B1", "wbt-1:bt"), ("B2", "wbt-2:bt")])
    rows = [(3000 + i, tag, "B1", "linuxbthci", -60 - (i % 3)) for i in range(0, 45, 8)]   # 6 sightings
    rows += [(3000 + i, tag, "B2", "linuxbthci", -75) for i in range(0, 45, 9)]            # 5 sightings
    rows += [(3050, tag, "B1", "linuxbthci", 0)]                                           # no signal, log caught up
    db.executemany("insert into data values (?, ?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    klog = KismetLog(logs)
    assert klog.newest() == 3050
    assert klog.spot(tag.lower(), 3000, 45) == {"rssi": {"wbt-1": -61.0, "wbt-2": -75.0}, "n": {"wbt-1": 6, "wbt-2": 5}}


def _add_bt_rows(path, rows):
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("create table if not exists data (ts_sec int, devmac text, datasource text, type text, signal int)")
    db.executemany("insert or replace into datasources values (?, ?)", [("B1", "wbt-1:bt"), ("B2", "wbt-2:bt")])
    db.executemany("insert into data values (?, ?, ?, 'linuxbthci', ?)", rows)
    db.commit()
    db.close()


def test_log_tail_returns_each_reading_once_and_follows_new_logs(tmp_path):
    import sqlite3
    from server import LogTail
    logs = tmp_path / "logs"
    logs.mkdir()
    ap, tag = "AA:BB:CC:00:00:01", "C8:D0:83:00:00:02"
    first = logs / "Kismet-20261108-10-00-00-1.kismet"
    _fake_kismet_log(first, [(900, ap, "U1", -40)] + [(1000 + i, ap, "U1", -55) for i in range(5)] + [(1004, ap, "U2", 0)])
    _add_bt_rows(first, [(1002, tag, "B2", -70)])
    tail = LogTail(KismetLog(logs))
    rows = tail.read(now=1005, backfill_s=60)       # 900 is older than the backfill; signal 0 skipped
    assert sorted(set(rows)) == sorted({(ap.lower(), "wbt-1", float(1000 + i), -55) for i in range(5)} | {(tag.lower(), "wbt-2", 1002.0, -70)})
    assert tail.read(now=1006, backfill_s=60) == []
    db = sqlite3.connect(first)
    db.execute("insert into packets values (1006, ?, 'U3', -66)", (ap,))
    db.commit()
    db.close()
    assert tail.read(now=1007, backfill_s=60) == [(ap.lower(), "wbt-3", 1006.0, -66)]
    # Kismet restarted: a newer log file takes over.
    _fake_kismet_log(logs / "Kismet-20261108-11-00-00-1.kismet", [(1100, ap, "U2", -61)])
    assert tail.read(now=1101, backfill_s=60) == [(ap.lower(), "wbt-2", 1100.0, -61)]


class PeakPoller:
    """Kismet REST whose per-second history holds each second's peak (-40)."""
    base_url = "http://fake"

    def __init__(self, macs):
        self.macs = macs

    def fetch_devices(self, since=None):
        import time
        now = int(time.time())
        hist = {n: {now - i: -40 for i in range(10)} for n in ("wbt-1", "wbt-2")}
        out = [dev(self.macs[0], now, {"wbt-1": -40, "wbt-2": -40}, history=hist)]
        out.append(dev(self.macs[1], now, {"wbt-1": -40, "wbt-2": -40}, kind="BTLE", phy="BTLE", history=hist))
        return out


def test_live_rssi_comes_from_the_log_not_kismet_peaks(tmp_path):
    import time
    logs = tmp_path / "logs"
    logs.mkdir()
    ap, tag = "AA:BB:CC:00:00:01", "C8:D0:83:00:00:02"
    now = int(time.time())
    path = logs / "Kismet-test.kismet"
    _fake_kismet_log(path, [(now - i, ap, "U1", -60 + (i % 3) - 1) for i in range(20)]
                     + [(now - i, ap, "U2", -72) for i in range(0, 20, 2)])
    _add_bt_rows(path, [(now - i, tag, "B1", -66) for i in range(0, 30, 8)] + [(now - 5, tag, "B2", -80)])
    poller = PeakPoller([ap, tag])
    app = App(poller, load_layout(LAYOUT), window_s=180, poll_s=3, kismet_log=KismetLog(logs))
    app.poll_once()
    assert app.status["rssi_source"] == "log"
    devs = {d["id"]: d for d in app.tracker.snapshot(time.time())}
    wifi, bt = devs[ap.lower()]["nodes"], devs[tag.lower()]["nodes"]
    assert wifi["wbt-1"]["n"] == 20 and abs(wifi["wbt-1"]["rssi"] + 60) < 1 and wifi["wbt-2"]["rssi"] == -72
    assert bt["wbt-1"]["rssi"] == -66 and bt["wbt-1"]["n"] == 4 and bt["wbt-2"]["n"] == 1
    # Another poll adds nothing that was already read.
    app.poll_once()
    assert app.tracker.snapshot(time.time())[0]["nodes"]["wbt-1"]["n"] == 20
    # Without a readable log the console falls back to Kismet's per-second history.
    fallback = App(poller, load_layout(LAYOUT), window_s=180, poll_s=3, kismet_log=KismetLog(tmp_path / "none"))
    fallback.poll_once()
    assert fallback.status["rssi_source"] == "kismet"
    assert {d["id"]: d for d in fallback.tracker.snapshot(time.time())}[ap.lower()]["nodes"]["wbt-1"]["rssi"] == -40


def test_record_endpoint_uses_packet_log_medians(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    phone = "AA:BB:CC:00:00:42"
    t0 = 2000
    packets = [(t0 + i, phone, "U1", -48 - (i % 5)) for i in range(15)]       # 15 packets, median -50
    packets += [(t0 + i, phone, "U2", -66 + (i % 3)) for i in range(0, 15, 2)]  # 8 packets
    packets += [(t0 + 20, "11:22:33:44:55:66", "U1", -40)]                    # log has caught up past the window
    _fake_kismet_log(logs / "Kismet-test.kismet", packets)
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3,
              cal_store=CalibrationStore(tmp_path / "cal.json"), kismet_log=KismetLog(logs))
    httpd, base = _serve(app)
    try:
        out = _post(base, "/api/calibration/record", {"band": "wifi", "device": phone, "device_name": "phone hotspot",
                                                      "device_type": "wifi_ap", "x": 5, "y": 6, "t0": t0, "seconds": 15})["point"]
        assert out["rssi"] == {"wbt-1": -50.0, "wbt-2": -65.0} and out["n"] == {"wbt-1": 15, "wbt-2": 8}
        assert out["device"] == phone.lower() and out["device_name"] == "phone hotspot" and out["source"] == "kismet-log"
        assert [p["id"] for p in json.load(urlopen(base + "/api/calibration"))["points"]] == [out["id"]]
    finally:
        httpd.shutdown()


def test_walls_are_changed_one_operation_at_a_time(tmp_path):
    """Regression: the page used to POST its whole wall list. A page whose list
    had just been overwritten by a stale refresh then saved an empty list over
    a room the user had drawn."""
    from urllib.error import HTTPError

    path = tmp_path / "cal.json"
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3, cal_store=CalibrationStore(path))
    httpd, base = _serve(app)
    room = [{"x1": -8.5, "y1": 29, "x2": 7.5, "y2": 29}, {"x1": 7.5, "y1": 29, "x2": 7.5, "y2": 42},
            {"x1": 7.5, "y1": 42, "x2": -8.5, "y2": 42}, {"x1": -8.5, "y1": 42, "x2": -8.5, "y2": 29}]
    try:
        stale = json.load(urlopen(base + "/api/state"))                    # page B loads: no walls
        a = _post(base, "/api/walls/add", {"walls": room})                 # page A draws a room
        assert len(a["walls"]) == 4 and all(w["id"] for w in a["walls"])
        assert a["version"] > stale["calibration_version"]                 # B's refresh is older: B must not adopt it
        with pytest.raises(HTTPError) as exc:                              # B (old code) saving its stale list is refused
            _post(base, "/api/walls", {"walls": []})
        assert exc.value.code == 410
        b = _post(base, "/api/walls/add", {"walls": [{"x1": 10, "y1": -5, "x2": 10, "y2": 45, "draft": True}]})
        assert len(b["walls"]) == 5                                        # both pages' walls kept
        c = _post(base, "/api/walls/delete", {"ids": [a["walls"][0]["id"]]})
        assert len(c["walls"]) == 4
        d = _post(base, "/api/walls/accept", {})
        assert not any(w.get("draft") for w in d["walls"])
        assert json.load(urlopen(base + "/api/state"))["walls"] == d["walls"]
        with pytest.raises(HTTPError) as exc:
            _post(base, "/api/walls/add", {"walls": [{"x1": "a", "y1": 0, "x2": 1, "y2": 1}]})
        assert exc.value.code == 400
        e = _post(base, "/api/walls/clear", {})
        assert e["walls"] == []
        version = e["version"]
    finally:
        httpd.shutdown()
    reloaded = CalibrationStore(path)
    assert reloaded.data["walls"] == [] and reloaded.version == version   # counter survives a restart
    actions = [json.loads(line).get("action") for line in (tmp_path / "cal.json.journal.jsonl").read_text().splitlines()]
    assert actions == ["add", "add", "delete", "accept", "clear"]
    assert load_layout(LAYOUT)["models"]["wifi"]["wall_db"] == 4


def test_position_accuracy_fields(tmp_path):
    from urllib.error import HTTPError

    assert load_layout(LAYOUT)["node_sd_ft"] == 2
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3, cal_store=CalibrationStore(tmp_path / "cal.json"))
    httpd, base = _serve(app)
    try:
        pt = _post(base, "/api/calibration/points", {**SPOT, "pos_sd_ft": 0.5})["point"]
        assert pt["pos_sd_ft"] == 0.5
        with pytest.raises(HTTPError) as exc:
            _post(base, "/api/calibration/points", {**SPOT, "pos_sd_ft": -1})
        assert exc.value.code == 400
        walls = _post(base, "/api/walls/add", {"walls": [{"x1": 0, "y1": 0, "x2": 5, "y2": 0, "sd_ft": 4},
                                                          {"x1": 0, "y1": 1, "x2": 5, "y2": 1, "sd_ft": 99}]})["walls"]
        assert walls[0]["sd_ft"] == 4.0 and "sd_ft" not in walls[1]   # out-of-range accuracy is dropped (default applies)
    finally:
        httpd.shutdown()


def test_kismet_log_reads_wait_for_the_write_lock(tmp_path):
    """Regression: unlocked reads of the live log failed with "database disk
    image is malformed" while Kismet was writing, and the error dropped the
    connection. Reads now take the shared lock and retry while Kismet writes."""
    import sqlite3
    from urllib.error import HTTPError

    logs = tmp_path / "logs"
    logs.mkdir()
    db_path = logs / "Kismet-live.kismet"
    phone = "AA:BB:CC:00:00:77"
    _fake_kismet_log(db_path, [(3000 + i, phone, "U1", -50) for i in range(15)] + [(3000 + i, phone, "U2", -70) for i in range(15)]
                     + [(3030, phone, "U1", -50)])
    writer = sqlite3.connect(db_path, timeout=0, check_same_thread=False)
    writer.execute("begin exclusive")                       # Kismet mid-write
    threading.Timer(1.2, writer.rollback).start()             # ... finishes a moment later
    klog = KismetLog(logs)
    assert klog.spot(phone, 3000, 15)["n"] == {"wbt-1": 15, "wbt-2": 15}

    writer.execute("begin exclusive")                        # a lock that never clears
    klog.RETRIES, klog.RETRY_S = 3, 0.05
    app = App(None, load_layout(LAYOUT), window_s=180, poll_s=3,
              cal_store=CalibrationStore(tmp_path / "cal.json"), kismet_log=klog)
    httpd, base = _serve(app)
    try:
        with pytest.raises(HTTPError) as exc:
            _post(base, "/api/calibration/record", {"band": "wifi", "device": phone, "x": 1, "y": 1, "t0": 3000, "seconds": 15})
        assert exc.value.code == 503 and "busy" in json.load(exc.value)["error"]
    finally:
        httpd.shutdown()
        writer.rollback()
        writer.close()


def test_source_health_flags_running_sources_without_packets():
    """Regression: three Bluetooth sources showed as running in Kismet, with no
    error, while delivering zero packets for hours. Nothing flagged it."""
    from server import SOURCE_SILENT_S, SourceHealth

    h = SourceHealth()
    src = lambda name, pkts, running=True, err=None: {"name": name, "uuid": name, "running": running, "error": err, "packets": pkts}
    h.update([src("wbt-1:bt", 0), src("wbt-3:bt", 100), src("wbt-1:tpms", 0), src("wbt-2:bt", 5, running=False, err="IPC closed")], now=0)
    assert {r["name"]: r["status"] for r in h.report(now=60)} == {"wbt-1:bt": "ok", "wbt-1:tpms": "ok", "wbt-2:bt": "error", "wbt-3:bt": "ok"}
    h.update([src("wbt-1:bt", 0), src("wbt-3:bt", 900), src("wbt-1:tpms", 0), src("wbt-2:bt", 5, running=False, err="IPC closed")], now=SOURCE_SILENT_S + 10)
    status = {r["name"]: r["status"] for r in h.report(now=SOURCE_SILENT_S + 10)}
    assert status["wbt-1:bt"] == "silent"       # running, nothing new for 5+ min
    assert status["wbt-3:bt"] == "ok"           # still growing
    assert status["wbt-1:tpms"] == "ok"         # TPMS is quiet by nature
    h.update([src("wbt-1:bt", 3)], now=SOURCE_SILENT_S + 40)
    assert {r["name"]: r["status"] for r in h.report(now=SOURCE_SILENT_S + 40)}["wbt-1:bt"] == "ok"   # packets again
    # Kismet restarted: counters start again from 0. That is activity, not silence.
    h.update([src("wbt-3:bt", 12)], now=SOURCE_SILENT_S + 50)
    h.update([src("wbt-3:bt", 40)], now=2 * SOURCE_SILENT_S + 60)
    assert {r["name"]: r["status"] for r in h.report(now=2 * SOURCE_SILENT_S + 60)}["wbt-3:bt"] == "ok"


def test_positions_history_and_geo(tmp_path):
    """The server places devices, stores their positions per site and adds lat/lon once node GPS is set."""
    import time
    from sites import SiteManager
    template = load_layout(LAYOUT)
    sites = SiteManager(tmp_path / "data", template, CalibrationStore)
    poller = FakePoller()
    app = App(poller, None, window_s=60, poll_s=3, sites=sites)
    httpd, base = _serve(app)
    try:
        app.poll_once()
        dev_state = _get(base, "/api/state")["devices"][0]
        pos = dev_state["position"]
        assert pos["nodes"] == 3 and not pos["calibrated"] and "lat" not in pos
        hist = _get(base, f"/api/history?id={dev_state['id']}&since=0")["points"]
        assert len(hist) == 1 and (hist[0]["x"], hist[0]["y"]) == (pos["x"], pos["y"])
        app.poll_once()                                           # within HISTORY_MIN_S: not stored again
        assert len(_get(base, f"/api/history?id={dev_state['id']}&since=0")["points"]) == 1
        nodes = sites.layout()["nodes"]
        n1, n2 = list(nodes)[:2]
        _post(base, "/api/site/geo", {"node": n1, "lat": 45.0, "lon": 10.0})
        assert _get(base, "/api/state")["geo"] is None             # one point and no heading: not enough
        _post(base, "/api/site/geo", {"heading_deg": 0})
        app.poll_once()
        st = _get(base, "/api/state")
        assert st["geo"]["method"] == "heading" and "lat" in st["devices"][0]["position"]
        # heading 0: page "up" is north, so a device north of wbt-1 on the page is north of it on the map
        p = st["devices"][0]["position"]
        assert (p["lat"] > 45.0) == (p["y"] < nodes[n1][1])
        with pytest.raises(Exception):
            _post(base, "/api/site/geo", {"node": "wbt-99", "lat": 1, "lon": 2})
        assert (tmp_path / "data" / "sites").exists()
        assert any(f.name.endswith("-positions.jsonl") for f in (tmp_path / "data" / "sites").glob("*/history/*"))
    finally:
        httpd.shutdown()


# ---------- crediting readings to the frame's transmitter ----------

def _mac(s):
    return bytes(int(x, 16) for x in s.split(":"))


def _frame(kind, a1, a2=None, a3=None):
    """Radiotap (8 bytes) + 802.11 header. kind: beacon | data_to_ds | data_from_ds | ack."""
    rt = bytes([0, 0, 8, 0, 0, 0, 0, 0])
    fc = {"beacon": b"\x80\x00", "data_to_ds": b"\x08\x01", "data_from_ds": b"\x08\x02", "ack": b"\xd4\x00"}[kind]
    if kind == "ack":
        return rt + fc + b"\x00\x00" + _mac(a1)
    return rt + fc + b"\x00\x00" + _mac(a1) + _mac(a2) + _mac(a3) + b"\x00\x00"


def _frames_log(path, rows):
    """Kismet log with raw frames: rows of (ts, sourcemac as Kismet logs it, datasource, signal, frame)."""
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("create table datasources (uuid text, name text)")
    db.execute("create table packets (ts_sec int, ts_usec int, sourcemac text, datasource text, signal int, dlt int, packet blob)")
    db.executemany("insert into datasources values (?, ?)", [("U1", "wbt-1:wifi"), ("U2", "wbt-2:wifi")])
    db.executemany("insert into packets values (?, 0, ?, ?, ?, 127, ?)", rows)
    db.commit()
    db.close()


AP, CLIENT, BCAST = "aa:00:00:00:00:01", "cc:00:00:00:00:02", "ff:ff:ff:ff:ff:ff"


def _client_and_ap_rows(t0):
    rows = []
    for i in range(6):
        rows.append((t0 + i, CLIENT, "U1", -70, _frame("data_to_ds", AP, CLIENT, AP)))      # the client itself
        rows.append((t0 + i, CLIENT, "U1", -30, _frame("ack", CLIENT)))                   # AP's ACK, logged under the client
        rows.append((t0 + i, CLIENT, "U1", -32, _frame("data_from_ds", BCAST, AP, CLIENT)))  # relayed by the AP
        rows.append((t0 + i, AP, "U2", -50, _frame("beacon", BCAST, AP, AP)))
    return rows


def test_frame_transmitter_reads_address_2():
    from server import frame_transmitter
    assert frame_transmitter(127, _frame("data_from_ds", BCAST, AP, CLIENT)) == AP
    assert frame_transmitter(127, _frame("ack", CLIENT)) is None
    assert frame_transmitter(127, b"") is False and frame_transmitter(1, b"\x00" * 30) is False


def test_spot_credits_readings_to_the_transmitter(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _frames_log(logs / "Kismet-test.kismet", _client_and_ap_rows(1000))
    klog = KismetLog(logs)
    # Before: the AP's ACKs and relays (-30, -32) were counted as the client's.
    assert klog.spot(CLIENT, 1000, 10) == {"rssi": {"wbt-1": -70.0}, "n": {"wbt-1": 6}}
    ap = klog.spot(AP, 1000, 10)
    assert ap["rssi"] == {"wbt-1": -32.0, "wbt-2": -50.0} and ap["n"] == {"wbt-1": 6, "wbt-2": 6}


def test_log_tail_credits_readings_to_the_transmitter(tmp_path):
    from server import LogTail
    logs = tmp_path / "logs"
    logs.mkdir()
    _frames_log(logs / "Kismet-test.kismet", _client_and_ap_rows(1000))
    rows = LogTail(KismetLog(logs)).read(now=1006, backfill_s=60)
    from collections import Counter
    assert Counter((mac, node, sig) for mac, node, _, sig in rows) == Counter(
        {(CLIENT, "wbt-1", -70): 6, (AP, "wbt-1", -32): 6, (AP, "wbt-2", -50): 6})   # ACKs dropped


def test_node_links_from_the_log(tmp_path):
    """Each node radio heard by the other nodes, credited to the radio (not to the AP answering it)."""
    import sqlite3
    import time
    layout = load_layout(LAYOUT)
    radios = layout["self_emitters"]
    nodes = [n for n in radios if n in layout["nodes"]][:3]
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "Kismet-test.kismet"
    db = sqlite3.connect(path)
    db.execute("create table datasources (uuid text, name text)")
    db.execute("create table packets (ts_sec int, ts_usec int, sourcemac text, datasource text, signal int, dlt int, packet blob)")
    db.executemany("insert into datasources values (?, ?)", [(f"U{i}", f"{n}:wifi") for i, n in enumerate(nodes)])
    now = int(time.time())
    rows = []
    for i, tx in enumerate(nodes):
        mac = radios[tx]
        for j, rx in enumerate(nodes):
            for k in range(12):
                sig = -30 if rx == tx else -60 - 5 * abs(i - j)
                rows.append((now - k, mac, f"U{j}", sig, _frame("data_to_ds", AP, mac, AP)))
                rows.append((now - k, mac, f"U{j}", -25, _frame("ack", mac)))                  # AP's ACK: not the radio
    db.executemany("insert into packets values (?, 0, ?, ?, ?, 127, ?)", rows)
    db.commit()
    db.close()
    app = App(None, layout, window_s=60, poll_s=3, kismet_log=KismetLog(logs))
    doc = app.refresh_links()
    links = {x["node"]: x for x in doc["links"]}
    assert set(links) == set(nodes)
    a, b, c = nodes
    assert links[a]["rssi"] == {b: -65.0, c: -70.0}              # own node excluded, ACKs ignored
    assert links[a]["n"] == {b: 12, c: 12}
    state_links = {x["node"]: x for x in app.state()["node_links"]["links"]}
    assert state_links[a]["x"] == layout["nodes"][a][0] and state_links[a]["device"] == radios[a]
    # Re-reading the same log keeps the stored doc (no change of 2 dB or more)
    assert app.refresh_links() is doc


def test_rebuild_spots_from_the_log(tmp_path):
    """Spots recorded before the transmitter fix are re-read from the log where it still covers them."""
    logs = tmp_path / "logs"
    logs.mkdir()
    later = [(1015, AP, "U2", -50, _frame("beacon", BCAST, AP, AP))]       # the log runs past the spots' windows
    _frames_log(logs / "Kismet-test.kismet", _client_and_ap_rows(1000) + later)
    store = CalibrationStore(tmp_path / "cal.json")
    old = store.add_point({**SPOT, "device": CLIENT, "t": 1000, "seconds": 10, "rssi": {"wbt-1": -31.0, "wbt-2": -60.0}})
    gone = store.add_point({**SPOT, "device": CLIENT, "t": 50, "seconds": 10})            # before the log starts
    manual = store.add_point({k: v for k, v in SPOT.items() if k not in ("t", "device")})
    app = App(None, load_layout(LAYOUT), window_s=60, poll_s=3, cal_store=store, kismet_log=KismetLog(logs))
    ap_spot = store.add_point({**SPOT, "device": AP, "t": 1000, "seconds": 10, "rssi": {"wbt-1": -30.0, "wbt-2": -50.0}})
    out = app.rebuild_spots()
    assert out["rebuilt"] == [ap_spot["id"]]
    assert {s["id"]: s["why"] for s in out["skipped"]} == {
        old["id"]: "heard by 1 node(s)",          # without the AP's frames only wbt-1 heard the client: left as is
        gone["id"]: "its log is not available here", manual["id"]: "not recorded from the log"}
    pts = {p["id"]: p for p in store.data["points"]}
    assert pts[ap_spot["id"]]["rssi"] == {"wbt-1": -32.0, "wbt-2": -50.0} and pts[ap_spot["id"]]["rebuilt"]
    assert app.rebuild_spots()["unchanged"] == [ap_spot["id"]]
