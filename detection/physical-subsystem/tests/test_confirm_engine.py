"""Regression tests for the confirming audit (t_mid-adjacent low items): an IQ insert without `zoneid` must trigger a
coalesced resync, `Engine.restore()` is a transaction like every other transition, and tamper / battery / stale / renamed
events carry the zone `state` as of the event in the log row."""
import asyncio, json
import pytest
from br2 import config as C
from br2.adapters import iq_remote as IQ
from br2.normalizer.core import Engine, Clock, PRIMARY, zinfo
from br2.normalizer.store import Store, Reader
from br2.outbound import formats as F


class FC(Clock):
    t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1.8e9 + s.t


def Z(i, st="closed", name=None, **kw):
    return {"panel_zone_id": i, "name": name or f"Z{i}", "type": "Door_Window", "group": "x", "status": st, **kw}


def mk():
    return Engine(C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}}), FC())


def feed(e, m):
    b = e.handle(m); e.commit(); return b


# ---------------------------------------------------------------- 8a: insert without zoneid
def _adapter():
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), "/tmp/none")
    a.up = True; sent = []
    async def emit(**m): sent.append(m)
    a.emit = emit
    return a, sent


@pytest.mark.parametrize("cv", [{"sensorname": "New sensor", "sensorstatus": "Closed"}, {}, {"zoneid": ""}, {"zoneid": None}])
def test_insert_without_a_zone_id_triggers_a_resync_and_invents_nothing(cv):
    a, sent = _adapter()
    asyncio.run(a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "insert", "uri": IQ.URI_SENSOR, "contentValues": cv}))
    assert a._resync.is_set() and sent == [] and a.zones == {}


def test_inserts_without_a_zone_id_are_coalesced_into_one_resync():
    a, sent = _adapter()
    d = {"eventName": "dbChanged", "dbOperation": "insert", "uri": IQ.URI_SENSOR, "contentValues": {"sensorname": "x"}}
    for _ in range(50):
        asyncio.run(a.on_iq2meid(d))
    assert a._resync.is_set()
    a._resync.clear()                                   # ONE consumer pass handles all 50
    assert not a._resync.is_set()


def test_an_insert_whose_zone_id_is_in_the_selection_args_is_still_placed_not_resynced():
    a, sent = _adapter()
    d = {"eventName": "dbChanged", "dbOperation": "insert", "uri": IQ.URI_SENSOR,
         "contentValues": {"zoneid": "9", "sensorstatus": "Open"}}
    asyncio.run(a.on_iq2meid(d))
    assert 9 in a.zones and not a._resync.is_set() and sent[0]["new"] is True


def test_an_update_without_a_zone_id_still_does_not_resync():
    a, sent = _adapter()
    asyncio.run(a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR,
                              "contentValues": {"sensorstatus": "Open"}}))
    assert not a._resync.is_set()


# ---------------------------------------------------------------- 8b: restore() is a transaction
ZROW = {"id": "zone-1", "panel_zone_id": 1, "name": "Door", "kind": "contact", "state": "open", "stale": False,
        "partition_id": 0, "source": "iq_remote", "tamper": False, "battery_low": False, "online": True}


def test_restore_opens_a_transaction_that_commit_and_rollback_both_understand():
    e = mk()
    b = e.restore([dict(ZROW)], [])
    assert e._saved is not None and e.zones[1]["stale"] is True and [x["type"] for x in b.events] == ["zone.stale"]
    e.rollback()                                          # the batch could not be persisted
    assert e.zones == {} and e._saved is None and e.resync_needed is True
    e2 = mk(); e2.restore([dict(ZROW)], []); e2.commit()
    assert e2.zones[1]["stale"] is True and e2._saved is None and e2.resync_needed is False


def test_restore_of_a_malformed_cache_row_leaves_no_half_restored_engine():
    e = mk()
    with pytest.raises(KeyError):
        e.restore([dict(ZROW), {"id": "zone-2", "name": "no panel_zone_id"}], [])
    assert e.zones == {} and e._saved is None             # zone-1 (restored before the bad row) was rolled back
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1)], "partitions": []})
    assert "zone.added" in [x["type"] for x in b.events]


def test_a_failed_persist_of_the_restore_batch_rolls_the_engine_back(tmp_path, monkeypatch):
    from br2.normalizer.service import NormalizerService
    cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, "storage": {"data_dir": str(tmp_path)}})
    svc = NormalizerService(cfg, ipc_dir=str(tmp_path))
    b = svc.engine.restore([dict(ZROW)], [])
    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(svc.store, "commit", boom)
    assert svc.process_batch(b) is False
    assert svc.engine.zones == {} and svc.engine.resync_needed is True
    svc.store.close()


# ---------------------------------------------------------------- 9: zone state as of the event, in the log row
def test_zinfo_records_the_state_and_every_attribute_event_carries_it(tmp_path):
    assert zinfo({"name": "n", "kind": "contact", "partition_id": 0, "state": "open"})["state"] == "open"
    e = mk()
    feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Closed", name="Door")], "partitions": []})
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Open", name="Door", battery="Low")], "partitions": []})
    by = {x["type"]: x for x in b.events}
    assert by["zone.state"]["zone"]["state"] == "open" and by["zone.battery"]["zone"]["state"] == "open"
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Open", name="Renamed", battery="Low")], "partitions": []})
    assert [x["type"] for x in b.events] == ["zone.renamed"] and b.events[0]["zone"]["state"] == "open"
    b = e.ipc_down(PRIMARY); e.commit()
    stale = [x for x in b.events if x["type"] == "zone.stale"]
    assert stale and stale[0]["zone"]["state"] == "open"


def test_the_state_survives_the_sqlite_log_and_the_team_message_uses_it_not_the_live_state(tmp_path):
    db = str(tmp_path / "b.db"); store = Store(db, 30)
    e = mk()
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Open", name="Door")], "partitions": []})
    b2 = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, "Open", name="Door2")], "partitions": []})   # renamed while open
    store.commit(b.events + b2.events, {}, [], {}, [])
    rows = Reader(db).events_after(0, 50, with_zone=True)
    ren = next(r for r in rows if r["type"] == "zone.renamed")
    assert ren["zone"]["state"] == "open"
    live = {"id": "zone-1", "name": "Door2", "kind": "contact", "partition_id": 0, "state": "closed", "stale": False}
    _mid, build = F.event_builder({"id": "s", "name": "s"}, ren, live, "zone_renamed", 1.0, "db")
    m = build()
    assert m["state"] == "open" and m["previous_state"] is None and m["previous_name"] == "Door"      # NOT live "closed"
    store.close()


@pytest.mark.parametrize("et", ["tamper", "tamper_cleared", "battery_low", "battery_ok", "zone_stale", "zone_fresh"])
def test_attribute_events_use_the_logged_state(et):
    row = {"seq": 3, "id": "evt-3", "ts": "2026-01-01T00:00:00.000Z", "type": "x", "subject": "zone-1", "from": None,
           "to": True, "source": "iq_remote", "synthetic": False,
           "zone": {"name": "Door", "kind": "contact", "partition": 0, "stale": False, "state": "open"}}
    live = {"id": "zone-1", "name": "Door", "kind": "contact", "partition_id": 0, "state": "closed", "stale": False}
    _m, build = F.event_builder({"id": "s", "name": "s"}, row, live, et, 1.0, "db")
    assert build()["state"] == "open"
    legacy = dict(row, zone={"name": "Door", "kind": "contact", "partition": 0, "stale": False})    # row from an old version
    _m, build = F.event_builder({"id": "s", "name": "s"}, legacy, live, et, 1.0, "db")
    assert build()["state"] == "closed"                                                          # falls back to live state


def test_the_contract_documents_state_as_of_the_event():
    import pathlib
    t = (pathlib.Path(__file__).resolve().parents[1] / "MQTT-CONTRACT.md").read_text()
    assert "as of the event" in t and "not the state at the time the message is built" in t
