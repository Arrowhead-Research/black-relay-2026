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
