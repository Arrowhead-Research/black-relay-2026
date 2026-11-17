"""Round-2 regressions (audits f2-oneway e1..e4): exact-shape guard, transport-level fence, hostile responses,
library-identical TLS/timing, Control4 framer resync, hygiene + deploy files."""
import asyncio, json, os, pathlib, re, shutil, ssl, subprocess, tempfile, uuid
import pytest, yaml
from br2 import config as CFG
from br2.adapters import iq_remote as IQ, control4 as C4
from br2.adapters.base import OneWayViolation
from lib import certs as CT

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAC = "f2:16:3e:aa:bb:cc"


class Fake:
    def __init__(s): s.pub = []; s.sub = []
    async def publish(s, t, p, qos=0, retain=False, **kw): s.pub.append((t, p, qos, retain))
    async def subscribe(s, t, qos=0, **kw): s.sub.append(t)


def env(ev, **extra):
    return {"requestID": str(uuid.uuid4()), "responseTopic": f"response_{MAC}", "eventName": ev,
            "remoteMacAddress": MAC, **extra}


def adapter():
    a = IQ.IQRemoteAdapter.__new__(IQ.IQRemoteAdapter); a.remote_client_id = "qolsys-controller-aabbccddeeff"
    return a


async def test_guard_exact_key_sets_block_every_audit_e1_bypass():
    f = Fake(); g = IQ.GuardedPanel(f, MAC)
    ping = IQ.IQRemoteAdapter.build_ping(MAC, "10.0.0.2")
    conn = adapter().build_connect(MAC, "10.0.0.2")
    await g.publish("mastermeid", env("pingevent", **ping))               # the real shapes pass
    await g.publish("mastermeid", env("connect_v204", **conn))
    await g.publish("mastermeid", env("pair_status_request")); await g.publish("mastermeid", env("syncdatabase"))
    n0 = len(f.pub); assert n0 == 4
    bad = {
        "ipcCall": env("ipcCall"), "db update": env("database", dbOperation="update"),
        "ping+extra scalar": env("pingevent", **ping, acStatus="Connected"),
        "sync+extra": env("syncdatabase", startTimestamp="1"),
        "ping+nested ipc": env("pingevent", **ping, x={"ipcRequest": [1]}),
        "ping+operation_name": env("pingevent", **ping, operation_name="ui_armaway"),
        "ping missing key": env("pingevent", **{k: v for k, v in ping.items() if k != "ipAddress"}),
        "foreign responseTopic": {**env("syncdatabase"), "responseTopic": "attacker_topic"},
        "foreign mac": {**env("syncdatabase"), "remoteMacAddress": "aa:aa"},
        "bad requestID": {**env("syncdatabase"), "requestID": "../../x"},
        "connect pairing false": env("connect_v204", **{**conn, "pairing_request": False}),
        "list value": env("pingevent", **{**ping, "ipAddress": ["1"]}),
    }
    for name, payload in bad.items():
        with pytest.raises(OneWayViolation, match=r".+"):
            await g.publish("mastermeid", payload)
    with pytest.raises(OneWayViolation): await g.publish("other", env("pingevent", **ping))
    with pytest.raises(OneWayViolation): await g._client.publish("mastermeid", '{"eventName":"ipcCall"}')   # e1 direct bypass
    with pytest.raises(OneWayViolation): await g._client.publish("mastermeid", json.dumps(env("syncdatabase")), retain=True)
    with pytest.raises(OneWayViolation): await g.request("pingevent", {"responseTopic": "attacker_topic", **ping}, 0.1)
    with pytest.raises(OneWayViolation): await g.request("pingevent", {"eventName": "x", **ping}, 0.1)
    assert len(f.pub) == n0                                              # nothing extra reached the wire


async def test_request_builds_envelope_itself_and_publishes_exact_shape():
    f = Fake(); g = IQ.GuardedPanel(f, MAC)
    t = asyncio.create_task(g.request("syncdatabase", None, 5)); await asyncio.sleep(0.05)
    sent = json.loads(f.pub[-1][1]); assert set(sent) == set(IQ.ENVELOPE_KEYS) and sent["responseTopic"] == f"response_{MAC}"
    g.deliver_response({"requestID": sent["requestID"], "fulldbdata": []}); assert (await t)["fulldbdata"] == []


def test_deliver_response_never_raises_on_hostile_payloads():
    g = IQ.GuardedPanel(Fake(), MAC)
    for hostile in ({"requestID": ["x"]}, {"requestID": {"a": 1}}, [1], "s", None, 5, {"requestID": None}, {}):
        g.deliver_response(hostile)


def test_transport_level_fence_wraps_the_paho_client():
    class Paho:
        def __init__(s): s.sent = []
        def publish(s, topic, payload=None, qos=0, retain=False, *a, **k): s.sent.append(topic); return "ok"
        def subscribe(s, topic, *a, **k): s.sent.append("sub:" + str(topic))
    class AC: _client = Paho()
    ac = AC(); g = IQ.GuardedPanel(Fake(), MAC); IQ.harden_paho(ac, g)
    ac._client.publish("mastermeid", json.dumps(env("syncdatabase")), 0, False)
    with pytest.raises(OneWayViolation): ac._client.publish("mastermeid", json.dumps(env("ipcCall")), 0, False)
    with pytest.raises(OneWayViolation): ac._client.publish("mastermeid", json.dumps(env("syncdatabase")), 1, False)
    with pytest.raises(OneWayViolation): ac._client.subscribe("ZWAVE_RESPONSE")
    ac._client.subscribe("iq2meid"); ac._client.subscribe([(f"response_{MAC}", 0)])
    assert ac._client.sent == ["mastermeid", "sub:iq2meid", f"sub:[('response_{MAC}', 0)]"]


# ------------------------------------------------------------------ library fidelity
def lib_reference_ctx(ca, secure, key):
    """Verbatim copy of qolsys_controller.controller.mqtt_open_transport_task's create_tls_context."""
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca)
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.load_cert_chain(certfile=secure, keyfile=key)
    return ctx


def test_iq_tls_context_matches_the_library_exactly(tmp_path):
    ca = CT.make_ca("panelCA", bc_critical=False)
    cc, ck = CT.issue(ca, "client", server=False, client=True)
    (tmp_path / "c.key").write_bytes(CT.key_pem(ck)); (tmp_path / "c.secure").write_bytes(CT.cert_pem(cc))
    (tmp_path / "ca.pem").write_bytes(ca.pem)
    mine = IQ.tls_context(str(tmp_path / "c.key"), str(tmp_path / "c.secure"), str(tmp_path / "ca.pem"))
    ref = lib_reference_ctx(str(tmp_path / "ca.pem"), str(tmp_path / "c.secure"), str(tmp_path / "c.key"))
    assert mine.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN and not mine.verify_flags & ssl.VERIFY_X509_STRICT
    assert (mine.verify_flags, mine.verify_mode, mine.check_hostname, mine.minimum_version) == \
           (ref.verify_flags, ref.verify_mode, ref.check_hostname, ref.minimum_version)
    assert [c["name"] for c in mine.get_ciphers()] == [c["name"] for c in ref.get_ciphers()]
    assert mine.get_ca_certs() == ref.get_ca_certs() and len(mine.get_ca_certs()) == 1      # only the panel CA


async def test_iq_tls_trust_decisions_match_the_library(tmp_path):
    ca, rogue = CT.make_ca("panelCA", bc_critical=False), CT.make_ca("rogue")
    cc, ck = CT.issue(ca, "client", server=False, client=True)
    (tmp_path / "c.key").write_bytes(CT.key_pem(ck)); (tmp_path / "c.secure").write_bytes(CT.cert_pem(cc)); (tmp_path / "ca.pem").write_bytes(ca.pem)
    ctx = IQ.tls_context(str(tmp_path / "c.key"), str(tmp_path / "c.secure"), str(tmp_path / "ca.pem"))
    async def attempt(cert, key):
        cp, kp = tmp_path / f"{id(cert)}.crt", tmp_path / f"{id(cert)}.key"
        cp.write_bytes(CT.cert_pem(cert)); kp.write_bytes(CT.key_pem(key))
        sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sc.load_cert_chain(cp, kp)
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=sc)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1], ssl=ctx); w.close(); return True
        except ssl.SSLError: return False
        finally: srv.close()
    assert await attempt(*CT.issue(ca, "panel", ips=("10.9.9.9",)))          # hostname check off, like the library
    assert not await attempt(*CT.issue(rogue, "panel"))
    assert not await attempt(*CT.self_signed("panel"))


def test_timing_and_identity_constants_match_the_library():
    assert IQ.HANDSHAKE_TIMEOUT_S == 30 and IQ.LIB_PING_PERIOD_S == 300 and IQ.SETTLE_S == 2.0
    src = (ROOT / "src/br2/adapters/iq_remote.py").read_text()
    assert "identifier=self.remote_client_id" in src and '"remoteClientID": remote_client_id' in src \
        and "connect_payload(mac, plugin_ip, self.remote_client_id)" in src
    assert src.count("token_hex(6)") == 1                                    # ONE id per process, like the library
    a = adapter(); assert a.build_connect(MAC, "1.1.1.1")["remoteClientID"] == a.remote_client_id
    assert re.fullmatch(r"qolsys-controller-[0-9a-f]{12}", a.remote_client_id)


async def test_unconditional_300s_ping_is_sent_even_when_the_panel_is_chatty(monkeypatch):
    """Drive _body with a fake guard and a fake clock: a ping is issued at +300 s although traffic never stops."""
    a = IQ.IQRemoteAdapter.__new__(IQ.IQRemoteAdapter)
    a.remote_client_id = "qolsys-controller-aabbccddeeff"; a.silence_probe_s = 10**6; a.probe_timeout_s = 1
    a.stats = {"probes": 0, "probe_failures": 0}; a._buffer = []; a._resync = asyncio.Event(); a.up = False
    a.zones, a.parts = {}, {}
    now = [1000.0]; calls = []
    import types
    monkeypatch.setattr(IQ, "time", types.SimpleNamespace(monotonic=lambda: now[0])); monkeypatch.setattr(IQ, "SETTLE_S", 0)
    class G:
        async def request(s, ev, fields=None, timeout=30):
            calls.append((ev, now[0]))
            if ev == "syncdatabase": return {"fulldbdata": []}
            return {}
    async def noop(*a, **k): pass
    a.emit_snapshot = noop
    waits = [0]
    orig = asyncio.wait_for
    async def fast(aw, timeout):
        waits[0] += 1; now[0] += 100; a.last_panel_msg = now[0]      # chatty: silence probe never triggers
        aw.close() if hasattr(aw, "close") else None
        await asyncio.sleep(0.001)
        raise asyncio.TimeoutError
    monkeypatch.setattr(IQ.asyncio, "wait_for", fast)
    t = asyncio.create_task(a._body(G(), MAC, "1.1.1.1")); await orig(asyncio.sleep(0.2), 1)
    t.cancel()
    pings = [c for c in calls if c[0] == "pingevent"]
    assert len(pings) >= 2 and pings[-1][1] - 1000.0 >= 300           # handshake ping + the unconditional one


# ------------------------------------------------------------------ control4 framer / wedge
def test_framer_survives_garbage_split_chunks_and_unicode():
    f = C4.C4Framer()
    msgs = [{"event": "ZONE_EVENT", "n": i, "s": "é{\"}\\"} for i in range(3)]
    raw = ("\x00garbage" + "".join(json.dumps(m) for m in msgs) + "}}}{not json}" + json.dumps({"ok": 1})).encode()
    out = []
    for i in range(0, len(raw), 7):                              # arbitrary chunking (decoder is per-chunk text)
        import codecs
        out += f.feed(codecs.getincrementaldecoder("utf-8")("replace").decode(raw[i:i + 7]) if False else raw[i:i + 7].decode("utf-8", "ignore"))
    assert [m for m in out if "n" in m] and out[-1] == {"ok": 1}
    f2 = C4.C4Framer(); o = []
    for part in ('{"a":', '1}{"b"', ':[1,2', ']}'): o += f2.feed(part)
    assert o == [{"a": 1}, {"b": [1, 2]}]


def test_framer_damaged_object_costs_one_char_and_never_wedges_or_grows():
    f = C4.C4Framer(max_buf=2000)
    assert f.feed('{"a": nope} {"b": 2}') == [{"b": 2}] and f.resyncs >= 1
    f.feed("{" + "x" * 5000)                                     # a stray '{' followed by endless junk
    assert len(f.buf) <= 2001 or f.buf.find("{") == -1
    assert f.feed('{"after": true}') == [{"after": True}]
    f3 = C4.C4Framer(); assert f3.feed("[1,2,3] plain text \n") == [] and f3.buf == ""


class W:
    def __init__(s): s.b = b""
    def write(s, x): s.b += x
    async def drain(s): pass


async def test_audit_e4_garbage_does_not_wedge_the_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("BR2_BEACON_DIR", str(tmp_path))
    cfg = CFG.parse({"iq_remote": {"enabled": False}, "control4": {"enabled": True, "host": "x", "token_file": "/dev/null"},
                     "timers": {"silence_probe_s": 1, "probe_timeout_s": 1}})
    a = C4.Control4Adapter(cfg, str(tmp_path)); a.w = C4.GuardedC4Writer(W())
    r = asyncio.StreamReader()
    summ = lambda st: json.dumps({"event": "INFO", "info_type": "SUMMARY", "partition_list": [
        {"partition_id": 0, "zone_list": [{"id": "1", "type": "Motion", "status": st}]}]}) + "\n"
    r.feed_data(summ("Idle").encode()); t = asyncio.create_task(a._run(r, "t"))
    await asyncio.sleep(0.7); assert a.zones[1]["status"] == "Idle"
    r.feed_data(b"\x00garbage\n")
    for _ in range(3):
        await asyncio.sleep(0.3); r.feed_data(summ("Active").encode())
    await asyncio.sleep(0.3)
    assert a.zones[1]["status"] == "Active" and not t.done()
    t.cancel()


# ------------------------------------------------------------------ deploy / hygiene
def test_compose_isolation_and_uids():
    c = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text()); S = c["services"]
    vols = lambda n: {v.split(":")[0] + ":" + v.split(":")[-1] if v.count(":") == 2 else v.split(":")[0]
                      for v in S[n].get("volumes", [])}
    mounts = lambda n: [v for v in S[n].get("volumes", [])]
    has = lambda n, vol: [v for v in mounts(n) if v.startswith(vol + ":")]
    assert has("iq-remote", "ipc-iq") and not has("iq-remote", "ipc-c4") and not has("iq-remote", "ipc-out")
    assert has("control4", "ipc-c4") and not has("control4", "ipc-iq") and not has("control4", "ipc-out")
    assert has("outbound", "ipc-out") and not any(has("outbound", v) for v in ("ipc-iq", "ipc-c4", "beacon"))
    assert has("normalizer", "ipc-iq") and has("normalizer", "ipc-c4") and not has("normalizer", "beacon")
    assert has("iq-remote", "beacon")[0].endswith("/beacon") and not has("iq-remote", "beacon")[0].endswith(":ro")
    assert has("control4", "beacon")[0].endswith("/beacon:ro")                       # control4 can never write it
    assert S["normalizer"]["network_mode"] == "none"
    uids = {n: S[n]["user"].split(":")[0] for n in ("iq-remote", "control4", "outbound", "normalizer")}
    assert uids == {"iq-remote": "10001", "control4": "10002", "outbound": "10003", "normalizer": "10004"}
    out_mounts = " ".join(mounts("outbound"))
    assert "./certs/api:" in out_mounts and "./certs:/certs" not in out_mounts and "broker" not in out_mounts
    assert not any("pki" in v for v in mounts("outbound") + mounts("control4") + mounts("normalizer"))
    assert not any("ca-private" in v for n in S for v in S[n].get("volumes", []))     # CA key never mounted
    for n, u in uids.items():
        assert f"USER {u}:10000" in (ROOT / f"docker/Dockerfile.{n}").read_text()
    for n in ("iq-remote", "control4"):
        assert "adapters/beacon.py" in (ROOT / f"docker/Dockerfile.{n}").read_text()


def test_nftables_egress_policy_pins_adapters_to_panel_ports():
    p = ROOT / "deploy/nftables/blackrelay-egress.nft"; t = p.read_text()
    assert re.search(r"meta skuid \$UID_IQ\s+ip daddr \$PANEL_IP tcp dport 8883\s+accept", t)
    assert re.search(r"meta skuid \$UID_C4\s+ip daddr \$PANEL_IP tcp dport 12345\s+accept", t)
    assert re.search(r"meta skuid \$UID_IQ\s+counter drop", t) and re.search(r"meta skuid \$UID_C4\s+counter drop", t)
    assert t.index("dport 8883") < t.index("$UID_IQ  counter drop") and "define UID_IQ  = 10001" in t
    if shutil.which("nft") and os.geteuid() == 0:
        r = subprocess.run(["nft", "-c", "-f", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_gen_certs_modes_ownership_and_ca_key_location(tmp_path):
    if not shutil.which("openssl") or not (shutil.which("mosquitto_passwd")):
        pytest.skip("openssl/mosquitto_passwd missing")
    dep = tmp_path / "deploy"; shutil.copytree(ROOT / "deploy", dep, ignore=shutil.ignore_patterns("certs", "secrets", "data", "ca-private"))
    r = subprocess.run([str(dep / "scripts/gen-certs.sh"), "127.0.0.1"], cwd=dep, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    mode = lambda p: oct((dep / p).stat().st_mode & 0o777); uid = lambda p: (dep / p).stat().st_uid
    for k in ("certs/broker/broker.key", "certs/api/api.key", "secrets/mqtt_password", "secrets/control4_token",
              "ca-private/ca.key", "mosquitto/passwd"):
        assert mode(k) == "0o600", k
    assert mode("ca-private") == "0o700" and mode("data/pki") == "0o700" and mode("secrets") == "0o750"
    assert not list((dep / "certs").rglob("ca.key")) and (dep / "certs/ca.crt").exists()
    if os.geteuid() == 0:
        assert (uid("certs/api/api.key"), uid("secrets/mqtt_password"), uid("secrets/control4_token"),
                uid("certs/broker/broker.key"), uid("data/pki")) == (10003, 10003, 10002, 1883, 10001)


def test_example_config_documents_every_new_key_and_loads():
    c = CFG.load(ROOT / "deploy/config/config.example.yaml")
    assert c.api["metrics_host"] == "127.0.0.1" and c.mqtt["insecure_dev"] is False
    assert c.timers["failback_holddown_s"] == 3 and c.timers["ipc_liveness_s"] == 5
    assert c.api["tls_cert"] == "/certs/api/api.crt"
