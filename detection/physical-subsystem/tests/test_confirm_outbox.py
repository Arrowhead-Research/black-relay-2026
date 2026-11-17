"""Regression tests for the confirming audit (t_outbox2.py): stale `_journal_bytes` after a failed reopen, and a corrupt
mid-journal record that left the event cursor ahead of a lost message."""
import errno, json, os, tempfile
from br2.outbound.outbox import Outbox, Item, EVENT
from br2.outbound import formats as F


def I(k, n=10):
    return Item(k, "t", EVENT, lambda s, n=n: b'{"k":"%s","p":"%s"}' % (k.encode(), b"x" * n))


def reopen(d, **kw):
    o = Outbox(d, **kw)
    r = (sorted(o._msgs), o.cursor, o.recovered_bad_lines, o.cursor_rewinds)
    o.close()
    return r


def test_journal_bytes_follow_the_file_after_a_compaction_whose_reopen_failed():
    d = tempfile.mkdtemp(); o = Outbox(d, max_bytes=10 ** 7)
    for i in range(300):
        o.append([I("m%d" % i, 2000)])
    orig, boom = o._reopen, {"on": True}
    def bad_reopen():
        if boom["on"]:
            boom["on"] = False
            raise OSError(errno.EMFILE, "too many open files")
        return orig()
    o._reopen = bad_reopen
    for i in range(299):
        o.ack("m%d" % i)
    assert o.compact_errors >= 1 or boom["on"] is False
    assert o._journal_bytes == os.path.getsize(o.path)          # t_outbox2 c: it kept the pre-compaction size
    # a failed write now rolls back to the TRUE end of file, never extends the file with zero bytes
    o.append([I("z")], {"seq": 5})
    assert o._journal_bytes == os.path.getsize(o.path)
    f = o._f; real = f.write
    def fail(b): raise OSError(errno.ENOSPC, "full")
    f.write = fail
    size = os.path.getsize(o.path)
    try:
        o.append([I("lost")], {"seq": 6})
    except OSError:
        pass
    f.write = real
    assert os.path.getsize(o.path) == size and b"\x00" not in open(o.path, "rb").read()
    o.append([I("y")], {"seq": 7})
    ids, cur, bad, rew = reopen(d)
    assert "y" in ids and "z" in ids and cur["seq"] == 7 and bad == 0 and rew == 0


def test_write_after_a_failed_reopen_uses_the_real_file_size():
    d = tempfile.mkdtemp(); o = Outbox(d)
    o.append([I("a")], {"seq": 1})
    o._f.close(); o._f = None                                      # as after a failed reopen
    o._journal_bytes = 10 ** 6                                     # ... with a stale offset
    o.append([I("b")], {"seq": 2})
    assert o._journal_bytes == os.path.getsize(o.path)
    assert b"\x00" not in open(o.path, "rb").read()
    assert reopen(d)[:2] == (["a", "b"], {"seq": 2})


def _corrupt_line(path, pred):
    data = open(path, "rb").read().split(b"\n")
    idx = [i for i, l in enumerate(data) if pred(l)][0]
    data[idx] = data[idx][:20] + b"#" + data[idx][21:]
    open(path, "wb").write(b"\n".join(data))


def test_corrupt_mid_journal_message_rewinds_the_cursor_to_before_the_lost_record():
    """t_outbox2 d: message 'b' is unreadable but the cursor record after it says seq 2: 'b' would never be re-derived."""
    d = tempfile.mkdtemp(); o = Outbox(d)
    o.append([I("a")], {"seq": 1}); o.append([I("b")], {"seq": 2}); o.append([I("c")], {"seq": 3}); o.close()
    _corrupt_line(o.path, lambda l: b'"k":"b"' in l and b'"o":"m"' in l)
    ids, cur, bad, rew = reopen(d)
    assert ids == ["a", "c"] and bad == 1 and rew == 1
    assert cur["seq"] == 1                                         # rewound to the cursor in force before the damage
    ids, cur, bad, rew = reopen(d)                                 # the compaction after the first load healed the journal
    assert rew == 0 and bad == 0 and cur["seq"] == 1 and ids == ["a", "c"]
    d2 = tempfile.mkdtemp(); o = Outbox(d2)
    o.append([I("a")], {"seq": 1}); o.append([I("b")], {"seq": 2}); o.append([I("c")], {"seq": 3}); o.close()
    _corrupt_line(o.path, lambda l: b'"k":"b"' in l and b'"o":"m"' in l)
    o3 = Outbox(d2)
    assert o3.stats()["cursor_rewinds"] == 1 and o3.stats()["recovered_bad_lines"] == 1
    o3.close()


def test_corruption_before_any_cursor_rewinds_to_the_start_of_the_log():
    d = tempfile.mkdtemp(); o = Outbox(d)
    o.append([I("a")]); o.append([I("b")], {"seq": 5}); o.close()
    _corrupt_line(o.path, lambda l: b'"k":"a"' in l and b'"o":"m"' in l)
    ids, cur, bad, rew = reopen(d)
    assert ids == ["b"] and rew == 1 and cur == {"seq": 0, "db_id": None, "pub": None}


def test_a_torn_tail_alone_is_not_a_mid_journal_corruption():
    d = tempfile.mkdtemp(); o = Outbox(d)
    o.append([I("a")], {"seq": 1}); o.append([I("b")], {"seq": 2}); o.close()
    with open(o.path, "ab") as f:
        f.write(b"deadbeef {\"o\":\"m\",\"k\":\"c")                 # crash mid-append: no newline
    ids, cur, bad, rew = reopen(d)
    assert ids == ["a", "b"] and cur["seq"] == 2 and rew == 0


def test_rewound_events_are_resent_under_the_same_ids():
    """Ids are derived from the log row, so re-deriving the events after a rewind dedupes at the consumer."""
    e = {"seq": 2, "id": "evt-2", "ts": "2026-01-01T00:00:00.000Z", "type": "zone.state", "subject": "zone-1",
         "from": "closed", "to": "open", "source": "iq_remote", "synthetic": False}
    z = {"id": "zone-1", "name": "Door", "kind": "contact", "partition_id": 0, "state": "open", "stale": False}
    a, _ = F.event_builder({"id": "s", "name": "s"}, e, z, "zone_opened", 1.0, "dbid", prev_seq=1)
    b, _ = F.event_builder({"id": "s", "name": "s"}, dict(e), z, "zone_opened", 99.0, "dbid", prev_seq=None)
    assert a == b
