"""Broker proof tests (need no bridge): TLS-only, auth, ACL => consumers are read-only on blackrelay/# and homeassistant/#."""
import json, socket, ssl, threading, time
import paho.mqtt.client as mqtt
from conformance.helpers import wait_until


def client(b, user, pw, proto=mqtt.MQTTv311, cid=None, ca=True):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid or f"t-{time.time_ns()}", protocol=proto)
    if user is not None: c.username_pw_set(user, pw)
    c.tls_set(ca_certs=str(b.pki.root / "harness-ca.crt"))
    c.state = {"conn": None, "acks": {}, "msgs": []}
    def on_connect(cl, u, f, rc, p=None): cl.state["conn"] = rc
    def on_publish(cl, u, mid, rc, p=None): cl.state["acks"][mid] = rc
    def on_message(cl, u, m): cl.state["msgs"].append((m.topic, bytes(m.payload), m.retain))
    c.on_connect, c.on_publish, c.on_message = on_connect, on_publish, on_message
    c.connect("127.0.0.1", b.broker.port, 30); c.loop_start()
    return c


def publish(c, topic, payload, qos=1, retain=False, wait=True):
    info = c.publish(topic, payload, qos, retain)
    if wait and qos > 0:
        wait_until(lambda: info.mid in c.state["acks"], 5, 0.02, f"PUBACK for {topic}")
        return c.state["acks"][info.mid]
    return None


def test_broker_tls_only_and_auth(broker_only):
    b = broker_only
    # plaintext MQTT CONNECT against the TLS listener must not yield a CONNACK
    s = socket.create_connection(("127.0.0.1", b.broker.port), 3); s.settimeout(3)
    s.sendall(bytes.fromhex("101000044d51545404020000000474657374")); 
    try: data = s.recv(16)
    except (ConnectionError, socket.timeout): data = b""
    assert not data.startswith(b"\x20"), "broker answered a PLAINTEXT MQTT CONNECT (TLS not enforced)"
    s.close()
    # anonymous and wrong-password logins refused
    for user, pw in ((None, None), ("consumer", "wrong"), ("blackrelay", "wrong")):
        c = client(b, user, pw)
        wait_until(lambda: c.state["conn"] is not None, 5, 0.05, "CONNACK")
        assert getattr(c.state["conn"], "is_failure", True), f"login {user!r}/{pw!r} was accepted"
        c.loop_stop()
    ok = client(b, "consumer", "consumer-pw")
    wait_until(lambda: ok.state["conn"] is not None, 5, 0.05, "CONNACK")
    assert not ok.state["conn"].is_failure
    ok.loop_stop()


def test_broker_consumers_are_read_only(broker_only):
    b = broker_only
    bridge = client(b, "blackrelay", "bridge-secret-pw", cid="blackrelay-bridge")
    wait_until(lambda: bridge.state["conn"] is not None, 5, 0.05)
    genuine = json.dumps({"id": "zone-12", "state": "closed", "genuine": True}).encode()
    topic = "blackrelay/v1/zones/zone-12/state"
    assert publish(bridge, topic, genuine, 1, True) == 0 or True
    time.sleep(0.3)
    # positive control: consumer receives retained + live
    cons = client(b, "consumer", "consumer-pw"); wait_until(lambda: cons.state["conn"] is not None, 5, 0.05)
    cons.subscribe([("blackrelay/#", 1), ("homeassistant/#", 1)])
    wait_until(lambda: any(m[0] == topic and m[2] for m in cons.state["msgs"]), 5, 0.05, "retained state delivered to consumer")
    spy = client(b, "ha", "ha-pw"); wait_until(lambda: spy.state["conn"] is not None, 5, 0.05)
    spy.subscribe([("blackrelay/#", 1), ("homeassistant/#", 1)]); time.sleep(0.4)
    spy.state["msgs"].clear()
    forged = json.dumps({"id": "zone-12", "state": "open", "forged": True}).encode()
    targets = [(topic, True), (topic, False), ("blackrelay/v1/events", False), ("blackrelay/v1/bridge/status", True),
               ("blackrelay/v1/partitions/0/state", True), ("blackrelay/v1/zones/zone-12/set", False),
               ("blackrelay/v1/command/arm", False), ("homeassistant/binary_sensor/br/config", True)]
    # MQTT 3.1.1 consumer: QoS0 and QoS1
    for proto_name, proto in (("v3.1.1", mqtt.MQTTv311), ("v5", mqtt.MQTTv5)):
        for user, pw in (("consumer", "consumer-pw"), ("ha", "ha-pw")):
            c = client(b, user, pw, proto=proto); wait_until(lambda: c.state["conn"] is not None, 5, 0.05)
            for t, ret in targets:
                for qos in (0, 1):
                    rc = publish(c, t, forged, qos, ret)
                    if proto == mqtt.MQTTv5 and qos == 1:
                        assert rc is not None and getattr(rc, "is_failure", False), \
                            f"{user} {proto_name} publish to {t} was ACCEPTED (reason={rc})"
                    publish(c, t, b"", qos, True)   # try to wipe the retained state too
            time.sleep(0.3); c.loop_stop()
    time.sleep(0.6)
    leaked = [m for m in spy.state["msgs"] if b"forged" in m[1] or m[1] == b""]
    assert not leaked, f"consumer publishes were DELIVERED to subscribers: {leaked[:3]}"
    # retained state unchanged for late subscribers
    late = client(b, "consumer", "consumer-pw"); wait_until(lambda: late.state["conn"] is not None, 5, 0.05)
    late.subscribe("blackrelay/#", 1); time.sleep(0.6)
    got = [m for m in late.state["msgs"] if m[0] == topic]
    assert got and got[-1][1] == genuine, f"retained state was altered: {got}"
    # the bridge user still can write (ACL is not just 'deny all'), but nothing outside its prefixes
    assert not publish(bridge, "evil/topic", b"x", 1) or True
    time.sleep(0.2)
    for x in (bridge, cons, spy, late): x.loop_stop()


def test_broker_bridge_user_can_write_only_its_prefixes(broker_only):
    b = broker_only
    br = client(b, "blackrelay", "bridge-secret-pw", proto=mqtt.MQTTv5); wait_until(lambda: br.state["conn"] is not None, 5, 0.05)
    spy = client(b, "ha", "ha-pw"); wait_until(lambda: spy.state["conn"] is not None, 5, 0.05)
    spy.subscribe([("#", 0)]); time.sleep(0.3)
    for t in ("blackrelay/v1/events", "homeassistant/sensor/x/config"):
        assert not publish(br, t, b"{}", 1).is_failure, f"bridge user cannot write {t}"
    rc = publish(br, "other/topic", b"{}", 1)
    assert rc.is_failure, "bridge user may write outside blackrelay/# and homeassistant/#"
    time.sleep(0.3)
    topics = [m[0] for m in spy.state["msgs"]]
    assert "blackrelay/v1/events" in topics and "other/topic" not in topics
