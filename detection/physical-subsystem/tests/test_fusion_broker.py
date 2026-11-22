"""Real mosquitto (plain TCP, anonymous: the team broker as it is today). Skipped if mosquitto is not installed."""
import ast, json, pathlib, shutil, threading, time
import pytest
import paho.mqtt.client as mqtt
from lib.mosq import MosqProc, MOSQUITTO
from br2.outbound import formats as F
from br2.outbound.fusion import FusionSender, PublishOnlyClient
from br2.outbound.outbox import Outbox, Item, EVENT, STATUS
from fusion_helpers import validate, zone, log_event, free_port, plain_broker_conf, wait_until, ROOT, TOPIC_FOR

pytestmark = pytest.mark.skipif(not (shutil.which("mosquitto") or pathlib.Path("/usr/sbin/mosquitto").exists()),
                                reason="mosquitto not installed")
SITE = {"id": "t", "name": "T"}
CID = "physical-test-client"


class Sub:
    """Independent subscriber (test-only). Records (topic, payload, retain) and re-subscribes on reconnect."""
    def __init__(self, port):
        self.got, self.lock = [], threading.Lock()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-sub")
        self.c.on_connect = lambda c, u, f, rc, p=None: c.subscribe("detection/physical/#", 1)
        self.c.on_message = self._msg
        self.c.reconnect_delay_set(0.1, 0.1)   # back (and subscribed) well before the bridge retries
        self.c.connect_async("127.0.0.1", port); self.c.loop_start()

    def _msg(self, c, u, m):
        with self.lock:
            self.got.append((m.topic, m.payload, m.retain))

    def ids(self):
        with self.lock:
            return [json.loads(p)["id"] for _, p, _ in self.got]

    def stop(self):
        self.c.loop_stop(); self.c.disconnect()


def mk_event(i):
    e = log_event(i, "zone.state", "zone-1", "closed", "open", ts=f"2026-01-01T12:00:{i:02d}.007Z")
    mid, b = F.event_builder(SITE, e, zone(1, state="open"), "zone_opened", 0, "db")
    return Item(mid, "detection/physical/events", EVENT, lambda _n, b=b: F.encode(b()))


def mk_status(n):
    mid, b = F.panel_builder(SITE, F.panel_view({"status": "online", "active_source": "iq_remote", "links": {"iq_remote": "up"}}),
                             None, 1767268800.0 + n)
    return Item(mid, "detection/physical/status/panel", STATUS, lambda _n, b=b: F.encode(b()))


@pytest.fixture
def rig(tmp_path):
    port = free_port()
    broker = MosqProc(plain_broker_conf(tmp_path / "b", port), tmp_path / "b" / "mosq.log")
    broker.start([port])
    ob = Outbox(str(tmp_path / "ob"))
    s = FusionSender({"host": "127.0.0.1", "port": port, "client_id": CID, "tls": None, "max_inflight": 20}, ob)
    sub = Sub(port)
    r = type("R", (), dict(port=port, broker=broker, ob=ob, sender=s, sub=sub, dir=tmp_path / "b"))()
    yield r
    s.stop(); sub.stop(); broker.kill(); ob.close()


def test_delivers_valid_messages_qos1_unretained_and_acks(rig):
    assert wait_until(lambda: rig.sub.c.is_connected())
    rig.sender.start()
    rig.ob.append([mk_event(1), mk_event(2), mk_status(0)])
    rig.sender.wake()
    assert wait_until(lambda: len(rig.sub.ids()) >= 3 and rig.ob.stats()["depth"] == 0)
    for topic, payload, retain in rig.sub.got:
        m = validate(payload)
        assert topic == TOPIC_FOR[m["schema"]] and retain is False
    assert rig.ob.stats()["acked_total"] == 3                       # removed from the outbox only on PUBACK


def test_nothing_is_retained_on_the_broker(rig):
    assert wait_until(lambda: rig.sub.c.is_connected())
    rig.sender.start()
    rig.ob.append([mk_event(1), mk_status(0)]); rig.sender.wake()
    assert wait_until(lambda: rig.ob.stats()["depth"] == 0 and len(rig.sub.ids()) == 2)
    late = Sub(rig.port)                                            # a late subscriber gets nothing replayed
    time.sleep(0.6)
    try:
        assert late.got == []
    finally:
        late.stop()


def test_outage_replay_resends_unacked_with_original_id_and_timestamp(rig):
    assert wait_until(lambda: rig.sub.c.is_connected())
    rig.sender.start()
    assert wait_until(lambda: rig.sender.connected.is_set())
    rig.ob.append([mk_event(1)]); rig.sender.wake()
    assert wait_until(lambda: len(rig.sub.ids()) == 1 and rig.ob.stats()["depth"] == 0)
    rig.broker.freeze()                                             # TCP stays open, no PUBACK will ever come
    rig.ob.append([mk_event(2), mk_event(3)]); rig.sender.wake()
    assert wait_until(lambda: rig.sender.inflight() == 2)
    assert rig.ob.stats()["depth"] == 2                             # sent, not acknowledged => still on disk
    originals = {e.id: e.payload for e in rig.ob.pending(10)}
    rig.broker.kill()                                               # broker dies (kills the frozen process)
    rig.ob.append([mk_event(4)]); rig.sender.wake()                 # produced while the broker is down
    time.sleep(0.3)
    originals.update({e.id: e.payload for e in rig.ob.pending(10)})
    assert rig.ob.stats()["depth"] == 3
    rig.broker.start([rig.port])
    assert wait_until(lambda: rig.ob.stats()["depth"] == 0, timeout=15)
    assert wait_until(lambda: set(originals) <= set(rig.sub.ids()), timeout=15)
    sent = {}
    for _, p, _ in rig.sub.got:
        o = json.loads(p)
        if o["id"] in originals:
            sent.setdefault(o["id"], set()).add(p)
    for mid, payloads in sent.items():
        assert payloads == {originals[mid]}, "re-send must be byte-identical (same id, timestamp, seq)"
    assert rig.sender.counters["reconnects"] >= 1


def test_duplicate_resend_carries_the_same_id(rig):
    """At-least-once: if the PUBACK is lost the message is sent again; subscribers dedupe on `id`."""
    assert wait_until(lambda: rig.sub.c.is_connected())
    rig.sender.start(); assert wait_until(lambda: rig.sender.connected.is_set())
    rig.ob.append([mk_event(1)])
    e = rig.ob.pending(1)[0]
    rig.sender.client.publish(e.topic, e.payload, qos=1)           # a delivery whose ack the bridge never saw...
    rig.sender.wake()                                              # ...followed by the outbox's own send
    assert wait_until(lambda: len(rig.sub.ids()) >= 2)
    assert len(set(rig.sub.ids())) == 1


def test_client_never_subscribes(rig):
    rig.sender.start(); assert wait_until(lambda: rig.sender.connected.is_set())
    rig.ob.append([mk_event(1)]); rig.sender.wake()
    assert wait_until(lambda: rig.ob.stats()["depth"] == 0)
    rig.sender.stop()
    subs = {}                                    # `log_type subscribe` lines: "<ts>: <client_id> <qos> <topic>"
    for ln in (rig.dir / "mosq.log").read_text().splitlines():
        f = ln.split(": ", 1)[-1].split()
        if len(f) == 3 and f[1] in "012":
            subs.setdefault(f[0], []).append(f[2])
    assert subs.get("test-sub") == ["detection/physical/#"]      # the check can see subscriptions...
    assert CID not in subs                                        # ...and the fusion client made none


def test_publish_only_client_refuses_to_subscribe():
    c = PublishOnlyClient(mqtt.CallbackAPIVersion.VERSION2, client_id="x")
    with pytest.raises(RuntimeError):
        c.subscribe("detection/#")
    with pytest.raises(RuntimeError):
        c.unsubscribe("detection/#")


def test_fusion_source_contains_no_subscribe_call():
    for f in ("fusion.py", "outbox.py", "formats.py"):
        tree = ast.parse((ROOT / "src/br2/outbound" / f).read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert n.func.attr not in ("subscribe", "unsubscribe", "message_callback_add"), (f, n.lineno)


async def test_end_to_end_publisher_log_to_broker(rig, tmp_path):
    """FusionPublisher.run() with the real sender: event log + live state in, schema-valid team messages out."""
    import asyncio
    from br2 import config as C
    from br2.normalizer.store import Store, Reader
    from br2.outbound.fusion import FusionPublisher
    from br2.outbound.state import OutState
    from fusion_helpers import dump
    cfg = C.parse({"storage": {"data_dir": str(tmp_path / "d")}, "site": {"id": "e2e", "name": "E2E"},
                   "fusion": {"enabled": True, "host": "127.0.0.1", "port": rig.port, "client_id": "physical-e2e",
                              "status_interval_s": 60, "outbox": {"dir": str(tmp_path / "e2e-ob")}}})
    store, st = Store(cfg.db_path), OutState()
    pub = FusionPublisher(cfg, st, Reader(cfg.db_path))
    assert wait_until(lambda: rig.sub.c.is_connected())
    task = asyncio.create_task(pub.run())
    try:
        st.apply(dump([zone(12, "contact", "closed", name="Front door")], last_seq=0))
        await asyncio.sleep(0.5)
        evs = store.commit([{"ts": "2026-01-01T12:00:01.250Z", "type": "zone.state", "subject": "zone-12", "from": "closed",
                             "to": "open", "source": "iq_remote", "synthetic": False}], {}, [], {}, [])
        st.apply({"t": "batch", "events": evs, "zones": [], "removed_zones": [], "partitions": [],
                  "removed_partitions": [], "status": "online"})
        assert await asyncio.to_thread(wait_until, lambda: {json.loads(p)["schema"] for _, p, _ in rig.sub.got} ==
                                       set(TOPIC_FOR) and pub.outbox.stats()["depth"] == 0, 10)
        for topic, payload, retain in rig.sub.got:
            m = validate(payload)
            assert topic == TOPIC_FOR[m["schema"]] and not retain and m["site"]["id"] == "e2e"
        ev = [json.loads(p) for _, p, _ in rig.sub.got if json.loads(p)["schema"] == "physical.event.v0"]
        assert [e["event_type"] for e in ev] == ["zone_opened"] and ev[0]["timestamp"] == "2026-01-01T12:00:01.250Z"
        assert pub.metrics()["connected"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
