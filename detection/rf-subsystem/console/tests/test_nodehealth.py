"""Node agent heartbeats: problems, token, restart queue, node status and alerts."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alerting import AlertEngine  # noqa: E402
from nodehealth import NodeHealth  # noqa: E402
from publish import Publisher, node_states  # noqa: E402
from test_publish import NODE_STATUS, FakeDurable, FakeLive  # noqa: E402

AGENT = Path(__file__).resolve().parents[2] / "node" / "agent" / "rf-node-agent"
UNITS_OK = {u: {"active": "active", "sub": "running", "restarts": 0}
            for u in ("kismet-cap-wifi", "kismet-cap-bt", "kismet-cap-tpms", "tailscaled")}


def beat(node="wbt-1", **kw):
    return {"node": node, "agent": "1", "uptime_s": 3600, "temp_c": 55.0, "throttled": 0, "disk_free_pct": 60,
            "units": UNITS_OK, "adapters": {"wifi": True, "bt": True, "sdr": True}, "tailscale_ip": "100.64.0.14", **kw}


def test_problems_from_heartbeats():
    h = NodeHealth()
    h.heartbeat(beat(), now=1000)
    assert h.report(1010)["wbt-1"]["problems"] == []
    bad_units = {**UNITS_OK, "kismet-cap-bt": {"active": "failed", "sub": "failed", "restarts": 5}}
    h.heartbeat(beat(temp_c=83.0, throttled=0x50005, units=bad_units, adapters={"wifi": True, "bt": False, "sdr": True},
                     disk_free_pct=4), now=1020)
    assert h.report(1030)["wbt-1"]["problems"] == ["hot (83 °C)", "undervoltage", "throttled", "kismet-cap-bt failed",
                                                   "no Bluetooth adapter", "disk 4% free"]
    assert "agent silent" in h.report(1020 + 91)["wbt-1"]["problems"]
    with pytest.raises(ValueError):
        h.heartbeat({"node": "../etc"}, now=1)


def test_restart_queue():
    h = NodeHealth()
    with pytest.raises(KeyError):
        h.queue_restart("wbt-1", "kismet-cap-bt")          # never heard from
    h.heartbeat(beat(), now=0)
    with pytest.raises(ValueError):
        h.queue_restart("wbt-1", "sshd")                    # only capture units
    h.queue_restart("wbt-1", "kismet-cap-bt")
    assert h.report(1)["wbt-1"]["pending"] == ["kismet-cap-bt"]
    assert h.heartbeat(beat(), now=30) == {"restart": ["kismet-cap-bt"]}
    assert h.heartbeat(beat(), now=60) == {"restart": []}


def test_node_status_carries_health_without_alerts():
    h = NodeHealth()
    h.heartbeat(beat(), now=0)
    sources = [{"name": "wbt-1:wifi", "status": "ok"}, {"name": "wbt-1:bt", "status": "ok"}]
    st = node_states(sources, h.report(5))
    assert st["wbt-1"]["state"] == "online" and st["wbt-1"]["health"]["temp_c"] == 55.0
    eng = AlertEngine()
    assert eng.evaluate("s", 5, {}, {}, {}, [], st, set()) == []
    h.heartbeat(beat(throttled=0x1), now=10)
    st = node_states(sources, h.report(15))
    assert st["wbt-1"]["state"] == "degraded"
    assert eng.evaluate("s", 15, {}, {}, {}, [], st, set()) == []      # health problems: node status only, no alert
    durable = FakeDurable()
    Publisher(FakeLive(), durable).nodes(sources, {"id": "s", "name": "S"}, 15, h.report(15))
    NODE_STATUS.validate(durable.sent[0][1])
    assert durable.sent[0][1]["health"]["problems"] == ["undervoltage"]
    # a node the agent reports for but Kismet has no sources for still appears
    assert node_states([], h.report(200))["wbt-1"]["state"] == "offline"


def test_heartbeat_endpoint_token_and_admin_restart(tmp_path):
    from server import App, load_layout
    from test_server import LAYOUT, _post, _serve
    app = App(None, load_layout(LAYOUT), window_s=60, poll_s=3, node_token="s3cret-node-token")
    httpd, base = _serve(app)
    try:
        def hb(token):
            req = Request(base + "/api/nodes/heartbeat", data=json.dumps(beat()).encode(),
                          headers={"Content-Type": "application/json", **({"X-Node-Token": token} if token else {})})
            return json.load(urlopen(req))
        with pytest.raises(HTTPError) as e:
            hb(None)
        assert e.value.code == 401
        assert hb("s3cret-node-token") == {"restart": []}
        _post(base, "/api/nodes/wbt-1/restart", {"unit": "kismet-cap-tpms"})   # sign-in off in tests: admin
        assert hb("s3cret-node-token") == {"restart": ["kismet-cap-tpms"]}
        st = json.load(urlopen(base + "/api/state"))
        assert st["node_token"] is True and st["nodes_health"]["wbt-1"]["tailscale_ip"] == "100.64.0.14"
    finally:
        httpd.shutdown()


def test_agent_collects_without_raising(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("rf_node_agent", str(AGENT))
    spec = importlib.util.spec_from_loader("rf_node_agent", loader)
    agent = importlib.util.module_from_spec(spec)
    loader.exec_module(agent)
    env = tmp_path / "rf-node.env"
    env.write_text("# test\nCENTRAL_HOST=central\nNODE_NAME=wbt-9\nWIFI_IFACE=lo\nNODE_TOKEN='abc'\n")
    cfg = agent.load_env(str(env))
    assert cfg["CENTRAL_HOST"] == "central" and cfg["NODE_TOKEN"] == "abc"
    data = agent.collect(cfg, [])
    assert data["node"] == "wbt-9" and set(data["units"]) == set(agent.UNITS)
    assert all(isinstance(v, bool) for v in data["adapters"].values())
    json.dumps(data)


SS_SAMPLE = """0      0      100.64.0.14:35656 100.64.0.16:3501 users:(("kismet_cap_sdr_",pid=121135,fd=5))
\t cubic wscale:8,7 rto:952 mss:1228 bytes_sent:45062 bytes_retrans:1220 bytes_acked:43843 bytes_received:43667 segs_out:2385
0      0      100.64.0.14:35662 100.64.0.16:3501 users:(("kismet_cap_linu",pid=121152,fd=5))
\t cubic wscale:8,7 rto:452 mss:1228 bytes_sent:4080017 bytes_acked:4021101 bytes_received:43661 segs_out:11926
"""


def _agent():
    loader = importlib.machinery.SourceFileLoader("rf_node_agent", str(AGENT))
    spec = importlib.util.spec_from_loader("rf_node_agent", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_agent_reads_capture_data_per_connection():
    conns = _agent().parse_ss(SS_SAMPLE)
    assert conns[121135] == ("100.64.0.14:35656 100.64.0.16:3501", 176)          # TPMS: keepalives only
    assert conns[121152] == ("100.64.0.14:35662 100.64.0.16:3501", 4021101 - 43661)


def test_watchdog_restarts_a_stuck_unit_only():
    a = _agent()
    dog = a.Watchdog(stall_s=600, min_bytes=2048, gap_s=600)
    k1, k2 = "c1", "c2"
    assert dog.step(0, {"bt": (k1, 1000), "wifi": (k2, 5000)}) == []
    t = 0
    for t in range(30, 600, 30):                    # wifi keeps delivering, bt delivers ~nothing (keepalives)
        assert dog.step(t, {"bt": (k1, 1000 + t // 30), "wifi": (k2, 5000 + 10000 * t)}) == []
    assert dog.step(600, {"bt": (k1, 1100), "wifi": (k2, 9_000_000)}) == ["bt"]     # 10 min without data
    assert dog.step(630, {"bt": (k1, 1100), "wifi": (k2, 9_100_000)}) == []          # just restarted
    assert dog.step(660, {"bt": ("c3", 0), "wifi": (k2, 9_200_000)}) == []           # new connection: clock restarts
    assert dog.step(1300, {"bt": ("c3", 10), "wifi": (k2, 9_300_000)}) == ["bt"]     # stuck again, gap passed
    # no connection for 10 min counts as stuck too (a hung helper may hold none), once per gap
    dog2 = a.Watchdog(stall_s=600, min_bytes=2048, gap_s=600)
    got = [t for t in range(0, 1900, 60) if dog2.step(t, {"bt": None})]
    assert got == [600, 1200, 1800]
    dog4 = a.Watchdog(stall_s=600, min_bytes=2048, gap_s=600)        # a connection dropping restarts the clock
    assert dog4.step(0, {"bt": ("c", 0)}) == [] and dog4.step(500, {"bt": None}) == [] and dog4.step(1000, {"bt": None}) == []
    assert dog4.step(1100, {"bt": None}) == ["bt"]
    # a restart at most once per gap
    dog3 = a.Watchdog(stall_s=60, min_bytes=2048, gap_s=600)
    assert dog3.step(0, {"bt": ("x", 0)}) == [] and dog3.step(61, {"bt": ("x", 0)}) == ["bt"]
    assert dog3.step(62, {"bt": ("x", 0)}) == [] and dog3.step(200, {"bt": ("x", 0)}) == []   # gap not passed
    assert dog3.step(700, {"bt": ("x", 0)}) == ["bt"]


def test_watchdog_restarts_show_as_node_problems():
    h = NodeHealth()
    h.heartbeat(beat(actions=[{"restart": "kismet-cap-bt", "ok": True, "t": 1000, "auto": True}]), now=1000)
    h.heartbeat(beat(actions=[]), now=1030)                          # reported once; still listed
    assert "kismet-cap-bt restarted by watchdog" in h.report(1040)["wbt-1"]["problems"]
    h.heartbeat(beat(), now=1000 + 1801)
    assert "kismet-cap-bt restarted by watchdog" not in h.report(2802)["wbt-1"]["problems"]
