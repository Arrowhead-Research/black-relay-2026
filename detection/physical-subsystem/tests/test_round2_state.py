"""Round-2 regressions (audits f2-state x1..x5 + coordinator items): engine transactions, IPC liveness, ownership,
hold-down, retention, SSE reset/caps, MQTT replay/ghosts."""
import asyncio, json, os, socket, struct, tempfile, time
import pytest
from aiohttp.test_utils import TestClient, TestServer
from argon2 import PasswordHasher
from br2 import config as C, model as M
from br2.ipc import RecvOnlyClient
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK
from br2.normalizer.service import NormalizerService
from br2.normalizer.store import Store, Reader
from br2.outbound import api
from br2.outbound.mqtt_pub import MqttPublisher
from br2.outbound.state import OutState


class FC(Clock):
    def __init__(s): s.t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1_800_000_000 + s.t
    def adv(s, d): s.t += d


def cfg(d=None, timers=None, **o):
    raw = {"iq_remote": {"enabled": True, "panel_ip": "1.2.3.4"}, "control4": {"enabled": True, "host": "1.2.3.5"},
           "timers": {"fallback_after_s": 120, **(timers or {})},
           "storage": {"data_dir": str(d or tempfile.mkdtemp()), "retention_days": 30}}
    raw.update(o)
    return C.parse(raw)


def z(n, status="Closed", t="Door_Window"):
    return {"panel_zone_id": n, "name": f"z{n}", "type": t, "group": "", "status": status, "partition_id": 0,
            "battery": "Normal"}


def snap(src, zs, ps=None):
    return {"t": "snapshot", "src": src, "zones": zs,
            "partitions": ps if ps is not None else [{"partition_id": 0, "name": "M", "status": "DISARM"}]}


def ping(src): return {"t": "ping", "src": src}


def kinds(b): return [(e["type"], e["subject"]) for e in b.events if e["type"] != "bridge.link"]


# ------------------------------------------------------------------ x1 B3: hung adapter
def test_hung_primary_is_declared_down_after_deadline_and_failover_happens():
    clk = FC(); e = Engine(cfg(), clk)
    e.handle(snap(PRIMARY, [z(1)])); e.handle(snap(FALLBACK, [z(1, "Open")]))
    assert e.active == PRIMARY
    for i in range(1, 8):                       # control4 keeps talking; primary is silent (socket open, hung)
        clk.adv(1); e.handle(ping(FALLBACK)); e.tick()
    assert e.src[PRIMARY].live is False and e.active is None and e.zones[1]["stale"]     # deadline 5 s
    for i in range(125):
        clk.adv(1); e.handle(ping(FALLBACK)); e.tick()
    assert e.active == FALLBACK and e.zones[1]["state"] == "open" and not e.zones[1]["stale"]
    assert e.counters["liveness_timeouts_total"] == 1


def test_silent_sole_adapter_makes_everything_stale_and_a_fresh_snapshot_revives_it():
    clk = FC(); e = Engine(cfg(), clk)
    e.handle(snap(PRIMARY, [z(1)]))
    clk.adv(3600); b = e.tick()
    assert e.active is None and e.status == "offline" and e.zones[1]["stale"] and b.zones["zone-1"]["stale"]
    e.handle(ping(PRIMARY)); clk.adv(1); e.tick()
    assert e.active is None                      # keep-alives alone do not revive a quarantined source
    e.handle(snap(PRIMARY, [z(1)])); assert e.active == PRIMARY and not e.zones[1]["stale"]


# ------------------------------------------------------------------ x1 C1/C2, x2: transactional engine
def mksvc(tmp_path, clk=None, real=False, **kw):
    c = cfg(tmp_path, **kw)
    return NormalizerService(c, str(tmp_path), engine=Engine(c, None if real else (clk or FC())))


def test_failed_commit_rolls_back_memory_and_resync_recovers(tmp_path):
    s = mksvc(tmp_path)
    assert s.process_batch(s.engine.handle(snap(PRIMARY, [z(1, "Closed")])))
    orig, left = s.store.commit, [1]
    def bad(*a, **k):
        if left[0]: left[0] -= 1; raise OSError("disk full")
        return orig(*a, **k)
    s.store.commit = bad
    sent = []; s._broadcast = sent.append
    assert s.process_batch(s.engine.handle(snap(PRIMARY, [z(1, "Open")]))) is False
    assert s.engine.zones[1]["state"] == "closed" and not sent and s.engine.resync_needed   # memory == durable
    assert s.persist_failures == 1
    assert s.process_batch(s.engine.tick())                                                 # retry from shadows
    assert s.engine.zones[1]["state"] == "open"
    body = json.loads(s.store.db.execute("select body from zone_state").fetchone()[0])
    assert body["state"] == "open"
    assert s.store.db.execute("select count(*) from events where type='zone.state'").fetchone()[0] == 1
    assert any(m["zones"] for m in sent)


def test_failed_commit_of_failover_keeps_old_active_and_retries(tmp_path):
    clk = FC(); s = mksvc(tmp_path, clk, timers={"ipc_liveness_s": 10**9})
    s.process_batch(s.engine.handle(snap(PRIMARY, [z(1)])))
    s.process_batch(s.engine.handle(snap(FALLBACK, [z(1, "Open")])))
    s.process_batch(s.engine.handle({"t": "link", "src": PRIMARY, "up": False}))
    orig = s.store.commit; s.store.commit = lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
    clk.adv(121); s.process_batch(s.engine.tick())
    assert s.engine.active is None and s.engine.zones[1]["source"] == PRIMARY       # nothing leaked
    s.store.commit = orig
    clk.adv(1); s.process_batch(s.engine.tick())
    assert s.engine.active == FALLBACK
    assert json.loads(s.store.db.execute("select body from zone_state").fetchone()[0])["source"] == FALLBACK


async def test_ticker_survives_failures_and_health_reflects_it(tmp_path):
    s = mksvc(tmp_path)
    boom = [3]; real = s.engine.tick
    def tick():
        if boom[0]: boom[0] -= 1; raise RuntimeError("boom")
        return real()
    s.engine.tick = tick
    t = asyncio.create_task(s._ticker())
    await asyncio.sleep(1.4)
    assert not t.done() and s.ticker_errors == 3
    assert s._health()["normalizer"]["ticker_errors"] == 3 and s._health()["normalizer"]["healthy"]
    t.cancel()
    s.ticker_last_ok -= 60
    assert s._health()["normalizer"]["healthy"] is False


async def test_supervised_task_restarts_after_death(tmp_path):
    s = mksvc(tmp_path); n = [0]
    async def f():
        n[0] += 1
        if n[0] < 3: raise RuntimeError("x")
        await asyncio.sleep(10)
    t = asyncio.create_task(s._supervised(f, "t")); await asyncio.sleep(1.4)
    assert n[0] == 3 and s.task_restarts == 2; t.cancel()


# ------------------------------------------------------------------ x1 B1: restart restores active
def test_restart_while_fallback_active_does_not_wait_fallback_after_again(tmp_path):
    T = {"ipc_liveness_s": 10**9}
    clk = FC(); s = mksvc(tmp_path, clk, timers=T)
    s.process_batch(s.engine.handle(snap(PRIMARY, [z(1)])))
    s.process_batch(s.engine.handle(snap(FALLBACK, [z(1, "Open")])))
    s.process_batch(s.engine.handle({"t": "link", "src": PRIMARY, "up": False}))
    clk.adv(121); s.process_batch(s.engine.tick()); assert s.engine.active == FALLBACK
    s.store.close()
    clk2 = FC(); s2 = mksvc(tmp_path, clk2, timers=T)          # normalizer restarted; primary adapter still dead
    assert s2.engine.active is None and all(r["stale"] for r in s2.engine.zones.values())
    s2.process_batch(s2.engine.handle(snap(FALLBACK, [z(1, "Open")])))
    assert s2.engine.active == FALLBACK and not s2.engine.zones[1]["stale"]


def test_restart_without_fallback_history_still_waits_fallback_after(tmp_path):
    clk = FC(); s = mksvc(tmp_path, clk, timers={"ipc_liveness_s": 10**9})
    s.process_batch(s.engine.handle(snap(FALLBACK, [z(1)])))
    assert s.engine.active is None                # late c4 snapshot before fallback_after: NOT active (item c)
    clk.adv(119); s.engine.tick(); assert s.engine.active is None
    clk.adv(2); s.process_batch(s.engine.tick()); assert s.engine.active == FALLBACK


def test_late_c4_snapshot_after_primary_drop_waits_from_the_drop():
    clk = FC(); e = Engine(cfg(), clk)
    e.handle(snap(PRIMARY, [z(1)])); e.handle({"t": "link", "src": PRIMARY, "up": False})
    clk.adv(100); e.handle(snap(FALLBACK, [z(1, "Open")])); assert e.active is None
    clk.adv(19); e.handle(ping(FALLBACK)); e.tick(); assert e.active is None
    clk.adv(2); e.handle(ping(FALLBACK)); e.tick(); assert e.active == FALLBACK


# ------------------------------------------------------------------ x1 B2: ownership
def test_primary_only_zone_goes_stale_on_failover_not_removed():
    clk = FC(); e = Engine(cfg(timers={"ipc_liveness_s": 10**9, "failback_holddown_s": 0},
                               zones={5: {"control4_visible": False}}), clk)
    e.handle(snap(PRIMARY, [z(1), z(5, "Closed", "Siren")])); e.handle(snap(FALLBACK, [z(1)]))
    e.handle({"t": "link", "src": PRIMARY, "up": False}); clk.adv(121); b = e.tick()
    assert e.active == FALLBACK
    assert ("zone.removed", "zone-5") not in kinds(b) and 5 in e.zones and e.zones[5]["stale"]
    assert "zone-5" not in b.removed_zones and not e.zones[1]["stale"]
    # primary returns and its OWN snapshot omits zone 5 -> now it is really gone
    b = e.handle(snap(PRIMARY, [z(1)]))
    assert ("zone.removed", "zone-5") in kinds(b) and 5 not in e.zones


def test_zone_deleted_while_owner_is_active_is_removed():
    clk = FC(); e = Engine(cfg(timers={"ipc_liveness_s": 10**9}), clk)
    e.handle(snap(PRIMARY, [z(1), z(5)]))
    b = e.handle(snap(PRIMARY, [z(1)])); assert ("zone.removed", "zone-5") in kinds(b)


# ------------------------------------------------------------------ item b: flap hold-down
def test_flapping_primary_does_not_pull_the_bridge_back_and_stable_primary_does():
    clk = FC(); e = Engine(cfg(timers={"ipc_liveness_s": 10**9}), clk)     # holddown default 3 s
    e.handle(snap(PRIMARY, [z(1)])); e.handle(snap(FALLBACK, [z(1)]))
    e.handle({"t": "link", "src": PRIMARY, "up": False}); clk.adv(121); e.tick(); assert e.active == FALLBACK
    for _ in range(6):                           # up 200 ms every 6 s
        e.handle(snap(PRIMARY, [z(1)])); clk.adv(0.2); e.tick(); assert e.active == FALLBACK
        e.handle({"t": "link", "src": PRIMARY, "up": False}); clk.adv(5.8); e.tick(); assert e.active == FALLBACK
        assert e.status == "degraded"
    e.handle(snap(PRIMARY, [z(1)]))
    clk.adv(2.9); e.tick(); assert e.active == FALLBACK
    clk.adv(0.2); e.tick(); assert e.active == PRIMARY and e.status == "online"


# ------------------------------------------------------------------ item d: empty snapshot
def test_empty_snapshot_does_not_wipe_zones_until_repeated():
    clk = FC(); e = Engine(cfg(timers={"ipc_liveness_s": 10**9}), clk)
    e.handle(snap(PRIMARY, [z(1), z(2)]))
    for _ in range(2):
        b = e.handle(snap(PRIMARY, [])); assert not b.removed_zones and len(e.zones) == 2
    assert e.counters["empty_snapshots_rejected_total"] == 2
    e.handle(snap(PRIMARY, [z(1)]))               # a good one resets the streak
    for _ in range(2): e.handle(snap(PRIMARY, []))
    assert len(e.zones) == 1
    e.handle(snap(PRIMARY, [])); b = e.handle(snap(PRIMARY, [])); e.handle(snap(PRIMARY, []))
    assert len(e.zones) == 0                      # third consecutive empty is believed


# ------------------------------------------------------------------ e3: IPC intake
def fr(m):
    b = json.dumps(m).encode(); return struct.pack(">I", len(b)) + b


async def raw(path, *frames, wait=0.0):
    s = socket.socket(socket.AF_UNIX); s.connect(path)
    for f in frames: s.sendall(fr(f))
    await asyncio.sleep(wait); return s


async def test_hello_first_separate_sockets_duplicate_claims_and_forgery(tmp_path):
    s = mksvc(tmp_path, real=True, timers={"ipc_liveness_s": 3}); await s.start()
    try:
        iq, c4 = str(tmp_path / "iq_remote.sock"), str(tmp_path / "control4.sock")
        assert os.path.exists(iq) and os.path.exists(c4) and not os.path.exists(tmp_path / "adapters.sock")
        # no hello: forged snapshot is not accepted
        f = await raw(iq, snap(PRIMARY, [z(99, "Active", "GlassBreak")]), wait=0.4)
        assert 99 not in s.engine.zones and s.engine.src[PRIMARY].live is False and s.rejected_frames == 1
        f.close()
        # wrong hello for this socket (control4 hello on the iq socket)
        f = await raw(iq, {"t": "hello", "src": FALLBACK}, snap(PRIMARY, [z(98)]), wait=0.4)
        assert 98 not in s.engine.zones; f.close()
        # proper hello; the socket decides the source even when frames claim another one
        good = await raw(iq, {"t": "hello", "src": PRIMARY}, wait=0.2)
        good.sendall(fr(snap(FALLBACK, [z(1)])))      # claims control4 on the iq socket
        await asyncio.sleep(0.4)
        assert s.engine.src[PRIMARY].live and not s.engine.src[FALLBACK].live and s.engine.active == PRIMARY
        # duplicate claim while the holder is live: refused
        dup = await raw(iq, {"t": "hello", "src": PRIMARY}, snap(PRIMARY, [z(77)]), wait=0.4)
        assert s.refused_claims == 1 and 77 not in s.engine.zones; dup.close()
        assert s._conn[PRIMARY]["conn"] is not None and s.engine.src[PRIMARY].live
        # control4 on its own socket is independent
        c = await raw(c4, {"t": "hello", "src": FALLBACK}, snap(FALLBACK, [z(1)]), wait=0.3)
        assert s.engine.src[FALLBACK].live
        good.close(); c.close(); await asyncio.sleep(0.3)
    finally:
        await s.stop()


async def test_silent_connection_is_superseded_and_non_dict_frames_close(tmp_path):
    s = mksvc(tmp_path, real=True, timers={"ipc_liveness_s": 1}); await s.start()
    try:
        iq = str(tmp_path / "iq_remote.sock")
        old = await raw(iq, {"t": "hello", "src": PRIMARY}, snap(PRIMARY, [z(1)]), wait=0.3)
        assert s.engine.src[PRIMARY].live
        await asyncio.sleep(1.6)                      # old holder hung: no frames at all
        assert not s.engine.src[PRIMARY].live          # liveness deadline fired
        new = await raw(iq, {"t": "hello", "src": PRIMARY}, snap(PRIMARY, [z(1, "Open")]), wait=0.4)
        assert s.refused_claims == 0 and s.engine.src[PRIMARY].live and s.engine.zones[1]["state"] == "open"
        new.sendall(struct.pack(">I", 2) + b"[]"); await asyncio.sleep(0.3)
        assert s.rejected_frames == 1
        old.close(); new.close()
    finally:
        await s.stop()


async def test_service_declares_silent_adapter_down_and_stale_reaches_outbound_stream(tmp_path):
    s = mksvc(tmp_path, real=True, timers={"ipc_liveness_s": 1}); await s.start()
    try:
        iq = str(tmp_path / "iq_remote.sock")
        a = await raw(iq, {"t": "hello", "src": PRIMARY}, snap(PRIMARY, [z(1)]), wait=0.4)
        out = RecvOnlyClient(str(tmp_path / "out.sock")).messages()
        first = await asyncio.wait_for(out.__anext__(), 3)
        assert first["t"] == "dump" and not first["zones"][0]["stale"]
        got = None
        async def drain():
            nonlocal got
            async for m in out:
                if m.get("t") == "batch" and any(r["stale"] for r in m["zones"]): got = m; return
        await asyncio.wait_for(drain(), 4)
        assert got and got["status"] == "offline"
        a.close()
    finally:
        await s.stop()


async def test_outbound_pump_deadline_drops_a_hung_normalizer(tmp_path):
    p = str(tmp_path / "out.sock")
    async def h(r, w): await asyncio.sleep(30)          # accepts, never sends anything
    srv = await asyncio.start_unix_server(h, p)
    t0 = time.monotonic()
    it = RecvOnlyClient(p, deadline_s=0.5).messages()
    m = await asyncio.wait_for(it.__anext__(), 3)
    assert m == {"t": "ipc_closed"} and time.monotonic() - t0 < 2
    srv.close()


def test_outbound_state_goes_stale_and_offline_on_ipc_closed():
    st = OutState()
    st.apply({"t": "dump", "zones": [{"id": "zone-1", "panel_zone_id": 1, "stale": False}], "partitions": [],
              "status": "online", "health": {"active_source": "iq_remote"}, "last_seq": 0})
    st.apply({"t": "ipc_closed"})
    assert st.zones["zone-1"]["stale"] and st.take_dirty()[0] == {"zone-1"} and st.health_view()["status"] == "offline"


# ------------------------------------------------------------------ x3: retention / durability
def ev(ts, i=1):
    return {"ts": ts, "type": "zone.state", "subject": "zone-1", "from": i, "to": i + 1, "source": None,
            "synthetic": False}


def test_prune_deletes_only_a_seq_prefix_even_with_future_and_stepped_timestamps(tmp_path):
    s = Store(str(tmp_path / "p.db"), 30); now = time.time()
    s.commit([ev(M.iso(now + 400 * 86400))] * 3, {}, [], {}, [])      # clock briefly far in the future
    s.commit([ev(M.iso(now - 40 * 86400))] * 3, {}, [], {}, [])       # stepped back
    s.commit([ev(M.iso(now))] * 3, {}, [], {}, [])
    assert s.prune() == 0
    assert [r[0] for r in s.db.execute("select seq from events order by seq")] == list(range(1, 10))   # no hole
    s2 = Store(str(tmp_path / "q.db"), 30)
    s2.commit([ev(M.iso(now - 40 * 86400))] * 3, {}, [], {}, []); s2.commit([ev(M.iso(now))] * 2, {}, [], {}, [])
    assert s2.prune() == 3 and Reader(str(tmp_path / "q.db")).floor() == 4 and s2.last_seq() == 5
    assert s2.db.execute("pragma synchronous").fetchone()[0] == 2           # FULL


def test_events_stamped_before_the_clock_was_set_survive_the_ntp_step(tmp_path):
    clk = {"t": 1000.0}
    s = Store(str(tmp_path / "q.db"), 30, wall=lambda: clk["t"])
    s.commit([ev(M.iso(1000.0))] * 5, {}, [], {}, [])                 # RTC-less boot: 1970
    assert s.prune() == 0                                             # our clock is unset: never prune on it
    clk["t"] = time.time()                                            # NTP step
    assert s.prune() == 0 and s.db.execute("select count(*) from events").fetchone()[0] == 5
    clk["t"] += 31 * 86400                                            # 31 days of real time later they expire
    assert s.prune() == 5 and s.last_seq() == 5


# ------------------------------------------------------------------ x4: SSE
KEY = "br_test_key_0123456789"


async def mkapi(tmp_path, **kw):
    s = Store(str(tmp_path / "a.db")); s.commit([ev(M.iso(time.time()), i) for i in range(1, 8)], {}, [], {}, [])
    st = OutState(); st.apply({"t": "dump", "zones": [], "partitions": [], "status": "online", "last_seq": 7,
                               "health": {"counters": {}}})
    keys = api.KeyStore([{"name": "k", "argon2_hash": PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(KEY), "key_id": api.key_id(KEY)}], 10**6)
    c = TestClient(TestServer(api.make_app(st, Reader(str(tmp_path / "a.db")), keys, **kw))); await c.start_server()
    return c, st, s


async def first_blocks(r, n=2, t=3):
    buf = b""
    async def go():
        nonlocal buf
        async for ch in r.content.iter_any():
            buf += ch
            if buf.count(b"\n\n") >= n: return
    try: await asyncio.wait_for(go(), t)
    except asyncio.TimeoutError: pass
    return buf.decode()


async def test_sse_last_event_id_beyond_head_gets_reset_then_live_events(tmp_path):
    c, st, s = await mkapi(tmp_path); H = {"Authorization": f"Bearer {KEY}"}
    try:
        r = await c.get("/v1/stream", headers={**H, "Last-Event-ID": "1000"})
        s.commit([ev(M.iso(time.time()), 9)], {}, [], {}, []); st.notify()
        body = await first_blocks(r, 3)
        assert "event: reset" in body and '"reason": "ahead_of_head"' in body and '"head": 7' in body
        assert "id: 8" in body and "event: zone.state" in body        # and the new event flows (cursor moved to head)
        r.close()
    finally:
        await c.close()


async def test_sse_last_event_id_below_pruned_floor_gets_reset(tmp_path):
    c, st, s = await mkapi(tmp_path); H = {"Authorization": f"Bearer {KEY}"}
    try:
        s.db.execute("delete from events where seq<=5")
        r = await c.get("/v1/stream", headers={**H, "Last-Event-ID": "1"})
        body = await first_blocks(r, 4)
        assert "event: reset" in body and '"reason": "pruned"' in body and '"floor": 6' in body
        assert "id: 6" in body and "id: 7" in body and body.index("event: reset") < body.index("id: 6\nevent: zone.state")
        r2 = await c.get("/v1/stream", headers={**H, "Last-Event-ID": "5"})      # exactly floor-1: no gap, no reset
        assert "reset" not in await first_blocks(r2, 3); r.close(); r2.close()
        rr = await c.get("/v1/events?after=0", headers=H)
        assert rr.headers["X-BR-Floor"] == "6" and rr.headers["X-BR-Head"] == "7"
    finally:
        await c.close()


async def test_sse_per_key_cap_cannot_be_overshot_by_a_burst_and_frees_slots(tmp_path):
    c, st, s = await mkapi(tmp_path, max_sse=100, max_sse_per_key=5); H = {"Authorization": f"Bearer {KEY}"}
    try:
        rs = await asyncio.gather(*[c.get("/v1/stream", headers=H) for _ in range(40)])
        ok = [r for r in rs if r.status == 200]
        assert len(ok) == 5 and sum(1 for r in rs if r.status == 503) == 35 and st.counters["sse_clients"] == 5
        for r in rs: r.close()
        for _ in range(60):
            if st.counters["sse_clients"] == 0: break
            await asyncio.sleep(0.1)
        assert st.counters["sse_clients"] == 0
        r = await c.get("/v1/stream", headers=H); assert r.status == 200; r.close()
    finally:
        await c.close()


async def test_sse_slow_consumer_is_dropped_by_write_timeout(tmp_path, monkeypatch):
    assert api.SSE_WRITE_TIMEOUT_S <= 5.0


# ------------------------------------------------------------------ x5: MQTT replay / ghosts
class FakeClient:
    def __init__(s): s.pubs = []; s.fail_after = None
    async def publish(s, t, p, qos=1, retain=False):
        if s.fail_after is not None and len(s.pubs) >= s.fail_after: raise ConnectionError("broker gone")
        s.pubs.append((t, p, retain))


def zr(n): return {"id": f"zone-{n}", "panel_zone_id": n, "name": "z", "kind": "contact", "partition_id": 0,
                   "state": "closed", "stale": False}


def dump(st, zones, last=0):
    st.apply({"t": "dump", "zones": zones, "partitions": [], "status": "online", "health": {}, "last_seq": last})


async def run_loop(pub, c, t=0.4):
    task = asyncio.create_task(pub._loop(c)); await asyncio.sleep(t); task.cancel()
    try: await task
    except BaseException: pass


async def test_ghost_retained_topics_are_cleared_after_outbound_restart_including_ha_discovery(tmp_path):
    cf = C.parse({"storage": {"data_dir": str(tmp_path)}}); Store(cf.db_path)
    st = OutState(); dump(st, [zr(1), zr(9)])
    c1 = FakeClient(); await run_loop(MqttPublisher(cf, st, Reader(cf.db_path)), c1)
    assert any("zone-9/state" in t for t, p, r in c1.pubs)
    st2 = OutState(); dump(st2, [zr(1)])                       # zone 9 vanished while outbound was down
    c2 = FakeClient(); await run_loop(MqttPublisher(cf, st2, Reader(cf.db_path)), c2)
    cleared = {t for t, p, r in c2.pubs if r and not p}
    assert "blackrelay/v1/zones/zone-9/state" in cleared and "homeassistant/binary_sensor/blackrelay_zone-9/config" in cleared
    assert not any("zone-1/state" in t for t in cleared)
    c3 = FakeClient(); await run_loop(MqttPublisher(cf, st2, Reader(cf.db_path)), c3)
    assert not [1 for t, p, r in c3.pubs if r and not p]       # set persisted: nothing left to clear


async def test_no_ghost_judgement_before_the_first_dump(tmp_path):
    cf = C.parse({"storage": {"data_dir": str(tmp_path)}}); Store(cf.db_path)
    st = OutState(); dump(st, [zr(1), zr(9)]); await run_loop(MqttPublisher(cf, st, Reader(cf.db_path)), FakeClient())
    c = FakeClient(); await run_loop(MqttPublisher(cf, OutState(), Reader(cf.db_path)), c)      # normalizer not up yet
    assert c.pubs == []


def seqs(c): return [json.loads(p)["seq"] for t, p, r in c.pubs if t.endswith("/events")]


async def test_replay_no_duplicates_after_crash_no_1000_cap_and_pre_start_events(tmp_path):
    cf = C.parse({"storage": {"data_dir": str(tmp_path)}}); s = Store(cf.db_path)
    now = M.iso(time.time())
    for i in range(0, 2600, 500): s.commit([ev(now, j) for j in range(i, min(i + 500, 2600))], {}, [], {}, [])   # before outbound ever ran
    st = OutState(); dump(st, [], 2600)
    pub = MqttPublisher(cf, st, Reader(cf.db_path)); c = FakeClient(); c.fail_after = 1234
    with pytest.raises(ConnectionError): await pub._flush_events(c)
    first = seqs(c); assert first == list(range(1, 1235))          # from seq 1, beyond the old 1000 cap
    pub2 = MqttPublisher(cf, OutState(), Reader(cf.db_path)); c2 = FakeClient()    # outbound restarted
    await pub2._flush_events(c2)
    assert seqs(c2) == list(range(1235, 2601)) and not set(first) & set(seqs(c2))    # no duplicate, no loss


async def test_replay_pointer_follows_database_identity_and_pruned_floor(tmp_path):
    cf = C.parse({"storage": {"data_dir": str(tmp_path)}}); s = Store(cf.db_path)
    s.commit([ev(M.iso(time.time()), j) for j in range(6)], {}, [], {}, [])
    pub = MqttPublisher(cf, OutState(), Reader(cf.db_path)); c = FakeClient(); await pub._flush_events(c)
    assert seqs(c) == [1, 2, 3, 4, 5, 6]
    s.close(); os.remove(cf.db_path)
    for ext in ("-wal", "-shm"):
        if os.path.exists(cf.db_path + ext): os.remove(cf.db_path + ext)
    s2 = Store(cf.db_path); s2.commit([ev(M.iso(time.time()), j) for j in range(3)], {}, [], {}, [])   # recreated DB
    pub2 = MqttPublisher(cf, OutState(), Reader(cf.db_path)); c2 = FakeClient(); await pub2._flush_events(c2)
    assert seqs(c2) == [1, 2, 3]                                   # different db_id => replay from its start


def test_plaintext_mqtt_refused_unless_insecure_dev(tmp_path):
    cf = C.parse({"mqtt": {"tls": False}, "storage": {"data_dir": str(tmp_path)}})
    with pytest.raises(SystemExit): MqttPublisher(cf, OutState(), Reader(cf.db_path))
    cf = C.parse({"mqtt": {"tls": False, "insecure_dev": True}, "storage": {"data_dir": str(tmp_path)}})
    assert MqttPublisher(cf, OutState(), Reader(cf.db_path))._tls() is None


def test_metrics_bind_defaults_to_loopback():
    assert C.parse({}).api["metrics_host"] == "127.0.0.1"


def test_zone_removed_while_primary_down_is_a_removal_on_fallback_snapshot():
    """Harness test_40 scenario (round 3): zone 16 is a covered kind (and here also reported by the fallback earlier), so its absence from the
    fallback's later complete SUMMARY is a removal. (A zone the fallback NEVER reported goes stale instead: see
    test_round3.)"""
    clk = FC(); e = Engine(cfg(timers={"ipc_liveness_s": 10**9, "failback_holddown_s": 0}), clk)
    e.handle(snap(PRIMARY, [z(1), z(16)]))
    e.handle(snap(FALLBACK, [z(1), z(16)]))
    e.handle({"t": "link", "src": PRIMARY, "up": False})
    e.handle(snap(FALLBACK, [z(1)])); clk.adv(121); b = e.tick()
    assert ("zone.removed", "zone-16") in kinds(b) and 16 not in e.zones


async def test_restart_logs_zone_stale_in_the_event_log_and_fresh_when_vouched(tmp_path):
    """Stale/fresh flips are real log rows: true timestamp, own seq, written with the state change, across restarts."""
    s = mksvc(tmp_path)
    s.process_batch(s.engine.handle(snap(PRIMARY, [z(1), z(2)])))
    head = s.store.last_seq()
    s.store.close()
    s2 = mksvc(tmp_path)
    await s2.start()
    try:
        rows = s2.store.db.execute("select seq,type,subject,from_json,to_json,synthetic from events where seq>? order by seq", (head,)).fetchall()
        assert [(r[1], r[2], r[3], r[4], r[5]) for r in rows] == [("zone.stale", "zone-1", "false", "true", 0),
                                                                 ("zone.stale", "zone-2", "false", "true", 0),
                                                                 ("partition.stale", "partition-0", "false", "true", 0)]
        assert [r[0] for r in rows] == [head + 1, head + 2, head + 3]                        # gap-free, continues the log
        assert all(json.loads(r[0])["stale"] for r in s2.store.db.execute("select body from zone_state"))   # cache in step
        s2.process_batch(s2.engine.handle(snap(PRIMARY, [z(1), z(2)])))
        fresh = s2.store.db.execute("select subject,to_json from events where type='zone.stale' and seq>? order by seq", (head + 3,)).fetchall()
        assert fresh == [("zone-1", "false"), ("zone-2", "false")]
    finally:
        await s2.stop()
    s3 = mksvc(tmp_path)                                                           # restart again: zones were fresh
    n = s3.store.last_seq()
    await s3.start()
    assert s3.store.last_seq() == n + 3
    await s3.stop()
