"""Beacon (iq-remote -> control4): wall-clock independent, tolerant of torn reads. Regressions: audit x1 A1-A3."""
import json, os, time
import pytest
from br2 import config as C
from br2.adapters import beacon, control4 as C4


class Mono:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def mk(tmp_path):
    clk = Mono(); p = str(tmp_path / "b")
    return clk, p, beacon.BeaconWriter(p), beacon.BeaconReader(p, clk)


def test_missing_beacon_counts_down_since_reader_start(tmp_path):
    clk, p, w, r = mk(tmp_path)
    clk.t += 10; r.poll()
    assert r.primary_down_for() == pytest.approx(10) and r.primary_up_for() == 0


def test_live_beacon_is_up_and_stale_beacon_is_down_since_last_change(tmp_path):
    clk, p, w, r = mk(tmp_path)
    for _ in range(8):
        w.write(True); r.poll(); clk.t += 0.25
    assert r.primary_down_for() == 0 and r.primary_up_for() > 0
    # writer dies leaving "up": ctr frozen
    clk.t += 10; r.poll()
    assert r.primary_up_for() == 0 and r.primary_down_for() == pytest.approx(10 - 0.25 + 0.25, abs=0.6)


def test_wall_clock_steps_do_not_matter(tmp_path, monkeypatch):
    """x1 A1/A2: the reader never calls time.time(); stepping it either way changes nothing."""
    clk, p, w, r = mk(tmp_path)
    monkeypatch.setattr(time, "time", lambda: 1000.0)       # RTC-less boot
    w.write(False); r.poll(); clk.t += 5; w.write(False); r.poll()
    d1 = r.primary_down_for()
    monkeypatch.setattr(time, "time", lambda: 1_790_000_000.0)   # NTP step forward
    clk.t += 1; w.write(False); r.poll()
    assert r.primary_down_for() == pytest.approx(d1 + 1, abs=0.01)   # exactly the monotonic elapsed time
    monkeypatch.setattr(time, "time", lambda: 5.0)               # and back
    clk.t += 1; w.write(False); r.poll()
    assert r.primary_down_for() == pytest.approx(d1 + 2, abs=0.01)
    assert "time" not in json.load(open(p)) and "hb" not in json.load(open(p))


def test_boot_id_change_with_counter_reset_is_a_change(tmp_path):
    clk, p, w, r = mk(tmp_path)
    for _ in range(4): w.write(True); r.poll(); clk.t += 0.25
    w2 = beacon.BeaconWriter(p)               # iq-remote restarted: new boot id, counter restarts at 1
    for _ in range(3): w2.write(True); r.poll(); clk.t += 0.25
    assert r.primary_down_for() == 0


def test_unreadable_beacon_keeps_previous_reading_until_two_failures(tmp_path):
    """x1 A3: one torn/empty/non-dict read does not flip state; two consecutive do."""
    clk, p, w, r = mk(tmp_path)
    for _ in range(4): w.write(True); r.poll(); clk.t += 0.25
    open(p, "w").write("")                      # torn
    r.poll(); assert r.primary_up_for() > 0
    open(p, "w").write("[1]")                   # valid JSON, not a dict
    r.poll(); assert r.primary_up_for() == 0 and r.primary_down_for() >= 0   # second consecutive failure
    w.write(True); r.poll(); clk.t += 0.1; assert r.primary_down_for() == 0   # recovers on the next good read
    open(p, "w").write('{"boot": 5, "ctr": "x", "up": 1}')                  # ill-typed fields are failures too
    r.poll(); open(p, "w").write('{"boot": 5}'); r.poll()
    assert r.primary_up_for() == 0


def test_second_reason_to_be_down_does_not_reset_down_since(tmp_path):
    clk, p, w, r = mk(tmp_path)
    w.write(False); r.poll(); clk.t += 6; r.poll()
    d = r.primary_down_for(); open(p, "w").write("x"); r.poll(); r.poll(); clk.t += 1
    assert r.primary_down_for() == pytest.approx(d + 1, abs=0.01)


def test_control4_gate_cold_standby(tmp_path, monkeypatch):
    monkeypatch.setenv("BR2_BEACON_DIR", str(tmp_path))
    cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "x"}, "control4": {"enabled": True, "host": "y"},
                   "timers": {"fallback_after_s": 3, "failback_holddown_s": 2}})
    a = C4.Control4Adapter(cfg, str(tmp_path))
    clk = Mono(); a.beacon = beacon.BeaconReader(a.beacon_path, clk)
    w = beacon.BeaconWriter(a.beacon_path)
    for _ in range(12): w.write(True); clk.t += 0.25; a.beacon.poll()
    assert not a.gate_open() and a.gate_should_close()           # healthy primary for 3 s: never contact the panel
    for _ in range(4): w.write(False); clk.t += 0.25; a.gate_open()
    assert not a.gate_open()                                     # down ~1 s < 3 s
    for _ in range(12): w.write(False); clk.t += 0.25; a.beacon.poll()
    assert a.gate_open() and not a.gate_should_close()
    cfg2 = C.parse({"iq_remote": {"enabled": False}, "control4": {"enabled": True, "host": "y"}})
    assert C4.Control4Adapter(cfg2, str(tmp_path)).gate_open()   # iq disabled => sole source: no gate


def test_flapping_primary_never_releases_control4(tmp_path, monkeypatch):
    """Primary up 200 ms every 6 s: control4 must stay connected (holddown 3 s never satisfied)."""
    monkeypatch.setenv("BR2_BEACON_DIR", str(tmp_path))
    cfg = C.parse({"iq_remote": {"enabled": True, "panel_ip": "x"}, "control4": {"enabled": True, "host": "y"},
                   "timers": {"fallback_after_s": 3}})
    a = C4.Control4Adapter(cfg, str(tmp_path)); clk = Mono()
    a.beacon = beacon.BeaconReader(a.beacon_path, clk); w = beacon.BeaconWriter(a.beacon_path)
    for _ in range(60): w.write(False); clk.t += 0.25; a.beacon.poll()
    released = False
    for cycle in range(5):
        w.write(True); clk.t += 0.2; a.beacon.poll(); released |= a.gate_should_close()
        for _ in range(24): w.write(False); clk.t += 0.25; a.beacon.poll(); released |= a.gate_should_close()
    assert not released
