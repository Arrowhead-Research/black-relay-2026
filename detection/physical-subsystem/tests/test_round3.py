"""Round-3 regressions (audit f2-r2 x1..x4): ghost zones, API range/idle handling, delta validation, mqtt CA."""
import asyncio, socket, tempfile
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from argon2 import PasswordHasher
from br2 import config as C
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK
from br2.normalizer.store import Store, Reader
from br2.outbound import api as A
from br2.outbound.mqtt_pub import MqttPublisher
from br2.outbound.state import OutState


class FC(Clock):
    def __init__(s): s.t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1_700_000_000 + s.t


def cfg(**o):
    raw = {"iq_remote": {"enabled": True, "panel_ip": "1.2.3.4"}, "control4": {"enabled": True, "host": "1.2.3.5"},
           "timers": {"fallback_after_s": 120, "failback_holddown_s": 3, "ipc_liveness_s": 5},
           "storage": {"data_dir": tempfile.mkdtemp(), "retention_days": 30}}
    raw.update(o)
    return C.parse(raw)


def z(n, status="Closed", t="Door_Window"):
    return {"panel_zone_id": n, "name": f"z{n}", "type": t, "group": "", "status": status, "partition_id": 0,
            "battery": "Normal"}


def snap(src, zs, ps=None):
    return {"t": "snapshot", "src": src, "zones": zs,
            "partitions": ps if ps is not None else [{"partition_id": 0, "name": "M", "status": "DISARM"}]}


def nl(b): return [(e["type"], e["subject"], e["source"], e["synthetic"]) for e in b.events if e["type"] != "bridge.link"]


def failed_over(zs_p, zs_c):
    e = Engine(cfg(), FC()); k = e.clock
    e.handle(snap(PRIMARY, zs_p)); e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    b = e.handle(snap(FALLBACK, zs_c))
    return e, k, b


# ---------------------------------------------------------------- x1 / x4: ghost zones
def test_c4_snapshot_never_removes_uncovered_primary_only_zone():
    e = Engine(cfg(control4={"enabled": True, "host": "1.2.3.5", "uncovered_kinds": ["other"]}), FC()); k = e.clock
    e.handle(snap(PRIMARY, [z(1), z(5, "Open", "Siren")])); e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    b = e.handle(snap(FALLBACK, [z(1)]))
    assert e.active == FALLBACK
    assert not [x for x in nl(b) if x[0] == "zone.removed"]
    assert e.zones[5]["stale"] is True and e.zones[5]["source"] == PRIMARY and e.zones[1]["stale"] is False
    assert b.removed_zones == []
    # primary returns and still has it: un-staled, refreshed, no removal
    b = e.handle(snap(PRIMARY, [z(1), z(5, "Closed", "Siren")])); k.t += 4; e.touch(FALLBACK); e.touch(PRIMARY)
    b2 = e.tick()
    assert e.active == PRIMARY and e.zones[5]["stale"] is False and e.zones[5]["state"] == "closed"
    assert not [x for x in nl(b) + nl(b2) if x[0] == "zone.removed"]


def test_primary_snapshot_is_authoritative_ghost_zone_removed():
    e, k, _ = failed_over([z(1), z(2)], [z(1), z(2)])
    for _ in range(3):
        k.t += 1; e.touch(FALLBACK)
    b = e.handle(snap(PRIMARY, [z(1)])); k.t += 4; e.touch(FALLBACK); e.touch(PRIMARY)
    b2 = e.tick()
    assert e.active == PRIMARY
    assert 2 not in e.zones, "zone absent from the primary's snapshot must be removed even though c4 owned it"
    rem = [x for x in nl(b) + nl(b2) if x[0] == "zone.removed"]
    assert rem == [("zone.removed", "zone-2", PRIMARY, True)]
    assert "zone-2" in b.removed_zones + b2.removed_zones
    # and it stays gone with the primary as sole source
    e.handle({"t": "link", "src": FALLBACK, "up": False})
    for _ in range(3):
        k.t += 60; e.touch(PRIMARY); e.handle(snap(PRIMARY, [z(1)]))
    assert 2 not in e.zones and e.health()["stale_zones"] == 0


def test_c4_snapshot_removes_zone_c4_itself_reported_earlier():
    """harness test_40 shape: c4 showed zone 16 while connected; it vanishes while the primary is down."""
    e = Engine(cfg(), FC()); k = e.clock
    e.handle(snap(PRIMARY, [z(1), z(16)])); e.handle(snap(FALLBACK, [z(1), z(16)]))
    e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    b = e.handle(snap(FALLBACK, [z(1)]))
    assert e.active == FALLBACK and 16 not in e.zones
    assert ("zone.removed", "zone-16", FALLBACK, True) in nl(b)


def test_c4_snapshot_never_removes_primary_only_partition():
    ps2 = [{"partition_id": 0, "name": "M", "status": "DISARM"}, {"partition_id": 1, "name": "G", "status": "ARM_AWAY"}]
    e = Engine(cfg(), FC()); k = e.clock
    e.handle(snap(PRIMARY, [z(1)], ps2)); e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    b = e.handle(snap(FALLBACK, [z(1)]))
    assert b.removed_partitions == [] and e.parts[1]["stale"] is True


# ---------------------------------------------------------------- delta validation
@pytest.mark.parametrize("bad", [
    {"panel_zone_id": 2, "partition_id": "abc"},
    {"panel_zone_id": "x"}, {"panel_zone_id": None}, {"panel_zone_id": 10 ** 30},
    {"panel_zone_id": 2, "name": {"a": 1}}, {"panel_zone_id": 2, "raw": [1]}, {"panel_zone_id": True},
])
def test_malformed_zone_delta_rejected_and_does_not_poison(bad):
    e = Engine(cfg(), FC()); e.handle(snap(PRIMARY, [z(1)]))
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": bad})
    assert b.empty() and e.counters["malformed_frames_total"] == 1
    assert set(e.src[PRIMARY].zones) == {1}
    e.resync_needed = True
    e.tick()                       # a forced reconcile must not raise
    assert e.zones[1]["stale"] is False


def test_malformed_snapshot_and_partition_rejected():
    e = Engine(cfg(), FC()); e.handle(snap(PRIMARY, [z(1)]))
    assert e.handle(snap(PRIMARY, [z(1), {"panel_zone_id": 3, "partition_id": "abc"}])).empty()
    assert e.handle({"t": "partition", "src": PRIMARY, "partition": {"partition_id": "q"}}).empty()
    assert e.counters["malformed_frames_total"] == 2 and set(e.zones) == {1}
    assert "malformed_frames_total" in e.health()["counters"]


def test_valid_string_ids_still_accepted():
    e = Engine(cfg(), FC()); e.handle(snap(PRIMARY, [z(1)]))
    d = dict(z(2), partition_id="0", panel_zone_id="2")
    assert e.handle({"t": "zone", "src": PRIMARY, "zone": d, "new": True}).events and 2 in e.zones


# ---------------------------------------------------------------- x2: API
def _ev():
    return {"ts": "2026-01-01T00:00:00.000Z", "type": "zone.state", "subject": "zone-1", "from": 1, "to": 2,
            "source": None, "synthetic": False}


def _client(tmp_path, **kw):
    s = Store(str(tmp_path / "b.db")); s.commit([_ev()] * 4, {}, [], {}, [])
    st = OutState(); st.last_seq = 4
    keys = A.KeyStore([{"name": "k", "argon2_hash": PasswordHasher().hash("sekret"), "key_id": A.key_id("sekret")}], 10 ** 6)
    return st, TestClient(TestServer(A.make_app(st, Reader(str(tmp_path / "b.db")), keys, **kw)))


H = {"Authorization": "Bearer sekret"}


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["after=99999999999999999999999", "after=-1", "after=9223372036854775808", "after=1e3"])
async def test_events_out_of_range_after_is_400(tmp_path, q):
    st, c = _client(tmp_path)
    async with c:
        r = await c.get(f"/v1/events?{q}", headers=H)
        assert r.status == 400
        assert (await c.get("/v1/events?after=9223372036854775807&limit=99999999999999999999", headers=H)).status == 200


@pytest.mark.asyncio
async def test_stream_out_of_range_last_event_id_is_400(tmp_path):
    st, c = _client(tmp_path)
    async with c:
        r = await c.get("/v1/stream?after=99999999999999999999999", headers=H)
        assert r.status == 400


@pytest.mark.asyncio
async def test_idle_sse_client_that_never_reads_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HB_SECONDS", 0.1)
    # heartbeats alone are absorbed by the dead peer's kernel (audit t5): the flow-control probe is what fills it
    st, c = _client(tmp_path, sse_idle_drop_s=0.6, sse_probe_bytes=8192, sse_probe_interval_s=0.1)
    async with c:
        host, port = c.server.host, c.server.port
        sk = socket.socket(); sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)    # BEFORE connect: fixed window
        sk.setblocking(False); await asyncio.get_running_loop().sock_connect(sk, (host, port))
        rd, wr = await asyncio.open_connection(sock=sk)
        wr.write(b"GET /v1/stream HTTP/1.1\r\nHost:x\r\nAuthorization: Bearer sekret\r\n\r\n"); await wr.drain()
        await asyncio.sleep(0.3)
        assert st.counters["sse_clients"] == 1
        for _ in range(60):
            await asyncio.sleep(0.1)
            if st.counters["sse_clients"] == 0:
                break
        assert st.counters["sse_clients"] == 0 and st.counters["sse_idle_dropped"] == 1
        wr.close()


@pytest.mark.asyncio
async def test_healthy_sse_client_is_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "HB_SECONDS", 0.1)
    st, c = _client(tmp_path, sse_idle_drop_s=0.5)
    async with c:
        r = await c.get("/v1/stream", headers=H)
        t = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t < 1.5:
            await asyncio.wait_for(r.content.readline(), 2)
        assert st.counters["sse_clients"] == 1 and st.counters["sse_idle_dropped"] == 0
        r.close()


# ---------------------------------------------------------------- mqtt CA
def test_mqtt_null_ca_refused_unless_insecure_dev(tmp_path):
    base = {"storage": {"data_dir": str(tmp_path)}}
    with pytest.raises(SystemExit):
        MqttPublisher(C.parse({**base, "mqtt": {"ca_file": None}}), OutState(), None)
    with pytest.raises(SystemExit):
        MqttPublisher(C.parse({**base, "mqtt": {"ca_file": ""}}), OutState(), None)
    MqttPublisher(C.parse({**base, "mqtt": {"ca_file": None, "insecure_dev": True}}), OutState(), None)
    MqttPublisher(C.parse({**base, "mqtt": {"ca_file": "/x/ca.crt"}}), OutState(), None)


def test_c4_snapshot_removes_any_kind_by_default():
    e, k, b = failed_over([z(1), z(16, "Closed", "Siren")], [z(1)])
    assert 16 not in e.zones and ("zone.removed", "zone-16", FALLBACK, True) in nl(b)


def test_control4_visible_false_override_forces_stale_only():
    e = Engine(cfg(zones={16: {"control4_visible": False}}), FC()); k = e.clock
    e.handle(snap(PRIMARY, [z(1), z(16)])); e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    b = e.handle(snap(FALLBACK, [z(1)]))
    assert e.zones[16]["stale"] is True and b.removed_zones == []


def test_uncovered_kinds_configurable():
    e = Engine(cfg(control4={"enabled": True, "host": "1.2.3.5", "uncovered_kinds": ["motion"]}), FC()); k = e.clock
    e.handle(snap(PRIMARY, [z(1), z(2, "Idle", "Motion"), z(3)])); e.handle({"t": "link", "src": PRIMARY, "up": False}); k.t += 121
    e.handle(snap(FALLBACK, [z(1)]))
    assert 3 not in e.zones and e.zones[2]["stale"] is True
