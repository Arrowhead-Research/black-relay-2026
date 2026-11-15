"""Regression tests for the confirming audit (t_api2.py, t_api3.py): failed attempts from an address must not block a
valid API key, and an SSE client that reads steadily through a long replay must not be dropped."""
import asyncio, socket, time
import pytest
from aiohttp import web
from argon2 import PasswordHasher
from br2.outbound import api as A
from br2.outbound.api import make_app, KeyStore, key_id
from br2.outbound.state import OutState

PH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
KEY, KEY2 = "br_valid", "br_valid2"


class R:
    def __init__(s, n=0): s.n = n
    def events_after(s, a, l): return [{"seq": i, "type": "zone.state", "id": "e%d" % i, "pad": "x" * 200} for i in range(a + 1, min(a + l, s.n) + 1)]
    def last_seq(s): return s.n
    def floor(s): return 1


def records(with_id=True):
    return [{"name": n, "argon2_hash": PH.hash(k), **({"key_id": key_id(k)} if with_id else {})} for n, k in (("k", KEY), ("k2", KEY2))]


async def conn(port, key=KEY, path="/v1/stream", hdr="", rcv=None):
    s = socket.socket()
    if rcv:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcv)
    s.setblocking(False); loop = asyncio.get_running_loop(); await loop.sock_connect(s, ("127.0.0.1", port))
    await loop.sock_sendall(s, f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n{hdr}\r\n".encode())
    return s


async def serve(reader, keys=None, **kw):
    st = OutState(); ks = keys or KeyStore(records(), 100000)
    app = make_app(st, reader, ks, 256, 64, **kw)
    r = web.AppRunner(app, access_log=None); await r.setup(); site = web.TCPSite(r, "127.0.0.1", 0); await site.start()
    return st, ks, r, site._server.sockets[0].getsockname()[1]


async def status(port, key, path="/v1/zones"):
    c = await conn(port, key=key, path=path)
    try:
        d = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(c, 300), 5)
    finally:
        c.close()
    return d.split(b"\r\n")[0]


def test_key_id_is_a_stable_non_secret_handle():
    assert key_id(KEY) == key_id(KEY) and key_id(KEY) != key_id(KEY2) and len(key_id(KEY)) == 16
    assert KEY not in key_id(KEY)


async def test_valid_key_still_authenticates_after_many_failures_from_the_same_ip():
    """t_api2 #3: 20 bad attempts from one address made the FIRST use of a valid key from it a 401."""
    st, ks, r, port = await serve(R())
    try:
        for i in range(60):
            assert b"401" in await status(port, "bad%d" % i)
        assert b"200" in await status(port, KEY)                  # first use, uncached, same IP (127.0.0.1)
        assert b"200" in await status(port, KEY2)
    finally:
        await r.cleanup()


async def test_garbage_costs_no_argon2_work_and_valid_keys_never_queue_behind_it(monkeypatch):
    ks = KeyStore(records(), 100000)
    calls = []
    real = ks._verify_sync
    def spy(presented, records=None):
        calls.append(presented); time.sleep(0.05)
        return real(presented, records) if records is not None else real(presented)
    monkeypatch.setattr(ks, "_verify_sync", spy)
    t0 = time.monotonic()
    res = await asyncio.gather(*[ks.authenticate("garbage%d" % i, "9.9.9.9") for i in range(300)],
                               ks.authenticate(KEY, "9.9.9.9"))
    assert res[:-1] == [None] * 300 and res[-1] == "k"
    assert calls == [KEY]                                          # argon2 ran for the valid key only
    assert time.monotonic() - t0 < 2.0


async def test_a_matching_key_id_with_a_wrong_secret_is_a_failure_that_the_limiter_sees():
    ks = KeyStore([{"name": "k", "argon2_hash": PH.hash(KEY), "key_id": key_id("br_other")}], 100000)
    assert await ks.authenticate("br_other", "1.2.3.4") is None
    assert ks.bad_keys == 1 and len(ks.fails["1.2.3.4"]) == 1


def test_a_record_without_key_id_refuses_to_start_with_a_clear_message():
    with pytest.raises(ValueError) as e:
        KeyStore(records(with_id=False), 100000)
    assert "key_id" in str(e.value) and "hash-key" in str(e.value) and "'k'" in str(e.value)


async def test_cancelling_the_first_of_several_same_key_verifications_does_not_break_the_others(monkeypatch):
    ks = KeyStore(records(), 100000)
    calls = []
    real = ks._verify_sync
    def slow(p, recs):
        calls.append(p); time.sleep(0.2); return real(p, recs)
    monkeypatch.setattr(ks, "_verify_sync", slow)
    first = asyncio.ensure_future(ks.authenticate(KEY, "3.3.3.3"))
    await asyncio.sleep(0.05)                                      # the verification is running
    others = [asyncio.ensure_future(ks.authenticate(KEY, "3.3.3.3")) for _ in range(5)]
    await asyncio.sleep(0.05)
    first.cancel()
    res = await asyncio.gather(*others, return_exceptions=True)
    assert res == ["k"] * 5 and len(calls) == 1
    assert await ks.authenticate(KEY, "3.3.3.3") == "k" and ks._pending == {}


async def test_a_steady_reader_of_a_long_replay_is_not_dropped_but_a_never_reader_is():
    """t_api2 #2: ~1 MB/s steady reader of a 20000-event backlog, idle_drop 1 s: it used to be dropped, because the send
    buffer was never empty. A client that never reads is still dropped."""
    st, ks, r, port = await serve(R(20000), sse_idle_drop_s=1.0, sse_probe_bytes=32768, sse_probe_interval_s=0.3)
    loop = asyncio.get_running_loop()
    try:
        s = await conn(port, hdr="Last-Event-ID: 0\r\n", rcv=8192)
        got, t0, alive = 0, time.time(), True
        while time.time() - t0 < 3.0:
            d = await asyncio.wait_for(loop.sock_recv(s, 4096), 3)
            if not d:
                alive = False
                break
            got += len(d)
            await asyncio.sleep(0.004)
        assert alive and got > 500_000
        assert st.counters["sse_idle_dropped"] == 0 and st.counters["sse_clients"] == 1
        s.close()
        nr = await conn(port, rcv=4096)                           # never reads
        for _ in range(100):
            await asyncio.sleep(0.2)
            if st.counters["sse_idle_dropped"]:
                break
        assert st.counters["sse_idle_dropped"] == 1
        nr.close()
    finally:
        await r.cleanup()
