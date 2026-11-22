import datetime, gc, glob, os, signal, sqlite3, time, threading
from pathlib import Path
import httpx, pytest
from conformance.helpers import *


def test_sqlite_log_persists_across_restart_seq_continues(env):
    env.settle()
    for st in ("Open", "Closed", "Open", "Closed"): env.sim.set_zone(12, st); time.sleep(0.2)
    env.sim.set_zone(13, "Active"); time.sleep(1.0)
    before = env.events(); last = before[-1]["seq"]
    assert last >= 5 and before[0]["seq"] == 1
    dbs = [p for p in Path(env.data_dir).rglob("*") if p.suffix in (".db", ".sqlite", ".sqlite3")]
    assert dbs, f"no SQLite file found under storage.data_dir {env.data_dir}: {list(Path(env.data_dir).rglob('*'))}"
    env.restart_bridge()
    env.wait_online(40); env.settle()
    after = env.events()
    assert [e["seq"] for e in after[:len(before)]] == [e["seq"] for e in before], "event log prefix changed across restart"
    assert [e["id"] for e in after[:len(before)]] == [e["id"] for e in before]
    assert all(a == b for a, b in zip(after, before)), "persisted events differ after restart"
    assert_contiguous([e["seq"] for e in after], start=1, what="log after restart")
    assert env.health()["last_seq"] >= last
    s0 = env.last_seq(); env.sim.set_zone(14, "Active")
    wait_until(lambda: env.last_seq() > s0, 8, 0.1, "new event after restart")
    new = env.events(after=s0)
    assert new[0]["seq"] == s0 + 1
    assert_contiguous([e["seq"] for e in env.events()], start=1)
    env.assert_matches_sim(check_stale=True)


def test_restart_midstream_sse_resume_and_kill9_durability(env):
    """Bridge SIGKILLed while consumers are connected: every seq a consumer ever saw is in the log after restart,
    resume via Last-Event-ID is gap-free, no duplicates, seq continues."""
    env.settle(); w = env.watcher(); s = env.sse(); assert s.opened.wait(10)
    base = env.last_seq(); stop = threading.Event()
    def churn():
        i = 0
        while not stop.is_set():
            i += 1; env.sim.set_zone(12, ["Open", "Closed"][i % 2]); time.sleep(0.05)
    t = threading.Thread(target=churn, daemon=True); t.start(); time.sleep(1.5)
    seen_sse = list(s.seqs()); seen_mqtt = w.seqs()
    env.bridge.kill9(); stop.set(); t.join()
    last_seen = max(s.seqs() + [base]); seen_all = set(s.seqs()) | set(w.seqs())
    env.start_bridge(); env.wait_online(40); env.settle()
    log = env.events(); have = {e["seq"] for e in log}
    missing = sorted(x for x in seen_all if x not in have)
    assert not missing, f"events delivered to consumers before SIGKILL are missing from the persisted log: {missing[:10]}"
    assert_contiguous([e["seq"] for e in log], start=1, what="log after kill -9")
    assert_zone_chains(log)
    s2 = env.sse(last_event_id=last_seen); assert s2.opened.wait(10) and s2.status == 200
    expect = [e["seq"] for e in log if e["seq"] > last_seen]
    wait_until(lambda: s2.seqs()[:len(expect)] == expect, 8, 0.1, "resume after crash restart")
    assert s2.seqs()[:len(expect)] == expect
    # old SSE connection died with the bridge: it must have ended, not hung forever on a dead socket
    wait_until(lambda: not s.t.is_alive() or s.err is not None, 10, 0.2, "pre-crash SSE reader terminated")
    env.assert_matches_sim(check_stale=True)
    # graceful restart mid-stream too
    s3 = env.sse(); assert s3.opened.wait(10); env.sim.set_zone(13, "Active"); time.sleep(0.5)
    mark = max(s3.seqs() or [0])
    env.restart_bridge(); env.wait_online(40)
    s4 = env.sse(last_event_id=mark); assert s4.opened.wait(10)
    expect = [e["seq"] for e in env.events() if e["seq"] > mark]
    wait_until(lambda: s4.seqs()[:len(expect)] == expect, 8, 0.1, "resume after graceful restart")


def _find_event_table(db):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    for (name,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'").fetchall():
        cols = [r[1] for r in c.execute(f'pragma table_info("{name}")').fetchall()]
        tcol = next((x for x in cols if x.lower() in ("ts", "timestamp", "time", "created_at", "occurred_at", "at")), None)
        if "seq" in cols and tcol: return c, name, tcol
    c.close(); return None


def _age(val, days):
    """shift a stored timestamp (epoch s/ms/us/ns number or ISO-8601 text) back by `days`"""
    if isinstance(val, (int, float)):
        scale = 1 if val < 1e11 else 1e3 if val < 1e14 else 1e6 if val < 1e17 else 1e9
        return type(val)(val - days * 86400 * scale) if isinstance(val, int) else val - days * 86400 * scale
    s = str(val).replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s) - datetime.timedelta(days=days)
    out = dt.isoformat()
    return out.replace("+00:00", "Z") if str(val).endswith("Z") else out


def test_retention_prune_30_days(env):
    """Age the whole log by 40 days directly in SQLite (bridge stopped), restart: old rows are pruned, seq never resets or reuses."""
    env.settle()
    for st in ("Open", "Closed", "Open"): env.sim.set_zone(12, st); time.sleep(0.2)
    time.sleep(0.8)
    old = env.events(); old_last = old[-1]["seq"]; assert len(old) >= 3
    env.stop_bridge()
    dbs = [p for p in Path(env.data_dir).rglob("*") if p.suffix in (".db", ".sqlite", ".sqlite3")]
    found = None
    for d in dbs:
        found = _find_event_table(d)
        if found: break
    if not found:
        pytest.skip(f"cannot locate an events table with `seq` and a timestamp column in {dbs}; prune must be verified manually")
    c, table, tcol = found
    rows = c.execute(f'select rowid, "{tcol}" from "{table}"').fetchall()
    for rid, v in rows: c.execute(f'update "{table}" set "{tcol}"=? where rowid=?', (_age(v, 40), rid))
    c.commit(); c.close()
    env.start_bridge(); env.wait_online(40); env.settle(3)
    def pruned():
        left = [e for e in env.events() if e["seq"] <= old_last and e["type"] != "bridge.link"]
        return not any(e["seq"] in {o["seq"] for o in old if o["type"] == "zone.state"} for e in left)
    wait_until(pruned, 40, 1.0, "events older than retention_days (30) pruned from /v1/events")
    h = env.health(); assert h["last_seq"] >= old_last, f"last_seq went backwards after prune: {h['last_seq']} < {old_last}"
    s0 = env.last_seq(); env.sim.set_zone(13, "Active"); wait_until(lambda: env.last_seq() > s0, 8, 0.1, "new event")
    new = env.events(after=old_last)
    assert new and new[0]["seq"] > old_last, "seq was reused/reset after prune"
    assert_contiguous([e["seq"] for e in new], what="post-prune events")


def test_sse_open_close_500_streams_no_resource_growth(env):
    """500 open/close SSE cycles (some abruptly dropped mid-read): RSS and fds of the bridge process tree must not grow unbounded."""
    env.settle()
    def sample(): time.sleep(1.0); gc.collect(); return env.bridge.rss_fds()
    # warm-up so lazily-allocated pools don't count as growth
    ca = str(env.pki.root / "harness-ca.crt"); H = {"Authorization": f"Bearer {API_KEY}", "Accept": "text/event-stream"}
    def cycle(n, abrupt_every=3):
        with httpx.Client(verify=ca, timeout=httpx.Timeout(10, read=3)) as c:
            for i in range(n):
                try:
                    with c.stream("GET", env.api_url + "/v1/stream", headers=H) as r:
                        assert r.status_code == 200
                        if i % 7 == 0:       # let some streams actually receive an event before closing
                            env.sim.set_zone(13, ["Active", "Idle"][(i // 7) % 2])
                            try: next(r.iter_lines())
                            except (StopIteration, httpx.ReadTimeout): pass
                except httpx.HTTPError: pass
    cycle(60); rss0, fd0 = sample()
    threads = [threading.Thread(target=cycle, args=(125,)) for _ in range(4)]    # 500 streams, 4 in parallel
    t0 = time.time(); [t.start() for t in threads]; [t.join() for t in threads]
    # 40 streams held open together, then dropped at once
    held = [env.sse() for _ in range(40)]; [h.opened.wait(10) for h in held]; time.sleep(1); [h.close() for h in held]
    # streams whose client vanished may only be noticed at the next heartbeat write: allow up to 30 s to drain
    deadline = time.time() + 30
    while True:
        rss2, fd2 = sample()
        if (fd2 - fd0 <= 25 and (rss2 - rss0) < 40 * 1048576) or time.time() > deadline: break
        time.sleep(2)
    mb = lambda b: b / 1048576
    assert fd2 - fd0 <= 25, f"fd leak: {fd0} -> {fd2} after 500+40 SSE streams (still not drained 30 s after the last close)"
    assert mb(rss2 - rss0) < 40, f"RSS growth after 500+40 SSE streams: {mb(rss0):.1f} -> {mb(rss2):.1f} MiB"
    # server still healthy and not holding the streams: open one more, event flows
    s = env.sse(); assert s.opened.wait(10) and s.status == 200
    env.sim.set_zone(12, "Open"); wait_until(lambda: s.seqs(), 8, 0.1, "event on fresh stream after the stress")
    env.http.get("/v1/health", headers={}).raise_for_status()
