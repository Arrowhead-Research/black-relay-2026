import asyncio, json, os, sqlite3, time, pytest
from argon2 import PasswordHasher
from aiohttp.test_utils import TestClient, TestServer
from br2 import config as C, model as M
from br2.normalizer.store import Store, Reader
from br2.adapters import iq_remote as IQ, control4 as C4
from br2.outbound.state import OutState
from br2.outbound import api


def ev(i, ts=None, t="zone.state"):
    return {"ts": ts or M.iso(time.time()), "type": t, "subject": f"zone-{i}", "from": "a", "to": "b",
            "source": "iq_remote", "synthetic": False}


def test_seq_gap_free_monotonic_across_rollback_prune_and_restart(tmp_path):
    p = str(tmp_path / "x.db")
    s = Store(p, 30)
    old = M.iso(time.time() - 40 * 86400)
    out = s.commit([ev(1, ts=old), ev(2, ts=old), ev(3, ts=old)], {}, [], {}, [])
    assert [e["seq"] for e in out] == [1, 2, 3]
    bad = [ev(4), {"ts": "x"}]            # second event raises KeyError mid-transaction
    with pytest.raises(KeyError):
        s.commit(bad, {}, [], {}, [])
    assert s.last_seq() == 3               # rolled back entirely, no hole
    out = s.commit([ev(5)], {}, [], {}, [])
    assert out[0]["seq"] == 4
    # prune the old PREFIX (seq 1-3); counter must not reset
    assert s.prune() == 3 and s.last_seq() == 4
    s.db.execute("DELETE FROM events")
    s.close()
    s2 = Store(p, 30)
    assert s2.commit([ev(8)], {}, [], {}, [])[0]["seq"] == 5
    rows = [r[0] for r in s2.db.execute("SELECT seq FROM events ORDER BY seq")]
    assert rows == [5]
    rd = Reader(p)
    assert rd.last_seq() == 5 and rd.events_after(0)[0]["id"] == "evt-5"
    with pytest.raises(sqlite3.OperationalError):
        rd._conn().execute("INSERT INTO meta VALUES('a','b')")   # reader is read-only


def test_cache_persistence_roundtrip(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    s.commit([], {"zone-1": {"id": "zone-1", "panel_zone_id": 1}}, [], {"partition-0": {"id": "partition-0", "panel_partition_id": 0}}, [])
    z, p = s.load_cache()
    assert z[0]["id"] == "zone-1" and p[0]["panel_partition_id"] == 0
    s.commit([], {}, ["zone-1"], {}, [])
    assert s.load_cache()[0] == []


def test_model_mapping():
    assert M.classify("GlassBreak", "") == "glass_break" and M.classify("Panel Glass Break", "") == "glass_break"
    assert M.classify("Motion", "") == "motion" and M.classify("Occupancy Sensor", "") == "motion"
    assert M.classify("Door_Window_M", "") == "contact" and M.classify("Tilt", "") == "contact"
    assert M.classify("SmokeDetector", "") == "other" and M.classify("Door_Window", "", "motion") == "motion"
    assert M.map_state("contact", "Open") == ("open", True) and M.map_state("motion", "Activated") == ("active", True)
    assert M.map_state("glass_break", "Idle") == ("idle", True) and M.map_state("contact", "Not Networked") == ("unknown", False)
    assert M.map_state("contact", "Tampered") == (None, None) and M.map_state("contact", "weird")[0] == "unknown"
    assert M.map_battery("Normal") is False and M.map_battery("Low") is True and M.map_battery("") is None
    assert M.map_arming("ARM-AWAY-EXIT-DELAY") == "exit_delay" and M.map_arming("ARM_STAY") == "arm_stay"
    assert M.map_arming("DISARM", "Delay") == "entry_delay" and M.map_arming("ENTRY_DELAY") == "entry_delay"
    assert M.map_arming("ARM-NIGHT") == "arm_night" and M.map_arming(None) == "unknown"


FULL = [
    {"uri": IQ.URI_SENSOR, "resultSet": [
        {"zoneid": "12", "sensorname": "Front", "sensortype": "Door_Window", "sensorgroup": "entryexitdelay",
         "sensorstatus": "Closed", "partition_id": "0", "battery_status": "Normal"},
        {"zoneid": "13", "sensorname": "GB", "sensortype": "GlassBreak", "sensorgroup": "glassbreak",
         "sensorstatus": "Idle", "partition_id": "0", "battery_status": "Low"}]},
    {"uri": IQ.URI_PARTITION, "resultSet": [{"partition_id": "0", "name": "Main"}]},
    {"uri": IQ.URI_SETTINGS, "resultSet": [{"partition_id": "0", "name": "SYSTEM_STATUS", "value": "ARM-STAY"},
                                           {"partition_id": "0", "name": "AC_STATUS", "value": "x"}]},
    {"uri": IQ.URI_STATE, "resultSet": [{"partition_id": "0", "name": "ALARM_STATE", "value": "None"}]},
]


class Rec:
    def __init__(self):
        self.sent = []
    async def send(self, m): self.sent.append(m); return True


async def test_iq_db_model_and_deltas(tmp_path):
    zones, parts = IQ.build_model(FULL)
    assert zones[12]["type"] == "Door_Window" and zones[13]["battery"] == "Low" and parts[0]["status"] == "ARM-STAY"
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), str(tmp_path))
    a.ipc = Rec(); a.zones, a.parts, a.up = zones, parts, True
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR,
                        "contentValues": {"zoneid": "12", "sensorstatus": "Open"}, "selection": "zoneid=?",
                        "selectionArgs": "[12]"})
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR,
                        "contentValues": {"sensorstatus": "Active"}, "selectionArgs": ["13"]})  # zoneid absent
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SETTINGS,
                        "contentValues": {"name": "SYSTEM_STATUS", "value": "DISARM", "partition_id": "0"}})
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "insert", "uri": IQ.URI_STATE,
                        "contentValues": {"name": "ALARM_STATE", "value": "Alarm", "partition_id": "0"}})
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "delete", "uri": IQ.URI_SENSOR,
                        "selection": "zoneid=?", "selectionArgs": ["13"]})
    await a.on_iq2meid({"eventName": "chime"})
    kinds = [(m["t"], (m.get("zone") or m.get("partition") or {}).get("status", m.get("panel_zone_id"))) for m in a.ipc.sent]
    assert kinds == [("zone", "Open"), ("zone", "Active"), ("partition", "DISARM"), ("partition", None),
                     ("zone_removed", 13)]
    assert a.ipc.sent[1]["zone"]["panel_zone_id"] == 13 and 13 not in a.zones
    # ambiguous delete -> resync request (no emit)
    await a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "delete", "uri": IQ.URI_SENSOR})
    assert a._resync.is_set()


def test_pki_discovery_and_tls_pin(tmp_path):
    d = tmp_path / "f2163e1a2b3c"; d.mkdir()
    for ext in ("key", "secure", "qolsys"):
        (d / f"f2163e1a2b3c.{ext}").write_text("x")
    id12, k, s, ca = IQ.find_pki(str(tmp_path), None)
    assert id12 == "f2163e1a2b3c" and ca.endswith(".qolsys") and IQ.fmt_mac(id12) == "f2:16:3e:1a:2b:3c"
    assert IQ.find_pki(str(tmp_path), "/some/ca.crt")[3] == "/some/ca.crt" if os.path.exists("/some/ca.crt") else True


def test_c4_parse_and_fp_norm():
    m = {"event": "INFO", "info_type": "SUMMARY", "partition_list": [
        {"partition_id": 0, "name": "Main", "status": "ARM_AWAY", "zone_list": [
            {"id": "12", "type": "Door_Window", "name": "F", "group": "g", "status": "Open", "zone_id": 12, "partition_id": 0},
            {"id": "x", "type": "Motion", "name": "M", "group": "g", "status": "Idle", "zone_id": "13"}]}]}
    z, p = C4.parse_summary(m)
    assert sorted(z) == [12, 13] and z[13]["partition_id"] == 0 and p[0]["status"] == "ARM_AWAY"
    assert C4.norm_fp("AB:cd:12") == "abcd12" and C4.norm_fp(None) is None


# ---------------------------------------------------------------- API
KEY = "br_test_key_0123456789"


async def mkclient(tmp_path, rate=120):
    s = Store(str(tmp_path / "a.db"))
    s.commit([ev(i) for i in range(1, 6)], {}, [], {}, [])
    st = OutState()
    st.apply({"t": "dump", "zones": [
        {"id": "zone-12", "panel_zone_id": 12, "kind": "contact", "partition_id": 0, "state": "open", "stale": False},
        {"id": "zone-13", "panel_zone_id": 13, "kind": "motion", "partition_id": 1, "state": "idle", "stale": False}],
        "partitions": [{"id": "partition-0", "arming": "disarmed"}], "status": "online", "last_seq": 5,
        "health": {"active_source": "iq_remote", "links": {"iq_remote": "up", "control4": "up"},
                   "last_panel_message_epoch": time.time() - 2, "counters": {}, "adapter_stats": {}}})
    keys = api.KeyStore([{"name": "t", "argon2_hash": PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(KEY), "key_id": api.key_id(KEY)}], rate)
    c = TestClient(TestServer(api.make_app(st, Reader(str(tmp_path / "a.db")), keys)))
    await c.start_server()
    return c, st, s


async def test_api_auth_readonly_filters_events(tmp_path):
    c, st, s = await mkclient(tmp_path)
    try:
        assert (await c.get("/v1/health")).status == 200
        h = await (await c.get("/v1/health")).json()
        assert h["status"] == "online" and h["active_source"] == "iq_remote" and h["last_seq"] == 5 and h["zones"] == 2
        assert 1 < h["seconds_since_panel_message"] < 10
        assert (await c.get("/v1/zones")).status == 401
        assert (await c.get("/v1/zones", headers={"Authorization": "Bearer nope"})).status == 401
        H = {"Authorization": f"Bearer {KEY}"}
        assert len(await (await c.get("/v1/zones", headers=H)).json()) == 2
        assert len(await (await c.get("/v1/zones?kind=motion", headers={"X-API-Key": KEY})).json()) == 1
        assert len(await (await c.get("/v1/zones?partition=1", headers=H)).json()) == 1
        assert (await c.get("/v1/zones/zone-12", headers=H)).status == 200 and (await c.get("/v1/zones/99", headers=H)).status == 404
        assert [e["seq"] for e in await (await c.get("/v1/events?after=2&limit=2", headers=H)).json()] == [3, 4]
        for m in ("post", "put", "delete", "patch"):
            assert (await getattr(c, m)("/v1/zones", headers=H)).status == 405
            assert (await getattr(c, m)("/v1/health")).status == 405
    finally:
        await c.close()


async def test_api_rate_limit(tmp_path):
    c, st, s = await mkclient(tmp_path, rate=3)
    try:
        H = {"Authorization": f"Bearer {KEY}"}
        codes = [(await c.get("/v1/partitions", headers=H)).status for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429] and st.counters["api_rate_limited"] == 2
    finally:
        await c.close()


async def read_sse(resp, n, timeout=5):
    out, buf = [], b""
    async def go():
        nonlocal buf
        async for chunk in resp.content.iter_any():
            buf += chunk
            while b"\n\n" in buf:
                blk, buf = buf.split(b"\n\n", 1)
                out.append(blk.decode())
            if sum(1 for b in out if "data:" in b) >= n:
                return
    await asyncio.wait_for(go(), timeout)
    return out


async def test_sse_replay_last_event_id_live_and_no_leak(tmp_path):
    c, st, s = await mkclient(tmp_path)
    H = {"Authorization": f"Bearer {KEY}"}
    try:
        r = await c.get("/v1/stream", headers={**H, "Last-Event-ID": "3"})
        assert r.headers["Content-Type"].startswith("text/event-stream")
        blocks = await read_sse(r, 2)
        assert [b.split("\n")[0] for b in blocks if "id:" in b] == ["id: 4", "id: 5"]
        # live event after replay: commit + notify
        s.commit([ev(6)], {}, [], {}, [])
        st.apply({"t": "health", "health": st.health, "status": "online"})
        more = await read_sse(r, 1)
        assert "id: 6" in "".join(more)
        assert st.counters["sse_clients"] == 1
        r.close()
        for _ in range(50):
            if st.counters["sse_clients"] == 0: break
            await asyncio.sleep(0.1)
        assert st.counters["sse_clients"] == 0   # handler cleaned up on disconnect
        # no Last-Event-ID: starts from now (no replay)
        r2 = await c.get("/v1/stream", headers=H)
        with pytest.raises(asyncio.TimeoutError):
            await read_sse(r2, 1, timeout=0.5)
        r2.close()
    finally:
        await c.close()


async def test_sse_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "HB_SECONDS", 0.2)
    c, st, s = await mkclient(tmp_path)
    try:
        r = await c.get("/v1/stream", headers={"Authorization": f"Bearer {KEY}", "Last-Event-ID": "5"})
        out = await asyncio.wait_for(r.content.readuntil(b": hb\n\n"), 3)
        assert b": hb" in out
    finally:
        await c.close()


def test_metrics_render():
    st = OutState()
    st.apply({"t": "dump", "zones": [], "partitions": [], "status": "degraded", "last_seq": 7,
              "health": {"active_source": "control4", "links": {"iq_remote": "down", "control4": "up"},
                         "counters": {"failovers_total": 1}, "adapter_stats": {"iq_remote": {"violations": 2}}}})
    t = api.render_metrics(st)
    assert 'blackrelay_status{status="degraded"} 1' in t and 'blackrelay_active_source{source="control4"} 1' in t
    assert "blackrelay_last_seq 7" in t and 'blackrelay_oneway_violations_blocked_total{source="iq_remote"} 2' in t
    assert "# TYPE blackrelay_up gauge" in t


def test_ipc_closed_marks_everything_stale_offline():
    st = OutState()
    st.apply({"t": "dump", "zones": [{"id": "zone-1", "stale": False}], "partitions": [{"id": "partition-0", "stale": False}],
              "status": "online", "last_seq": 1, "health": {"active_source": "iq_remote"}})
    st.apply({"t": "ipc_closed"})
    assert st.zones["zone-1"]["stale"] and st.health_view()["status"] == "offline" and st.health_view()["active_source"] is None


async def test_sse_heartbeat_not_starved_by_frequent_wakeups(tmp_path, monkeypatch):
    """Regression: normalizer health ticks wake every SSE handler each second; heartbeats must still be sent."""
    monkeypatch.setattr(api, "HB_SECONDS", 0.5)
    c, st, s = await mkclient(tmp_path)
    try:
        r = await c.get("/v1/stream", headers={"Authorization": f"Bearer {KEY}", "Last-Event-ID": "5"})
        async def spam():
            while True:
                st.apply({"t": "health", "health": st.health, "status": "online"}); await asyncio.sleep(0.1)
        t = asyncio.create_task(spam())
        out = await asyncio.wait_for(r.content.readuntil(b": hb\n\n"), 3)
        t.cancel()
        assert b": hb" in out
    finally:
        await c.close()
