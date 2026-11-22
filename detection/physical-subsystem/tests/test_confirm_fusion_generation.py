"""Regression tests for the confirming audit (t_fusion2/3/4.py, t_mid.py): after a watchdog restart the OLD client's late
callbacks must not clear `connected` for the new client nor ack a message by a mid the new client reused."""
import logging, socket, tempfile, threading, time
import pytest
from br2.outbound.fusion import FusionSender
from br2.outbound.outbox import Outbox, Item, EVENT


def I(k, n=2000):
    return Item(k, "t", EVENT, lambda s, n=n: b'{"k":"%s","p":"%s"}' % (k.encode(), b"x" * n))


class Info:
    def __init__(s, mid): s.rc = 0; s.mid = mid


@pytest.fixture
def broker():
    """A minimal MQTT 3.1.1 broker: CONNACK, PUBACK (unless muted), PINGRESP."""
    hold = {}
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(20)
    stop = threading.Event()

    def serve(c):
        c.settimeout(30)
        try:
            c.recv(4096); c.sendall(b"\x20\x02\x00\x00")
            while True:
                d = c.recv(65536)
                if not d:
                    break
                if hold.get("mute"):
                    continue
                if d[0] >> 4 == 3 and (d[0] >> 1) & 3 == 1:
                    i = 1
                    while d[i] & 0x80:
                        i += 1
                    i += 1
                    tl = d[i] << 8 | d[i + 1]
                    c.sendall(b"\x40\x02" + d[i + 2 + tl: i + 4 + tl])
                elif d[0] >> 4 == 12:
                    c.sendall(b"\xd0\x00")
        except Exception:
            pass

    def acc():
        while not stop.is_set():
            try:
                c, _ = ls.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    threading.Thread(target=acc, daemon=True).start()
    yield ls.getsockname()[1], hold
    stop.set(); ls.close()


def wait(fn, t=6.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def mk(port, cid, **kw):
    ob = Outbox(tempfile.mkdtemp(), **kw)
    return FusionSender({"host": "127.0.0.1", "port": port, "client_id": cid, "tls": None}, ob), ob


def test_late_disconnect_of_the_old_client_does_not_clear_the_new_clients_connected_flag(broker):
    """t_fusion2 #2: 25 restart trials; the flag must stay set and delivery must work."""
    port, _ = broker
    bad = 0
    for trial in range(8):
        fs, ob = mk(port, "y%d" % trial)
        fs.start()
        try:
            assert fs.connected.wait(3)
            old = fs.client
            fs._restart_client()
            assert fs.client is not old and fs.client._br_gen == fs._gen
            assert wait(lambda: fs.client.is_connected() and fs.connected.is_set())
            old.on_disconnect(old, None, None, 0)               # the old client's late on_disconnect, delivered anyway
            assert fs.connected.is_set(), "a stale on_disconnect cleared the NEW client's connected flag"
            ob.append([I("q")]); fs.wake()
            if not wait(lambda: ob.stats()["depth"] == 0):
                bad += 1
        finally:
            fs.stop()
    assert bad == 0


def test_the_old_clients_loop_thread_is_joined_before_the_new_client_starts(broker):
    port, _ = broker
    fs, ob = mk(port, "join")
    order = []
    fs.start()
    try:
        assert fs.connected.wait(3)
        old = fs.client; th = old._thread
        real_start = fs._start_client
        def start():
            order.append(("old thread alive", th is not None and th.is_alive()))
            real_start()
        fs._start_client = start
        fs._restart_client()
        assert order == [("old thread alive", False)]
    finally:
        fs.stop()


def test_stale_puback_from_the_old_client_cannot_ack_a_message(broker):
    """t_fusion2 #3: a late PUBACK of the OLD client, for a mid the new client reuses, acked the wrong message."""
    port, hold = broker
    fs, ob = mk(port, "z")
    fs.start()
    try:
        assert fs.connected.wait(3)
        hold["mute"] = True
        ob.append([I("victim")]); fs.wake()
        assert wait(lambda: fs.inflight() == 1)
        old = fs.client
        fs._restart_client()
        assert fs.connected.wait(3); fs.wake()
        assert wait(lambda: fs.inflight() == 1)
        mid = next(iter(fs._inflight))
        fs._on_publish(old, None, mid)                              # late PUBACK on the OLD client's thread
        assert ob.stats()["depth"] == 1 and fs.counters["acked"] == 0, "old client's PUBACK acked a message"
        assert fs.counters["stale_callbacks"] >= 1
        old.on_publish(old, None, mid)                              # through the real bound callback too
        assert ob.stats()["depth"] == 1
        hold["mute"] = False
    finally:
        fs.stop()


def test_callbacks_are_tagged_with_their_generation(broker):
    port, _ = broker
    fs, _ob = mk(port, "tag")
    fs.start()
    try:
        g1 = fs.client._br_gen
        fs._restart_client()
        assert fs.client._br_gen == g1 + 2 or fs.client._br_gen > g1
        assert fs._gen == fs.client._br_gen
    finally:
        fs.stop()


def test_stray_puback_with_nothing_inflight_does_not_ack_the_next_publish_t_mid():
    """t_mid.py: an unclaimed PUBACK for mid 7 must not ack a later publish that is issued mid 7."""
    class C:
        def publish(s, *a, **k): return Info(7)
    ob = Outbox(tempfile.mkdtemp()); ob.append([I("m", 10)])
    fs = FusionSender({"host": "h", "port": 1, "client_id": "x"}, ob, client_factory=C)
    fs.client = C(); fs.connected.set()
    fs._on_publish(fs.client, None, 7)
    fs.pump_once()
    assert ob.stats()["depth"] == 1 and fs.inflight() == 1


def test_a_current_generation_callback_still_works():
    class C:
        mid = 0
        def publish(s, *a, **k): C.mid += 1; return Info(C.mid)
    ob = Outbox(tempfile.mkdtemp()); ob.append([I("m", 10)])
    fs = FusionSender({"host": "h", "port": 1, "client_id": "x"}, ob, client_factory=C)
    fs.client = C(); fs.connected.set()
    fs.pump_once()
    fs._on_publish(fs.client, None, 1)
    assert ob.stats()["depth"] == 0 and fs.counters["stale_callbacks"] == 0
