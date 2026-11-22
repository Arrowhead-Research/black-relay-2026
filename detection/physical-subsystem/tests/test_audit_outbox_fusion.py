"""Regression tests for the final adversarial audit (repro t4, t4b, t7, t8): outbox torn writes, compaction failure
inside paho callbacks, the stuck-publisher watchdog, mid-wrap spurious acks, hostile-broker memory, oversize messages."""
import builtins, errno, json, socket, struct, threading, time
import pytest
import paho.mqtt.client as mqtt
from br2.outbound.outbox import Outbox, Item, EVENT, STATUS
from br2.outbound.fusion import FusionSender, PublishOnlyClient


def item(i, cls=EVENT, pad=0):
    return Item(f"id{i}", "t", cls, lambda seq, i=i: json.dumps({"i": i, "seq": seq, "pad": "x" * pad}).encode())


class HalfWrite:
    """File wrapper: the first `fail_times` writes put half the bytes on disk, then raise ENOSPC."""
    def __init__(self, f, fail_times=1):
        self.f, self.n = f, fail_times
    def write(self, b):
        if self.n > 0:
            self.n -= 1
            self.f.write(b[: len(b) // 2])
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.f.write(b)
    def fileno(self): return self.f.fileno()
    def close(self): self.f.close()


# ---------------------------------------------------------------- 1: torn append (repro t4 b)
def test_partial_write_is_rolled_back_cursor_not_advanced_and_retry_loses_nothing(tmp_path):
    ob = Outbox(str(tmp_path))
    ob.append([item(1)], cursor={"seq": 1, "db_id": "x"})
    good = ob._journal_bytes
    real = ob._f
    ob._f = HalfWrite(real)
    with pytest.raises(OSError):
        ob.append([item(2)], cursor={"seq": 2, "db_id": "x"})
    ob._f = real                                       # space freed
    assert ob.cursor["seq"] == 1 and sorted(e.id for e in ob.pending(9)) == ["id1"]   # nothing advanced
    assert ob.stats()["write_errors"] == 1 and "ENOSPC" in ob.stats()["last_write_error"] or "space" in ob.stats()["last_write_error"]
    import os
    assert os.path.getsize(ob.path) == good            # the torn half-record was cut off
    assert ob.append([item(2), item(3), item(4)], cursor={"seq": 4, "db_id": "x"}) == 3   # producer retry
    assert ob.stats()["last_write_error"] is None
    ob.close()
    o2 = Outbox(str(tmp_path))
    assert sorted(o2._msgs) == ["id1", "id2", "id3", "id4"] and o2.cursor["seq"] == 4 and o2.recovered_bad_lines == 0


def test_failed_rollback_is_repaired_before_the_next_append(tmp_path, monkeypatch):
    import os
    ob = Outbox(str(tmp_path))
    ob.append([item(1)], cursor={"seq": 1})
    real, real_trunc = ob._f, os.truncate
    ob._f = HalfWrite(real)
    monkeypatch.setattr(os, "truncate", lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(OSError):
        ob.append([item(2)], cursor={"seq": 2})
    monkeypatch.setattr(os, "truncate", real_trunc)
    ob._f = real
    ob.append([item(2)], cursor={"seq": 2})            # _repair_tail truncates the torn half first
    ob.close()
    o2 = Outbox(str(tmp_path))
    assert sorted(o2._msgs) == ["id1", "id2"] and o2.recovered_bad_lines == 0


def test_fsync_failure_also_rolls_back(tmp_path, monkeypatch):
    import os
    ob = Outbox(str(tmp_path)); ob.append([item(1)], cursor={"seq": 1}); good = ob._journal_bytes
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(OSError):
        ob.append([item(2)], cursor={"seq": 2})
    monkeypatch.undo()
    assert os.path.getsize(ob.path) == good and ob.cursor["seq"] == 1 and ob._journal_bytes == good


# ---------------------------------------------------------------- 2: compaction failure must not escape ack (t4 c)
def test_compaction_failure_inside_ack_is_nonfatal_and_retried_later(tmp_path, monkeypatch):
    now = [1000.0]
    ob = Outbox(str(tmp_path), clock=lambda: now[0])
    ob.append([item(i) for i in range(1, 50)], cursor={"seq": 49, "db_id": "x"})
    ob._journal_bytes = 10 ** 9                         # force the "compact now" branch
    orig = builtins.open
    def deny(p, *a, **k):
        if str(p).endswith(".tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return orig(p, *a, **k)
    monkeypatch.setattr(builtins, "open", deny)
    assert ob.ack("id1") is True                        # does not raise
    assert ob.stats()["compact_errors"] == 1
    assert ob.ack("id2") is True and ob.stats()["compact_errors"] == 1      # backed off: not retried every ack
    monkeypatch.setattr(builtins, "open", orig)
    ob.append([item(100)])                              # the live journal handle still works
    now[0] += 60
    ob._journal_bytes = 10 ** 9
    ob.ack("id3")                                       # retried after the back-off and succeeds
    assert ob._journal_bytes < 10 ** 6
    ob.close()
    assert "id100" in Outbox(str(tmp_path))._msgs


# ---------------------------------------------------------------- 2: real paho thread survives a raising ack (t8)
def _acking_broker():
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(5)
    def serve():
        while True:
            try:
                c, _ = ls.accept()
            except OSError:
                return
            c.settimeout(10); c.recv(4096); c.sendall(b"\x20\x02\x00\x00")
            try:
                while True:
                    d = c.recv(65536)
                    if not d:
                        break
                    if d[0] >> 4 == 3 and (d[0] >> 1) & 3 == 1:
                        i = 1
                        while d[i] & 0x80:
                            i += 1
                        i += 1
                        tl = d[i] << 8 | d[i + 1]
                        c.sendall(b"\x40\x02" + d[i + 2 + tl: i + 4 + tl])
            except Exception:
                pass
    threading.Thread(target=serve, daemon=True).start()
    return ls


def test_exception_in_ack_does_not_kill_the_paho_thread(tmp_path):
    ls = _acking_broker(); port = ls.getsockname()[1]
    ob = Outbox(str(tmp_path))
    ob.append([Item("a", "t", EVENT, lambda s: b"{}")])
    real_ack = ob.ack
    ob.ack = lambda k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
    fs = FusionSender({"host": "127.0.0.1", "port": port, "client_id": "x", "tls": None}, ob); fs.start()
    try:
        t0 = time.time()
        while fs.counters["callback_errors"] == 0 and time.time() - t0 < 8:
            time.sleep(0.05)
        assert fs.counters["callback_errors"] >= 1
        assert fs.client._thread.is_alive() and fs.connected.is_set()
        ob.ack = real_ack
        ob.append([Item("b", "t", EVENT, lambda s: b"{}")]); fs.wake()
        t0 = time.time()
        while ob.stats()["depth"] and time.time() - t0 < 8:
            time.sleep(0.05)
        assert ob.stats()["depth"] == 0                 # both delivered (a was re-sent, b is new)
    finally:
        fs.stop(); ls.close()


def test_every_paho_callback_swallows_exceptions(tmp_path):
    ob = Outbox(str(tmp_path))
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c"}, ob)
    ob.stats = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    fs.connected.set()
    fs._on_disconnect(None, None)                       # logs via outbox.stats() -> raises inside; must be swallowed
    fs._on_connect(None, None, None, object())          # malformed rc
    fs._on_publish(None, None, "not-a-mid")
    assert fs.counters["callback_errors"] >= 1


# ---------------------------------------------------------------- 2: watchdog
class _Info:
    def __init__(self, mid): self.mid, self.rc = mid, 0


def test_watchdog_restarts_a_stuck_connected_publisher(tmp_path):
    ob = Outbox(str(tmp_path)); ob.append([item(1)])
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c", "watchdog_stall_s": 30}, ob)
    clock = [100.0]; fs._mono = lambda: clock[0]; fs._progress = 100.0
    started = []
    fs._start_client = lambda: (started.append(1), setattr(fs, "client", object()))
    class Old:
        _thread = None
        def disconnect(self): pass
        def loop_stop(self): pass
    fs.client = Old(); fs.connected.set()
    clock[0] = 120.0
    assert fs.watchdog_check() is False                 # not stalled long enough
    clock[0] = 140.0
    assert fs.watchdog_check() is True                  # connected + non-empty outbox + no progress for 40 s
    assert started == [1] and fs.counters["watchdog_restarts"] == 1 and not fs.connected.is_set()


def test_watchdog_restarts_when_the_paho_thread_is_dead_and_not_when_idle(tmp_path):
    ob = Outbox(str(tmp_path))
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c", "watchdog_stall_s": 30}, ob)
    clock = [0.0]; fs._mono = lambda: clock[0]
    fs._start_client = lambda: setattr(fs, "client", type("C", (), {"_thread": threading.current_thread()})())
    t = threading.Thread(target=lambda: None); t.start(); t.join()
    class Dead:
        _thread = t
        def disconnect(self): pass
        def loop_stop(self): pass
    fs.client = Dead(); fs.connected.set(); fs._loop_started = True
    assert fs.watchdog_check() is True                  # dead thread: immediate
    fs.connected.set(); clock[0] = 10_000.0
    assert fs.watchdog_check() is False                 # connected, empty outbox, however long: idle is healthy


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_end_to_end_replaces_a_dead_paho_thread(tmp_path):
    ls = _acking_broker(); port = ls.getsockname()[1]
    ob = Outbox(str(tmp_path))
    fs = FusionSender({"host": "127.0.0.1", "port": port, "client_id": "x", "tls": None}, ob); fs.start()
    try:
        assert _wait(lambda: fs.connected.is_set())
        # kill paho's thread the way the audit did: an exception escaping a callback
        fs.client.on_publish = lambda *a: (_ for _ in ()).throw(RuntimeError("escaped"))
        ob.append([Item("a", "t", EVENT, lambda s: b"{}")]); fs.wake()
        assert _wait(lambda: fs.client._thread is None or not fs.client._thread.is_alive(), 8)
        assert fs.watchdog_check() is True
        assert _wait(lambda: fs.connected.is_set() and fs.client._thread.is_alive())
        assert _wait(lambda: ob.stats()["depth"] == 0, 8)    # re-sent and acked on the new client
    finally:
        fs.stop(); ls.close()


def _wait(fn, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


# ---------------------------------------------------------------- 5: mid wrap / stale early entries (t7)
class Fake:
    def __init__(self, mid=0): self.mid = mid
    def publish(self, *a, **k): self.mid += 1; return _Info(self.mid)


def test_stale_early_entry_cannot_ack_a_different_message(tmp_path):
    ob = Outbox(str(tmp_path)); ob.append([item(1)])
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c"}, ob)
    fs.client = Fake(mid=6); fs.connected.set()
    fs._on_publish(None, None, 7)                        # an ack nobody claimed (arrived with no publish in progress)
    fs.pump_once()                                       # the next message is issued mid 7
    assert ob.stats()["depth"] == 1 and fs.inflight() == 1 and fs.counters["acked"] == 0
    fs._on_publish(None, None, 7)                        # the real PUBACK
    assert ob.stats()["depth"] == 0


def test_early_map_is_bounded_and_cleared_on_disconnect(tmp_path):
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c"}, Outbox(str(tmp_path)))
    for m in range(5000):
        fs._on_publish(None, None, m)
    assert len(fs._early) <= 256
    fs._on_disconnect(None, None)
    assert len(fs._early) == 0


# ---------------------------------------------------------------- 6: hostile broker (t4b)
def _rl(n):
    out = bytearray()
    while True:
        b = n % 128; n //= 128
        out.append(b | (128 if n else 0))
        if not n:
            return bytes(out)


def _hostile(ls, make_packets):
    c, _ = ls.accept(); c.settimeout(5); c.recv(4096); c.sendall(b"\x20\x02\x00\x00")
    try:
        for pkt in make_packets():
            c.sendall(pkt)
        time.sleep(1)
    except OSError:
        pass


def test_unsolicited_qos2_publishes_are_not_buffered_and_drop_the_connection(tmp_path):
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); port = ls.getsockname()[1]
    def pk():
        topic = b"detection/physical/cmd"
        for mid in range(1, 500):
            body = struct.pack(">H", len(topic)) + topic + struct.pack(">H", mid) + b"A" * 8192
            yield bytes([0x34]) + _rl(len(body)) + body
    threading.Thread(target=_hostile, args=(ls, pk), daemon=True).start()
    ob = Outbox(str(tmp_path))
    fs = FusionSender({"host": "127.0.0.1", "port": port, "client_id": "x", "tls": None}, ob)
    fs.start()
    try:
        assert _wait(lambda: fs.client.inbound_dropped >= 1, 5)
        assert len(fs.client._in_messages) == 0
        assert fs.client.max_queued_messages > 0         # bounded outgoing queue
    finally:
        fs.stop(); ls.close()


def test_oversized_inbound_packet_is_cut_off(tmp_path):
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); port = ls.getsockname()[1]
    def pk():
        yield bytes([0x30]) + _rl(5_000_000) + b"B" * 200_000        # declares 5 MB, sends part of it
    threading.Thread(target=_hostile, args=(ls, pk), daemon=True).start()
    fs = FusionSender({"host": "127.0.0.1", "port": port, "client_id": "x", "tls": None}, Outbox(str(tmp_path)))
    fs.start()
    try:
        assert _wait(lambda: fs.client.inbound_dropped >= 1, 5)
    finally:
        fs.stop(); ls.close()


def test_client_still_refuses_subscribe():
    c = PublishOnlyClient(mqtt.CallbackAPIVersion.VERSION2)
    with pytest.raises(RuntimeError):
        c.subscribe("#")


# ---------------------------------------------------------------- 8: oversize single message (t7)
def test_single_message_over_the_cap_is_rejected_not_the_whole_queue_evicted(tmp_path):
    ob = Outbox(str(tmp_path), max_bytes=2_000_000, max_messages=3000)
    ob.append([item(i, pad=300) for i in range(1, 11)], cursor={"seq": 10})
    ob.append([Item("big", "t", EVENT, lambda s: b'"' + b"y" * 3_000_000 + b'"')], cursor={"seq": 11})
    s = ob.stats()
    assert s["depth"] == 10 and s["oversize_rejected"] == 1 and s["dropped_size"] == 0
    assert ob.cursor["seq"] == 11 and s["last_seq"] == 10      # seq numbers stay gap-free
    ob.close()
    assert len(Outbox(str(tmp_path), max_bytes=2_000_000)._msgs) == 10


def test_every_paho_callback_in_the_codebase_is_guarded():
    """No exception may escape a paho callback anywhere: each `_on_*` handler of the fusion sender carries @guarded, and
    no other module installs paho callbacks (the other MQTT users go through aiomqtt)."""
    import pathlib, re
    src = pathlib.Path(__import__("br2").__file__).parent
    t = (src / "outbound/fusion.py").read_text()
    for m in re.finditer(r"^( *)def (_on_\w+)\(", t, re.M):
        before = t[:m.start()].rstrip().splitlines()[-1].strip()
        assert before == "@guarded", f"{m.group(2)} is not wrapped by @guarded"
    assert len(re.findall(r"self\.client\.on_\w+,", t)) >= 1
    for p in src.rglob("*.py"):
        if p.name != "fusion.py" and "paho" in p.read_text() and "import paho" in p.read_text():
            assert not re.search(r"\.on_(connect|disconnect|publish|message|log|subscribe)\s*=", p.read_text()), p
