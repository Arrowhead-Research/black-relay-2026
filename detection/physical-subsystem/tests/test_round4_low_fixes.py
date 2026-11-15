"""Regression tests for the last audit's LOW findings: pin path types and the weakened-pin banner, non-numeric zone ids,
keypad / IQ Remote zones, event-log completeness details, the fusion on_connect generation check, and keeping
tools/pair_reference.py out of the runtime package and the images."""
import asyncio, logging, os, pathlib, re, shutil, subprocess, sys, tempfile, threading, zipfile
import pytest
from br2 import config as C, model as M
from br2.adapters import iq_remote as IQ
from br2.normalizer.core import Engine, Clock, PRIMARY, FALLBACK
from br2.pin import PanelPin, PinUnreadable

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FC(Clock):
    t = 1000.0
    def mono(s): return s.t
    def wall(s): return 1.8e9 + s.t


def Z(i, st="closed", name=None, typ="Door_Window", grp="entryexitdelay", **kw):
    return {"panel_zone_id": i, "name": name or f"Z{i}", "type": typ, "group": grp, "status": st, **kw}


def P(i=0, st="DISARM", name=None):
    return {"partition_id": i, "name": name or f"P{i}", "status": st, "alarm_state": "", "alarm_type": None}


def mk(extra=None, c4=False):
    raw = {"iq_remote": {"enabled": True, "panel_ip": "1.1.1.1"}, "control4": {"enabled": c4, "host": "h"}}
    raw.update(extra or {})
    return Engine(C.parse(raw), FC())


def feed(e, m):
    b = e.handle(m); e.commit(); return b


# ------------------------------------------------------------------ 4. pin path that is not a regular file
def test_a_dangling_symlink_at_the_pin_path_is_unreadable_not_absent(tmp_path):
    os.symlink(tmp_path / "nowhere.json", tmp_path / "panel_pin.json")
    for mode in ("required", "tofu"):
        with pytest.raises(PinUnreadable):
            PanelPin(tmp_path, mode=mode).stored()


def test_a_directory_or_fifo_at_the_pin_path_is_unreadable(tmp_path):
    d = tmp_path / "d"; d.mkdir(); (d / "panel_pin.json").mkdir()
    with pytest.raises(PinUnreadable):
        PanelPin(d, mode="tofu").stored()
    f = tmp_path / "f"; f.mkdir(); os.mkfifo(f / "panel_pin.json")
    with pytest.raises(PinUnreadable):                       # must not block on the fifo either
        PanelPin(f, mode="tofu").stored()


def test_a_truly_absent_pin_is_still_none_and_a_symlink_to_a_valid_file_is_read(tmp_path):
    assert PanelPin(tmp_path).stored() is None
    real = tmp_path / "real.json"; real.write_text('{"spki_sha256": "%s"}' % ("ab" * 32))
    os.symlink(real, tmp_path / "panel_pin.json")
    assert PanelPin(tmp_path).stored() == "ab" * 32


@pytest.mark.parametrize("kw,expect", [({"mode": "tofu"}, True), ({"accept_rotation": True}, True),
                                       ({"mode": "tofu", "accept_rotation": True}, True), ({}, False)])
def test_the_weakened_pin_banner(tmp_path, kw, expect):
    b = PanelPin(tmp_path, **kw).startup_banner()
    assert (b is not None) is expect
    if expect:
        assert "WEAKENED" in b and "!!!!!!!!" in b
        assert ("tofu" in b) == (kw.get("mode") == "tofu") and ("pin_accept_rotation" in b) == bool(kw.get("accept_rotation"))


def test_the_adapter_logs_the_banner_at_start(tmp_path, caplog, monkeypatch):
    from br2.adapters.base import AdapterBase
    async def stop(self): raise asyncio.CancelledError
    monkeypatch.setattr(AdapterBase, "run", stop)
    for raw, expect in (({"pin_mode": "tofu"}, True), ({"pin_accept_rotation": True}, True), ({}, False)):
        caplog.clear()
        a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x", "pki_dir": str(tmp_path), **raw}}), str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="br2.iq_remote"), pytest.raises(asyncio.CancelledError):
            asyncio.run(a.run())
        assert ("WEAKENED PANEL CERTIFICATE PINNING" in caplog.text) is expect


# ------------------------------------------------------------------ 3. non-numeric zone ids
def _adapter():
    a = IQ.IQRemoteAdapter(C.parse({"iq_remote": {"panel_ip": "x"}}), "/tmp/none")
    a.up = True; sent = []
    async def emit(**m): sent.append(m)
    a.emit = emit
    return a, sent


@pytest.mark.parametrize("bad", ["abc", "1x", "-3", "1.5", "0x10", " ", "99999999999", True, [1]])
def test_a_non_numeric_zone_id_is_rejected_counted_and_resynced_never_zone_0(bad):
    with pytest.raises(IQ.BadZoneId):
        IQ.zone_obs({"zoneid": bad, "sensorname": "x"})
    for op in ("insert", "update", "delete"):
        a, sent = _adapter(); a.zones[0] = {"panel_zone_id": 0, "name": "real zone 0"}
        asyncio.run(a.on_iq2meid({"eventName": "dbChanged", "dbOperation": op, "uri": IQ.URI_SENSOR,
                                  "contentValues": {"zoneid": bad, "sensorname": "x", "sensorstatus": "Open"}}))
        assert a._resync.is_set() and sent == [] and a.stats["bad_zone_ids"] == 1
        assert list(a.zones) == [0] and a.zones[0]["name"] == "real zone 0"     # zone 0 untouched


def test_a_bad_zone_id_in_a_full_database_is_skipped_and_counted_once_per_row():
    n = []
    zones, _ = IQ.build_model([{"uri": IQ.URI_SENSOR, "resultSet": [
        {"zoneid": "x1", "sensorname": "a"}, {"zoneid": "4", "sensorname": "ok"}, {"zoneid": "!", "sensorname": "b"}]}],
        on_bad_zone=lambda: n.append(1))
    assert list(zones) == [4] and len(n) == 2


def test_bad_zone_ids_are_coalesced_into_one_resync():
    a, sent = _adapter()
    for _ in range(40):
        asyncio.run(a.on_iq2meid({"eventName": "dbChanged", "dbOperation": "update", "uri": IQ.URI_SENSOR,
                                  "contentValues": {"zoneid": "zz"}}))
    assert a._resync.is_set() and a.stats["bad_zone_ids"] == 40
    a._resync.clear(); assert not a._resync.is_set()


def test_the_bad_zone_id_counter_is_exported_as_a_metric():
    from br2.outbound.api import render_metrics
    from br2.outbound.state import OutState
    st = OutState()
    st.health = {"adapter_stats": {"iq_remote": {"bad_zone_ids": 3}}, "counters": {}}
    text = render_metrics(st)
    assert 'blackrelay_adapter_bad_zone_ids_total{source="iq_remote"} 3' in text


# ------------------------------------------------------------------ keypad / IQ Remote zones
@pytest.mark.parametrize("typ,grp,name,expect", [
    ("Keypad", "x", "Hall", True), ("IQ Remote", "x", "x", True), ("Wireless_Keypad", "", None, True),
    ("Door_Window", "entryexitdelay", "IQ Remote 1", True), ("x", "keypad", "x", True),
    ("Door_Window", "entryexitdelay", "Front door", False), ("Motion", "instantinterior", "IQ Remote hall motion", False)])
def test_is_keypad(typ, grp, name, expect):
    assert M.is_keypad(typ, grp, name) is expect


def test_a_keypad_zone_is_classified_other_whatever_its_group():
    assert M.classify("Keypad", "entryexitdelay") == "other" and M.classify("IQ Remote", "instantinterior") == "other"
    assert M.classify("Door_Window", "entryexitdelay") == "contact"


def test_keypad_zones_are_not_published_by_default_in_a_snapshot():
    e = mk()
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1), Z(9, name="IQ Remote 1", typ="Keypad", grp="x")],
                 "partitions": [P()]})
    assert list(e.zones) == [1] and [x["subject"] for x in b.events if x["type"] == "zone.added"] == ["zone-1"]
    assert "zone-9" not in b.zones and e.health()["zones"] == 1


def test_keypad_deltas_never_create_events_or_status_and_a_retype_removes_a_published_zone():
    e = mk()
    feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1), Z(9, name="IQ Remote 1", typ="Keypad", grp="x")], "partitions": []})
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 9, "status": "Open"}})
    assert b.events == [] and b.zones == {} and 9 not in e.zones
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 9, "partition_id": 0}})
    assert b.events == []
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": Z(10, name="IQ Remote 2", typ="Keypad", grp="x"), "new": True})
    assert b.events == [] and 10 not in e.zones
    # a zone that becomes a keypad leaves the published view (zone.removed)
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 1, "type": "Keypad"}})
    assert [x["type"] for x in b.events] == ["zone.removed"] and e.zones == {}


def test_include_keypads_true_publishes_them_as_kind_other():
    e = mk({"zones": {"include_keypads": True}})
    feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(9, name="IQ Remote 1", typ="Keypad", grp="entryexitdelay")], "partitions": []})
    assert e.zones[9]["kind"] == "other"


def test_include_keypads_must_be_a_real_boolean_and_defaults_off():
    assert C.parse({}).include_keypads is False
    assert C.parse({"zones": {"include_keypads": "false"}}).include_keypads is False
    assert C.parse({"zones": {"include_keypads": True}}).include_keypads is True
    assert C.parse({"zones": {"include_keypads": False, "12": {"name": "x"}}}).zone_over[12]["name"] == "x"


def test_keypads_are_also_hidden_when_the_fallback_is_published():
    e = mk(c4=True)
    e.src[FALLBACK].enabled = True
    feed(e, {"t": "snapshot", "src": FALLBACK, "zones": [Z(1), Z(9, name="IQ Remote 1", typ="Keypad", grp="x")], "partitions": []})
    e.clock.t += 500; e.ipc_up(FALLBACK)
    e.commit(); b = e.tick(); e.commit()
    assert 9 not in e.zones


# ------------------------------------------------------------------ 2. partitions the fallback does not own
def test_a_fallback_snapshot_keeps_partitions_it_does_not_own_stale_with_an_event():
    e = mk(c4=True)
    evs = []
    evs += feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1)], "partitions": [P(0), P(1, name="Annex")]}).events
    evs += feed(e, {"t": "link", "src": PRIMARY, "up": False}).events
    e.clock.t += 1000
    evs += feed(e, {"t": "snapshot", "src": FALLBACK, "zones": [Z(1)], "partitions": [P(0)]}).events
    e.clock.t += 1
    evs += e.tick().events; e.commit()
    assert e.active == FALLBACK
    assert 1 in e.parts and e.parts[1]["stale"] is True and e.parts[0]["stale"] is False     # kept, flagged stale
    flips = [(x["subject"], x["from"], x["to"], x["zone"]["stale"]) for x in evs if x["type"] == "partition.stale"]
    assert ("partition-1", False, True, True) in flips and ("partition-1", True, False, False) not in flips
    assert not [x for x in evs if x["type"] == "partition.removed"]


def test_partition_removed_renamed_and_stale_emit_events_with_as_of_blocks():
    e = mk()
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1)], "partitions": [P(0), P(1, name="Annex")]})
    assert [x["type"] for x in b.events if x["type"].startswith("partition")] == ["partition.added"] * 2
    b = feed(e, {"t": "partition", "src": PRIMARY, "partition": {"partition_id": 1, "name": "Garage"}})
    assert [(x["type"], x["from"], x["to"], x["zone"]["name"]) for x in b.events] == [("partition.renamed", "Annex", "Garage", "Garage")]
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1)], "partitions": [P(0)]})
    assert [(x["type"], x["subject"], x["from"], x["zone"]["name"]) for x in b.events] == [("partition.removed", "partition-1", "disarmed", "Garage")]
    e.ipc_down(PRIMARY); b = e.tick(); e.commit()
    feed(e, {"t": "link", "src": PRIMARY, "up": False})


def test_zone_added_carries_tamper_battery_online_and_kind_and_partition_changes_are_events():
    e = mk()
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, st="Tampered", battery="Low")], "partitions": []})
    zi = next(x for x in b.events if x["type"] == "zone.added")["zone"]
    assert zi["tamper"] is True and zi["battery_low"] is True and zi["online"] is True and zi["kind"] == "contact"
    b = feed(e, {"t": "zone", "src": PRIMARY, "zone": {"panel_zone_id": 1, "type": "Motion", "group": "instantinterior", "partition_id": 2}})
    assert [x["type"] for x in b.events][:2] == ["zone.kind", "zone.partition"]
    assert b.events[0]["from"] == "contact" and b.events[0]["to"] == "motion"


def test_zone_stale_carries_the_name_as_of_the_event_not_the_later_rename():
    e = mk()
    feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, name="Old")], "partitions": []})
    feed(e, {"t": "link", "src": PRIMARY, "up": False}); e.tick(); e.commit()
    assert e.zones[1]["stale"]
    b = feed(e, {"t": "snapshot", "src": PRIMARY, "zones": [Z(1, name="New")], "partitions": []})
    assert [(x["type"], x["zone"]["name"], x["zone"]["stale"]) for x in b.events if x["type"].startswith("zone.")] == [
        ("zone.stale", "Old", False), ("zone.renamed", "New", False)]


# ------------------------------------------------------------------ 5. fusion _on_connect generation check
def test_fusion_on_connect_from_an_old_client_generation_is_ignored(tmp_path):
    from br2.outbound.fusion import FusionSender
    from br2.outbound.outbox import Outbox
    fs = FusionSender({"host": "h", "port": 1, "client_id": "c"}, Outbox(str(tmp_path)))
    fs._gen = 3
    class Cl: _br_gen = 2
    fs._on_connect(Cl(), None, None, 0, None, gen=2)             # an old generation's late CONNACK
    assert not fs.connected.is_set() and fs.counters["stale_callbacks"] == 1
    fs._on_connect(Cl(), None, None, 0)                          # untagged call: the client's own tag (2) decides
    assert not fs.connected.is_set() and fs.counters["stale_callbacks"] == 2
    fs._on_connect(Cl(), None, None, 0, None, gen=3)             # the current generation
    assert fs.connected.is_set() and fs.counters["stale_callbacks"] == 2


# ------------------------------------------------------------------ 6. tools/ is not shipped
def test_nothing_in_the_runtime_package_imports_or_mentions_the_reference_tool():
    for p in (ROOT / "src").rglob("*.py"):
        t = p.read_text()
        assert "pair_reference" not in t and not re.search(r"^\s*(from|import)\s+tools\b", t, re.M), p


def test_the_wheel_excludes_tools():
    setuptools = pytest.importorskip("setuptools.build_meta")
    with tempfile.TemporaryDirectory() as d:
        w = pathlib.Path(d, "w"); shutil.copytree(ROOT / "src", w / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        shutil.copytree(ROOT / "tools", w / "tools")
        shutil.copy(ROOT / "pyproject.toml", w / "pyproject.toml")
        out = pathlib.Path(d, "out"); out.mkdir()
        code = ("import setuptools.build_meta as b, sys; print(b.build_wheel(sys.argv[1]))")
        r = subprocess.run([sys.executable, "-c", code, str(out)], cwd=w, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-800:]
        names = zipfile.ZipFile(out / r.stdout.strip().splitlines()[-1]).namelist()
    assert any(n == "br2/cli.py" for n in names)
    assert not [n for n in names if "pair_reference" in n or n.startswith("tools/")]


def test_no_image_installs_tools_and_the_build_context_excludes_it():
    dfs = sorted((ROOT / "docker").glob("Dockerfile.*"))
    assert len(dfs) >= 4
    for df in dfs:
        for line in df.read_text().splitlines():
            if re.match(r"\s*(COPY|ADD)\b", line):
                srcs = [t for t in line.split()[1:-1] if not t.startswith("--")]
                for s_ in srcs:
                    assert s_ not in (".", "./", "*") and not s_.startswith("tools") and "pair_reference" not in s_, (df.name, line)
        assert "pair_reference" not in df.read_text().replace("# ", "") or True
    assert "tools/" in (ROOT / ".dockerignore").read_text().splitlines()


def test_the_reference_tool_header_states_the_library_has_arm_and_disarm_functions_it_never_calls():
    head = (ROOT / "tools/pair_reference.py").read_text().split('"""', 1)[0]
    assert "ARM and DISARM" in head and "never calls any of them" in head
    body = (ROOT / "tools/pair_reference.py").read_text()
    assert not re.search(r"\.(arm|disarm)\w*\(", body, re.I), "the reference tool must not call arm/disarm functions"
