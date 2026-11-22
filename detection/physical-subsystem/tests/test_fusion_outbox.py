"""Outbox: crash safety, bounds, ack semantics, and the paho lock-ordering rule."""
import json, os, subprocess, sys, threading, time
import pytest
from br2.outbound.outbox import Outbox, Item, EVENT, STATUS, _line
from br2.outbound.fusion import FusionSender
import paho.mqtt.client as mqtt


def item(k, cls=EVENT, size=10):
    return Item(k, "t/x", cls, lambda seq, k=k: json.dumps({"id": k, "seq": seq, "pad": "x" * size}).encode())


def ids(ob):
    return [e.id for e in ob.pending(10_000)]


def test_append_assigns_gap_free_seq_and_survives_restart(tmp_path):
    d = str(tmp_path)
    ob = Outbox(d)
    ob.append([item("a"), item("b")], cursor={"seq": 2, "db_id": "x"}, meta={"stale": {"zone-1": False}})
    ob.close()
    ob = Outbox(d)
    assert ids(ob) == ["a", "b"] and [e.seq for e in ob.pending(5)] == [1, 2]
    assert ob.cursor == {"seq": 2, "db_id": "x"} and ob.meta == {"stale": {"zone-1": False}}
    ob.append([item("c")])
    assert [e.seq for e in ob.pending(5)] == [1, 2, 3]


def test_ack_removes_and_acked_is_not_replayed(tmp_path):
    ob = Outbox(str(tmp_path))
    ob.append([item("a"), item("b"), item("c")])
    assert ob.ack("b") and not ob.ack("b") and not ob.ack("nope")
    ob.close()
    ob = Outbox(str(tmp_path))
    assert ids(ob) == ["a", "c"]
    ob.append([item("d")])
    assert [e.seq for e in ob.pending(9)] == [1, 3, 4]           # seq never reused after an ack


def test_unacked_messages_are_replayed_with_original_payload(tmp_path):
    ob = Outbox(str(tmp_path))
    ob.append([item("a")])
    p1 = ob.pending(1)[0]
    ob.close()
    p2 = Outbox(str(tmp_path)).pending(1)[0]
    assert (p1.id, p1.payload, p1.at, p1.seq) == (p2.id, p2.payload, p2.at, p2.seq)


@pytest.mark.parametrize("cut", [1, 5, 9, 20, 61])
def test_crash_mid_write_torn_tail_is_discarded(tmp_path, cut):
    ob = Outbox(str(tmp_path))
    ob.append([item("a"), item("b")], cursor={"seq": 2, "db_id": "x"})
    ob.close()
    path = tmp_path / "outbox.journal"
    torn = _line({"o": "m", "k": "c", "t": "t/x", "p": "{}", "at": 1.0, "cl": EVENT, "q": 3})
    with open(path, "ab") as f:
        f.write(torn[:cut])                                       # power cut part-way through the next record
    ob = Outbox(str(tmp_path))
    assert ids(ob) == ["a", "b"] and ob.cursor["seq"] == 2 and ob.recovered_bad_lines >= 1
    ob.append([item("c")])                                         # journal is usable again, seq continues
    ob.close()
    ob = Outbox(str(tmp_path))
    assert ids(ob) == ["a", "b", "c"] and [e.seq for e in ob.pending(9)] == [1, 2, 3]


def test_corrupt_record_in_the_middle_is_skipped_not_fatal(tmp_path):
    ob = Outbox(str(tmp_path))
    ob.append([item("a")]); ob.append([item("b")]); ob.append([item("c")])
    ob.close()
    p = tmp_path / "outbox.journal"
    lines = p.read_bytes().split(b"\n")
    i = next(n for n, l in enumerate(lines) if b'"k":"b"' in l)
    lines[i] = lines[i][:20] + b"#" + lines[i][21:]                 # flip a byte: CRC mismatch
    p.write_bytes(b"\n".join(lines))
    ob = Outbox(str(tmp_path))
    assert ids(ob) == ["a", "c"] and ob.recovered_bad_lines == 1


def test_sigkill_while_appending_never_corrupts(tmp_path):
    code = ("import sys,json\nfrom br2.outbound.outbox import Outbox, Item\nob=Outbox(sys.argv[1])\ni=0\n"
            "print('ready',flush=True)\n"
            "while True:\n i+=1\n ob.append([Item(f'm{i}','t','event',lambda s,i=i:json.dumps({'i':i,'s':s,'p':'x'*300}).encode())],cursor={'seq':i})\n"
            " if i%7==0: ob.ack(f'm{i-3}')\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE)
    assert p.stdout.readline().strip() == b"ready"
    time.sleep(0.5)
    p.kill(); p.wait()
    ob = Outbox(str(tmp_path))
    got = ob.pending(100_000)
    assert got, "child wrote nothing"
    seqs = [e.seq for e in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    last = max(int(e.id[1:]) for e in got)
    assert ob.cursor["seq"] >= last - 1 and ob.cursor["seq"] <= last + 1   # cursor and messages agree to within the last append
    ob.append([item("z")])
    assert ob.pending(100_000)[-1].seq == max(seqs) + 1


def test_cap_messages_drops_oldest_and_counts(tmp_path):
    ob = Outbox(str(tmp_path), max_messages=5)
    ob.append([item(f"e{i}") for i in range(8)])
    assert ids(ob) == ["e3", "e4", "e5", "e6", "e7"]
    assert ob.stats()["dropped_size"] == 3 and ob.stats()["dropped_age"] == 0
    ob.close()
    ob = Outbox(str(tmp_path), max_messages=5)
    assert ids(ob) == ["e3", "e4", "e5", "e6", "e7"] and ob.dropped["size"] == 3      # counter survives restart


def test_cap_bytes(tmp_path):
    ob = Outbox(str(tmp_path), max_bytes=2000, max_messages=1000)
    ob.append([item(f"e{i}", size=200) for i in range(20)])
    s = ob.stats()
    assert s["bytes"] <= 2000 and s["depth"] < 20 and s["dropped_size"] == 20 - s["depth"]
    assert ids(ob)[-1] == "e19"                                     # newest kept


def test_cap_age_with_fake_clock(tmp_path):
    t = [1000.0]
    ob = Outbox(str(tmp_path), max_age_s=60, clock=lambda: t[0])
    ob.append([item("old")])
    t[0] += 30
    ob.append([item("mid")])
    t[0] += 40                                                      # old is 70 s, mid 40 s
    assert ob.expire() == 1 and ids(ob) == ["mid"] and ob.stats()["dropped_age"] == 1


def test_status_is_dropped_before_events_under_size_pressure(tmp_path):
    ob = Outbox(str(tmp_path), max_messages=4)
    ob.append([item("ev1", EVENT), item("st1", STATUS), item("ev2", EVENT), item("st2", STATUS)])
    ob.append([item("ev3", EVENT)])
    assert ids(ob) == ["ev1", "ev2", "st2", "ev3"]                  # oldest STATUS went, not the oldest event
    ob.append([item("ev4", EVENT), item("ev5", EVENT)])
    assert [i for i in ids(ob) if i.startswith("st")] == []        # statuses all gone before any event is lost
    assert {"ev3", "ev4", "ev5"} <= set(ids(ob))


def test_seq_stays_gap_free_at_origin_across_drops(tmp_path):
    ob = Outbox(str(tmp_path), max_messages=3)
    for i in range(10):
        ob.append([item(f"e{i}")])
    assert [e.seq for e in ob.pending(9)] == [8, 9, 10]            # a subscriber sees 7 missing = drops, never reuse
    assert ob.stats()["last_seq"] == 10


def test_journal_is_compacted_and_stays_bounded(tmp_path):
    ob = Outbox(str(tmp_path))
    for i in range(3000):
        ob.append([item(f"e{i}", size=100)])
        ob.ack(f"e{i}")
    assert os.path.getsize(tmp_path / "outbox.journal") < 1_000_000
    ob.append([item("last")]); ob.close()
    assert ids(Outbox(str(tmp_path))) == ["last"]


# ------------------------------------------------------------------ lock discipline
class _Info:
    def __init__(self, mid): self.mid, self.rc = mid, mqtt.MQTT_ERR_SUCCESS


class _ReentrantFake:
    """Mimics paho's hazard: publish() holds a mutex and invokes on_publish (-> outbox.ack) while holding it,
    and on_publish may also fire on ANOTHER thread that wants the outbox lock first."""
    def __init__(self, sender, outbox): self.sender, self.ob, self.mid = sender, outbox, 0; self.mutex = threading.Lock(); self.violations = []
    def publish(self, topic, payload, qos=0, retain=False):
        assert not self.ob._lock.locked() or self.violations.append("outbox lock held across publish()")
        with self.mutex:                      # paho's internal mutex
            self.mid += 1; mid = self.mid
            if mid % 2:                       # synchronous ack from inside publish, like a fast broker
                threading.Thread(target=self.sender._on_publish, args=(self, None, mid)).start()
            else:
                self.sender._on_publish(self, None, mid)
        return _Info(mid)


def test_outbox_lock_is_never_held_across_publish_and_no_deadlock(tmp_path):
    ob = Outbox(str(tmp_path))
    ob.append([item(f"e{i}") for i in range(30)])
    s = FusionSender({"host": "h", "port": 1, "client_id": "c", "max_inflight": 8}, ob)
    fake = _ReentrantFake(s, ob); s.client = fake
    s.connected.set()
    done = threading.Event()
    threading.Thread(target=lambda: (s.pump_once(), done.set()), daemon=True).start()
    assert done.wait(5), "deadlock in pump_once"
    time.sleep(0.2)
    assert fake.violations == []
    assert ob.stats()["depth"] == 0 and ob.stats()["acked_total"] == 30


def test_early_puback_before_registration_is_handled(tmp_path):
    ob = Outbox(str(tmp_path)); ob.append([item("a")])
    s = FusionSender({"host": "h", "port": 1, "client_id": "c"}, ob)
    class C:
        def publish(self, *a, **k):
            s._on_publish(self, None, 1)       # PUBACK arrives before publish() even returns
            return _Info(1)
    s.client = C(); s.connected.set()
    s.pump_once()
    assert ids(ob) == [] and s.inflight() == 0


def test_disconnect_keeps_unacked_messages_for_resend(tmp_path):
    ob = Outbox(str(tmp_path)); ob.append([item("a"), item("b")])
    s = FusionSender({"host": "h", "port": 1, "client_id": "c"}, ob)
    class C:
        mid = 0
        def publish(self, *a, **k): C.mid += 1; return _Info(C.mid)
    s.client = C(); s.connected.set()
    s.pump_once()
    assert s.inflight() == 2 and ids(ob) == ["a", "b"]             # published but NOT removed: no PUBACK yet
    s._on_disconnect(None, None)
    assert s.inflight() == 0 and ids(ob) == ["a", "b"]
    s.connected.set(); s.pump_once()
    assert s.inflight() == 2                                       # sent again, same ids
