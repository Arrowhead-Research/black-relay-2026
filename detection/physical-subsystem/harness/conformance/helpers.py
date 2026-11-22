from __future__ import annotations
import json, os, signal, ssl, subprocess, threading, time
from pathlib import Path
import httpx, yaml
import paho.mqtt.client as mqtt
from sim.protocol import STATE_BY_STATUS, ARMING_IQ_TO_CONTRACT
from lib import procs as registry

API_KEY = "br_test_key_0123456789"
PREFIX = "blackrelay/v1"
ZONE_OVERRIDES = {12: {"name": "Front door", "kind": "contact"}, 13: {"name": "Garage motion", "kind": "motion"},
                  40: {"ignore": True}}
TIMERS = dict(silence_probe_s=4, probe_timeout_s=2, backoff_initial_s=1, backoff_max_s=2, fallback_after_s=3)


def wait_until(fn, timeout=10.0, interval=0.1, msg="condition"):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            r = fn()
            if r: return r
        except Exception as e:  # noqa
            last = e
        time.sleep(interval)
    raise AssertionError(f"timeout after {timeout}s waiting for: {msg}" + (f" (last error: {last!r})" if last else ""))


def listify(body, *keys):
    if isinstance(body, list): return body
    if isinstance(body, dict):
        for k in keys + ("items", "data", "results"):
            if isinstance(body.get(k), list): return body[k]
    raise AssertionError(f"cannot find a list in response body: {str(body)[:200]}")


def zid(z) -> int:
    """panel zone number of a normalized zone record"""
    v = z.get("panel_zone_id", z.get("id"))
    return int(str(v).replace("zone-", ""))


def pid_of(p) -> int:
    v = p.get("id", p.get("panel_partition_id"))
    return int(str(v).replace("partition-", ""))


def assert_contiguous(seqs, start=None, what="events"):
    seqs = list(seqs)
    assert seqs == sorted(seqs), f"{what}: seq not ordered {seqs[:30]}"
    assert len(set(seqs)) == len(seqs), f"{what}: duplicate seq {sorted(s for s in set(seqs) if seqs.count(s) > 1)}"
    if seqs:
        if start is not None: assert seqs[0] == start, f"{what}: first seq {seqs[0]} != {start}"
        for a, b in zip(seqs, seqs[1:]):
            assert b == a + 1, f"{what}: gap between seq {a} and {b}"


def assert_zone_chains(events):
    """per-zone consistency: each zone.state event's `from` equals the previous event's `to` (no lost/duplicated transition)."""
    last = {}
    for e in sorted(events, key=lambda e: e["seq"]):
        if e.get("type") in ("zone.removed", "zone.added"):
            last.pop(e.get("subject"), None); 
            if e["type"] == "zone.added": last[e["subject"]] = e.get("to") if not isinstance(e.get("to"), dict) else e["to"].get("state")
            continue
        if e.get("type") != "zone.state": continue
        s = e["subject"]
        if s in last and last[s] is not None:
            assert e.get("from") == last[s], f"zone chain broken for {s}: event seq {e['seq']} from={e.get('from')!r} but previous to={last[s]!r}"
        last[s] = e.get("to")


class Bridge:
    def __init__(self, impl: Path, cfg_path: Path, log: Path):
        self.impl, self.cfg_path, self.log = impl, cfg_path, log
        self.p: subprocess.Popen | None = None

    def start(self):
        env = dict(os.environ, BR_HARNESS="1")
        with open(self.log, "ab") as out:
            # registered with lib.procs: own session, tracked for kill_all() on every exit path
            self.p = registry.spawn([str(self.impl / "harness_start.sh"), str(self.cfg_path)], cwd=self.impl, env=env,
                                    stdout=out, stderr=subprocess.STDOUT)

    def alive(self): return self.p is not None and self.p.poll() is None

    def stop(self, sig=signal.SIGTERM, grace=10):
        """signal the bridge's group, wait `grace`, SIGKILL whatever is left in the group (stragglers included)"""
        if self.p is not None: registry.stop(self.p, sig, grace)

    def kill9(self): self.stop(signal.SIGKILL, 5)

    def procs(self):
        out = []
        if not self.p: return out
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    if os.getpgid(int(d.name)) == self.p.pid: out.append(int(d.name))
                except (ProcessLookupError, PermissionError): pass
        return out

    def rss_fds(self):
        rss = fds = 0
        for pid in self.procs():
            try:
                for ln in open(f"/proc/{pid}/status"):
                    if ln.startswith("VmRSS:"): rss += int(ln.split()[1]) * 1024
                fds += len(os.listdir(f"/proc/{pid}/fd"))
            except (FileNotFoundError, ProcessLookupError, PermissionError): pass
        return rss, fds


class MqttWatcher:
    """Independent consumer (read-only user) of the bridge's broker."""
    def __init__(self, env, user="consumer", password="consumer-pw", topics=(PREFIX + "/#",), client_id=None, qos=1):
        self.lock = threading.Lock()
        self.retained: dict[str, dict] = {}     # topic -> payload for retained-flag messages (latest)
        self.latest: dict[str, dict] = {}       # topic -> latest payload any kind
        self.events: list[dict] = []
        self.raw: list[tuple[str, bytes, bool]] = []
        self.status_history: list[str] = []
        self.connected = threading.Event(); self.suback = threading.Event()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id or f"watch-{id(self)}")
        self.c.username_pw_set(user, password)
        self.c.tls_set(ca_certs=str(env.pki.root / "harness-ca.crt"))
        self.topics, self.qos = topics, qos
        self.c.on_connect = lambda c, u, f, rc, p=None: (c.subscribe([(t, qos) for t in topics]), self.connected.set())
        self.c.on_subscribe = lambda *a, **k: self.suback.set()
        self.c.on_message = self._msg
        self.c.connect("127.0.0.1", env.broker.port, 30); self.c.loop_start()
        assert self.connected.wait(10), "watcher could not connect to harness broker"
        self.suback.wait(5)

    def _msg(self, c, u, m):
        with self.lock:
            self.raw.append((m.topic, bytes(m.payload), m.retain))
            try: j = json.loads(m.payload) if m.payload else None
            except Exception: j = {"_unparsable": m.payload[:100].decode("latin1")}
            if m.topic.endswith("/events") and isinstance(j, dict): self.events.append(j)
            if m.topic.endswith("bridge/status"):
                self.status_history.append(j.get("status") if isinstance(j, dict) else (m.payload.decode() if m.payload else ""))
            if m.retain: self.retained[m.topic] = j
            self.latest[m.topic] = j

    def zone(self, n): 
        with self.lock: return self.latest.get(f"{PREFIX}/zones/zone-{n}/state")

    def partition(self, n):
        with self.lock: return self.latest.get(f"{PREFIX}/partitions/{n}/state") or self.latest.get(f"{PREFIX}/partitions/partition-{n}/state")

    @property
    def status(self):
        with self.lock: return self.status_history[-1] if self.status_history else None

    def seqs(self):
        with self.lock: return [e["seq"] for e in self.events if "seq" in e]

    def close(self): self.c.loop_stop(); self.c.disconnect()


class SSE:
    """Minimal SSE client in a thread. .items = [{'id','event','data'(parsed json or str)}], .comments, .lines"""
    def __init__(self, env, last_event_id=None, key=API_KEY, path="/v1/stream", header="Authorization"):
        self.items, self.comments, self.lines = [], 0, 0
        self.stop = threading.Event(); self.status = None; self.err = None; self.opened = threading.Event()
        h = {"Accept": "text/event-stream"}
        h["Authorization" if header == "Authorization" else "X-API-Key"] = f"Bearer {key}" if header == "Authorization" else key
        if last_event_id is not None: h["Last-Event-ID"] = str(last_event_id)
        self.t = threading.Thread(target=self._run, args=(env.api_url + path, h, env.pki.root / "harness-ca.crt"), daemon=True)
        self.t.start()

    def _run(self, url, headers, ca):
        try:
            with httpx.Client(verify=str(ca), timeout=httpx.Timeout(10, read=None)) as c:
                self.client = c
                with c.stream("GET", url, headers=headers) as r:
                    self.resp = r
                    self.status = r.status_code; self.opened.set()
                    cur = {}
                    for line in r.iter_lines():
                        if self.stop.is_set(): break
                        self.lines += 1
                        if line == "":
                            if cur.get("data") is not None or "id" in cur: self._emit(cur)
                            cur = {}; continue
                        if line.startswith(":"): self.comments += 1; continue
                        k, _, v = line.partition(":"); v = v[1:] if v.startswith(" ") else v
                        cur[k] = (cur.get(k, "") + "\n" + v) if k == "data" and "data" in cur else v
        except Exception as e:  # noqa
            self.err = e
        finally:
            self.opened.set()

    def _emit(self, cur):
        d = cur.get("data")
        try: d = json.loads(d)
        except Exception: pass
        self.items.append({"id": cur.get("id"), "event": cur.get("event"), "data": d})

    def event_items(self): return [i for i in self.items if i["id"] not in (None, "")]
    def seqs(self): return [int(i["id"]) for i in self.event_items()]
    def close(self):
        self.stop.set()
        for o in (getattr(self, "resp", None), getattr(self, "client", None)):
            try:
                if o is not None: o.close()
            except Exception: pass
