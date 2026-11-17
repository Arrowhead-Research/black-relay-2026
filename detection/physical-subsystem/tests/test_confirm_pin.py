"""Regression tests for the confirming audit (t_pin.py): the pin must fail CLOSED, trust-on-first-use is opt-in, and a
root-run command must leave the pin readable by the service uid."""
import asyncio, io, json, os, pathlib, re, ssl, stat, time
import pytest
from cryptography.hazmat.primitives import serialization
from lib import certs as CT
from br2 import config as C, field as FLD, pin as PIN
from br2.adapters import iq_remote as IQ
from br2.pin import PanelPin, PinMismatch, PinMissing, PinUnreadable, spki_sha256
from test_audit_pin import Panel, pki, ctx_for, connect  # noqa: F401  (fixtures + helpers)

DER = serialization.Encoding.DER
ROOT = pathlib.Path(__file__).resolve().parents[1]


def fp_of(cert):
    return spki_sha256(cert.public_bytes(DER))


@pytest.mark.parametrize("content", [b"{", b'{"spki_sha256":"zz"}', b"", b"[]", b"\xff\xfe\x00", b'{"spki_sha256": 5}',
                                     b'{"other": "x"}'])
@pytest.mark.parametrize("mode", ["required", "tofu"])
def test_a_pin_file_that_exists_but_is_invalid_is_refused_never_re_trusted(pki, content, mode):
    """t_pin 7: a corrupt/empty/bad-hash pin file used to read as 'no pin' and silently re-trust (and overwrite) on
    first use. It must refuse, leave the file alone, say pin_unreadable and send nothing."""
    imp_c, imp_k = CT.issue(pki.ca, "phone")
    imp = Panel(pki.tmp, imp_c, imp_k)
    (pki.dir / "panel_pin.json").write_bytes(content)
    pin = PanelPin(pki.dir, mode=mode)
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, pin), imp.port)
    time.sleep(0.2)
    assert imp.got == [] and pin.last_problem == "pin_unreadable"
    assert (pki.dir / "panel_pin.json").read_bytes() == content          # not overwritten
    with pytest.raises(PinUnreadable):
        pin.stored()
    imp.close()


def test_an_unreadable_pin_file_is_refused_too(pki, monkeypatch):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    (pki.dir / "panel_pin.json").write_text(json.dumps({"spki_sha256": fp_of(c)}))
    real = pathlib.Path.read_bytes
    def deny(self):
        if self.name == "panel_pin.json":
            raise PermissionError(13, "Permission denied")
        return real(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", deny)
    pin = PanelPin(pki.dir, mode="tofu")
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, pin), p.port)
    assert pin.last_problem == "pin_unreadable" and p.got == []
    p.close()


def test_only_a_truly_absent_file_means_no_pin_and_required_mode_refuses(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    pin = PanelPin(pki.dir)                                            # default mode: required
    assert pin.mode == "required" and pin.stored() is None
    with pytest.raises(ssl.SSLError):
        connect(ctx_for(pki, pin), p.port)
    time.sleep(0.2)
    assert p.got == [] and pin.last_problem == "pin_missing"
    assert not (pki.dir / "panel_pin.json").exists()                   # nothing recorded automatically
    with pytest.raises(PinMissing):
        pin.check(c.public_bytes(DER))
    p.close()


def test_tofu_is_opt_in_and_records_exactly_once(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    pin = PanelPin(pki.dir, mode="tofu")
    assert connect(ctx_for(pki, pin), p.port)
    assert PanelPin(pki.dir).stored() == fp_of(c)                      # a later required-mode process finds it
    assert connect(ctx_for(pki, PanelPin(pki.dir)), p.port)
    p.close()


def test_preset_mode_is_implied_by_the_hash_and_validated():
    assert PanelPin("/nonexistent", "ab" * 32).mode == "preset"
    assert PanelPin("/nonexistent", "ab" * 32, mode="tofu").mode == "preset"
    with pytest.raises(ValueError):
        PanelPin("/nonexistent", mode="preset")
    with pytest.raises(ValueError):
        PanelPin("/nonexistent", mode="sometimes")


def test_a_preset_ignores_even_a_corrupt_file(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    (pki.dir / "panel_pin.json").write_text("{")
    assert connect(ctx_for(pki, PanelPin(pki.dir, fp_of(c))), p.port)
    p.close()


def test_config_default_is_required_and_only_the_env_var_of_the_harness_script_changes_it(monkeypatch):
    monkeypatch.delenv("BR2_IQ_PIN_MODE", raising=False)
    assert C.parse({})["iq_remote"]["pin_mode"] == "required"
    assert C.parse({})["iq_remote"]["owner"] == "10001:10000"
    monkeypatch.setenv("BR2_IQ_PIN_MODE", "tofu")
    assert C.parse({})["iq_remote"]["pin_mode"] == "tofu"
    sh = (ROOT / "harness_start.sh").read_text()
    assert "export BR2_IQ_PIN_MODE=tofu" in sh and "export BR2_IQ_PIN_ACCEPT_ROTATION=1" in sh
    for f in list((ROOT / "deploy").rglob("*.yml")) + list((ROOT / "deploy").rglob("*.yaml")) + list((ROOT / "docker").glob("Dockerfile.*")):
        t = f.read_text()
        assert not re.search(r"pin_mode:\s*tofu|BR2_IQ_PIN_MODE|pin_accept_rotation:\s*true", t), f


@pytest.mark.parametrize("which", ["pin_unreadable", "pin_missing"])
def test_adapter_reports_the_refusal_as_the_link_reason_and_sends_nothing(pki, tmp_path, which):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    if which == "pin_unreadable":
        (pki.dir / "panel_pin.json").write_text("{")
    cfg = C.parse({"iq_remote": {"panel_ip": "127.0.0.1", "panel_port": p.port, "pki_dir": str(pki.dir), "plugin_ip": "127.0.0.1"}})
    a = IQ.IQRemoteAdapter(cfg, str(tmp_path / "ipc"))
    with pytest.raises(PinMismatch):
        asyncio.run(a.session())
    assert a.down_reason == which and a.guard is None
    time.sleep(0.2)
    assert p.got == []
    p.close()


def test_a_restart_never_re_trusts_with_a_read_only_pki_dir(pki, monkeypatch):
    """t_pin 8: a required-mode service with no recorded pin refuses on every start (nothing held in memory)."""
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    for _ in range(2):
        with pytest.raises(ssl.SSLError):
            connect(ctx_for(pki, PanelPin(pki.dir)), p.port)
    assert p.got == [] and not (pki.dir / "panel_pin.json").exists()
    p.close()


def test_snapshot_without_a_pin_says_to_run_pair_or_repin(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    st = {"panel_ip": "127.0.0.1", "port": p.port, "pki_dir": str(pki.dir), "plugin_ip": "127.0.0.1", "ca_file": None}
    with pytest.raises(FLD.FieldError, match="br2 pair"):
        asyncio.run(FLD.PanelSession(st).__aenter__())
    st["pin_mode"] = "tofu"                                            # what `br2 pair` sets for its own verification
    (pki.dir / "panel_pin.json").write_text("")
    with pytest.raises(FLD.FieldError, match="UNREADABLE"):
        asyncio.run(FLD.PanelSession(st).__aenter__())
    assert p.got == []
    p.close()


def test_repin_replaces_a_damaged_pin_file(pki):
    c, k = CT.issue(pki.ca, "panel"); p = Panel(pki.tmp, c, k)
    (pki.dir / "panel_pin.json").write_text("{")
    st = {"panel_ip": "127.0.0.1", "port": p.port, "pki_dir": str(pki.dir), "ca_file": None, "spki_preset": None}
    err = io.StringIO()
    assert asyncio.run(FLD.run_repin(st, yes=True, err=err)) == 0
    assert "UNREADABLE" in err.getvalue() and PanelPin(pki.dir).stored() == fp_of(c)
    p.close()


# ---------------------------------------------------------------- ownership of what a root-run command writes
def _as_root(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((str(path), uid, gid)))
    return calls


def test_pin_written_as_root_is_handed_to_the_service_uid_by_default(pki, monkeypatch):
    calls = _as_root(monkeypatch)
    PanelPin(pki.dir).persist("ab" * 32, "repin")
    assert [(pathlib.Path(p).name, u, g) for p, u, g in calls] == [("panel_pin.json.tmp", 10001, 10000)]
    assert stat.S_IMODE(os.stat(pki.dir / "panel_pin.json").st_mode) == 0o600


def test_owner_is_configurable_or_the_directorys(pki, monkeypatch):
    calls = _as_root(monkeypatch)
    PanelPin(pki.dir, owner="1234:5678").persist("ab" * 32, "repin")
    assert calls[-1][1:] == (1234, 5678)
    calls.clear()
    PanelPin(pki.dir, owner="dir").persist("cd" * 32, "repin")
    st = os.stat(pki.dir)
    assert calls[-1][1:] == (st.st_uid, st.st_gid)
    with pytest.raises(ValueError):
        PIN.parse_owner("nonsense")


def test_nothing_is_chowned_when_not_root(pki, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "chown", lambda *a: calls.append(a))
    PanelPin(pki.dir).persist("ab" * 32, "repin")
    assert calls == []


def test_cli_hands_pair_and_repin_to_the_configured_owner_when_root(tmp_path, monkeypatch):
    from br2 import cli, pairing
    seen = {}
    async def fake_pair(st, **kw):
        seen["pair"] = (kw["owner"], st["pin_mode"], st["pin_source"]); return 0
    async def fake_repin(st, yes=False):
        seen["repin"] = st["owner"]; return 0
    monkeypatch.setattr(pairing, "run_pair", fake_pair); monkeypatch.setattr(FLD, "run_repin", fake_repin)
    cfg = tmp_path / "c.yaml"; cfg.write_text("iq_remote:\n  panel_ip: 127.0.0.1\n  pki_dir: /x\n")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    for argv in (["pair", "--config", str(cfg)], ["repin", "--config", str(cfg)]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 0
    assert seen["pair"] == ("10001:10000", "tofu", "pair") and seen["repin"] == "10001:10000"
    with pytest.raises(SystemExit):
        cli.main(["pair", "--config", str(cfg), "--owner", "7:8"])
    assert seen["pair"][0] == "7:8"
    monkeypatch.setattr(os, "geteuid", lambda: 1000)                   # not root: nothing to hand over
    with pytest.raises(SystemExit):
        cli.main(["pair", "--config", str(cfg)])
    assert seen["pair"][0] is None


def test_chown_tree_follows_the_owner_spec(tmp_path, monkeypatch):
    from br2 import pairing
    pki_dir = tmp_path / "pki"; folder = pki_dir / "f2163e1a2b3c"; folder.mkdir(parents=True)
    (folder / "a.key").write_text("x"); (pki_dir / "panel_pin.json").write_text("{}")
    calls = []
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((pathlib.Path(p).name, u, g)))
    pairing.chown_tree(folder, "10001:10000")
    assert {c[0] for c in calls} == {"f2163e1a2b3c", "pki", "a.key", "panel_pin.json"} and {c[1:] for c in calls} == {(10001, 10000)}
