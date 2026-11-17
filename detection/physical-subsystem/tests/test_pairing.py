"""`br2 pair`: the write fence around the pairing handshake, and the whole exchange against a fake panel.

The harness panel simulator does not implement pairing (it starts from an already-paired PKI), so the exchange is
exercised here against a small in-process fake panel; `br2 snapshot` / `br2 watch` against the simulator are in
tests/integration/test_field_commands.py."""
import asyncio, json, os, pathlib, re, ssl, stat, uuid
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from lib import certs as CT
from br2 import pairing as P, field as FLD
from br2.adapters import iq_remote as IQ
from br2.adapters.base import OneWayViolation

SRC = pathlib.Path(P.__file__).resolve().parent


def mk_guard(tmp_path):
    ident = P.create_identity(tmp_path / "pki", key_size=2048)
    return ident, P.PairGuard(ident.mac, ident.path(".csr").read_bytes(), "192.168.1.9", 51000)


def frames(g):
    return [len(g.random_mac).to_bytes(2, "big") + g.random_mac.encode(), g.csr_pem, b"sent"]


# ------------------------------------------------------------------ the guard
def test_guard_allows_exactly_the_three_pairing_frames_in_order(tmp_path):
    _, g = mk_guard(tmp_path)
    assert [g.check_frame(f) for f in frames(g)] == ["mac", "csr", "sent"]
    assert g.sent == ["mac", "csr", "sent"]
    for extra in (b"sent", b"anything", frames(g)[0]):          # nothing more after `sent`
        with pytest.raises(OneWayViolation):
            g.check_frame(extra)


@pytest.mark.parametrize("bad", [
    b"sent",                                                      # out of order
    b"\x00\x11f2:16:3e:00:00:00",                                 # someone else's identity
    b"\x00\x11" + b"f2:16:3e:aa:bb:cc",
    b"", b"\x00", b"disconnect", b'{"eventName":"ipcCall"}', b"x" * 5000,
])
def test_guard_blocks_any_other_first_frame(tmp_path, bad):
    ident, g = mk_guard(tmp_path)
    if bad == b"\x00\x11" + ident.mac.encode():
        pytest.skip("that is the real frame")
    n = []
    g._on_violation = lambda: n.append(1)
    with pytest.raises(OneWayViolation):
        g.check_frame(bad)
    assert g.sent == [] and n == [1]


def test_guard_csr_must_be_byte_exact_and_non_bytes_are_refused(tmp_path):
    _, g = mk_guard(tmp_path)
    g.check_frame(frames(g)[0])
    with pytest.raises(OneWayViolation):
        g.check_frame(g.csr_pem + b"\n")
    with pytest.raises(OneWayViolation):
        g.check_frame(g.csr_pem.replace(b"REQUEST", b"CERTIFICATE"))
    with pytest.raises(OneWayViolation):
        g.check_frame("sent")                                      # str, not bytes
    assert g.check_frame(g.csr_pem) == "csr"


def test_guard_refuses_to_exist_around_a_private_key_or_odd_mac():
    with pytest.raises(ValueError):
        P.PairGuard("aa:bb:cc:dd:ee:ff", P.CSR_BEGIN + b"\n")
    with pytest.raises(ValueError):
        P.PairGuard("f2:16:3e:00:00:00", b"-----BEGIN " b"PRIVATE KEY-----\nxx\n-----END " b"PRIVATE KEY-----\n")


def test_guard_new_exchange_resets_order_but_is_bounded(tmp_path):
    _, g = mk_guard(tmp_path)
    for _ in range(P.MAX_EXCHANGES):
        g.new_exchange()
        for f in frames(g):
            g.check_frame(f)
    with pytest.raises(OneWayViolation):
        g.new_exchange()


def test_guard_mdns_only_the_pairing_service_with_our_address(tmp_path):
    _, g = mk_guard(tmp_path)
    g.check_mdns(P.MDNS_TYPE, P.MDNS_NAME, "192.168.1.9", 51000)
    for args in (("_mqtt._tcp.local.", P.MDNS_NAME, "192.168.1.9", 51000),
                 (P.MDNS_TYPE, "Evil._http._tcp.local.", "192.168.1.9", 51000),
                 (P.MDNS_TYPE, P.MDNS_NAME, "10.0.0.5", 51000), (P.MDNS_TYPE, P.MDNS_NAME, "192.168.1.9", 8883)):
        with pytest.raises(OneWayViolation):
            g.check_mdns(*args)


async def test_guarded_writer_blocks_before_bytes_reach_the_socket(tmp_path):
    class W:
        def __init__(s): s.buf = b""
        def write(s, b): s.buf += b
        async def drain(s): pass
    _, g = mk_guard(tmp_path)
    w = W(); out = P.GuardedPairWriter(w, g)
    with pytest.raises(OneWayViolation):
        await out.send(b"anything else")
    assert w.buf == b""
    for f in frames(g):
        await out.send(f)
    assert w.buf == b"".join(frames(g))
    assert not hasattr(out, "write") and not hasattr(out, "writer")


def test_the_mqtt_leg_of_pairing_uses_the_adapters_exact_allow_list():
    """No MQTT message type was added for pairing: the session helper builds a GuardedPanel and the allow-list is
    unchanged (connect_v204, pingevent, pair_status_request, syncdatabase)."""
    assert set(IQ.ALLOWED_EVENTS) == {"connect_v204", "pingevent", "pair_status_request", "syncdatabase"}
    src = (SRC / "field.py").read_text()
    assert "IQ.GuardedPanel(" in src and "harden_paho(" in src
    assert not re.search(r"\.publish\(|\._client\b|\.write\(", src), "field.py must not have its own write path"


def test_source_audit_pairing_has_one_writer_and_no_library_command_builders():
    text = (SRC / "pairing.py").read_text()
    assert len(re.findall(r"\._w\.write\(", text)) == 1                    # only GuardedPairWriter.send
    import ast
    mods = {n.module or "" for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if m.startswith(("qolsys_controller", "aiomqtt"))], mods
    assert not re.search(r"\.publish\(", text)
    # every send on the pairing socket goes through the guarded writer
    assert all(l.strip().startswith("await out.send(") for l in text.splitlines() if "out.send(" in l and "def send" not in l)
    # key material is never printed or logged
    for m in re.finditer(r"(print|log\.\w+)\((.*)", text):
        assert not re.search(r"private_bytes|read_bytes|\.key\b|key_pem", m.group(2)), m.group(0)


# ------------------------------------------------------------------ PKI files
def mode(p): return stat.S_IMODE(os.stat(p).st_mode)


def test_create_identity_modes_layout_and_find_pki_compat(tmp_path):
    ident = P.create_identity(tmp_path / "pki")
    assert re.fullmatch(r"f2163e[0-9a-f]{6}", ident.id12) and P.RANDOM_MAC_RE.match(ident.mac)
    assert mode(tmp_path / "pki") == 0o700 and mode(ident.folder) == 0o700
    for ext in (".key", ".cer", ".csr"):
        assert mode(ident.path(ext)) == 0o600
    key = serialization.load_pem_private_key(ident.path(".key").read_bytes(), None)
    csr = x509.load_pem_x509_csr(ident.path(".csr").read_bytes())
    assert csr.is_signature_valid and csr.public_key().public_numbers() == key.public_key().public_numbers()
    assert P.preflight(tmp_path / "pki").id12 == ident.id12                # unfinished: resumable
    # no leftover temp files
    assert not [p for p in ident.folder.iterdir() if p.name.endswith(".tmp")]


def test_preflight_refuses_when_already_paired_or_ambiguous(tmp_path):
    root = tmp_path / "pki"
    ident = P.create_identity(root)
    for ext in (".secure", ".qolsys"):
        ident.path(ext).write_bytes(b"x")
    with pytest.raises(P.PairingError, match="already paired"):
        P.preflight(root)
    P.create_identity(root).path(".key")                                  # a second identity
    with pytest.raises(P.PairingError, match="identity folders"):
        P.preflight(root)


# ------------------------------------------------------------------ the whole exchange against a fake panel
class FakePanel:
    """Plays the panel's side of the pairing exchange (as documented by the library) against our server."""

    def __init__(self, port, ca, sign_key=None, mac="00:11:22:33:44:55"):
        self.port, self.ca, self.sign_key, self.mac = port, ca, sign_key, mac
        self.seen: list[bytes] = []

    async def run(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        for _ in range(100):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.port, ssl=ctx)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("pairing server never came up")
        w.write(len(self.mac.encode()).to_bytes(2, "big") + self.mac.encode()); await w.drain()
        mac_len = int.from_bytes(await r.readexactly(2), "big")
        panel_view_mac = (await r.readexactly(mac_len)).decode()
        blob = await r.readuntil(b"sent")
        self.seen = [panel_view_mac.encode(), blob]
        csr = x509.load_pem_x509_csr(blob[:-4])
        pub = csr.public_key() if self.sign_key is None else self.sign_key.public_key()
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.ca.cert.subject).public_key(pub)
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365)).sign(self.ca.key, hashes.SHA256()))
        w.write(CT.cert_pem(cert)); w.write(self.ca.pem); await w.drain()
        w.close()
        return panel_view_mac


async def pair_with_fake_panel(tmp_path, **fake):
    from lib.mosq import free_port
    port = free_port()
    ca = CT.make_ca("Fake Qolsys Panel CA", bc_critical=False)
    panel = FakePanel(port, ca, **fake)
    st = {"panel_ip": "127.0.0.1", "port": 8883, "pki_dir": str(tmp_path / "pki"), "plugin_ip": "127.0.0.1", "ca_file": None}
    task = asyncio.create_task(panel.run())
    try:
        rc = await asyncio.wait_for(P.run_pair(st, timeout_s=20, pairing_port=port, advertise=False, verify=False,
                                               key_size=2048), 30)
    finally:
        r = await asyncio.gather(task, return_exceptions=True)
    return rc, panel, ca, r[0]


async def test_pair_end_to_end_against_a_fake_panel_stores_pki_with_strict_modes(tmp_path):
    rc, panel, ca, panel_view = await pair_with_fake_panel(tmp_path)
    assert rc == 0
    (folder,) = P.list_identities(tmp_path / "pki")
    ident = P.Identity(folder.name, folder)
    assert panel_view == ident.mac                                         # panel was told our random MAC
    assert panel.seen[1].startswith(b"-----BEGIN CERTIFICATE REQUEST-----") and panel.seen[1].endswith(b"sent")
    assert P.is_complete(folder)
    assert mode(tmp_path / "pki") == 0o700 and mode(folder) == 0o700
    for ext in (".key", ".cer", ".csr", ".secure", ".qolsys"):
        assert mode(ident.path(ext)) == 0o600, ext
    secure = x509.load_pem_x509_certificate(ident.path(".secure").read_bytes())
    assert secure.issuer == ca.cert.subject
    # the stored identity is exactly what the adapter's own loader accepts
    id12, key, sec, ca_path = IQ.find_pki(str(tmp_path / "pki"), None)
    assert id12 == ident.id12 and sec.endswith(".secure") and ca_path.endswith(".qolsys")
    IQ.tls_context(key, sec, ca_path)
    # second run refuses: do not overwrite a working pairing
    with pytest.raises(P.PairingError, match="already paired"):
        await P.run_pair({"panel_ip": "127.0.0.1", "port": 8883, "pki_dir": str(tmp_path / "pki"),
                          "plugin_ip": "127.0.0.1", "ca_file": None}, timeout_s=1, advertise=False, verify=False)


async def test_pair_refuses_a_certificate_for_a_different_key_and_stores_nothing(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(P.PairingError, match="not for our key"):
        await pair_with_fake_panel(tmp_path, sign_key=other)
    (folder,) = P.list_identities(tmp_path / "pki")
    assert not (folder / f"{folder.name}.secure").exists() and not (folder / f"{folder.name}.qolsys").exists()
    assert P.preflight(tmp_path / "pki").id12 == folder.name              # still resumable


async def test_pair_times_out_with_an_actionable_message(tmp_path):
    from lib.mosq import free_port
    with pytest.raises(P.PairingError, match="Pair was tapped"):
        await P.run_pair({"panel_ip": "127.0.0.1", "port": 8883, "pki_dir": str(tmp_path / "pki"),
                          "plugin_ip": "127.0.0.1", "ca_file": None}, timeout_s=0.5, pairing_port=free_port(),
                         advertise=False, verify=False)


async def test_pairing_server_ignores_connections_from_other_addresses(tmp_path):
    ident, g = mk_guard(tmp_path)
    srv = P.PairingServer(ident, g, "127.0.0.1", 0, panel_ip="10.9.9.9")      # we expect another panel address
    await srv.start()
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        r, w = await asyncio.open_connection("127.0.0.1", srv.bound_port, ssl=ctx)
        w.write(b"\x00\x11" + b"00:11:22:33:44:55"); await w.drain()
        assert await asyncio.wait_for(r.read(10), 3) == b""                   # closed without a single byte from us
        assert g.sent == [] and not srv.done.is_set()
        w.close()
    finally:
        await srv.stop()
