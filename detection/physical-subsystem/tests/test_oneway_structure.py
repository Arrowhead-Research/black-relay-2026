"""Structural one-way proofs: IPC direction (kernel-enforced), panel write guards, source-level import audit."""
import asyncio, uuid, json, os, re, socket, pathlib, pytest
from br2 import ipc
from br2.adapters import iq_remote as IQ, control4 as C4
from br2.adapters.base import OneWayViolation

SRC = pathlib.Path(IQ.__file__).resolve().parents[1]


async def test_adapter_to_normalizer_socket_cannot_carry_reverse_data(tmp_path):
    got = []
    holder = {}

    async def on_conn(s):
        c = ipc.RecvOnlyConn(s)
        holder["c"] = c
        async for m in c.frames():
            got.append(m)

    path = str(tmp_path / "a.sock")
    lst = ipc.Listener(path, on_conn)
    await lst.start()
    cl = ipc.SendOnlyClient(path, {"t": "hello", "src": "x"})
    t = asyncio.create_task(cl.maintain())
    await asyncio.wait_for(cl.connected.wait(), 3)
    assert await cl.send({"t": "zone", "n": 1})
    await asyncio.sleep(0.2)
    assert [m["t"] for m in got] == ["hello", "zone"]
    # receiver (normalizer) cannot write: kernel refuses
    assert holder["c"].peer_can_receive() is False
    # sender (adapter) cannot read: immediate EOF, not data
    assert cl.recv_possible() is False
    # raw reverse write attempt via the underlying socket also fails
    with pytest.raises(OSError):
        holder["c"]._sock.send(b"command")
    assert not hasattr(ipc.RecvOnlyConn, "send") and not hasattr(ipc.SendOnlyClient, "recv")
    assert not hasattr(ipc.SendOnlyConn, "frames") and not hasattr(ipc.RecvOnlyClient, "send")
    t.cancel(); await lst.stop()


async def test_normalizer_to_outbound_socket_is_one_way(tmp_path):
    path = str(tmp_path / "o.sock")
    holder = {}

    async def on_conn(s):
        holder["c"] = ipc.SendOnlyConn(s)
        await holder["c"].send({"t": "dump"})
    lst = ipc.Listener(path, on_conn); await lst.start()
    rc = ipc.RecvOnlyClient(path)
    it = rc.messages()
    assert (await asyncio.wait_for(it.__anext__(), 3))["t"] == "dump"
    await asyncio.sleep(0.1)
    assert holder["c"].peer_can_send() is False   # outbound can never send to normalizer
    await it.aclose(); await lst.stop()


def test_frame_roundtrip_and_partial():
    buf = bytearray(ipc.encode({"a": 1}) + ipc.encode({"b": "é"})[:5])
    assert ipc.decode_frames(buf) == [{"a": 1}] and len(buf) == 5
    with pytest.raises(ValueError):
        ipc.decode_frames(bytearray(b"\xff\xff\xff\xff"))


class FakeClient:
    def __init__(self): self.pubs, self.subs = [], []
    async def publish(self, t, p, qos=0, **k): self.pubs.append((t, p))
    async def subscribe(self, t, qos=0): self.subs.append(t)


@pytest.mark.parametrize("topic,payload", [
    ("mastermeid", {"eventName": "ipcCall", "ipcServiceName": "qinternalservice", "ipcRequest": []}),
    ("mastermeid", {"eventName": "database", "dbOperation": "update", "uri": "x", "contentValues": {}}),
    ("mastermeid", {"eventName": "database", "dbOperation": "read", "uri": "x"}),   # stricter than harness: unneeded
    ("mastermeid", {"eventName": "acStatus"}),
    ("mastermeid", {"eventName": "timeSync"}),
    ("mastermeid", {"eventName": "disconnect"}),
    ("mastermeid", {"eventName": "pingevent", "ipcRequest": [1]}),
    ("other/topic", {"eventName": "pingevent"}),
    ("mastermeid", "not json"),
    ("mastermeid", {"no": "event"}),
])
async def test_iq_guard_blocks_everything_not_readonly_required(topic, payload):
    n = []
    c = FakeClient()
    g = IQ.GuardedPanel(c, "aa:bb:cc:dd:ee:ff", lambda: n.append(1))
    with pytest.raises(OneWayViolation):
        await g.publish(topic, payload if isinstance(payload, (dict, str)) else json.dumps(payload))
    assert c.pubs == [] and n == [1]


def g_connect(g):
    a = IQ.IQRemoteAdapter.__new__(IQ.IQRemoteAdapter); a.remote_client_id = "qolsys-controller-0123456789ab"
    return a.build_connect(g.mac, "10.0.0.2")


def g_ping(g):
    return IQ.IQRemoteAdapter.build_ping(g.mac, "10.0.0.2")


async def test_iq_guard_allows_exact_handshake_set_and_restricts_subs():
    c = FakeClient(); g = IQ.GuardedPanel(c, "aa:bb")
    for ev, f in (("connect_v204", g_connect(g)), ("pingevent", g_ping(g)), ("pair_status_request", {}),
                  ("syncdatabase", {})):
        env = {"requestID": str(uuid.uuid4()), "responseTopic": g.resp_topic, "eventName": ev,
               "remoteMacAddress": "aa:bb", **f}
        await g.publish("mastermeid", env)
    assert [json.loads(p)["eventName"] for _, p in c.pubs] == ["connect_v204", "pingevent", "pair_status_request", "syncdatabase"]
    await g.subscribe_allowed()
    assert c.subs == ["iq2meid", "response_aa:bb"]
    for bad in ("mastermeid", "#", "response_other", "ZWAVE_RESPONSE"):
        with pytest.raises(OneWayViolation):
            await g.subscribe(bad)


class FakeWriter:
    def __init__(self): self.buf = b""
    def write(self, b): self.buf += b
    async def drain(self): pass


@pytest.mark.parametrize("msg", [
    {"action": "ARMING", "arming_type": "DISARM", "partition_id": 0, "usercode": "1234"},
    {"action": "ARMING", "arming_type": "ARM_AWAY"},
    {"action": "ALARM", "alarm_type": "POLICE"},
    {"action": "INFO", "info_type": "SECURE_ARM"},
    {"action": "INFO"}, {}, {"info_type": "SUMMARY"},
])
async def test_c4_guard_blocks_state_changing_and_unneeded(msg):
    n = []
    w = FakeWriter(); g = C4.GuardedC4Writer(w, lambda: n.append(1))
    with pytest.raises(OneWayViolation):
        await g.send(msg)
    assert w.buf == b"" and n == [1]


async def test_c4_guard_allows_only_info_summary():
    w = FakeWriter(); g = C4.GuardedC4Writer(w)
    await g.send(C4.build_info_summary("T"))
    assert json.loads(w.buf)["info_type"] == "SUMMARY" and json.loads(w.buf)["action"] == "INFO"


def _imports(path):
    import ast
    mods = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(("." * n.level) + (n.module or ""))
            mods |= {("." * n.level) + (n.module or "") + "." + a.name for a in n.names}
    return mods


def test_source_audit_topology_and_no_command_builders():
    # adapters never import the qolsys_controller command builders (arm/disarm/ipcCall machinery)
    for f in (SRC / "adapters").glob("*.py"):
        assert not any(m.startswith("qolsys_controller") for m in _imports(f)), f
    # downstream tiers never import adapters nor the sender-side IPC class, and cannot reach a panel
    for d in ("normalizer", "outbound"):
        for f in (SRC / d).glob("*.py"):
            imps = _imports(f)
            assert not any("adapters" in m for m in imps), f
            assert not any(m.endswith("SendOnlyClient") for m in imps), f
    # normalizer never imports the outbound tier and vice versa (no back-reference exists)
    for f in (SRC / "normalizer").glob("*.py"):
        assert not any("outbound" in m for m in _imports(f)), f
    for f in (SRC / "outbound").glob("*.py"):
        assert not any(m.endswith("RecvOnlyConn") or m.endswith("Listener") for m in _imports(f)), f
    # the ONLY code that writes to a panel connection lives in the guarded modules: the two adapters and the
    # pairing guard (br2 pair: the exact three pairing frames, see tests/test_pairing.py)
    writers = [f.name for f in SRC.rglob("*.py") if re.search(r"\._client\.publish|\._w\.write", f.read_text())]
    assert sorted(writers) == ["control4.py", "iq_remote.py", "pairing.py"]
    ipc_src = (SRC / "ipc.py").read_text()
    assert "SHUT_WR" in ipc_src and "SHUT_RD" in ipc_src
