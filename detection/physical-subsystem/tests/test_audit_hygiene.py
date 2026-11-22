"""Regression tests for the audit's hygiene items: key permissions, compose hardening, image/dependency pinning,
no machine-specific paths, nftables PANEL_IP marker, the reference pairing tool."""
import os, pathlib, re, stat
import pytest
import yaml
from br2 import config as C
from br2.keyperm import require_private_key, check_startup, KeyPermissionError

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())


# ---------------------------------------------------------------- key permissions
@pytest.mark.parametrize("mode,ok", [(0o600, True), (0o400, True), (0o640, False), (0o604, False), (0o660, False), (0o644, False)])
def test_private_key_modes(tmp_path, mode, ok):
    k = tmp_path / "x.key"; k.write_text("k"); os.chmod(k, mode)
    if ok:
        require_private_key(k, "test key")
    else:
        with pytest.raises(KeyPermissionError, match="chmod 600"):
            require_private_key(k, "test key")


def test_services_refuse_to_start_on_a_loose_key_but_only_warn_for_loose_secrets(tmp_path, caplog):
    k = tmp_path / "api.key"; k.write_text("k"); os.chmod(k, 0o644)
    pw = tmp_path / "pw"; pw.write_text("s"); os.chmod(pw, 0o644)
    cfg = C.parse({"api": {"tls_key": str(k)}, "mqtt": {"password_file": str(pw)}})
    with pytest.raises(KeyPermissionError):
        check_startup("outbound", cfg)
    os.chmod(k, 0o600)
    check_startup("outbound", cfg)                       # key fine; the 0644 password file is only logged
    assert "readable by everyone" in caplog.text
    tls = {"keyfile": str(k)}; os.chmod(k, 0o640)
    with pytest.raises(KeyPermissionError):
        check_startup("fusion", C.parse({"fusion": {"tls": tls}}))


def test_iq_adapter_refuses_a_group_readable_pairing_key(tmp_path):
    from br2 import cli
    d = tmp_path / "pki" / "f2163e1a2b3c"; d.mkdir(parents=True)
    for e in (".key", ".secure", ".qolsys"):
        (d / f"f2163e1a2b3c{e}").write_text("x")
    os.chmod(d / "f2163e1a2b3c.key", 0o644)
    cfg = C.parse({"iq_remote": {"panel_ip": "1.1.1.1", "pki_dir": str(tmp_path / "pki")}})
    with pytest.raises(SystemExit, match="refusing to start iq-remote"):
        cli.run_service("iq-remote", cfg)


# ---------------------------------------------------------------- compose
def test_every_service_is_hardened_and_non_root():
    for name, s in COMPOSE["services"].items():
        assert s.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in s.get("security_opt", []), name
        assert s.get("read_only") is True, name
        # the one-shot root helpers: init-perms sets volume ownership, bootstrap hands generated files to their uids
        if name not in ("init-perms", "bootstrap"):
            u = str(s.get("user", "0"))
            assert u.split(":")[0] not in ("0", "root"), f"{name} runs as root"
    assert COMPOSE["services"]["mosquitto"]["user"] == "1883:1883"


def test_fusion_mounts_the_store_read_only_and_has_its_own_state_volume():
    f = COMPOSE["services"]["fusion"]["volumes"]
    assert "store:/data:ro" in f
    assert any(v.startswith("fusion-state:/state") and not v.endswith(":ro") for v in f)
    assert not any(v.startswith("out-state:") for v in f)
    assert "fusion-state" in COMPOSE["volumes"]
    assert "store:/data:ro" in COMPOSE["services"]["outbound"]["volumes"]
    assert "fusion-state" in " ".join(COMPOSE["services"]["init-perms"]["volumes"])


def test_images_are_pinned_by_digest():
    for name in ("init-perms", "mosquitto"):
        assert re.search(r"@sha256:[0-9a-f]{64}$", COMPOSE["services"][name]["image"]), name
    for p in (ROOT / "docker").glob("Dockerfile.*"):
        assert re.search(r"^ARG PY_BASE=\S+@sha256:[0-9a-f]{64}$", p.read_text(), re.M), p.name
    assert "@sha256:" in (ROOT / "deploy/scripts/gen-certs.sh").read_text()


def test_dependencies_are_pinned_exactly_and_the_images_install_hashed_locks_only():
    for p in (ROOT / "docker").glob("requirements-*.txt"):
        if p.name.endswith(".lock.txt"):
            body = p.read_text()
            pins = re.findall(r"^([A-Za-z0-9_.\-]+)==([\w.]+) \\$", body, re.M)
            assert pins, p.name
            assert body.count("--hash=sha256:") >= len(pins), p.name       # every pin carries at least one hash
            for chunk in re.split(r"\n(?=\S)", body.split("\n", 2)[2].strip()):
                assert "--hash=sha256:" in chunk, chunk.split("\n")[0]
        else:
            for ln in p.read_text().splitlines():
                if ln.strip() and not ln.startswith("#"):
                    assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[\w.]+", ln.strip()), f"{p.name}: {ln!r} is not an exact pin"
    for p in (ROOT / "docker").glob("Dockerfile.*"):
        t = p.read_text()
        assert "--require-hashes --no-deps -r /tmp/req.txt" in t and ".lock.txt" in t, p.name


# ---------------------------------------------------------------- tree hygiene
def test_no_hardcoded_home_claude_path_anywhere_in_the_tree():
    needle = "/home/" + "claude"                  # spelled in two pieces so this file does not match itself
    bad = []
    for p in ROOT.rglob("*"):
        parts = set(p.relative_to(ROOT).parts)
        if not p.is_file() or parts & {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}:
            continue
        if p.suffix in (".pyc", ".db"):
            continue
        try:
            if needle in p.read_text(errors="strict"):
                bad.append(str(p.relative_to(ROOT)))
        except (UnicodeDecodeError, OSError):
            pass
    assert not bad, bad


def test_integration_tests_find_the_harness_only_through_the_env_var():
    for f in ("conftest.py", "test_field_commands.py"):
        t = (ROOT / "tests/integration" / f).read_text()
        assert 'os.environ.get("BR_HARNESS_DIR")' in t and ("/home/" + "claude") not in t
        assert 'get("BR_HARNESS_DIR",' not in t              # no default location


def test_nftables_panel_ip_is_a_marked_variable_with_a_readme_step():
    n = (ROOT / "deploy/nftables/blackrelay-egress.nft").read_text()
    assert re.search(r"^define PANEL_IP = ", n, re.M) and "EDIT THIS" in n
    assert n.count("$PANEL_IP") >= 2 and not re.search(r"daddr 192\.168", n)    # only the define holds an address
    r = (ROOT / "README.md").read_text()
    assert "Install the egress firewall" in r and "define PANEL_IP" in r


def test_reference_pairing_tool_is_labelled_and_not_wired_into_anything():
    t = (ROOT / "tools/pair_reference.py").read_text()
    head = t[:2500]
    assert "FALLBACK" in head and "BYPASSES the bridge's write guard" in head and "not used at runtime" in head
    assert "br2 pair" in head
    for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "docker").glob("Dockerfile.*")):
        assert "pair_reference" not in p.read_text(), p
    assert "tools/pair_reference.py" in (ROOT / "README.md").read_text()
    import ast; ast.parse(t)
    assert (ROOT / "tools/pair_reference.requirements.txt").read_text().count("qolsys-controller==") == 1


def test_harness_start_sets_the_rotation_flag_and_shipped_config_leaves_it_off():
    assert "BR2_IQ_PIN_ACCEPT_ROTATION=1" in (ROOT / "harness_start.sh").read_text()
    ex = yaml.safe_load((ROOT / "deploy/config/config.example.yaml").read_text())
    assert ex["iq_remote"]["pin_accept_rotation"] is False and ex["iq_remote"]["panel_spki_sha256"] is None
    assert "BR2_IQ_PIN_ACCEPT_ROTATION" not in (ROOT / "deploy/docker-compose.yml").read_text()
