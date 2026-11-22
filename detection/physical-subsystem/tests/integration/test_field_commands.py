"""`br2 snapshot` and `br2 watch` against the harness panel simulator (real TLS MQTT broker requiring a client
certificate; every byte the commands send is audited by the simulator). Pairing itself is not simulated by the
harness: see tests/test_pairing.py."""
import json, os, pathlib, re, subprocess, sys, tempfile, time
import pytest

_H = os.environ.get("BR_HARNESS_DIR")                  # no default location: unset => skipped
HARNESS = pathlib.Path(_H) if _H else pathlib.Path("/nonexistent-harness")
IMPL = pathlib.Path(__file__).resolve().parents[2]
PY = HARNESS / ".venv/bin/python"
pytestmark = pytest.mark.skipif(not _H or not PY.exists(), reason="BR_HARNESS_DIR not set / shared harness not available")


@pytest.fixture
def sim(tmp_path):
    p = subprocess.Popen([str(PY), "-m", "sim.run_standalone", str(tmp_path / "w")], cwd=HARNESS, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
    head = ""
    t0 = time.time()
    while "audit=" not in head and time.time() - t0 < 30:
        head += p.stdout.readline()
    info = dict(pki_dir=re.search(r"pki_dir=(\S+)", head).group(1), ca_file=re.search(r"ca_file=(\S+)", head).group(1),
                port=re.search(r"panel_port=(\d+)", head).group(1), audit=re.search(r"audit=(\S+)", head).group(1))
    cfg = tmp_path / "c.yaml"
    cfg.write_text("iq_remote:\n  panel_ip: 127.0.0.1\n  plugin_ip: 127.0.0.1\n")
    info["cfg"] = str(cfg)
    info["send"] = lambda line: (p.stdin.write(line + "\n"), p.stdin.flush())
    yield info
    try:
        p.stdin.write("quit\n"); p.stdin.flush(); p.wait(10)
    except Exception:  # noqa: BLE001
        p.kill()


def audit_entries(sim):
    p = pathlib.Path(sim["audit"])
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def br2(args, **kw):
    env = dict(os.environ, BR2_SETTLE_S="0.2", PYTHONPATH=str(IMPL / "src"))
    return subprocess.Popen([sys.executable, "-m", "br2", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, **kw)


def common(sim):
    return ["--config", sim["cfg"], "--pki-dir", sim["pki_dir"], "--ca-file", sim["ca_file"], "--panel-port", sim["port"]]


def record_pin(sim):
    """pin_mode is `required` by default: nothing is trusted automatically, the operator records the pin (`br2 repin --yes`)."""
    p = br2(["repin", "--yes", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err


def test_snapshot_prints_zones_partitions_and_tls_evidence_and_sends_only_allowed_messages(sim):
    record_pin(sim)
    p = br2(["snapshot", *common(sim)])
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    snap = json.loads(out)
    assert snap["panel"] == "127.0.0.1" and len(snap["zones"]) >= 5 and snap["partitions"]
    z12 = next(z for z in snap["zones"] if z["id"] == 12)
    assert z12["status"] in ("Closed", "Open") and z12["name"]
    assert re.fullmatch(r"TLSv1\.[23]", snap["tls"]["version"]) and snap["tls"]["cipher"]
    assert f"version={snap['tls']['version']}" in err and "TLS to the panel" in err
    for secret in ("BEGIN", "PRIVATE", ".key"):
        assert secret not in out and secret not in err
    entries = audit_entries(sim)
    sent = {json.loads(e["raw"])["eventName"] for e in entries if e["side"] == "iq" and e["kind"] == "mastermeid"}
    assert sent == {"connect_v204", "pingevent", "pair_status_request", "syncdatabase"}, sent
    assert any(e["verdict"] == "allowed" for e in entries) and not [e for e in entries if e["verdict"] == "violation"]


def test_watch_prints_changes_for_the_requested_time_and_exits(sim):
    record_pin(sim)
    p = br2(["watch", "--minutes", "0.25", *common(sim)])
    time.sleep(5)                                              # connected and synced by now
    sim["send"]("zone 12 Open"); time.sleep(0.5); sim["send"]("zone 12 Closed"); time.sleep(0.5)
    sim["send"]("arm 0 ARM-AWAY")
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    z = [l for l in lines if l["event"] == "zone" and l["id"] == 12]
    assert [l["status"] for l in z] == ["Open", "Closed"], lines
    assert any(l["event"] == "partition" for l in lines)
    assert "TLS to the panel" in err and "change(s) printed" in err
    assert not [e for e in audit_entries(sim) if e["verdict"] == "violation"]


def test_snapshot_without_pairing_material_says_to_run_pair(sim, tmp_path):
    p = br2(["snapshot", "--config", sim["cfg"], "--pki-dir", str(tmp_path / "empty"), "--panel-port", sim["port"]])
    out, err = p.communicate(timeout=30)
    assert p.returncode == 2 and "br2 pair" in err and "Traceback" not in err


def test_snapshot_refuses_without_a_pin_repin_records_it_and_a_changed_certificate_is_refused(sim):
    pin = pathlib.Path(sim["pki_dir"]) / "panel_pin.json"
    assert not pin.exists()
    sent = lambda: len([e for e in audit_entries(sim) if e["side"] == "iq" and e["kind"] == "mastermeid"])
    p = br2(["snapshot", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 2 and "br2 pair" in err and out == "" and not pin.exists() and sent() == 0
    p = br2(["repin", "--yes", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    p = br2(["snapshot", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    rec = json.loads(pin.read_text())
    assert re.fullmatch(r"[0-9a-f]{64}", rec["spki_sha256"]) and oct(pin.stat().st_mode & 0o777) == "0o600"
    before = pin.read_text()
    p = br2(["repin", *common(sim)]); out, err = p.communicate(timeout=60)      # same certificate: nothing to do
    assert p.returncode == 0 and "already matches" in err and pin.read_text() == before
    sent = lambda: len([e for e in audit_entries(sim) if e["side"] == "iq" and e["kind"] == "mastermeid"])
    n = sent()
    sim["send"]("iq trusted"); time.sleep(3)                                      # the panel presents a NEW certificate (same CA)
    p = br2(["snapshot", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 2 and "PIN MISMATCH" in err and out == "" and pin.read_text() == before
    assert sent() == n                                                            # not one panel message was sent to it
    p = br2(["repin", *common(sim)]); out, err = p.communicate(timeout=60)      # no --yes: prints old/new, changes nothing
    assert p.returncode == 2 and pin.read_text() == before and ":" in err
    p = br2(["repin", "--yes", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    assert json.loads(pin.read_text())["spki_sha256"] != rec["spki_sha256"]
    p = br2(["snapshot", *common(sim)]); out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    assert sent() > n                                                             # the good snapshot after repin did talk
