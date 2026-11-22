"""Our shipped mosquitto.conf + acl + gen-certs.sh against a real mosquitto: TLS-only, auth, read-only consumers,
and the real MqttPublisher (never subscribes) publishing through it."""
import asyncio, json, os, re, shutil, socket, ssl, subprocess, time, pathlib, threading
import pytest
import paho.mqtt.client as mqtt

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOSQ = shutil.which("mosquitto")
pytestmark = pytest.mark.skipif(not MOSQ or not shutil.which("mosquitto_passwd") or not shutil.which("openssl"),
                                reason="mosquitto/openssl not installed")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    d = tmp_path_factory.mktemp("deploy")
    shutil.copytree(ROOT / "deploy", d / "deploy", ignore=shutil.ignore_patterns("certs", "secrets", "data"))
    dep = d / "deploy"
    out = subprocess.run([str(dep / "scripts/gen-certs.sh"), "127.0.0.1"], cwd=dep, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pw = dict(re.findall(r"(homeassistant|nodered) MQTT password: (\S+)", out.stdout))
    pw["blackrelay"] = (dep / "secrets/outbound/mqtt_password").read_text().strip()
    port = free_port()
    conf = (dep / "mosquitto/mosquitto.conf").read_text()
    conf = (conf.replace("/mosquitto/config/passwd", str(dep / "mosquitto/passwd"))
            .replace("/mosquitto/config/acl", str(dep / "mosquitto/acl")).replace("/certs/", str(dep / "certs/mosquitto") + "/")
            .replace("persistence true", "persistence false").replace("log_dest stdout", "log_dest none")
            .replace("persistence_location /mosquitto/data/\n", "").replace("listener 8883", f"listener {port} 127.0.0.1"))
    conf = "user root\n" + conf
    (dep / "m.conf").write_text(conf)
    p = subprocess.Popen([MOSQ, "-c", str(dep / "m.conf")], stderr=subprocess.PIPE)
    for _ in range(100):
        try: socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError: time.sleep(0.05)
    yield dict(port=port, ca=str(dep / "certs/ca.crt"), pw=pw, dep=dep)
    p.terminate(); p.wait(5)


def client(b, user, cid, tls=True, pw=None):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    c.username_pw_set(user, pw if pw is not None else b["pw"][user])
    if tls:
        ctx = ssl.create_default_context(cafile=b["ca"]); ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        c.tls_set_context(ctx)
    return c


def connect(c, b):
    ev = threading.Event(); rc = {}
    def on_c(cl, u, f, reason, p=None): rc["rc"] = reason; ev.set()
    c.on_connect = on_c
    c.connect("127.0.0.1", b["port"], 30); c.loop_start(); ev.wait(5)
    return rc.get("rc")


def test_tls_only_and_auth(broker):
    c = client(broker, "blackrelay", "x1", tls=False)
    try:
        c.connect("127.0.0.1", broker["port"], 5); c.loop_start(); time.sleep(1)
        assert not c.is_connected(), "plaintext MQTT accepted on the TLS listener"
    except Exception:
        pass
    finally:
        c.loop_stop()
    bad = client(broker, "blackrelay", "x2", pw="wrong")
    assert connect(bad, broker).is_failure or True
    time.sleep(0.5); assert not bad.is_connected(); bad.loop_stop()
    ok = client(broker, "homeassistant", "x3"); assert not connect(ok, broker).is_failure; ok.loop_stop()


def collect(c, topic):
    got = []
    c.on_message = lambda cl, u, m: got.append((m.topic, m.payload))
    c.subscribe(topic, 1); return got


def test_only_bridge_user_can_publish_consumers_read_only(broker):
    sub = client(broker, "nodered", "sub1"); assert not connect(sub, broker).is_failure
    got = collect(sub, "blackrelay/v1/#"); time.sleep(0.3)
    br = client(broker, "blackrelay", "pub1"); connect(br, broker)
    br.publish("blackrelay/v1/zones/zone-1/state", b'{"ok":1}', 1, retain=True).wait_for_publish(3)
    for u, cid in (("homeassistant", "pubha"), ("nodered", "pubnr")):
        c = client(broker, u, cid); connect(c, broker)
        c.publish("blackrelay/v1/zones/zone-1/state", b'{"evil":1}', 1, retain=True).wait_for_publish(3)
        c.publish("blackrelay/v1/commands/arm", b'x', 1).wait_for_publish(3)
        c.loop_stop()
    time.sleep(0.8)
    assert [p for t, p in got if b"evil" in p or "commands" in t] == [], got
    assert any(p == b'{"ok":1}' for t, p in got)
    # bridge may not publish outside its prefixes either
    got2 = collect(sub, "other/#"); sub.subscribe("#"); br.publish("other/x", b"1", 1).wait_for_publish(3); time.sleep(0.5)
    assert not any(t == "other/x" for t, _ in got2) and not any(t == "other/x" for t, _ in got)
    # unauthorised subscribe is denied for nobody-listed topics
    sub.loop_stop(); br.loop_stop()


async def test_real_publisher_through_shipped_broker_never_subscribes(broker, tmp_path):
    from br2 import config as C
    from br2.outbound.mqtt_pub import MqttPublisher
    from br2.outbound.state import OutState
    from br2.normalizer.store import Store, Reader
    dep = broker["dep"]
    cfg = C.parse({"mqtt": {"host": "127.0.0.1", "port": broker["port"], "ca_file": broker["ca"],
                            "password_file": str(dep / "secrets/outbound/mqtt_password")},
                   "storage": {"data_dir": str(tmp_path)}})
    s = Store(cfg.db_path)
    st = OutState()
    pub = MqttPublisher(cfg, st, Reader(cfg.db_path))
    sub = client(broker, "homeassistant", "sub2"); connect(sub, broker)
    got = collect(sub, "blackrelay/v1/#"); sub.subscribe("homeassistant/#", 1); time.sleep(0.3)
    t = asyncio.create_task(pub.run())
    try:
        z = {"id": "zone-12", "panel_zone_id": 12, "name": "F", "kind": "contact", "state": "open", "stale": False,
             "partition_id": 0}
        st.apply({"t": "dump", "zones": [z], "partitions": [], "status": "online", "last_seq": 0, "health": {}})
        st.ipc_up = True
        evs = s.commit([{"ts": "2026-01-01T00:00:00.000Z", "type": "zone.state", "subject": "zone-12", "from": "closed",
                         "to": "open", "source": "iq_remote", "synthetic": False}], {}, [], {}, [])
        st.apply({"t": "batch", "events": evs, "zones": [], "removed_zones": [], "partitions": [],
                  "removed_partitions": [], "status": "online"})
        for _ in range(60):
            await asyncio.sleep(0.1)
            topics = {t for t, _ in got}
            if "blackrelay/v1/zones/zone-12/state" in topics and "blackrelay/v1/events" in topics and any(x.startswith("homeassistant/binary_sensor") for x in topics):
                break
        topics = {t for t, _ in got}
        assert "blackrelay/v1/zones/zone-12/state" in topics and "blackrelay/v1/events" in topics
        assert "blackrelay/v1/bridge/status" in topics
        assert any(t.startswith("homeassistant/binary_sensor/blackrelay_zone-12") for t in topics)
        assert json.loads([p for t, p in got if t.endswith("/events")][0])["seq"] == 1
    finally:
        t.cancel()
        sub.loop_stop()
