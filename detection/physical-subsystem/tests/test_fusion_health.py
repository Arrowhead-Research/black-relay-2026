"""Standalone fusion container: metrics (depth, dropped, last PUBACK age) and the /healthz + healthcheck command."""
import asyncio, time, urllib.request, urllib.error
import pytest
from aiohttp import web
from br2 import config as C, cli
from br2.outbound.api import make_fusion_health_app, render_metrics
from br2.outbound.state import OutState
from test_fusion_publisher import Rig, mk, commit, zone


async def test_metrics_expose_depth_dropped_and_puback_age(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1)])
    await r.pub.tick()
    text = render_metrics(r.st)
    assert "physical_fusion_outbox_depth " in text and 'physical_fusion_outbox_dropped_total{reason="size"} 0' in text
    assert "physical_fusion_last_puback_age_seconds -1" in text                 # nothing acknowledged yet
    r.pub.sender.last_puback_mono = r.mono() - 7
    m = r.pub.metrics()
    assert m["last_puback_age_s"] == 7 and m["depth"] >= 2 and m["producer_age_s"] == 0
    assert "physical_fusion_last_puback_age_seconds 7" in render_metrics(r.st)


async def test_health_ok_then_unhealthy_when_producer_stalls_or_backlog_ages(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1)])
    await r.pub.tick()
    h = r.pub.health()
    assert h["ok"] and h["problems"] == [] and h["depth"] >= 2 and h["dropped"] == 0
    r.mono.t += 31                                                              # producer loop has not run for 31 s
    h = r.pub.health()
    assert not h["ok"] and "producer stalled" in h["problems"][0]
    await r.pub.tick()
    assert r.pub.health()["ok"]
    real = time.time
    r.pub.outbox.clock = lambda: real() + 400                                   # oldest queued message is 400 s old
    h = r.pub.health()
    assert not h["ok"] and "broker unreachable or not acking" in h["problems"][0]
    r.pub.outbox.clock = real
    # an idle connected publisher (empty outbox) is healthy however long ago the last PUBACK was
    for e in r.pub.outbox.pending(1000):
        r.pub.outbox.ack(e.id)
    r.pub.sender.last_puback_mono = r.mono() - 10_000
    assert r.pub.health()["ok"]


async def test_healthz_endpoint_and_cli_healthcheck(tmp_path, unused_tcp_port=None):
    from lib.mosq import free_port
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1)])
    await r.pub.tick()
    port = free_port()
    runner = web.AppRunner(make_fusion_health_app(r.st), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    loop = asyncio.get_running_loop()
    c = C.parse({"fusion": {"enabled": True, "metrics_port": port, "metrics_host": "0.0.0.0"}})   # 0.0.0.0 -> probe loopback
    try:
        body = await loop.run_in_executor(None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3).read())
        assert b'"ok": true' in body
        assert (await loop.run_in_executor(None, cli.healthcheck, "fusion", c)) == 0
        met = await loop.run_in_executor(None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3).read().decode())
        assert "physical_fusion_outbox_depth" in met
        r.mono.t += 120                                                         # producer stalled -> 503 -> exit 1
        with pytest.raises(urllib.error.HTTPError) as ei:
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3))
        assert ei.value.code == 503
        assert (await loop.run_in_executor(None, cli.healthcheck, "fusion", c)) == 1
    finally:
        await runner.cleanup()
    assert cli.healthcheck("fusion", C.parse({"fusion": {"metrics_port": 0}})) == 1          # no endpoint configured
    assert cli.healthcheck("fusion", c) == 1                                                  # nothing listening any more


def test_metrics_default_is_loopback_and_configurable():
    f = C.parse({}).fusion
    assert f["metrics_host"] == "127.0.0.1" and f["metrics_port"] == 9101
    f = C.parse({"fusion": {"metrics_host": "10.1.2.3", "metrics_port": 9200}}).fusion
    assert (f["metrics_host"], f["metrics_port"]) == ("10.1.2.3", 9200)
