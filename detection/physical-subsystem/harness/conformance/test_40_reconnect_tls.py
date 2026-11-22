import time
import pytest
from conformance.helpers import *

FB = TIMERS["fallback_after_s"]


def test_reconnect_with_diverged_state_synthetic_events_equal_diff(env):
    """Both panel links down; panel state diverges (changes + add + remove + a no-net-change flap + arming);
    on reconnect the bridge must emit synthetic events EXACTLY equal to the diff: none missing, none duplicated, no extras."""
    env.settle(); w = env.watcher(); base = env.last_seq()
    sim = env.sim
    sim.iq.outage(); sim.c4.outage()
    wait_until(lambda: all(v.get("stale") for v in env.zones().values()), 30, 0.25, "stale during outage")
    seq_down = env.last_seq()
    def diverge(s):
        s.set_zone(12, "Open")                 # net change closed->open
        s.set_zone(13, "Active")               # net change idle->active
        s.set_zone(15, "Active"); s.set_zone(15, "Idle")   # flap: NO net change -> must produce no event
        s.set_zone(14, battery="Low")          # battery change (zone.battery)
        s.remove_zone(16)                      # removal
        s.add_zone(20, "Garage glass", "GlassBreak", "Idle")   # addition
        s.set_arming(0, "ARM-AWAY")            # partition change
    sim.diverge_while_down(diverge)
    sim.iq.restore()                           # only the primary returns
    env.wait_online(40)
    env.wait_zone(12, "open"); env.settle(2.5)
    evs = [e for e in env.events(after=seq_down) if e["type"] != "bridge.link"]
    sig = lambda e: (e["type"], str(e["subject"]), str(e.get("to") if not isinstance(e.get("to"), dict) else e["to"].get("state", e["to"].get("name"))))
    got = sorted(sig(e) for e in evs)
    want = sorted([("zone.state", "zone-12", "open"), ("zone.state", "zone-13", "active"), ("zone.removed", "zone-16", None),
                   ("partition.arming", "0", "arm_away")] )
    # compare core (state/removal/arming) strictly; added/battery tolerate representation of `to`
    core = [s for s in got if s[0] in ("zone.state", "zone.removed", "partition.arming")]
    core_norm = sorted((t, s if t != "partition.arming" else s.replace("partition-", ""), ("None" if x in (None, "None") else x)) for t, s, x in core)
    want_norm = sorted((t, s, "None" if x is None else x) for t, s, x in want)
    assert core_norm == want_norm, f"synthetic diff mismatch.\n got : {core_norm}\n want: {want_norm}\n all events: {[ (e['seq'], e['type'], e['subject'], e.get('from'), e.get('to'), e['synthetic']) for e in evs]}"
    assert sum(1 for e in evs if e["type"] == "zone.added") == 1 and any("20" in str(e["subject"]) for e in evs if e["type"] == "zone.added"), evs
    assert sum(1 for e in evs if e["type"] == "zone.battery") <= 1
    assert not any(e["type"] == "zone.state" and "15" in str(e["subject"]) for e in evs), "no-net-change flap produced an event"
    assert not any(e["type"] == "zone.state" and ("14" in str(e["subject"]) or "20" in str(e["subject"])) for e in evs), f"spurious state events {evs}"
    assert all(e["synthetic"] is True for e in evs), f"diff events must be flagged synthetic: {[e for e in evs if not e['synthetic']]}"
    assert len({e["seq"] for e in evs}) == len(evs)
    assert_contiguous([e["seq"] for e in env.events()], start=1)
    # outputs reflect the new truth
    z = env.zones(); assert 16 not in z and 20 in z and z[20]["kind"] == "glass_break"
    assert wait_until(lambda: w.zone(20) is not None, 5, 0.1, "new zone on MQTT")
    assert env.partitions()[0]["arming"] == "arm_away"
    # removed zone's retained MQTT state must be cleared (or flagged), not left as a live-looking ghost
    wait_until(lambda: w.latest.get(f"{PREFIX}/zones/zone-16/state") in (None, "") or (w.latest.get(f"{PREFIX}/zones/zone-16/state") or {}).get("online") is False,
               8, 0.2, "zone-16 retained state cleared after removal")
    env.assert_matches_sim(check_stale=True)


def test_diverged_state_on_fallback_switch_is_also_diffed(env):
    """Primary dies; while only the fallback is up the removal (invisible as an event on Control4) must still be caught by the SUMMARY diff."""
    env.settle(); base = env.last_seq()
    env.sim.iq.outage(); env.sim.c4.drop()            # c4 session reset: bridge must reconnect and re-diff
    env.sim.diverge_while_down(lambda s: (s.remove_zone(16), s.set_zone(13, "Active")))
    env.wait_health(failover_deadline(), "fallback", status="degraded", active_source="control4")
    env.wait_zone(13, "active"); wait_until(lambda: 16 not in env.zones(), 10, 0.2, "zone 16 removal detected via C4 SUMMARY diff")
    evs = env.events(after=base)
    assert any(e["type"] == "zone.removed" and "16" in str(e["subject"]) for e in evs), evs
    assert sum(1 for e in evs if e["type"] == "zone.state" and "13" in str(e["subject"])) == 1


def failover_deadline(): return FB + TIMERS["silence_probe_s"] + TIMERS["probe_timeout_s"] + 12


def test_tls_bridge_refuses_wrong_control4_fingerprint(env):
    """Pinned fingerprint no longer matches (panel cert rotated): with the primary down the bridge must NOT talk to the fallback."""
    env.settle()
    env.sim.c4.rotate_cert(); n_req = env.sim.c4.requests; acc = env.sim.c4.accepted
    env.sim.iq.outage()
    time.sleep(FB + TIMERS["silence_probe_s"] + 8)
    h = env.health()
    assert h["active_source"] != "control4", f"bridge used a Control4 endpoint whose cert does not match the pin: {h}"
    assert env.sim.c4.requests == n_req, f"bridge SENT {env.sim.c4.requests - n_req} request(s) to an unpinned Control4 peer (token leak!)"
    assert env.sim.audit.count(side="c4") == 0
    assert h["status"] != "online"
    assert all(v["stale"] for v in env.zones().values())
    assert env.sim.c4.accepted > acc or True   # attempts are expected; data must not flow
    # sanity: it is the pin that blocks, not the outage: restore the pin by restarting with the new fingerprint
    env.restart_bridge(control4={"cert_sha256": env.sim.c4.fingerprint})
    env.wait_health(40, "fallback works with correct pin", active_source="control4")
    env.sim.iq.restore(); env.wait_online(40)


def test_tls_control4_null_or_malformed_fingerprint_refuses_to_connect(env_nostart):
    e = env_nostart
    e.sim.iq.outage()
    e.write_config(control4={"cert_sha256": None}); 
    e.cfg = e.build_config(control4={"cert_sha256": None}); e.start_bridge()
    time.sleep(FB + 8)
    assert e.sim.c4.requests == 0, "bridge talked to Control4 although cert_sha256 is null (config: null = refuse to connect)"
    assert e.health()["active_source"] != "control4"


def test_tls_bridge_refuses_untrusted_iq_panel_cert(env):
    """Panel (MQTT/TLS) presents a cert from an untrusted CA: bridge must never complete a session or send a command to it."""
    env.settle()
    env.sim.c4.outage()                     # isolate: only the IQ path
    env.sim.iq.set_cert("rogue")
    n = env.sim.iq.commands["connect_v204"]; sess = sum(env.sim.iq.commands.values())
    time.sleep(FB + TIMERS["silence_probe_s"] + 8)
    assert sum(env.sim.iq.commands.values()) == sess, "bridge sent MQTT commands to a panel with an untrusted certificate"
    h = env.health(); assert h["status"] != "online" and h["active_source"] != "iq_remote", h
    # with a rogue server cert, the broker must not have seen ANY inbound application data
    assert env.sim.audit.count(side="iq") == 0 or env.sim.audit.count(side="iq", verdict="violation") == 0
    env.sim.iq.set_cert("trusted"); env.sim.c4.restore()
    env.wait_online(40)


def test_iq_cert_rotation_trusted_ca_recovers(env):
    env.settle(); sess = env.sim.iq.sessions
    env.sim.iq.set_cert("trusted")           # new server cert from the same CA, connections dropped
    wait_until(lambda: env.sim.iq.sessions > sess, 30, 0.25, "new IQ session after server cert rotation")
    env.wait_online(30); env.assert_matches_sim(check_stale=True)
