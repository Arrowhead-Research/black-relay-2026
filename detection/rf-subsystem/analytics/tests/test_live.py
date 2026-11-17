"""Live path: Kismet device JSON -> observations -> analytics -> alerts -> wire.

Covers the contract between the Kismet adapter and the alert pipeline, and
runs the ``rfanalytics live`` command against a fake Kismet REST server.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rfanalytics.alerts import AlertEngine, load_rules
from rfanalytics.analyzer import Analyzer, AnalyzerConfig
from rfanalytics.cli import main
from rfanalytics.formats import to_wire
from rfanalytics.friendly import FriendlyList, load_friendly
from rfanalytics.kismet import KismetPoller
from rfanalytics.observation import parse_observation
from rfanalytics.pipeline import Pipeline
from rfanalytics.site import Node, Site, Zone, load_site

from conftest import RF_ROOT

CONFIG = RF_ROOT / "analytics" / "config"

WATCHED = "aa:bb:cc:00:00:01"


def _site() -> Site:
    zones = tuple(Zone(f"zone-wbt-{i}", (i - 1) * 10, 0, i * 10, 10) for i in (1, 2))
    nodes = {f"wbt-{i}": Node(f"wbt-{i}", f"zone-wbt-{i}", ((i - 1) * 10 + 5, 5)) for i in (1, 2)}
    return Site("wbt-test", zones, nodes)


def _device(t: float, rssi_by_node: dict[str, int | None], mac: str = WATCHED) -> dict:
    """A Kismet Wi-Fi AP as returned with keep_per_datasource_stats=true."""
    seenby = []
    for node, rssi in rssi_by_node.items():
        sig = ({"kismet.common.signal.type": "dbm", "kismet.common.signal.last_signal": rssi}
               if rssi is not None else {"kismet.common.signal.type": "none", "kismet.common.signal.last_signal": 0})
        seenby.append({
            "kismet.common.seenby.last_time": t,
            "kismet.common.seenby.source": {"kismet.datasource.name": f"{node}:wifi"},
            "kismet.common.seenby.signal": sig,
        })
    return {
        "kismet.device.base.macaddr": mac.upper(),
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.frequency": 2437000,
        "kismet.device.base.last_time": t,
        "kismet.device.base.signal": {"kismet.common.signal.type": "dbm", "kismet.common.signal.last_signal": -40},
        "kismet.device.base.seenby": seenby,
    }


def _rules(silence_s: float = 300) -> list[dict]:
    return [
        {"id": "watchlist", "type": "watchlist", "severity": "high", "emitter_ids": [WATCHED]},
        {"id": "node-offline", "type": "node_offline", "severity": "high", "silence_s": silence_s},
    ]


def test_per_seenby_signal_is_used_per_node():
    poller = KismetPoller("http://example.invalid")
    recs = poller.observations_from_device(_device(1_700_000_000, {"wbt-1": -48, "wbt-2": -81}))
    by_node = {r["node_id"]: r for r in recs}
    assert by_node["wbt-1"]["rssi_dbm"] == -48
    assert by_node["wbt-2"]["rssi_dbm"] == -81
    assert not any(r["attributes"].get("rssi_is_aggregate") for r in recs)


def test_missing_seenby_signal_falls_back_to_aggregate_and_flags_it():
    poller = KismetPoller("http://example.invalid")
    recs = poller.observations_from_device(_device(1_700_000_000, {"wbt-1": -48, "wbt-2": None}))
    by_node = {r["node_id"]: r for r in recs}
    assert "rssi_is_aggregate" not in by_node["wbt-1"]["attributes"]
    assert by_node["wbt-2"]["rssi_dbm"] == -40
    assert by_node["wbt-2"]["attributes"]["rssi_is_aggregate"] is True


def test_kismet_polls_produce_watchlist_alert_on_alert_topic():
    site = _site()
    pipeline = Pipeline(Analyzer(site, FriendlyList(), AnalyzerConfig()),
                        AlertEngine(_rules(), node_ids=sorted(site.nodes)))
    poller = KismetPoller("http://example.invalid")
    wire, events = [], []
    t0 = 1_700_000_000
    # Successive polls, as Kismet's last_time advances for the device.
    for i in range(4):
        for rec in poller.observations_from_device(_device(t0 + 5 * i, {"wbt-1": -45, "wbt-2": -78})):
            evs, messages = pipeline.process(parse_observation(rec))
            events += evs
            wire += [to_wire(m) for m in messages]
    alerts = [p for topic, p in wire if topic == "detection/rf/alerts"]
    assert len(alerts) == 1
    assert alerts[0]["schema"] == "rf.alert.v0"
    assert alerts[0]["rule_id"] == "watchlist"
    assert alerts[0]["emitter"]["id"] == WATCHED
    # Per-node RSSI places the AP at the node that hears it loudest.
    zones = [e.data["zone"] for e in events if e.type == "zone_estimate"]
    assert zones and zones[-1] == "zone-wbt-1"


def test_wall_clock_tick_reports_silent_node_without_observations():
    site = _site()
    pipeline = Pipeline(Analyzer(site, FriendlyList(), AnalyzerConfig()),
                        AlertEngine(_rules(silence_s=60), node_ids=sorted(site.nodes)))
    poller = KismetPoller("http://example.invalid")
    t0 = 1_700_000_000
    for t in (t0, t0 + 40):  # only wbt-1 hears anything
        for rec in poller.observations_from_device(_device(t, {"wbt-1": -45})):
            pipeline.process(parse_observation(rec))
    assert pipeline.tick(t0 + 50) == []
    wire = [to_wire(m) for m in pipeline.tick(t0 + 61)]
    status = [p for topic, p in wire if topic == "detection/rf/status/nodes"]
    assert [(p["node_id"], p["state"]) for p in status] == [("wbt-2", "offline")]
    # Ticks never run backwards (e.g. a late observation timestamp).
    assert pipeline.tick(t0 + 10) == []


class _FakeKismet(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).paths.append(self.path)
        now = int(time.time())
        heard_now = _device(now, {"wbt-1": -45, "wbt-2": -79})
        # Heard now by wbt-1, but wbt-2's seenby entry is ten minutes old.
        stale_seenby = _device(now, {"wbt-1": -60, "wbt-2": -70}, mac="aa:bb:cc:00:00:02")
        stale_seenby["kismet.device.base.seenby"][1]["kismet.common.seenby.last_time"] = now - 600
        body = json.dumps([heard_now, stale_seenby]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fake_kismet():
    _FakeKismet.paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeKismet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_live_command_end_to_end(fake_kismet, tmp_path):
    site = tmp_path / "site.yaml"
    site.write_text(
        "name: t\n"
        "zones:\n  - {name: zone-wbt-1, rect: [0, 0, 10, 10]}\n  - {name: zone-wbt-2, rect: [10, 0, 20, 10]}\n"
        "nodes:\n  - {node_id: wbt-1, zone: zone-wbt-1, position: [5, 5]}\n"
        "  - {node_id: wbt-2, zone: zone-wbt-2, position: [15, 5]}\n"
    )
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        f"rules:\n  - {{id: watchlist, type: watchlist, severity: high, emitter_ids: ['{WATCHED}']}}\n"
        "  - {id: node-offline, type: node_offline, severity: high, silence_s: 300}\n"
    )
    alerts = tmp_path / "alerts.jsonl"
    obs = tmp_path / "obs.jsonl"
    rc = main([
        "live", "--kismet-url", fake_kismet, "--site", str(site), "--rules", str(rules),
        "--poll", "1", "--duration", "4.5", "--alerts-out", str(alerts), "--observations-out", str(obs),
        "--outbox", str(tmp_path / "outbox.jsonl"), "--backfill-s", "60",
    ])
    assert rc == 0
    # First poll asks Kismet only for recently heard devices (relative last-time).
    assert _FakeKismet.paths[0].startswith("/devices/last-time/-60/")
    out = [json.loads(line) for line in alerts.read_text().splitlines()]
    assert [(m["topic"], m["payload"]["rule_id"]) for m in out] == [("detection/rf/alerts", "watchlist")]
    recs = [json.loads(line) for line in obs.read_text().splitlines()]
    assert {r["node_id"] for r in recs} == {"wbt-1", "wbt-2"}
    # The ten-minute-old seenby entry is dropped, so it cannot replay history
    # (it would otherwise report wbt-2 offline in the past).
    assert not any(r["emitter_id"] == "aa:bb:cc:00:00:02" and r["node_id"] == "wbt-2" for r in recs)
    assert not any(r["attributes"].get("rssi_is_aggregate") for r in recs)


@pytest.mark.parametrize("rules", sorted(CONFIG.glob("rules*.yaml")), ids=lambda p: p.name)
def test_shipped_rules_load(rules):
    """The rf-analytics service starts from these files; a bad one crash-loops it."""
    specs, tz = load_rules(rules)
    AlertEngine(specs, node_ids=["wbt-1"], tz=tz)


def test_shipped_wbt_site_and_friendly_load():
    site = load_site(CONFIG / "site.wbt-nodes.example.yaml")
    assert sorted(site.nodes) == ["wbt-1", "wbt-2", "wbt-3", "wbt-4"]
    zones = {z.name for z in site.zones}
    specs, _ = load_rules(CONFIG / "rules.wbt-live.example.yaml")
    for spec in specs:
        assert set(spec.get("zones") or []) <= zones, f"{spec['id']} names zones not in the WBT site"
    friendly = load_friendly(CONFIG / "friendly.wbt-nodes.example.yaml")
    assert len(friendly) == 4
