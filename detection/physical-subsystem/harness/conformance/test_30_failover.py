import threading, time
import pytest
from conformance.helpers import *

FB = TIMERS["fallback_after_s"]


def failover_deadline(): return FB + TIMERS["silence_probe_s"] + TIMERS["probe_timeout_s"] + 12


def test_primary_outage_fails_over_to_control4_and_back(env):
    env.settle(); w = env.watcher(); base = env.last_seq()
    t0 = time.time(); env.sim.iq.outage()
    h = env.wait_health(failover_deadline(), "status degraded + active_source control4", status="degraded", active_source="control4")
    took = time.time() - t0
    assert took >= FB - 1.5, f"switched to fallback after only {took:.1f}s (< fallback_after_s={FB})"
    wait_until(lambda: w.status == "degraded", 5, 0.1, "retained bridge/status degraded on MQTT")
    # data is correct from the fallback, including a change that happens while on the fallback
    env.sim.set_zone(12, "Open"); env.sim.set_zone(14, "Active")
    env.wait_zone(12, "open"); env.wait_zone(14, "active")
    env.assert_matches_sim()
    z = env.zones(); assert z[12]["source"] == "control4" and z[12]["stale"] is False, z[12]
    wait_until(lambda: (w.zone(12) or {}).get("source") == "control4", 5, 0.1, "MQTT zone source control4")
    ev = [e for e in env.events(after=base) if e["type"] == "zone.state"]
    assert [(str(e["subject"]), e["to"]) for e in ev if e["synthetic"] is False][-2:] == [("zone-12", "open"), ("zone-14", "active")] or \
        {(e["subject"], e["to"]) for e in ev} >= {("zone-12", "open"), ("zone-14", "active")}, ev
    assert all(e["source"] == "control4" for e in ev), ev
    # primary returns -> switch back, status online, state still correct, NO duplicate/spurious zone events
    env.sim.iq.restore(); seq_before_back = env.last_seq()
    env.wait_online(30, "iq_remote")
    env.sim.set_zone(12, "Closed"); env.wait_zone(12, "closed")
    env.settle()
    allev = env.events()
    assert_contiguous([e["seq"] for e in allev], start=1)
    assert_zone_chains(allev)
    spurious = [e for e in allev if e["seq"] > seq_before_back and e["type"] == "zone.state" and str(e["subject"]) != "zone-12"]
    assert not spurious, f"spurious zone events after switching back: {spurious}"
    assert env.zones()[12]["source"] == "iq_remote"
    wait_until(lambda: w.status == "online", 5, 0.1, "MQTT status back to online")
    env.assert_matches_sim(check_stale=True)
    assert env.sim.c4.bad_token == 0


def test_stall_tcp_open_no_data_detected(env):
    """IQ broker frozen (TCP open, never answers): probe timeout must mark link dead and fail over."""
    env.settle(); base = env.last_seq()
    t0 = time.time(); env.sim.iq.stall()
    env.wait_health(failover_deadline() + 10, "failover after silent stall", status="degraded", active_source="control4")
    detect = time.time() - t0
    assert detect >= 2, f"suspiciously fast ({detect:.1f}s)"
    h = env.health(); 
    env.sim.set_zone(13, "Active"); env.wait_zone(13, "active")
    assert env.zones()[13]["source"] == "control4"
    env.sim.iq.unstall()
    env.wait_online(40, "iq_remote")
    env.assert_matches_sim(check_stale=True)


def test_stall_app_level_silence_detected(env):
    """Panel stays TCP/MQTT-alive but stops answering commands and emitting events (muted): silence probe must catch it."""
    env.settle(); env.sim.iq.mute(True)
    env.wait_health(failover_deadline() + 10, "failover on app-level silence", status="degraded", active_source="control4")
    h = env.health(); assert h["seconds_since_panel_message"] is not None
    env.sim.set_zone(12, "Open"); env.wait_zone(12, "open")
    env.sim.iq.mute(False)
    env.wait_online(40, "iq_remote"); env.assert_matches_sim(check_stale=True)


def test_total_outage_marks_everything_stale_then_recovers(env):
    env.settle(); w = env.watcher()
    env.sim.iq.outage(); env.sim.c4.outage()
    deadline = failover_deadline() + 10
    def all_stale():
        z = env.zones(); return z and all(v.get("stale") is True for v in z.values())
    wait_until(all_stale, deadline, 0.25, "every zone stale=true on REST during total outage")
    wait_until(lambda: all((w.zone(n) or {}).get("stale") is True for n in env.expected()), 10, 0.2, "stale=true on retained MQTT zone state")
    p = env.partitions()[0]; assert p.get("stale") is True, p
    assert wait_until(lambda: (w.partition(0) or {}).get("stale") is True, 10, 0.2, "stale partition on MQTT")
    h = env.health(); assert h["status"] in ("offline", "degraded"), h
    assert h["status"] != "online" and h["active_source"] in (None, "control4", "iq_remote")
    t1 = h["seconds_since_panel_message"]; time.sleep(2.2); t2 = env.health()["seconds_since_panel_message"]
    assert t2 is not None and t1 is not None and t2 >= t1 + 1.5, f"seconds_since_panel_message not advancing: {t1} -> {t2}"
    # values are retained (last known) while stale, REST still answers
    assert env.zones()[12]["state"] == "closed"
    wait_until(lambda: w.status in ("offline", "degraded"), 10, 0.2, "MQTT bridge/status leaves online")
    # recovery of only the fallback first: data flows, stale clears
    env.sim.c4.restore(); env.wait_health(40, "degraded via control4", status="degraded", active_source="control4")
    wait_until(lambda: all(v.get("stale") is False for v in env.zones().values()), 10, 0.2, "stale cleared by fallback snapshot")
    env.sim.iq.restore(); env.wait_online(40)
    wait_until(lambda: all((w.zone(n) or {}).get("stale") is False for n in env.expected()), 10, 0.2, "stale cleared on MQTT")
    env.assert_matches_sim(check_stale=True)


def test_flapping_primary_with_concurrent_changes_race(env):
    """Race hunting: flip zones continuously while the primary flaps (outage/restore/drop). Final state converges, log stays
    gap-free, each zone's from/to chain stays consistent, nothing is duplicated."""
    env.settle(); base = env.last_seq(); stop = threading.Event(); n = {"i": 0}
    def churn():
        sts = ["Open", "Closed"]
        while not stop.is_set():
            n["i"] += 1; env.sim.set_zone(12, sts[n["i"] % 2]); env.sim.set_zone(13, ["Active", "Idle"][n["i"] % 2]); time.sleep(0.12)
    t = threading.Thread(target=churn, daemon=True); t.start()
    try:
        for _ in range(3):
            env.sim.iq.outage(); time.sleep(FB + 1.5); env.sim.iq.restore(); time.sleep(2.5)
            env.sim.iq.drop(); time.sleep(1.5)
            env.sim.c4.drop(); time.sleep(1.0)
    finally:
        stop.set(); t.join()
    env.sim.set_zone(12, "Closed"); env.sim.set_zone(13, "Idle"); env.sim.iq.restore(); env.sim.c4.restore()
    env.wait_online(40)
    wait_until(lambda: env.zones()[12]["state"] == "closed" and env.zones()[13]["state"] == "idle", 10, 0.2, "converged final state")
    env.settle(2.5)
    allev = env.events()
    assert_contiguous([e["seq"] for e in allev], start=1, what="entire log after flapping")
    assert_zone_chains(allev)
    assert len({e["id"] for e in allev}) == len(allev), "duplicate event ids"
    env.assert_matches_sim(check_stale=True)
    # final state of every zone equals sim; chain end equals REST state
    last = {}
    for e in allev:
        if e["type"] == "zone.state": last[e["subject"]] = e["to"]
    for subj, to in last.items(): assert env.zones()[int(str(subj).replace("zone-", ""))]["state"] == to, f"event chain ends at {to} for {subj}, REST differs"
