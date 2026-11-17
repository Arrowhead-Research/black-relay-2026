"""F2-specific black-box-ish proofs over the live multi-process system."""
import os, re, signal, socket, time, pathlib
import pytest
from conformance.helpers import *  # noqa


def svc_pids(env):
    out = {}
    for pid in env.bridge.procs():
        try:
            cmd = open(f"/proc/{pid}/cmdline").read().split("\0")
        except OSError:
            continue
        if "service" in cmd:
            out[cmd[cmd.index("service") + 1]] = pid
        elif "run" in cmd and "br2" in cmd:
            out["supervisor"] = pid
    return out


def inet_peers(pid):
    """remote (ip,port) of every TCP socket held by pid, plus listening ports."""
    inodes = set()
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            l = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        m = re.match(r"socket:\[(\d+)\]", l)
        if m:
            inodes.add(m.group(1))
    remotes, listens = set(), set()
    for fam in ("tcp", "tcp6"):
        try:
            rows = open(f"/proc/{pid}/net/{fam}").read().splitlines()[1:]
        except OSError:
            continue
        for r in rows:
            f = r.split()
            if f[9] not in inodes:
                continue
            rport = int(f[2].split(":")[1], 16); lport = int(f[1].split(":")[1], 16)
            if f[3] == "0A":
                listens.add(lport)
            else:
                remotes.add(rport)
    return remotes, listens


def ipc_dir(env, pids):
    for line in open(f"/proc/{pids['normalizer']}/environ").read().split("\0"):
        if line.startswith("BR2_IPC_DIR="):
            return line.split("=", 1)[1]
    raise AssertionError("no BR2_IPC_DIR")


def test_four_separate_os_processes_and_panel_sockets_only_in_adapters(env):
    p = svc_pids(env)
    assert {"normalizer", "outbound", "iq-remote", "control4", "supervisor"} <= set(p), p
    assert len(set(p.values())) == 5
    iq, c4 = env.sim.iq.port, env.sim.c4.port
    r, l = inet_peers(p["normalizer"])
    assert not r and not l, "normalizer must have no IP sockets at all"
    r, l = inet_peers(p["outbound"])
    assert iq not in r and c4 not in r, "outbound must never hold a panel connection"
    assert env.broker.port in r and env.api_port in l and env.metrics_port in l
    r, _ = inet_peers(p["iq-remote"]); assert iq in r and c4 not in r
    r, l = inet_peers(p["control4"]); assert c4 not in r and iq not in r and not l   # cold standby: no panel socket while primary is healthy
    env.sim.iq.outage()
    wait_until(lambda: c4 in inet_peers(p["control4"])[0], 20, 0.1, "control4 adapter opens its panel socket only after failover threshold")
    r, _ = inet_peers(p["iq-remote"]); assert c4 not in r
    r, _ = inet_peers(p["outbound"]); assert c4 not in r and iq not in r
    r, l = inet_peers(p["normalizer"]); assert not r and not l
    env.sim.iq.restore()


def test_ipc_sockets_are_half_duplex_on_the_live_system(env):
    p = svc_pids(env); d = ipc_dir(env, p)
    # iq_remote.sock: a rogue client can send frames but the normalizer write half is shut -> it reads EOF at once
    s = socket.socket(socket.AF_UNIX); s.connect(os.path.join(d, "iq_remote.sock")); s.settimeout(2)
    assert s.recv(10) == b"", "normalizer wrote back on the adapter socket"
    s.close()
    # out.sock: a rogue client receives the public dump but can never write toward the normalizer
    s = socket.socket(socket.AF_UNIX); s.connect(os.path.join(d, "out.sock")); s.settimeout(2)
    assert s.recv(4)  # dump header
    s.shutdown(socket.SHUT_RD)  # even a well-behaved receiver closes read side; writing must fail regardless
    time.sleep(0.2)
    with pytest.raises(OSError):
        for _ in range(50):
            s.send(b"x" * 1000); time.sleep(0.01)
    s.close()
    assert env.health()["status"] == "online"   # the system shrugged off the probing


def test_normalizer_crash_outbound_flags_stale_then_resync_with_synthetic_diff(env):
    env.settle()
    p = svc_pids(env)
    seq0 = env.last_seq()
    os.kill(p["normalizer"], signal.SIGKILL)
    env.sim.set_zone(12, "Open")                      # happens while the normalizer is dead
    # outbound notices instantly: status offline + stale (before supervisor restarts the normalizer)
    wait_until(lambda: env.health()["status"] != "online" or env.zones()[12]["stale"], 5, 0.05, "outbound sees normalizer loss")
    wait_until(lambda: env.health()["status"] == "online", 40, 0.25, "recovery after normalizer restart")
    env.wait_zone(12, "open", 15)
    ev = [e for e in env.events(after=seq0) if e["type"] == "zone.state" and e["subject"] == "zone-12"]
    assert ev and ev[-1]["to"] == "open" and ev[-1]["synthetic"] is True, ev
    assert_contiguous([e["seq"] for e in env.events()], start=1)
    assert not any(z["stale"] for z in env.zones().values())


def test_outbound_crash_mqtt_replays_missed_events_gap_free(env):
    env.settle()
    w = env.watcher(); time.sleep(0.5)
    base = env.last_seq()
    p = svc_pids(env)
    os.kill(p["outbound"], signal.SIGKILL)
    for st in ("Open", "Closed", "Open", "Closed"):
        env.sim.set_zone(12, st); time.sleep(0.1)
    wait_until(lambda: env.http.get("/v1/health", headers={}).status_code == 200, 40, 0.25, "outbound back")
    log = wait_until(lambda: (lambda l: l if len([e for e in l if e["type"] == "zone.state"]) >= 4 else None)(env.events(after=base)), 20, 0.25, "events logged")
    wait_until(lambda: set(w.seqs()) >= {e["seq"] for e in log}, 20, 0.25, "MQTT replay of events missed during outbound outage")
    assert_contiguous([s for s in w.seqs() if s > base], start=base + 1, what="MQTT after outbound crash")


def test_iq_adapter_crash_is_isolated_and_recovers(env):
    p = svc_pids(env)
    os.kill(p["iq-remote"], signal.SIGKILL)
    wait_until(lambda: env.health()["links"].get("iq_remote") in ("down", "disabled", False) or env.health()["active_source"] != "iq_remote", 10, 0.1, "link down seen")
    # normalizer + outbound + control4 untouched
    p2 = svc_pids(env)
    for k in ("normalizer", "outbound", "control4"):
        assert p2[k] == p[k]
    env.wait_online(30)
    env.assert_matches_sim(check_stale=True)


def test_fallback_is_cold_while_primary_healthy_and_contacted_only_after_threshold(env):
    """Control4 panel must see NOTHING while the primary is healthy; first contact only after fallback_after_s of outage."""
    env.settle(2.0)
    time.sleep(TIMERS["silence_probe_s"] + 2)                      # longer than a probe interval
    assert env.sim.c4.accepted == 0 and env.sim.audit.count(side="c4") == 0, "control4 contacted while primary healthy"
    t0 = time.time(); env.sim.iq.outage()
    wait_until(lambda: env.sim.c4.accepted > 0, 20, 0.05, "control4 first contact")
    assert time.time() - t0 >= TIMERS["fallback_after_s"] - 1.0, "fallback contacted before fallback_after_s"
    env.wait_health(30, "degraded on control4", status="degraded", active_source="control4")
    env.sim.iq.restore(); env.wait_online(30)
    n = env.sim.c4.accepted
    wait_until(lambda: not env.sim.c4.conns, 15, 0.25, "control4 session released after primary healthy again")
