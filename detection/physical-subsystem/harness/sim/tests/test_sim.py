"""Self-tests of the simulator using hand-rolled fake clients (no bridge involved)."""
import json, socket, ssl, sys, time, uuid, threading
from pathlib import Path
import pytest
import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(HERE))
from lib.certs import Pki
from sim.panel_sim import PanelSim


@pytest.fixture(scope="module")
def pki(tmp_path_factory): return Pki.create(tmp_path_factory.mktemp("pki"))


@pytest.fixture()
def sim(pki, tmp_path):
    s = PanelSim(tmp_path, pki).start(); yield s; s.close()


class IQClient:
    def __init__(self, sim, pki, cafile=None, certfile=None, keyfile=None):
        d = pki.root / "pki" / pki.pki_id
        self.mac = pki.mac; self.resp = {}; self.events = []; self.connected = threading.Event()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="qolsys-controller-aabbcc")
        ctx = ssl.create_default_context(cafile=str(cafile or d / f"{pki.pki_id}.qolsys"))
        ctx.check_hostname = False; ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT   # as the library does
        ctx.load_cert_chain(certfile or d / f"{pki.pki_id}.secure", keyfile or d / f"{pki.pki_id}.key")
        self.c.tls_set_context(ctx)
        self.c.on_connect = lambda c, u, f, rc, p=None: (c.subscribe([("iq2meid", 0), ("response_" + self.mac, 0)]), self.connected.set())
        self.c.on_message = self._msg
        self.c.connect("127.0.0.1", sim.iq.port, 30); self.c.loop_start()

    def _msg(self, c, u, m):
        j = json.loads(m.payload)
        if m.topic == "iq2meid": self.events.append(j)
        else: self.resp[j["requestID"]] = j

    def cmd(self, name, timeout=3, **extra):
        rid = str(uuid.uuid4())
        self.c.publish("mastermeid", json.dumps({"requestID": rid, "responseTopic": "response_" + self.mac, "eventName": name,
                                                 "remoteMacAddress": self.mac, **extra}))
        end = time.time() + timeout
        while rid not in self.resp and time.time() < end: time.sleep(0.02)
        return self.resp.get(rid)

    def close(self): self.c.loop_stop(); self.c.disconnect()


def wait(pred, t=3):
    end = time.time() + t
    while time.time() < end:
        if pred(): return True
        time.sleep(0.02)
    return False


def test_iq_session_sync_and_events(sim, pki):
    c = IQClient(sim, pki); assert c.connected.wait(5)
    time.sleep(0.2)
    assert c.cmd("connect_v204")["master_imei"]
    assert c.cmd("pingevent") and c.cmd("pair_status_request")
    db = c.cmd("syncdatabase")["fulldbdata"]
    sensors = [t for t in db if t["uri"].endswith("SensorContentProvider/sensor")][0]["resultSet"]
    assert {r["zoneid"] for r in sensors} == {"12", "13", "14", "15", "16", "40"}
    sim.set_zone(12, "Open")
    assert wait(lambda: any(e["contentValues"].get("sensorstatus") == "Open" for e in c.events))
    sim.set_arming(0, "ARM-AWAY")
    assert wait(lambda: any(e["contentValues"].get("value") == "ARM-AWAY" for e in c.events))
    assert not sim.violations
    c.close()


def test_iq_requires_client_cert_and_trusted_ca(sim, pki):
    d = pki.root / "pki" / pki.pki_id
    # no client cert -> handshake must fail
    ctx = ssl.create_default_context(cafile=str(d / f"{pki.pki_id}.qolsys")); ctx.check_hostname = False; ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        with socket.create_connection(("127.0.0.1", sim.iq.port), 3) as s:
            with ctx.wrap_socket(s) as t:
                t.sendall(b"\x10\x00"); t.recv(1)       # TLS1.3: failure surfaces on first IO
    # client cert from rogue CA -> rejected
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        ctx2 = ssl.create_default_context(cafile=str(d / f"{pki.pki_id}.qolsys")); ctx2.check_hostname = False; ctx2.verify_flags &= ~ssl.VERIFY_X509_STRICT
        ctx2.load_cert_chain(pki.root / "rogue-client.crt", pki.root / "rogue-client.key")
        with socket.create_connection(("127.0.0.1", sim.iq.port), 3) as s:
            with ctx2.wrap_socket(s) as t: t.sendall(b"\x10\x00"); t.recv(1)


def test_iq_rogue_server_cert_detectable(sim, pki):
    sim.iq.set_cert("rogue")
    with pytest.raises(ssl.SSLError):
        IQClient(sim, pki)       # client verifies server against sim CA -> must fail
    sim.iq.set_cert("trusted")
    c = IQClient(sim, pki); assert c.connected.wait(5); c.close()


def test_iq_audit_flags_violations(sim, pki):
    c = IQClient(sim, pki); assert c.connected.wait(5); time.sleep(0.2)
    c.cmd("connect_v204"); c.cmd("syncdatabase")
    assert not sim.violations
    c.cmd("ipcCall", ipcServiceName="qinternalservice", ipcTransactionID=7,
          ipcRequest=[{"dataType": "string", "dataValue": json.dumps({"operation_name": "ui_armaway"})}])
    c.cmd("database", dbOperation="insert", uri="x", contentValues={})
    c.c.publish("some/other", "x"); time.sleep(0.3)
    v = sim.violations
    assert len(v) == 3 and any("ipcCall" in e["reason"] for e in v) and any("database insert" in e["reason"] for e in v)
    c.close()


def test_iq_outage_stall_mute_drop(sim, pki):
    c = IQClient(sim, pki); assert c.connected.wait(5); time.sleep(0.2)
    assert c.cmd("pingevent")
    sim.iq.mute(True); assert c.cmd("pingevent", timeout=0.6) is None; sim.iq.mute(False)
    assert c.cmd("pingevent")
    sim.iq.stall(); assert c.cmd("pingevent", timeout=0.6) is None; sim.iq.unstall()
    sim.iq.outage()
    with pytest.raises(Exception): IQClient(sim, pki)
    sim.iq.restore()
    c2 = IQClient(sim, pki); assert c2.connected.wait(5); time.sleep(0.2); assert c2.cmd("pingevent")
    sim.iq.drop(); time.sleep(0.5)
    c3 = IQClient(sim, pki); assert c3.connected.wait(5)
    for x in (c, c2, c3): x.close()
    assert not sim.violations


class C4Client:
    def __init__(self, sim, fp=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.raw = socket.create_connection(("127.0.0.1", sim.c4.port), 3)
        self.s = ctx.wrap_socket(self.raw); self.fp = __import__("hashlib").sha256(self.s.getpeercert(True)).hexdigest()
        self.s.settimeout(2); self.buf = ""; self.dec = json.JSONDecoder()

    def send(self, **o): self.s.sendall(json.dumps(o).encode())

    def recv(self, n=1):
        out = []
        while len(out) < n:
            self.buf = self.buf.lstrip()
            try: obj, end = self.dec.raw_decode(self.buf); self.buf = self.buf[end:]; out.append(obj); continue
            except json.JSONDecodeError: pass
            d = self.s.recv(65536)
            if not d: break
            self.buf += d.decode()
        return out


def test_c4_summary_events_token_and_fingerprint(sim):
    c = C4Client(sim); assert c.fp == sim.c4.fingerprint
    c.send(action="INFO", info_type="SUMMARY", token=sim.c4.token, nonce="qolsys", source="C4", version=0)
    ack, info = c.recv(2); assert ack["event"] == "ACK" and info["event"] == "INFO"
    zl = info["partition_list"][0]["zone_list"]; assert {z["zone_id"] for z in zl} == {12, 13, 14, 15, 16, 40}
    sim.set_zone(12, "Open"); ev = c.recv()[0]
    assert ev["zone_event_type"] == "ZONE_ACTIVE" and ev["zone"]["status"] == "Open"
    sim.set_zone(12, "Closed"); assert c.recv()[0]["zone_event_type"] == "ZONE_UPDATE"
    sim.add_zone(20, "New", "Motion", "Idle"); assert c.recv()[0]["zone_event_type"] == " ZONE_ADD"
    sim.set_arming(0, "ARM-STAY"); assert c.recv()[0]["arming_type"] == "ARM_STAY"
    assert not sim.violations
    c.send(action="ARMING", arming_type="DISARM", partition_id=0, token=sim.c4.token, usercode="1234")
    time.sleep(0.3); assert sim.audit.count(side="c4", verdict="violation") == 1
    # bad token
    c2 = C4Client(sim); c2.send(action="INFO", info_type="SUMMARY", token="nope")
    assert c2.recv()[0]["error_type"] == "INVALID_TOKEN"
    old = sim.c4.fingerprint; sim.c4.rotate_cert(); assert sim.c4.fingerprint != old
    assert C4Client(sim).fp == sim.c4.fingerprint


def test_c4_outage_stall_drop(sim):
    c = C4Client(sim)
    sim.c4.stall(); c.send(action="INFO", info_type="SUMMARY", token=sim.c4.token)
    c.s.settimeout(0.5)
    with pytest.raises(Exception): c.s.recv(10)
    sim.c4.unstall()
    sim.c4.drop(); time.sleep(0.2)
    with pytest.raises(Exception): c.s.settimeout(1); c.send(action="INFO", info_type="SUMMARY", token=sim.c4.token); c.s.recv(10) or (_ for _ in ()).throw(OSError())
    sim.c4.outage()
    with pytest.raises(OSError): C4Client(sim)
    sim.c4.restore(); C4Client(sim)


def test_diverge_while_down_emits_nothing(sim, pki):
    c = IQClient(sim, pki); assert c.connected.wait(5); time.sleep(0.2)
    sim.diverge_while_down(lambda s: s.set_zone(13, "Active"))
    time.sleep(0.4); assert not c.events
    sim.set_zone(13, "Idle"); assert wait(lambda: c.events)
    c.close()
