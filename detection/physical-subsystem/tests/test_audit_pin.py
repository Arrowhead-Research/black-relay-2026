"""Regression tests for audit finding t2 (panel identity): the IQ link must pin the panel's leaf certificate, not just
trust the panel CA that also signs every paired device's certificate."""
import asyncio, json, os, socket, ssl, stat, threading, time
import pytest
from cryptography.hazmat.primitives import serialization
from lib import certs as CT
from br2 import config as C, field as FLD
from br2.adapters import iq_remote as IQ
from br2.pin import PanelPin, PinMismatch, spki_sha256, norm_hex

ID12 = "f2163e1a2b3c"


class Panel:
    """A TLS listener that presents `cert/key` and records every application byte it receives after the handshake."""
    def __init__(self, tmp, cert, key):
        self.got, self.handshakes = [], 0
        sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cp, kp = tmp / f"srv{id(self)}.pem", tmp / f"srv{id(self)}.key"
        cp.write_bytes(CT.cert_pem(cert)); kp.write_bytes(CT.key_pem(key))
        sc.load_cert_chain(cp, kp)
        self.ls = socket.socket(); self.ls.bind(("127.0.0.1", 0)); self.ls.listen(5)
        self.port = self.ls.getsockname()[1]
        def serve():
            while True:
                try:
                    c, _ = self.ls.accept()
                except OSError:
                    return
                try:
                    s = sc.wrap_socket(c, server_side=True); self.handshakes += 1
                    s.settimeout(1.0)
                    try:
                        d = s.recv(4096)
                        if d:
                            self.got.append(d)
                            s.sendall(b"\x20\x02\x00\x00")          # CONNACK, in case it is MQTT
                    except (OSError, ssl.SSLError):
                        pass
                    s.close()
                except Exception:
                    pass
        threading.Thread(target=serve, daemon=True).start()
    def close(self): self.ls.close()


@pytest.fixture()
def pki(tmp_path):
    ca = CT.make_ca("panel-ca", bc_critical=False)
    ccert, ckey = CT.issue(ca, "ours", server=False, client=True, ips=(), dns=())
    d = tmp_path / "pki"; f = d / ID12; f.mkdir(parents=True)
    (f / f"{ID12}.key").write_bytes(CT.key_pem(ckey)); os.chmod(f / f"{ID12}.key", 0o600)
    (f / f"{ID12}.secure").write_bytes(CT.cert_pem(ccert)); (f / f"{ID12}.qolsys").write_bytes(ca.pem)
    return type("P", (), {"ca": ca, "dir": d, "tmp": tmp_path})


def ctx_for(pki, pin):
    f = pki.dir / ID12
    return IQ.tls_context(str(f / f"{ID12}.key"), str(f / f"{ID12}.secure"), str(f / f"{ID12}.qolsys"), pin)


def connect(ctx, port):
    with socket.create_connection(("127.0.0.1", port), 3) as r:
        with ctx.wrap_socket(r) as s:
            s.sendall(b"HELLO")                     # stands for the MQTT CONNECT
            return s.version()


def test_spki_extraction_matches_cryptography(pki):
    cert, key = CT.issue(pki.ca, "panel")
    want = __import__("hashlib").sha256(cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    assert spki_sha256(cert.public_bytes(serialization.Encoding.DER)) == want


def test_phone_style_cert_signed_by_the_panel_ca_is_refused_and_nothing_is_sent(pki):
    """t2: a certificate signed by the panel CA (here: a paired device's) used to be ACCEPTED as the panel."""
    good_c, good_k = CT.issue(pki.ca, "panel")
    genuine = Panel(pki.tmp, good_c, good_k)
    pin = PanelPin(pki.dir, mode="tofu")
    assert connect(ctx_for(pki, pin), genuine.port)                    # first use: recorded (tofu is opt-in)
    assert pin.stored() == spki_sha256(good_c.public_bytes(serialization.Encoding.DER))
    assert stat.S_IMODE(os.stat(pin.path).st_mode) == 0o600
    phone_c, phone_k = CT.issue(pki.ca, "phone", server=False, client=False)    # no EKU: OpenSSL accepts it
    impostor = Panel(pki.tmp, phone_c, phone_k)
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, pin), impostor.port)
    time.sleep(0.3)
    assert impostor.got == [] and pin.mismatches == 1                  # not one application byte reached it
    assert pin.stored() == spki_sha256(good_c.public_bytes(serialization.Encoding.DER))   # pin not replaced
    assert genuine.got == [b"HELLO"]
    # the unpinned context (what the code did before) accepts the impostor: this is the hole
    assert connect(ctx_for(pki, None), impostor.port)
    genuine.close(); impostor.close()


def test_strict_preset_overrides_tofu_and_a_wrong_preset_refuses_the_real_panel(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    fp = spki_sha256(c.public_bytes(serialization.Encoding.DER))
    assert connect(ctx_for(pki, PanelPin(pki.dir, "sha256:" + ":".join(fp[i:i + 2] for i in range(0, 64, 2)))), p.port)
    assert not (pki.dir / "panel_pin.json").exists()                  # strict mode never records
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, PanelPin(pki.dir, "00" * 32)), p.port)
    with pytest.raises(ValueError):
        PanelPin(pki.dir, "not-a-hash")
    p.close()


def test_rotation_is_refused_by_default_and_followed_only_with_the_flag(pki):
    c1, k1 = CT.issue(pki.ca, "panel"); c2, k2 = CT.issue(pki.ca, "panel")      # same name, new key
    p1, p2 = Panel(pki.tmp, c1, k1), Panel(pki.tmp, c2, k2)
    pin = PanelPin(pki.dir, mode="tofu"); connect(ctx_for(pki, pin), p1.port)
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, pin), p2.port)
    assert p2.got == []
    pin2 = PanelPin(pki.dir, accept_rotation=True)
    assert connect(ctx_for(pki, pin2), p2.port)
    assert pin2.stored() == spki_sha256(c2.public_bytes(serialization.Encoding.DER))
    # accept_rotation does not weaken a strict preset
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, PanelPin(pki.dir, spki_sha256(c1.public_bytes(serialization.Encoding.DER)), accept_rotation=True)), p2.port)
    p1.close(); p2.close()


def test_config_flag_and_env_default_off(monkeypatch):
    monkeypatch.delenv("BR2_IQ_PIN_ACCEPT_ROTATION", raising=False)
    assert C.parse({})["iq_remote"]["pin_accept_rotation"] is False
    assert C.parse({})["iq_remote"]["panel_spki_sha256"] is None
    monkeypatch.setenv("BR2_IQ_PIN_ACCEPT_ROTATION", "1")
    assert C.parse({})["iq_remote"]["pin_accept_rotation"] is True


def test_unwritable_pki_dir_keeps_the_pin_in_memory(pki, monkeypatch):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    pin = PanelPin(pki.dir, mode="tofu")
    real_open = os.open
    def ro(path, *a, **k):
        if str(path).endswith("panel_pin.json.tmp"):
            raise PermissionError("read-only file system")
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", ro)
    connect(ctx_for(pki, pin), p.port)
    assert not (pki.dir / "panel_pin.json").exists() and pin.stored() is not None
    p.close()


def test_adapter_reports_pin_mismatch_as_the_link_reason_and_sends_nothing(pki, tmp_path):
    good_c, good_k = CT.issue(pki.ca, "panel"); imp_c, imp_k = CT.issue(pki.ca, "phone")
    genuine, impostor = Panel(pki.tmp, good_c, good_k), Panel(pki.tmp, imp_c, imp_k)
    PanelPin(pki.dir).persist(spki_sha256(good_c.public_bytes(serialization.Encoding.DER)), "test")
    cfg = C.parse({"iq_remote": {"panel_ip": "127.0.0.1", "panel_port": impostor.port, "pki_dir": str(pki.dir),
                                 "plugin_ip": "127.0.0.1"}})
    a = IQ.IQRemoteAdapter(cfg, str(tmp_path / "ipc"))
    with pytest.raises(Exception) as ei:
        asyncio.run(a.session())
    assert isinstance(ei.value, PinMismatch)
    assert a.down_reason == "pin_mismatch" and a.stats["pin_mismatches"] == 1
    time.sleep(0.3)
    assert impostor.got == [] and a.guard is None
    genuine.close(); impostor.close()


def test_normalizer_exposes_the_link_reason_in_health():
    from br2.normalizer.core import Engine, PRIMARY
    e = Engine(C.parse({"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}}))
    e.handle({"t": "link", "src": PRIMARY, "up": False, "reason": "pin_mismatch"})
    assert e.health()["link_reasons"] == {PRIMARY: "pin_mismatch"}
    e.handle({"t": "link", "src": PRIMARY, "up": False, "reason": "bad reason; <script>"})
    assert e.health()["link_reasons"] == {}
    from br2.outbound.state import OutState
    st = OutState(); st.apply({"t": "health", "health": {"link_reasons": {PRIMARY: "pin_mismatch"}}, "status": "offline"})
    assert st.health_view()["link_reasons"] == {PRIMARY: "pin_mismatch"}


# ---------------------------------------------------------------- br2 repin
def test_repin_prints_old_and_new_and_needs_yes(pki, capsys):
    c1, k1 = CT.issue(pki.ca, "panel"); c2, k2 = CT.issue(pki.ca, "panel")
    p1, p2 = Panel(pki.tmp, c1, k1), Panel(pki.tmp, c2, k2)
    pin = PanelPin(pki.dir); pin.persist(spki_sha256(c1.public_bytes(serialization.Encoding.DER)), "test")
    st = {"panel_ip": "127.0.0.1", "port": p2.port, "pki_dir": str(pki.dir), "ca_file": None, "spki_preset": None}
    import io
    err = io.StringIO()
    assert asyncio.run(FLD.run_repin(st, yes=False, err=err)) == 2
    from br2.pin import fmt
    txt = err.getvalue()
    assert fmt(spki_sha256(c1.public_bytes(serialization.Encoding.DER))) in txt and fmt(spki_sha256(c2.public_bytes(serialization.Encoding.DER))) in txt
    assert PanelPin(pki.dir).stored() == spki_sha256(c1.public_bytes(serialization.Encoding.DER))     # unchanged
    assert p2.got == []                                         # handshake only: no application bytes sent
    assert asyncio.run(FLD.run_repin(st, yes=True, err=io.StringIO())) == 0
    assert PanelPin(pki.dir).stored() == spki_sha256(c2.public_bytes(serialization.Encoding.DER))
    assert stat.S_IMODE(os.stat(pki.dir / "panel_pin.json").st_mode) == 0o600
    assert p2.got == []
    p1.close(); p2.close()


def test_repin_with_a_config_preset_warns_that_the_preset_still_wins(pki):
    c1, k1 = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c1, k1)
    st = {"panel_ip": "127.0.0.1", "port": p.port, "pki_dir": str(pki.dir), "ca_file": None, "spki_preset": "ab" * 32}
    import io
    err = io.StringIO()
    assert asyncio.run(FLD.run_repin(st, yes=True, err=err)) == 4 and "takes precedence" in err.getvalue()
    p.close()


def test_cli_has_repin_subcommand():
    from br2 import cli
    src = open(cli.__file__).read()
    assert '"repin"' in src and "--yes" in src


def test_snapshot_session_pins_the_panel_and_a_different_panel_is_refused(pki):
    """PanelSession (snapshot/watch/pair verification) uses the same pin."""
    c1, k1 = CT.issue(pki.ca, "panel"); imp_c, imp_k = CT.issue(pki.ca, "phone")
    imp = Panel(pki.tmp, imp_c, imp_k)
    PanelPin(pki.dir).persist(spki_sha256(c1.public_bytes(serialization.Encoding.DER)), "test")
    st = {"panel_ip": "127.0.0.1", "port": imp.port, "pki_dir": str(pki.dir), "plugin_ip": "127.0.0.1", "ca_file": None}
    with pytest.raises(FLD.FieldError, match="PIN MISMATCH"):
        asyncio.run(FLD.PanelSession(st).__aenter__())
    assert imp.got == []
    imp.close()
