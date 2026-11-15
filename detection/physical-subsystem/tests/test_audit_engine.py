"""Regression tests for audit findings t3/t6: ghost zones from late deltas, rename events, partial snapshots,
and events that carry the zone's name/kind/stale as of event time."""
import asyncio, json, os, sqlite3
import pytest
from br2 import config as C
from br2.adapters import control4 as C4, iq_remote as IQ
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK
from br2.normalizer.store import Store, Reader
from br2.outbound.state import OutState
from br2.outbound.fusion import FusionPublisher


class FC(Clock):
    t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1.8e9 + s.t


def Z(i, st="closed", name=None, typ="Door_Window", **kw):
    return {"panel_zone_id": i, "name": name or f"Z{i}", "type": typ, "group": "x", "status": st, **kw}


def mk(extra=None):
    cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, "control4": {"enabled": True, "host": "2.2.2.2"},
                   **(extra or {})})
    return Engine(cfg, FC())


def feed(e, m):
    b = e.handle(m); e.commit(); return b


def snap(e, src, zones, **kw):
    return feed(e, {"t": "snapshot", "src": src, "zones": zones, "partitions": [], **kw})


# ---------------------------------------------------------------- 4: ghost zones (t3 case 4)
def test_late_delta_for_a_deleted_zone_does_not_resurrect_it():
    e = mk(); snap(e, PRIMARY, [Z(1), Z(2)])
    feed(e, {"t": "zone_removed", "src": PRIMARY, "panel_zone_id": 2})
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 2, "status": "open", "raw": {}}})
    assert 2 not in e.zones and not b.events and not b.zones
    assert e.counters["unknown_zone_deltas_total"] == 1


def test_delta_for_a_never_seen_zone_is_ignored_and_a_resync_is_requested_once():
    e = mk(); snap(e, PRIMARY, [Z(1)])
    for _ in range(5):
        b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 77, "status": "open"}})
        assert not b.events
    assert 77 not in e.zones and e.counters["unknown_zone_deltas_total"] == 5
    assert e.resync_needed is True                     # one flag however many arrive: coalesced
    e.tick()
    assert e.resync_needed is False


def test_explicit_insert_and_snapshots_still_create_zones():
    e = mk(); snap(e, PRIMARY, [Z(1)])
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": Z(5), "new": True})
    assert 5 in e.zones and [x["type"] for x in b.events] == ["zone.added"]
    snap(e, PRIMARY, [Z(1), Z(5), Z(6)])
    assert set(e.zones) == {1, 5, 6}


def test_iq_adapter_update_for_unknown_zone_requests_a_resync_instead_of_inventing_it():
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), "/tmp/none")
    a.up = True; sent = []
    async def emit(**m): sent.append(m)
    a.emit = emit
    d = {"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR,
         "contentValues": {"zoneid": "9", "sensorstatus": "Open"}}
    asyncio.run(a.on_iq2meid(d))
    assert 9 not in a.zones and sent == [] and a._resync.is_set()
    asyncio.run(a.on_iq2meid({**d, "dbOperation": "insert"}))
    assert 9 in a.zones and sent[0]["new"] is True


def test_control4_event_for_unknown_zone_asks_for_a_fresh_summary():
    a = C4.Control4Adapter(C.parse({"control4": {"enabled": True, "host": "h"}}), "/tmp/none")
    a.up = True
    async def emit(**m): raise AssertionError("must not emit a delta for an unknown zone")
    a.emit = emit
    asyncio.run(a._on_event({"event": "ZONE_EVENT", "zone_event_type": "x", "zone": {"zone_id": 33, "status": "Open"}}))
    assert 33 not in a.zones and a._summary_due is True


# ---------------------------------------------------------------- 9: rename event, partial snapshots
def test_rename_produces_an_event_and_the_cache_follows():
    e = mk(); snap(e, PRIMARY, [Z(1), Z(3, name="Kitchen")])
    b = snap(e, PRIMARY, [Z(1), Z(3, name="Renamed")])
    ev = [(x["type"], x["subject"], x["from"], x["to"]) for x in b.events]
    assert ev == [("zone.renamed", "zone-3", "Kitchen", "Renamed")]
    assert b.events[0]["zone"]["name"] == "Renamed" and e.zones[3]["name"] == "Renamed"


def test_partial_primary_snapshot_does_not_remove_zones():
    e = mk(); snap(e, PRIMARY, [Z(i) for i in range(1, 6)])
    b = snap(e, PRIMARY, [Z(1, "open")], zones_complete=False)
    assert set(e.zones) == {1, 2, 3, 4, 5} and [x["type"] for x in b.events] == ["zone.state"]
    b = snap(e, PRIMARY, [Z(1, "open")])                # a complete one with the same content does remove (control)
    assert set(e.zones) == {1}
    assert e.counters["partial_snapshots_total"] == 1


def test_control4_partition_without_zone_list_keeps_its_zones():
    full = {"partition_list": [{"partition_id": 0, "zone_list": [{"zone_id": 1, "status": "Closed"}, {"zone_id": 2}]},
                               {"partition_id": 1, "zone_list": [{"zone_id": 3}]}]}
    zones, parts, complete = C4.parse_summary_ex(full, None)
    assert complete and set(zones) == {1, 2, 3}
    partial = {"partition_list": [{"partition_id": 0, "zone_list": [{"zone_id": 1, "status": "Open"}]},
                                  {"partition_id": 1}]}                       # no zone_list for partition 1
    z2, _, complete = C4.parse_summary_ex(partial, zones)
    assert not complete and set(z2) == {1, 3} and z2[1]["status"] == "Open"   # partition 0 was listed: zone 2 is gone
    z3, _, complete = C4.parse_summary_ex(partial | {"partition_list": [{"partition_id": 1, "zone_list": []}]}, zones)
    assert complete and z3 == {}                                              # an explicit empty list IS a statement
    z4, _, complete = C4.parse_summary_ex({"nothing": 1}, zones)
    assert not complete and set(z4) == {1, 2, 3}                              # no partition_list at all: keep everything


def test_iq_sync_without_a_sensor_table_keeps_zones_and_flags_the_snapshot_incomplete():
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), "/tmp/none")
    sensor = {"uri": IQ.URI_SENSOR, "resultSet": [{"zoneid": "1", "sensorname": "A", "sensorstatus": "Closed"}]}
    a.apply_sync([sensor]); assert a.zones_complete and set(a.zones) == {1}
    a.apply_sync([{"uri": IQ.URI_PARTITION, "resultSet": [{"partition_id": "0", "name": "P"}]}])
    assert not a.zones_complete and set(a.zones) == {1}
    a.apply_sync([{"uri": IQ.URI_SENSOR, "resultSet": []}])                    # an EMPTY table is a statement
    assert a.zones_complete and a.zones == {}


# ---------------------------------------------------------------- 9: events carry the zone as of event time (t6)
class NullSender:
    connected = type("E", (), {"is_set": lambda s: False})()
    counters = {"reconnects": 0}
    def start(s): pass
    def stop(s): pass
    def wake(s): pass
    def inflight(s): return 0


def test_events_keep_name_kind_and_stale_as_of_the_event_and_rename_is_published(tmp_path):
    cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, "site": {"id": "s", "name": "S"},
                   "fusion": {"enabled": True, "startup_grace_s": 0, "outbox": {"dir": str(tmp_path / "ob")}}})
    db = str(tmp_path / "b.db"); store = Store(db, 30); e = Engine(cfg, FC())
    def drive(m):
        b = e.handle(m); store.commit(b.events, b.zones, b.removed_zones, b.partitions, b.removed_partitions, b.meta or None)
        e.commit(); return b
    drive({"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Closed", name="Front door"), Z(2, "Idle", name="Hall", typ="Motion")], "partitions": []})
    st = OutState()
    def dump():
        st.apply({"t": "dump", "zones": list(e.zones.values()), "partitions": [], "status": e.status,
                  "health": {"links": {"iq_remote": "up", "control4": "disabled"}, "active_source": e.active,
                             "last_panel_message_epoch": 1.8e9}, "last_seq": store.last_seq()})
    dump()
    pub = FusionPublisher(cfg, st, Reader(db), sender=NullSender())
    asyncio.run(pub.tick())
    drive({"t": "zone", "src": PRIMARY, "zone": Z(1, "Open", name="Front door")}); dump()
    drive({"t": "link", "src": PRIMARY, "up": False}); dump()                 # zone_stale events, name "Front door"
    drive({"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Open", name="Main entrance")], "partitions": []}); dump()   # rename + fresh + zone 2 removed
    asyncio.run(pub.tick())
    msgs = [json.loads(x.payload) for x in pub.outbox.pending(10_000)]
    evs = [m for m in msgs if m["schema"] == "physical.event.v0"]
    by = [(m["event_type"], m["zone"]["id"], m["zone"]["name"], m["zone"]["kind"], m["stale"]) for m in evs]
    assert ("zone_opened", "zone-1", "Front door", "contact", False) in by        # NOT "Main entrance": as of the event
    assert ("zone_stale", "zone-1", "Front door", "contact", True) in by
    ren = [m for m in evs if m["event_type"] == "zone_renamed"]
    assert len(ren) == 1 and ren[0]["zone"]["name"] == "Main entrance" and ren[0]["previous_name"] == "Front door"
    rem = [m for m in evs if m["event_type"] == "zone_removed"]
    assert rem and rem[0]["zone"]["name"] == "Hall" and rem[0]["zone"]["kind"] == "motion"   # real name/kind, zone is gone
    assert [m for m in evs if m["event_type"] == "zone_fresh"][0]["stale"] is False
    from fusion_helpers import validate
    for m in evs:
        validate(m)


def test_old_database_without_the_zone_column_is_migrated_and_still_readable(tmp_path):
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL,
        type TEXT NOT NULL, subject TEXT NOT NULL, from_json TEXT, to_json TEXT, source TEXT, synthetic INTEGER NOT NULL DEFAULT 0);
        INSERT INTO events(id,ts,type,subject,from_json,to_json,source,synthetic) VALUES('evt-1','2026-01-01T00:00:00.000Z','zone.state','zone-1','"closed"','"open"','iq_remote',0);""")
    c.commit(); c.close()
    r = Reader(db)
    ev = r.events_after(0, 10, with_zone=True)                  # reader works before the normalizer migrated it
    assert ev[0]["subject"] == "zone-1" and ev[0]["zone"] is None
    s = Store(db, 30)
    assert "zone_json" in {x[1] for x in s.db.execute("PRAGMA table_info(events)")}
    assert Reader(db).events_after(0, 10)[0]["to"] == "open"
    assert "zone" not in Reader(db).events_after(0, 10)[0]       # REST/SSE shape is unchanged


# ---------------------------------------------------------------- 10: duplicate-key JSON in the wire guard (t1)
def test_wire_guard_rejects_duplicate_key_json_at_any_depth():
    import uuid
    from br2.adapters.base import OneWayViolation
    MAC = "aa:bb:cc:dd:ee:ff"
    class Raw:
        def __init__(s): s.out = []
        async def publish(s, *a, **k): s.out.append(a)
    raw = Raw(); g = IQ.GuardedPanel(raw, MAC)
    rid = str(uuid.uuid4())
    dup = ('{"requestID":"%s","responseTopic":"%s","eventName":"ipcCall","eventName":"syncdatabase","remoteMacAddress":"%s"}'
           % (rid, g.resp_topic, MAC))
    nested = ('{"requestID":"%s","responseTopic":"%s","eventName":"syncdatabase","remoteMacAddress":"%s","x":{"a":1,"a":2}}'
              % (rid, g.resp_topic, MAC))
    for bad in (dup, dup.encode(), nested):
        with pytest.raises(OneWayViolation):
            asyncio.run(g._client.publish("mastermeid", bad))
    assert raw.out == []
    ok = '{"requestID":"%s","responseTopic":"%s","eventName":"syncdatabase","remoteMacAddress":"%s"}' % (rid, g.resp_topic, MAC)
    asyncio.run(g._client.publish("mastermeid", ok))
    assert len(raw.out) == 1
