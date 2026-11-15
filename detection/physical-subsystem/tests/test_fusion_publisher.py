"""Producer logic: log -> outbox by seq with a persisted cursor; status heartbeats; stale/fresh; restarts."""
import json
import pytest
from br2 import config as C
from br2.normalizer.store import Store, Reader
from br2.outbound.fusion import FusionPublisher
from br2.outbound.outbox import Outbox
from br2.outbound.state import OutState
from fusion_helpers import validate, zone, dump, log_event, ROOT


class Clk:
    def __init__(s): s.t = 1_767_268_800.0
    def __call__(s): return s.t


class FakeSender:
    connected = type("E", (), {"is_set": lambda s: False})()
    counters = {"reconnects": 0, "published": 0, "acked": 0}
    def wake(self): pass
    def inflight(self): return 0
    def start(self): pass
    def stop(self): pass


def mk(tmp_path, **fusion):
    cfg = C.parse({"storage": {"data_dir": str(tmp_path)}, "site": {"id": "s1", "name": "Site One"},
                   "fusion": dict({"enabled": True, "status_interval_s": 60, "startup_grace_s": 0}, **fusion)})
    store = Store(cfg.db_path)
    return cfg, store


def commit(store, *evs):
    return store.commit([{"ts": e[0], "type": e[1], "subject": e[2], "from": e[3], "to": e[4], "source": "iq_remote",
                          "synthetic": False} for e in evs], {}, [], {}, [])


class Rig:
    def __init__(self, tmp_path, cfg, store):
        self.cfg, self.store, self.tmp = cfg, store, tmp_path
        self.clk, self.mono = Clk(), Clk()
        self.mono.t = 0.0
        self.st = OutState()
        self.new_pub()

    def new_pub(self):
        self.pub = FusionPublisher(self.cfg, self.st, Reader(self.cfg.db_path), sender=FakeSender(), clock=self.clk, mono=self.mono)
        return self.pub

    def msgs(self, topic_suffix=None):
        out = [validate(e.payload) for e in self.pub.outbox.pending(100000)]
        return [m for m in out if topic_suffix is None or m["schema"].endswith(topic_suffix)]

    def events(self): return self.msgs("event.v0")

    def apply_dump(self, zones, **kw): self.st.apply(dump(zones, last_seq=self.store.last_seq(), **kw))


async def test_events_flow_from_log_with_site_zone_and_cursor(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(12, "contact", "closed", name="Front door"), zone(13, "motion", "idle", name="Hall")])
    await r.pub.tick()                                       # first run: cursor starts at head, history not replayed
    assert r.events() == []
    commit(store, ("2026-01-01T12:00:01.500Z", "zone.state", "zone-12", "closed", "open"),
           ("2026-01-01T12:00:02.000Z", "zone.battery", "zone-12", False, True),
           ("2026-01-01T12:00:03.000Z", "zone.state", "zone-13", "idle", "active"))
    r.st.zones["zone-12"]["state"] = "open"
    await r.pub.tick()
    ev = r.events()
    assert [e["event_type"] for e in ev] == ["zone_opened", "battery_low", "motion_detected"]
    assert ev[0]["timestamp"] == "2026-01-01T12:00:01.500Z" and ev[0]["site"] == {"id": "s1", "name": "Site One"}
    assert ev[0]["zone"] == {"id": "zone-12", "name": "Front door", "kind": "contact", "partition": 0}
    assert ev[0]["previous_state"] == "closed" and ev[0]["state"] == "open"
    assert [e["seq"] for e in ev] == [1, 2, 3]               # seq IS the log seq
    assert [e["prev_seq"] for e in ev] == [None, 1, 2]
    assert ev[1]["battery_low"] is True and ev[1]["previous_state"] is None
    assert r.pub.outbox.cursor["seq"] == 3 and r.pub.outbox.cursor["pub"] == 3
    assert all("seq" not in m for m in r.msgs("status.v0") + r.msgs("zone-status.v0"))


async def test_restart_resumes_from_cursor_nothing_skipped_nothing_repeated(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(12)])
    await r.pub.tick()
    commit(store, ("2026-01-01T12:00:01.000Z", "zone.state", "zone-12", "closed", "open"))
    await r.pub.tick()
    first = {m["id"] for m in r.events()}
    assert len(first) == 1
    r.pub.outbox.close()
    commit(store, ("2026-01-01T12:00:05.000Z", "zone.state", "zone-12", "open", "closed"))   # while we were "down"
    r.new_pub()
    await r.pub.tick()
    ev = r.events()
    assert [e["event_type"] for e in ev] == ["zone_opened", "zone_closed"]
    assert first < {m["id"] for m in ev}                      # the first one is still there with the SAME id
    await r.pub.tick(); await r.pub.tick()
    assert len(r.events()) == 2                               # idempotent


async def test_crash_between_enqueue_and_cursor_does_not_duplicate(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(12)])
    await r.pub.tick()
    commit(store, ("2026-01-01T12:00:01.000Z", "zone.state", "zone-12", "closed", "open"))
    await r.pub.tick()
    # simulate a cursor that never made it to disk: journal only has the message
    ob = r.pub.outbox; ob.cursor = {"seq": 0, "db_id": ob.cursor["db_id"]}; ob.close()
    r.new_pub()
    r.pub.outbox.cursor = {"seq": 0, "db_id": r.pub.outbox.cursor["db_id"]}
    await r.pub.tick()
    assert len(r.events()) == 1                               # same derived id: not queued twice


async def test_status_on_change_and_every_interval(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1), zone(2, "motion", "idle", stale=True)])
    await r.pub.tick()
    p, z = r.msgs("panel-status.v0"), r.msgs("zone-status.v0")
    assert len(p) == 1 and len(z) == 2
    assert p[0]["state"] == "online" and p[0]["active_source"] == "iq_remote" and p[0]["stale_zones"] == 1
    assert p[0]["links"] == {"iq_remote": "up", "control4": "disabled"} and p[0]["zones"] == 2
    await r.pub.tick(); r.mono.t += 59
    await r.pub.tick()
    assert len(r.msgs("panel-status.v0")) == 1 and len(r.msgs("zone-status.v0")) == 2     # nothing changed, < 60 s
    r.mono.t += 2                                              # 61 s: heartbeat for the panel and for EVERY zone
    r.clk.t += 61
    await r.pub.tick()
    assert len(r.msgs("panel-status.v0")) == 2 and len(r.msgs("zone-status.v0")) == 4
    # change: panel degrades -> immediate panel message; a zone flips -> immediate message for that zone only
    r.st.apply({"t": "health", "health": {"active_source": "control4", "links": {"iq_remote": "down", "control4": "up"}}, "status": "degraded"})
    r.st.zones["zone-1"]["state"] = "open"
    await r.pub.tick()
    p, z = r.msgs("panel-status.v0"), r.msgs("zone-status.v0")
    assert p[-1]["state"] == "degraded" and p[-1]["active_source"] == "control4" and p[-1]["links"]["iq_remote"] == "down"
    assert len(z) == 5 and z[-1]["zone"]["id"] == "zone-1" and z[-1]["state"] == "open"
    assert len({m["id"] for m in p + z}) == len(p + z)         # every status message has its own id


async def test_ipc_loss_publishes_offline_status_and_stale_flags(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1), zone(2)])
    await r.pub.tick()
    r.st.apply({"t": "ipc_closed"})
    await r.pub.tick()
    p = r.msgs("panel-status.v0")[-1]
    assert p["state"] == "offline" and p["active_source"] is None and p["stale_zones"] == 2
    zs = [m for m in r.msgs("zone-status.v0") if m["stale"]]
    assert {m["zone"]["id"] for m in zs} == {"zone-1", "zone-2"}
    assert r.events() == []                                  # stale EVENTS come only from the log (normalizer), not from here


async def test_stale_and_fresh_events_come_from_the_log_with_their_own_seq(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1, state="open")])
    await r.pub.tick()
    commit(store, ("2026-01-01T12:00:03.000Z", "zone.stale", "zone-1", False, True),
           ("2026-01-01T12:00:04.000Z", "bridge.link", "iq_remote", "up", "down"),         # not published
           ("2026-01-01T12:00:09.000Z", "zone.stale", "zone-1", True, False))
    await r.pub.tick()
    ev = r.events()
    assert [e["event_type"] for e in ev] == ["zone_stale", "zone_fresh"]
    assert [e["seq"] for e in ev] == [1, 3] and [e["prev_seq"] for e in ev] == [None, 1]   # gap at 2 is explained by prev_seq
    assert [e["timestamp"] for e in ev] == ["2026-01-01T12:00:03.000Z", "2026-01-01T12:00:09.000Z"]   # true times
    assert [e["stale"] for e in ev] == [True, False] and ev[0]["state"] == "open"


async def test_stale_event_survives_restart_and_prev_seq_chain_continues(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(1)])
    await r.pub.tick()
    commit(store, ("2026-01-01T12:00:03.000Z", "zone.stale", "zone-1", False, True))
    await r.pub.tick()
    r.pub.outbox.close()
    commit(store, ("2026-01-01T12:00:09.000Z", "zone.stale", "zone-1", True, False))        # while we were down
    r.new_pub()
    await r.pub.tick()
    ev = r.events()
    assert [(e["event_type"], e["seq"], e["prev_seq"]) for e in ev] == [("zone_stale", 1, None), ("zone_fresh", 2, 1)]


async def test_zone_removed_event_uses_cached_zone_info(tmp_path):
    cfg, store = mk(tmp_path)
    r = Rig(tmp_path, cfg, store)
    r.apply_dump([zone(5, "glass_break", "idle", name="Lounge glass")])
    await r.pub.tick()
    commit(store, ("2026-01-01T12:00:09.000Z", "zone.removed", "zone-5", "idle", None))
    r.st.apply({"t": "batch", "events": [], "zones": [], "removed_zones": ["zone-5"], "partitions": [],
                "removed_partitions": [], "status": "online"})
    await r.pub.tick()
    e = r.events()[0]
    assert e["event_type"] == "zone_removed" and e["zone"]["name"] == "Lounge glass" and e["zone"]["kind"] == "glass_break"
    assert e["state"] == "idle" and e["previous_state"] is None


async def test_no_status_before_first_dump_until_grace(tmp_path):
    cfg, store = mk(tmp_path, startup_grace_s=10)
    r = Rig(tmp_path, cfg, store)
    await r.pub.tick()
    assert r.msgs() == []
    r.mono.t += 11
    await r.pub.tick()
    assert [m["state"] for m in r.msgs("panel-status.v0")] == ["offline"]   # normalizer never came up: say so


def test_disabled_by_default_and_config_example_loads():
    assert C.parse({}).fusion["enabled"] is False and C.parse({}).site["id"] == "default"
    c = C.load(ROOT / "deploy/config/config.example.yaml")
    assert c.fusion["enabled"] is False and c.fusion["topic_prefix"] == "detection/physical"
    assert c.fusion["tls"] is None and c.fusion["port"] == 1883
