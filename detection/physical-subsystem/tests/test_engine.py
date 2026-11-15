import copy
from br2 import config as C
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK


class FakeClock(Clock):
    def __init__(self): self.t = 1000.0
    def mono(self): return self.t
    def wall(self): return 1_700_000_000 + self.t
    def adv(self, s): self.t += s


def cfg(**over):
    raw = {"iq_remote": {"enabled": True, "panel_ip": "1.2.3.4"},
           "control4": {"enabled": True, "host": "1.2.3.5"},
           "timers": {"fallback_after_s": 120, "ipc_liveness_s": 10**9, "failback_holddown_s": 0},
           "zones": {12: {"name": "Front door", "kind": "contact"}, 40: {"ignore": True}}}
    raw.update(over)
    return C.parse(raw)


def z(n, status="Closed", type_="Door_Window", **kw):
    d = {"panel_zone_id": n, "name": f"z{n}", "type": type_, "group": "", "status": status, "partition_id": 0,
         "battery": "Normal"}
    d.update(kw)
    return d


def snap(src, zones, parts=None):
    return {"t": "snapshot", "src": src, "zones": zones,
            "partitions": parts if parts is not None else [{"partition_id": 0, "name": "Main", "status": "DISARM"}]}


def types(b, syn=None):
    return [(e["type"], e["subject"], e["from"], e["to"]) for e in b.events
            if syn is None or e["synthetic"] == syn]


def mk():
    clk = FakeClock()
    return Engine(cfg(), clk), clk


def test_cold_start_emits_synthetic_added_and_publishes_records():
    e, _ = mk()
    b = e.handle(snap(PRIMARY, [z(12), z(13, "Idle", "Motion"), z(40), z(14, "Idle", "GlassBreak")]))
    adds = [x for x in b.events if x["type"] == "zone.added"]
    assert sorted(x["subject"] for x in adds) == ["zone-12", "zone-13", "zone-14"]  # 40 ignored
    assert all(x["synthetic"] for x in adds)
    r = e.zones[12]
    assert (r["name"], r["kind"], r["state"], r["source"], r["stale"]) == ("Front door", "contact", "closed", PRIMARY, False)
    assert e.zones[13]["kind"] == "motion" and e.zones[13]["state"] == "idle"
    assert e.zones[14]["kind"] == "glass_break"
    assert e.status == "online" and e.active == PRIMARY
    assert [x["type"] for x in b.events if x["type"] == "bridge.link"] == ["bridge.link"]


def test_delta_events_are_not_synthetic_and_dedup():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Open"}})
    assert types(b) == [("zone.state", "zone-12", "closed", "open")] and not b.events[0]["synthetic"]
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Open"}})
    assert b.events == [] and "zone-12" in b.zones  # heartbeat refreshes last_seen only


def test_tamper_and_battery_and_online():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Tampered"}})
    assert types(b) == [("zone.tamper", "zone-12", False, True)] and e.zones[12]["state"] == "closed"
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Closed", "battery": "Low"}})
    assert ("zone.tamper", "zone-12", True, False) in types(b) and ("zone.battery", "zone-12", False, True) in types(b)
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Unreachable"}})
    assert ("zone.online", "zone-12", True, False) in types(b)


def test_reconnect_full_snapshot_diff_exact_synthetic_events():
    e, clk = mk()
    e.handle(snap(PRIMARY, [z(12), z(13, "Idle", "Motion"), z(14, "Idle", "GlassBreak")]))
    b = e.handle({"t": "link", "src": PRIMARY, "up": False})
    assert all(r["stale"] for r in e.zones.values()) and e.status == "offline"
    assert set(b.zones) == {"zone-12", "zone-13", "zone-14"}          # stale republished
    assert types(b) == [("bridge.link", PRIMARY, "up", "down"), ("zone.stale", "zone-12", False, True),
                        ("zone.stale", "zone-13", False, True), ("zone.stale", "zone-14", False, True),
                        ("partition.stale", "partition-0", False, True)]
    assert all(not x["synthetic"] for x in b.events)                  # observed live, not inferred
    # while down: door opened, motion removed, new zone, glass tamper, partition armed
    b = e.handle(snap(PRIMARY, [z(12, "Open"), z(14, "Tampered", "GlassBreak", battery="Low"), z(15, "Active", "Motion")],
                      [{"partition_id": 0, "name": "Main", "status": "ARM-AWAY"}]))
    got = types(b, syn=True)
    assert sorted(got, key=str) == sorted([
        ("zone.state", "zone-12", "closed", "open"),
        ("zone.removed", "zone-13", "idle", None),
        ("zone.tamper", "zone-14", False, True),
        ("zone.battery", "zone-14", False, True),
        ("zone.added", "zone-15", None, "active"),
        ("zone.stale", "zone-12", True, False), ("zone.stale", "zone-14", True, False),
        ("partition.stale", "partition-0", True, False),
        ("partition.arming", "partition-0", "disarmed", "arm_away")], key=str)
    assert [t for t in types(b, syn=False)] == [("bridge.link", PRIMARY, "down", "up")]
    assert not any(r["stale"] for r in e.zones.values()) and 13 not in e.zones
    assert b.removed_zones == ["zone-13"]
    # identical snapshot again => zero events
    b = e.handle(snap(PRIMARY, [z(12, "Open"), z(14, "Tampered", "GlassBreak", battery="Low"), z(15, "Active", "Motion")],
                      [{"partition_id": 0, "name": "Main", "status": "ARM-AWAY"}]))
    assert b.events == []


def test_alarm_partition_events():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    b = e.handle({"t": "partition", "src": PRIMARY, "partition": {"partition_id": 0, "alarm_state": "Alarm"}})
    assert ("partition.arming", "partition-0", "disarmed", "alarm") in types(b)
    assert ("partition.alarm", "partition-0", None, "unknown") in types(b)
    b = e.handle({"t": "partition", "src": PRIMARY, "partition": {"partition_id": 0, "alarm_type": "glassbreak"}})
    assert types(b) == [("partition.alarm", "partition-0", "unknown", "glassbreak")]
    b = e.handle({"t": "partition", "src": PRIMARY, "partition": {"partition_id": 0, "alarm_state": "None", "alarm_type": None}})
    assert ("partition.arming", "partition-0", "alarm", "disarmed") in types(b)
    assert ("partition.alarm", "partition-0", "glassbreak", None) in types(b)


def test_failover_primary_to_fallback_and_back_with_diffs():
    e, clk = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    e.handle(snap(FALLBACK, [z(12)]))
    assert e.active == PRIMARY and e.status == "online"
    # standby deltas: no events, shadow only
    b = e.handle({"t": "zone", "src": FALLBACK, "zone": {"panel_zone_id": 12, "status": "Open"}})
    assert b.events == [] and e.zones[12]["state"] == "closed"
    e.handle({"t": "link", "src": PRIMARY, "up": False})
    assert e.active is None and e.zones[12]["stale"]
    clk.adv(119.9); assert e.tick().empty() and e.active is None
    clk.adv(0.2)
    b = e.tick()
    assert e.active == FALLBACK and e.status == "degraded"
    assert types(b) == [("zone.stale", "zone-12", True, False), ("zone.state", "zone-12", "closed", "open"),
                        ("partition.stale", "partition-0", True, False)]
    assert all(x["synthetic"] for x in b.events)
    assert e.zones[12]["source"] == FALLBACK and not e.zones[12]["stale"]
    # events on fallback now flow
    b = e.handle({"t": "zone", "src": FALLBACK, "zone": {"panel_zone_id": 12, "status": "Closed"}})
    assert types(b) == [("zone.state", "zone-12", "open", "closed")] and b.events[0]["source"] == FALLBACK
    # primary returns with different truth -> failback with diff
    b = e.handle(snap(PRIMARY, [z(12, "Open")]))
    assert e.active == PRIMARY and e.status == "online"
    assert ("zone.state", "zone-12", "closed", "open") in types(b, syn=True)
    assert e.zones[12]["source"] == PRIMARY
    # primary delta while fallback is standby
    b = e.handle({"t": "zone", "src": FALLBACK, "zone": {"panel_zone_id": 12, "status": "Closed"}})
    assert b.events == []


def test_both_down_all_stale_offline_then_recover_via_fallback():
    e, clk = mk()
    e.handle(snap(PRIMARY, [z(12)])); e.handle(snap(FALLBACK, [z(12)]))
    e.handle({"t": "link", "src": FALLBACK, "up": False})
    assert e.active == PRIMARY and not e.zones[12]["stale"]
    e.handle({"t": "link", "src": PRIMARY, "up": False})
    assert e.status == "offline" and e.zones[12]["stale"] and e.health()["stale_zones"] == 1
    clk.adv(500); e.tick(); assert e.active is None
    b = e.handle(snap(FALLBACK, [z(12)]))
    assert e.active == FALLBACK and not e.zones[12]["stale"]  # fallback long past threshold -> immediate


def test_never_up_primary_falls_back_after_threshold_from_boot():
    e, clk = mk()
    e.handle(snap(FALLBACK, [z(12)]))
    assert e.active is None and e.status == "offline"
    clk.adv(121); e.tick()
    assert e.active == FALLBACK and e.zones[12]["state"] == "closed"


def test_ipc_down_marks_link_down_and_superseded_ignored_by_service_logic():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    b = e.ipc_down(PRIMARY)
    assert ("bridge.link", PRIMARY, "up", "down") in types(b) and e.zones[12]["stale"]


def test_fallback_only_config_is_online():
    e = Engine(cfg(iq_remote={"enabled": False}), FakeClock())
    e.handle(snap(FALLBACK, [z(12)]))
    assert e.active == FALLBACK and e.status == "online"


def test_zone_removed_delta_and_ignore_delta():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12), z(13)]))
    b = e.handle({"t": "zone_removed", "src": PRIMARY, "panel_zone_id": 13})
    assert types(b) == [("zone.removed", "zone-13", "closed", None)]
    assert e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 40, "status": "Open"}}).empty()


def test_restore_then_first_snapshot_diffs_against_persisted_cache():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12), z(13)]))
    zs, ps = list(copy.deepcopy(e.zones).values()), list(copy.deepcopy(e.parts).values())
    e2, _ = mk()
    e2.restore(zs, ps)
    assert all(r["stale"] for r in e2.zones.values())
    b = e2.handle(snap(PRIMARY, [z(12, "Open"), z(13)]))
    assert types(b, syn=True) == [("zone.stale", "zone-12", True, False), ("zone.state", "zone-12", "closed", "open"),
                                  ("zone.stale", "zone-13", True, False), ("partition.stale", "partition-0", True, False)]


def test_stale_flips_are_logged_in_the_same_batch_as_the_state_change():
    e, clk = mk()
    e.handle(snap(PRIMARY, [z(12), z(13, "Idle", "Motion")]))
    b = e.handle({"t": "link", "src": PRIMARY, "up": False})
    stale = [x for x in b.events if x["type"] == "zone.stale"]
    assert sorted(x["subject"] for x in stale) == ["zone-12", "zone-13"] and all(x["to"] is True for x in stale)
    assert all(x["ts"] and x["source"] == PRIMARY for x in stale)
    assert set(b.zones) == {"zone-12", "zone-13"}                      # the stale records ride in the same Batch
    assert e.tick().events == []                                       # no second flip while it stays stale
    # a live delta from a source that is back: fresh + the state change, one batch
    e.handle(snap(PRIMARY, [z(12), z(13, "Idle", "Motion")]))
    e.handle({"t": "link", "src": PRIMARY, "up": False})
    b = e.handle(snap(PRIMARY, [z(12), z(13, "Idle", "Motion")]))
    assert [x["to"] for x in b.events if x["type"] == "zone.stale"] == [False, False]


def test_live_delta_on_a_stale_zone_logs_fresh_then_the_change():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12)]))
    e.zones[12] = dict(e.zones[12], stale=True)                        # (as after a fallback snapshot kept it stale)
    b = e.handle({"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 12, "status": "Open"}})
    assert types(b) == [("zone.stale", "zone-12", True, False), ("zone.state", "zone-12", "closed", "open")]
    assert all(not x["synthetic"] for x in b.events)


def test_restore_records_stale_for_zones_that_were_fresh_and_only_those():
    e, _ = mk()
    e.handle(snap(PRIMARY, [z(12), z(13)]))
    zs = [dict(copy.deepcopy(r)) for r in e.zones.values()]
    zs[1]["stale"] = True                                              # zone 13 was already stale when we stopped
    ps = list(copy.deepcopy(e.parts).values())
    e2, _ = mk()
    b = e2.restore(zs, ps)
    assert types(b) == [("zone.stale", "zone-12", False, True), ("partition.stale", "partition-0", False, True)] and "zone-12" in b.zones
    assert all(r["stale"] for r in e2.zones.values())
    e3, _ = mk()
    assert e3.restore([], []).empty()
