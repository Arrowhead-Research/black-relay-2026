"""Regression tests for audit finding t5: never-reading SSE clients, unbounded auth-failure table, argon2 before rate limit."""
import asyncio, socket, time
import pytest
from aiohttp import web
from argon2 import PasswordHasher
from br2.outbound import api as A
from br2.outbound.api import make_app, KeyStore, key_id
from br2.outbound.state import OutState

KEY = "br_testkey"
PH = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)     # cheap hash: the tests are about counting, not cost


class R:
    def events_after(self, a, l, with_zone=False): return []
    def last_seq(self): return 5
    def floor(self): return 1


async def serve(**kw):
    st = OutState(); ks = KeyStore([{"name": "k", "argon2_hash": PH.hash(KEY), "key_id": key_id(KEY)}], 100000)
    app = make_app(st, R(), ks, 256, 64, **kw)
    runner = web.AppRunner(app, access_log=None); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    return st, ks, runner, site._server.sockets[0].getsockname()[1]


async def raw_get(port, path, key=KEY, rcvbuf=None, hdr=""):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)       # before connect: caps the advertised window
    s.setblocking(False)
    loop = asyncio.get_running_loop(); await loop.sock_connect(s, ("127.0.0.1", port))
    await loop.sock_sendall(s, f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n{hdr}\r\n".encode())
    return s


async def wait_for(fn, t=15.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        await asyncio.sleep(0.1)
    return fn()


async def test_a_client_that_never_reads_is_dropped_and_a_reading_one_is_kept():
    st, ks, runner, port = await serve(sse_probe_bytes=8192, sse_probe_interval_s=0.2, sse_idle_drop_s=2,
                                       sse_write_timeout_s=2)
    try:
        dead = [await raw_get(port, "/v1/stream", rcvbuf=2048) for _ in range(3)]
        live = await raw_get(port, "/v1/stream", rcvbuf=2048)
        async def drain():
            loop = asyncio.get_running_loop()
            while True:
                try:
                    d = await asyncio.wait_for(loop.sock_recv(live, 65536), 5)
                except Exception:
                    return
                if not d:
                    return
        t = asyncio.create_task(drain())
        assert await wait_for(lambda: st.counters["sse_clients"] == 4, 5)
        # the three non-readers (kernel acks their heartbeats) are dropped by the buffer/drain logic ...
        assert await wait_for(lambda: st.counters["sse_clients"] == 1, 25), (st.counters["sse_clients"], st.counters["sse_idle_dropped"])
        assert st.counters["sse_idle_dropped"] >= 3
        # ... and the reading one is still being served
        await asyncio.sleep(1.0)
        assert st.counters["sse_clients"] == 1
        t.cancel()
        for s in dead + [live]:
            s.close()
    finally:
        await runner.cleanup()


async def test_heartbeats_alone_do_not_count_as_a_live_client_by_default_settings():
    """The probe is on by default (the old code only sent a few bytes of heartbeat, which a dead peer's kernel absorbs)."""
    assert A.SSE_PROBE_BYTES >= 4096 and A.SSE_PROBE_INTERVAL_S > 0 and A.SSE_SNDBUF <= 65536


async def test_auth_failure_table_is_bounded_lru():
    ks = KeyStore([{"name": "k", "argon2_hash": PH.hash(KEY), "key_id": key_id(KEY)}], 100000, max_fail_ips=50)
    for i in range(500):
        assert await ks.authenticate(f"wrong{i}", f"10.0.{i // 250}.{i % 250}") is None
    assert len(ks.fails) == 50
    assert "10.0.1.249" in ks.fails and "10.0.0.0" not in ks.fails       # least recently seen evicted


async def test_verifications_run_at_most_two_at_a_time():
    keys = [f"br_k{i}" for i in range(12)]
    ks = KeyStore([{"name": f"n{i}", "argon2_hash": PH.hash(k), "key_id": key_id(k)} for i, k in enumerate(keys)], 100000)
    import threading
    cur = mx = 0; lock = threading.Lock()
    def slow(p, records):
        nonlocal cur, mx
        with lock: cur += 1; mx = max(mx, cur)
        time.sleep(0.05)
        with lock: cur -= 1
        return None
    ks._verify_sync = slow
    await asyncio.gather(*[ks.authenticate(k, f"6.6.6.{i}") for i, k in enumerate(keys)])
    assert mx <= A.MAX_PARALLEL_VERIFY


async def test_bad_key_flood_gets_401_not_a_stall_and_valid_key_still_works():
    st, ks, runner, port = await serve()
    try:
        async def one(i):
            s = await raw_get(port, "/v1/zones", key=f"wrong{i}")
            d = await asyncio.get_running_loop().sock_recv(s, 200); s.close(); return d.split(b"\r\n")[0]
        r = await asyncio.gather(*[one(i) for i in range(60)])
        assert all(x.endswith(b"401 Unauthorized") for x in r)
        assert st.counters["api_auth_failures"] == 60
    finally:
        await runner.cleanup()


async def test_a_burst_of_first_requests_with_the_valid_key_costs_one_verification():
    ks = KeyStore([{"name": "k", "argon2_hash": PH.hash(KEY), "key_id": key_id(KEY)}], 100000)
    calls = []
    orig = ks._verify_sync
    def counting(p, records):
        calls.append(p); time.sleep(0.05); return orig(p, records)
    ks._verify_sync = counting
    r = await asyncio.gather(*[ks.authenticate(KEY, "5.5.5.5") for _ in range(60)])
    assert r == ["k"] * 60 and len(calls) == 1            # not throttled by the per-IP limit, one argon2 run
    assert ks._pending == {}
