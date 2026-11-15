"""Fixtures: per-test fresh simulator + broker + bridge process.  The bridge under test is `$BR_IMPL/harness_start.sh <cfg>`."""
from __future__ import annotations
import os, shutil, signal, sys, threading, time
from pathlib import Path
import httpx, pytest, yaml
from argon2 import PasswordHasher

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))
from lib.certs import Pki
from lib.mosq import Broker, free_port
from lib import procs as registry
from sim.panel_sim import PanelSim
from conformance.helpers import *  # noqa

IMPL = Path(os.environ.get("BR_IMPL", "")).resolve() if os.environ.get("BR_IMPL") else None
RUN_DIR = Path(os.environ.get("BR_WORK", HARNESS / "work" / time.strftime("run-%Y%m%d-%H%M%S")))
SESSION_VIOLATIONS: list = []
SESSION_STATE = {"tests": 0, "static_done": False}
TEST_TIMEOUT_S = int(os.environ.get("HARNESS_TEST_TIMEOUT", "240"))

# every process this pytest process starts is reaped at exit, on SIGTERM/SIGINT, and by each fixture's finalizer
registry.install_exit_handlers()


def _teardown(e):
    try:
        e.close()
    finally:
        registry.kill_all()   # backstop: nothing a test started may outlive it


@pytest.fixture(autouse=True)
def _per_test_watchdog(request):
    """SIGALRM watchdog (no pytest-timeout dependency): a hung test, sim or bridge fails after HARNESS_TEST_TIMEOUT
    seconds instead of hanging the run. The alarm is a pytest failure, so the fixture finalizers still run and reap.
    The first test of a run also gets BR_FIRST_START_S extra, because the first bridge start may build the implementation."""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return
    budget = TEST_TIMEOUT_S + (int(os.environ.get("BR_FIRST_START_S", 600)) if not SESSION_STATE["tests"] else 0)

    def _alarm(signum, frame):
        pytest.fail(f"per-test timeout: still running after {budget}s (HARNESS_TEST_TIMEOUT={TEST_TIMEOUT_S})", pytrace=False)

    old = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, budget)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        registry.kill_all()


# ------------------------------------------------------------------ pretty PASS/FAIL
def pytest_report_teststatus(report, config):
    if report.when == "call" or (report.when in ("setup", "teardown") and report.failed) or (report.when == "setup" and report.skipped):
        if report.passed: return "passed", ".", "PASS"
        if report.failed: return "failed", "F", "FAIL"
        if report.skipped: return "skipped", "s", "SKIP"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when == "call" and rep.passed:
        env = item.funcargs.get("env")
        if env is not None and env.sim.violations:
            rep.outcome = "failed"
            rep.longrepr = "ONE-WAY VIOLATION: the bridge sent state-changing/forbidden messages to the panel:\n" + \
                "\n".join(f"  [{v['side']}] {v['reason']}  raw={v['raw'][:200]!r}" for v in env.sim.violations)


@pytest.fixture(scope="session")
def pki():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    return Pki.create(RUN_DIR / "pki-root")


@pytest.fixture(scope="session")
def api_key_hash():
    return PasswordHasher().hash(API_KEY)


def pytest_sessionstart(session):
    if IMPL is None:
        print("NOTE: BR_IMPL not set; tests needing a bridge will be skipped (run via harness/run.sh <impl_dir>)")


class Env:
    def __init__(self, request, pki, api_key_hash, name, **opt):
        self.pki, self.name = pki, name
        self.work = RUN_DIR / name; shutil.rmtree(self.work, ignore_errors=True); self.work.mkdir(parents=True)
        self.opt = opt
        self.sim = PanelSim(self.work / "sim", pki)
        self.broker = Broker(self.work, pki)
        self.api_port, self.metrics_port = free_port(), free_port()
        self.api_url = f"https://127.0.0.1:{self.api_port}"
        self.key_hash = api_key_hash
        self.bridge: Bridge | None = None
        self.watchers, self.sses = [], []
        self.http = httpx.Client(base_url=self.api_url, verify=str(pki.root / "harness-ca.crt"), timeout=10,
                                 headers={"Authorization": f"Bearer {API_KEY}"})
        self.data_dir = self.work / "data"; self.data_dir.mkdir()
        self.cfg_path = self.work / "config.yaml"
        self.cfg = self.build_config()

    # -------------------------------------------------------------- config
    def build_config(self, **over):
        pki = self.pki
        cfg = {
            "iq_remote": {"enabled": True, "panel_ip": "127.0.0.1", "panel_port": self.sim.iq.port, "plugin_ip": "127.0.0.1",
                          "pki_dir": str(pki.root / "pki"), "ca_file": str(pki.root / "sim-ca.crt")},
            "control4": {"enabled": True, "host": "127.0.0.1", "port": self.sim.c4.port,
                         "token_file": str(self.sim.c4.token_file), "cert_sha256": self.sim.c4.fingerprint},
            "timers": dict(TIMERS),
            "zones": {k: dict(v) for k, v in ZONE_OVERRIDES.items()},
            "mqtt": {"host": "127.0.0.1", "port": self.broker.port, "tls": True, "ca_file": str(pki.root / "harness-ca.crt"),
                     "username": "blackrelay", "password_file": str(self.broker.bridge_password_file),
                     "topic_prefix": PREFIX, "client_id": "blackrelay-bridge", "ha_discovery": True,
                     "ha_discovery_prefix": "homeassistant"},
            "api": {"host": "127.0.0.1", "port": self.api_port, "tls_cert": str(pki.root / "api.crt"),
                    "tls_key": str(pki.root / "api.key"),
                    "keys": [{"name": "harness", "argon2_hash": self.key_hash}],
                    "rate_limit_per_minute": 100000, "metrics_port": self.metrics_port},
            "storage": {"data_dir": str(self.data_dir), "retention_days": 30},
            "log_level": "DEBUG",
        }
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict): cfg[k].update(v)
            else: cfg[k] = v
        return cfg

    def write_config(self, **over):
        if over: self.cfg = self.build_config(**over)
        self.cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))

    # -------------------------------------------------------------- lifecycle
    def start_all(self, first_timeout=600):
        self.sim.start(); self.broker.start(); self.start_bridge(timeout=first_timeout)

    def start_bridge(self, timeout=60):
        self.write_config()
        self.bridge = Bridge(IMPL, self.cfg_path, self.work / f"bridge-{int(time.time()*1000)}.log")
        self.bridge.start()
        def ready():
            assert self.bridge.alive(), f"bridge exited early (rc={self.bridge.p.returncode}); see {self.bridge.log}"
            return self.http.get("/v1/health", headers={}).status_code == 200
        wait_until(ready, timeout, 0.25, "bridge /v1/health to answer 200")

    def stop_bridge(self, sig=None):
        import signal as _s
        if self.bridge: self.bridge.stop(sig or _s.SIGTERM)

    def restart_bridge(self, kill9=False, **over):
        if kill9: self.bridge.kill9()
        else: self.stop_bridge()
        if over: self.cfg = self.build_config(**over)
        self.start_bridge()

    def close(self):
        """Every step runs even if an earlier one raised, so a broken consumer cannot leave the broker or bridge running."""
        def step(fn):
            try: fn()
            except Exception as ex: sys.stderr.write(f"harness teardown step {getattr(fn, '__qualname__', fn)} failed: {ex!r}\n")
        for s in self.sses: step(s.close)
        for w in self.watchers: step(w.close)
        step(self.http.close)
        if self.bridge: step(self.bridge.stop)
        step(self.sim.close); step(self.broker.stop)
        SESSION_VIOLATIONS.extend(self.sim.violations)

    # -------------------------------------------------------------- API helpers
    def health(self): 
        r = self.http.get("/v1/health", headers={}); assert r.status_code == 200, r.text; return r.json()

    def get(self, path, **kw):
        r = self.http.get(path, **kw); return r

    def zones(self, **params):
        r = self.http.get("/v1/zones", params=params); assert r.status_code == 200, f"/v1/zones -> {r.status_code} {r.text[:200]}"
        return {zid(z): z for z in listify(r.json(), "zones")}

    def partitions(self):
        r = self.http.get("/v1/partitions"); assert r.status_code == 200, r.text
        return {pid_of(p): p for p in listify(r.json(), "partitions")}

    def events(self, after=0, limit=1000):
        out = []
        while True:
            r = self.http.get("/v1/events", params={"after": after, "limit": limit}); assert r.status_code == 200, r.text
            page = listify(r.json(), "events")
            if not page: break
            out += page; after = max(e["seq"] for e in page)
            if len(page) < limit: break
        return sorted(out, key=lambda e: e["seq"])

    def last_seq(self): return self.health().get("last_seq") or 0

    def wait_online(self, timeout=30, source="iq_remote"):
        def ok():
            h = self.health(); return h.get("status") == "online" and h.get("active_source") == source and h
        return wait_until(ok, timeout, 0.2, f"health status=online active_source={source}")

    def wait_health(self, timeout, msg, **expect):
        def ok():
            h = self.health()
            for k, v in expect.items():
                if callable(v):
                    if not v(h.get(k)): return False
                elif h.get(k) != v: return False
            return h
        return wait_until(ok, timeout, 0.2, msg or str(expect))

    def settle(self, quiet=1.5, timeout=20):
        """wait until last_seq stops changing"""
        end = time.time() + timeout; last = self.last_seq(); t0 = time.time()
        while time.time() < end:
            time.sleep(0.25); cur = self.last_seq()
            if cur != last: last, t0 = cur, time.time()
            elif time.time() - t0 >= quiet: return last
        return last

    def watcher(self, **kw):
        w = MqttWatcher(self, **kw); self.watchers.append(w); return w

    def sse(self, **kw):
        s = SSE(self, **kw); self.sses.append(s); return s

    def wait_zone(self, n, state, timeout=8):
        return wait_until(lambda: self.zones().get(n, {}).get("state") == state, timeout, 0.1, f"REST zone {n} state={state}")

    # expected contract view of the simulator
    def expected(self):
        out = {}
        for zn, z in self.sim.state.clone_zones().items():
            ov = ZONE_OVERRIDES.get(zn, {})
            if ov.get("ignore"): continue
            e = self.sim.state.expected_zone(z)
            if "name" in ov: e["name"] = ov["name"]
            if "kind" in ov: e["kind"] = ov["kind"]
            out[zn] = e
        return out

    def assert_matches_sim(self, check_stale=False):
        exp, got = self.expected(), self.zones()
        assert set(got) == set(exp), f"zone set mismatch: REST={sorted(got)} sim={sorted(exp)}"
        for zn, e in exp.items():
            g = got[zn]
            for k in ("kind", "state", "name"):
                assert g.get(k) == e[k], f"zone {zn} {k}: REST={g.get(k)!r} expected={e[k]!r}"
            if check_stale: assert g.get("stale") is False, f"zone {zn} stale={g.get('stale')!r}"


def _mk_env(request, pki, api_key_hash, **opt):
    if IMPL is None: pytest.skip("BR_IMPL not set")
    if not (IMPL / "harness_start.sh").exists(): pytest.fail(f"{IMPL}/harness_start.sh missing (see harness/CONVENTION.md)")
    name = request.node.name.replace("[", "_").replace("]", "")
    e = Env(request, pki, api_key_hash, name, **opt)
    return e


@pytest.fixture()
def env(request, pki, api_key_hash):
    e = _mk_env(request, pki, api_key_hash)
    try:
        e.start_all(first_timeout=int(os.environ.get("BR_FIRST_START_S", 600)) if not SESSION_STATE["tests"] else 60)
        SESSION_STATE["tests"] += 1
        e.wait_online(30)
        yield e
    finally:
        _teardown(e)


@pytest.fixture()
def env_nostart(request, pki, api_key_hash):
    """sim + broker started, bridge NOT started (the test configures/starts it)."""
    e = _mk_env(request, pki, api_key_hash)
    try:
        e.sim.start(); e.broker.start()
        yield e
    finally:
        _teardown(e)


@pytest.fixture()
def broker_only(request, pki, tmp_path_factory):
    class B: pass
    b = B(); b.pki = pki
    b.work = RUN_DIR / request.node.name; b.work.mkdir(parents=True, exist_ok=True)
    b.broker = Broker(b.work, pki)
    try:
        b.broker.start()
        yield b
    finally:
        try: b.broker.stop()
        finally: registry.kill_all()
