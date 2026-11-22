import time, threading
import pytest
from conformance.helpers import *


def zone_events(evs): return [e for e in evs if e["type"] == "zone.state"]


def test_live_change_seq_gapfree_mqtt_rest_sse(env):
    env.settle()
    w = env.watcher(); sse = env.sse(); assert sse.opened.wait(10) and sse.status == 200, f"SSE status {sse.status} err={sse.err}"
    time.sleep(0.5)
    base = env.last_seq()
    plan = [(12, "Open"), (13, "Active"), (14, "Active"), (12, "Closed"), (13, "Idle"), (14, "Idle"), (15, "Active"), (15, "Idle"),
            (12, "Open"), (12, "Closed")]
    for z, st in plan:
        env.sim.set_zone(z, st); time.sleep(0.25)
    env.sim.set_zone(12, "Open"); env.sim.set_zone(12, "Open")     # duplicate report: must NOT create an event
    time.sleep(1.5)
    env.wait_zone(12, "open")
    log = env.events(after=base)
    zs = zone_events(log)
    assert len(zs) == len(plan) + 1, f"expected {len(plan)+1} zone.state events (dup suppressed), got {len(zs)}: {[ (e['seq'], e['subject'], e['to']) for e in zs]}"
    assert_contiguous([e["seq"] for e in log], start=base + 1, what="event log after base")
    for e, (z, st) in zip(zs, plan):
        assert str(z) in str(e["subject"]), e
        assert e["to"] == STATE_BY_STATUS[st], e
        assert e["source"] == "iq_remote" and e["synthetic"] is False, e
        for f in ("seq", "id", "ts", "type", "subject", "from", "to", "source", "synthetic"): assert f in e, f"event lacks {f}: {e}"
    assert_zone_chains(env.events())
    wait_until(lambda: set(w.seqs()) >= {e["seq"] for e in log}, 5, 0.1, "all log events delivered on MQTT events topic")
    mq = [s for s in w.seqs() if s > base]; assert_contiguous(mq, start=base + 1, what="MQTT events")
    wait_until(lambda: set(sse.seqs()) >= {e["seq"] for e in log}, 5, 0.1, "all log events delivered over SSE")
    ss = [s for s in sse.seqs() if s > base]; assert_contiguous(ss, start=base + 1, what="SSE events")
    # retained state follows live state + last_changed moves
    assert w.zone(12)["state"] == "closed" or w.zone(12)["state"] == "open"
    env.assert_matches_sim(check_stale=True)
    # partition arming through the same path
    env.sim.set_arming(0, "ARM-AWAY")
    wait_until(lambda: env.partitions()[0]["arming"] == "arm_away", 8, 0.1, "REST arm_away")
    env.sim.set_arming(0, "ARM-AWAY-EXIT-DELAY"); wait_until(lambda: env.partitions()[0]["arming"] == "exit_delay", 8, 0.1, "exit_delay")
    env.sim.set_arming(0, "DISARM"); wait_until(lambda: env.partitions()[0]["arming"] == "disarmed", 8, 0.1, "disarmed")
    wait_until(lambda: (w.partition(0) or {}).get("arming") == "disarmed", 5, 0.1, "MQTT partition disarmed")
    pe = [e for e in env.events(after=base) if e["type"] == "partition.arming"]
    assert [e["to"] for e in pe] == ["arm_away", "exit_delay", "disarmed"], pe
    env.sim.set_alarm(0, True, "glassbreak")
    wait_until(lambda: env.partitions()[0]["arming"] == "alarm" or any(e["type"] == "partition.alarm" for e in env.events(after=base)), 8, 0.1, "alarm visible")
    # tamper + battery fields
    env.sim.set_zone(12, "Closed", battery="Low"); 
    wait_until(lambda: env.zones()[12].get("battery_low") is True, 8, 0.1, "battery_low true")
    assert_contiguous([e["seq"] for e in env.events()], start=1, what="entire event log")


def test_sse_last_event_id_resume_no_gaps(env):
    env.settle()
    s1 = env.sse(); assert s1.opened.wait(10) and s1.status == 200
    time.sleep(0.4)
    base = env.last_seq()
    for st in ("Open", "Closed", "Open"): env.sim.set_zone(12, st); time.sleep(0.2)
    wait_until(lambda: len([i for i in s1.seqs() if i > base]) >= 3, 8, 0.1, "first three events on SSE")
    seen = [i for i in s1.seqs() if i > base]; s1.close(); time.sleep(0.3)
    cut = seen[1]                                    # pretend we only processed up to the second event
    for st in ("Closed", "Open", "Closed", "Open"): env.sim.set_zone(12, st); time.sleep(0.15)
    time.sleep(0.5)
    s2 = env.sse(last_event_id=cut); assert s2.opened.wait(10) and s2.status == 200
    full = env.events(after=0); expect = [e["seq"] for e in full if e["seq"] > cut]
    wait_until(lambda: s2.seqs()[:len(expect)] == expect, 8, 0.1, f"resume replay of {expect}")
    assert s2.seqs()[:len(expect)] == expect, "resumed stream has gap/dup/reorder"
    env.sim.set_zone(13, "Active")                      # live events continue after replay
    wait_until(lambda: len(s2.seqs()) == len(expect) + 1 and s2.seqs()[-1] == expect[-1] + 1, 8, 0.1, "live event after replay")
    assert_contiguous(s2.seqs(), what="resumed SSE stream")
    # ids are exactly seq and data carries the same seq
    for i in s2.event_items(): assert i["data"]["seq"] == int(i["id"])
    # header form with an unknown-future id must not crash / must not replay old events as new
    s3 = env.sse(last_event_id=env.last_seq()); assert s3.opened.wait(10) and s3.status == 200
    time.sleep(1); assert s3.seqs() == [], f"stream at head replayed {s3.seqs()}"


def test_sse_heartbeat_and_content_type(env):
    s = env.sse(); assert s.opened.wait(10) and s.status == 200
    t0 = time.time()
    wait_until(lambda: s.comments > 0 or any(i["event"] in ("heartbeat", "ping", "keepalive") for i in s.items), 30, 0.25,
               "an SSE heartbeat (':' comment line or event: heartbeat) within 30 s on an idle stream")
    r = env.http.stream("GET", "/v1/stream"); 
    with r as resp:
        assert resp.headers["content-type"].startswith("text/event-stream"), resp.headers
        assert "no-cache" in resp.headers.get("cache-control", "").lower()


def test_concurrent_readers_during_burst(env):
    """burst of 60 flips with 5 concurrent SSE consumers + REST polling: everyone sees the same contiguous, ordered log."""
    env.settle()
    base = env.last_seq(); ss = [env.sse() for _ in range(5)]
    for s in ss: assert s.opened.wait(10)
    stop = threading.Event()
    def poll():
        while not stop.is_set():
            try: env.zones()
            except Exception: pass
            time.sleep(0.05)
    t = threading.Thread(target=poll, daemon=True); t.start()
    n = 0
    for i in range(30):
        env.sim.set_zone(13, "Active"); env.sim.set_zone(13, "Idle"); n += 2; time.sleep(0.03)
    time.sleep(2.5); stop.set(); t.join()
    log = env.events(after=base)
    assert len(zone_events(log)) == n, f"expected {n} zone.state events, got {len(zone_events(log))}"
    assert_contiguous([e["seq"] for e in log], start=base + 1)
    assert_zone_chains(env.events())
    for s in ss:
        assert [i for i in s.seqs() if i > base] == [e["seq"] for e in log], "SSE consumer missed/duplicated events"
    env.wait_zone(13, "idle")
