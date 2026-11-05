"""Tests for ``cameraprovision.store``: all-or-nothing saves across several files."""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from cameraprovision import store as store_module
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig, effective_ntp_server
from cameraprovision.store import NodeStore, Paths

WIFI_PSK = "w1fi-s3cret-Psk"
BROKER_PASSWORD = "br0ker-s3cret-Pw"


def config(**overrides: Any) -> NodeConfig:
    values: dict[str, Any] = {
        "node_id": "cam-01",
        "hostname": "edge-7",
        "wifi_ssid": "Site WiFi; 2.4",
        "wifi_psk": WIFI_PSK,
        "mediamtx_address": "10.0.0.2:8554",
        "covered_zones": ["perimeter-north", "driveway"],
        "broker": BrokerConfig(
            host="10.0.0.2", port=8883, username="camera-cam-01", password=BROKER_PASSWORD, tls=True
        ),
        "frigate": FrigateConfig(base_url="http://cam-01.local:5000"),
    }
    values.update(overrides)
    return NodeConfig(**values)


class SimulatedPowerCut(BaseException):
    """Stops execution with no chance to clean up. Not an ``Exception``, so nothing handles it."""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        bridge_config=tmp_path / "etc/camerabridge/bridge.yaml",
        mediamtx_env=tmp_path / "etc/camerabridge/mediamtx.env",
        nm_connections_dir=tmp_path / "etc/NetworkManager/system-connections",
        state_dir=tmp_path / "var/lib/camera-provision",
        ntp_dropin=tmp_path / "etc/systemd/timesyncd.conf.d/black-relay.conf",
        central_hosts=tmp_path / "black-relay" / "central-hosts.nft",
    )


@pytest.fixture
def store(paths: Paths) -> NodeStore:
    return NodeStore(paths, chown=lambda path: None)


def snapshot(paths: Paths) -> dict[str, bytes]:
    roots = {
        paths.bridge_config.parent,
        paths.nm_connections_dir,
        paths.state_dir,
        paths.ntp_dropin.parent,
        paths.central_hosts.parent,
    }
    found: dict[str, bytes] = {}
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                found[str(child)] = child.read_bytes()
    return found


def fail_nth_replace(monkeypatch: pytest.MonkeyPatch, n: int, error: BaseException) -> None:
    """Raise ``error`` from the nth ``os.replace`` a commit makes after staging."""
    real = os.replace
    seen = {"count": 0}

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name.endswith(".staged.tmp"):
            seen["count"] += 1
            if seen["count"] == n:
                raise error
        real(src, dst)

    monkeypatch.setattr(store_module.os, "replace", replace)


COMMIT_STEPS = [1, 2, 3, 4, 5, 6]


def test_save_writes_every_file_with_the_right_mode(store: NodeStore, paths: Paths) -> None:
    store.save(config())

    assert paths.nm_keyfile == paths.nm_connections_dir / "black-relay-uplink.nmconnection"
    for path in (paths.bridge_config, paths.nm_keyfile, paths.node_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(paths.mediamtx_env.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.central_hosts.stat().st_mode) == 0o644
    assert paths.mediamtx_env.read_text() == "MEDIAMTX_ADDRESS=10.0.0.2:8554\n"


def test_save_leaves_no_working_files(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    store.save(config(hostname="again"))

    names = {Path(name).name for name in snapshot(paths)}
    assert names == {
        "bridge.yaml",
        "mediamtx.env",
        "black-relay-uplink.nmconnection",
        "node.json",
        "central-hosts.nft",
        "black-relay.conf",
    }


def test_load_round_trips_everything_but_the_psk(store: NodeStore) -> None:
    store.save(config())

    saved = store.load()

    assert saved is not None
    assert saved.node_id == "cam-01"
    assert saved.hostname == "edge-7"
    assert saved.wifi_ssid == "Site WiFi; 2.4"
    assert saved.wifi_psk == ""
    assert saved.broker == config().broker
    assert list(saved.covered_zones) == ["perimeter-north", "driveway"]
    assert saved.mediamtx_address == "10.0.0.2:8554"
    assert saved.frigate.base_url == "http://cam-01.local:5000"
    assert saved.frigate.camera == "cam-01"
    assert saved.camera_upside_down is False


def test_load_round_trips_an_anonymous_broker(store: NodeStore) -> None:
    store.save(
        config(
            broker=BrokerConfig(host="10.0.0.2", username="", password=""),
            broker_anonymous=True,
        )
    )

    saved = store.load()

    assert saved is not None
    assert saved.broker_anonymous is True


def test_the_wifi_psk_is_only_in_the_keyfile(store: NodeStore, paths: Paths) -> None:
    store.save(config())

    holders = [name for name, data in snapshot(paths).items() if WIFI_PSK.encode() in data]

    assert holders == [str(paths.nm_keyfile)]


@pytest.mark.parametrize(
    "missing", ["bridge_config", "nm_keyfile", "node_file"]
)
def test_load_needs_every_part_of_a_config(store: NodeStore, paths: Paths, missing: str) -> None:
    store.save(config())

    getattr(paths, missing).unlink()

    assert store.load() is None


@pytest.mark.parametrize(
    "content",
    ["{not yaml", "[]", "node_id: BAD", "node_id: cam-01\n", ""],
)
def test_load_treats_a_corrupt_bridge_config_as_no_config(
    store: NodeStore, paths: Paths, content: str
) -> None:
    store.save(config())

    paths.bridge_config.write_text(content)

    assert store.load() is None


def test_load_rejects_a_complete_config_that_fails_validation(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())

    text = paths.bridge_config.read_text()
    paths.bridge_config.write_text(text.replace("node_id: cam-01", "node_id: BAD"))

    assert store.load() is None


@pytest.mark.parametrize("content", ["{not json", "[]", "{}", '{"hostname": "-bad-"}'])
def test_load_treats_a_corrupt_node_file_as_no_config(
    store: NodeStore, paths: Paths, content: str
) -> None:
    store.save(config())

    paths.node_file.write_text(content)

    assert store.load() is None


def test_load_on_an_empty_disk_is_none(store: NodeStore) -> None:
    assert store.load() is None


def test_save_rejects_an_invalid_config_before_touching_the_disk(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())
    before = snapshot(paths)

    with pytest.raises(ValueError):
        store.save(config(node_id="BAD"))

    assert snapshot(paths) == before


def test_validation_runs_before_the_keyfile_is_rendered(
    store: NodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*args: Any) -> Any:
        raise AssertionError("nm_wifi_profile ran on an unvalidated config")

    monkeypatch.setattr(store_module, "nm_wifi_profile", must_not_run)

    with pytest.raises(ValueError):
        store.save(config(wifi_psk="short"))


def test_validation_error_does_not_quote_secrets(store: NodeStore) -> None:
    with pytest.raises(ValueError) as raised:
        store.save(config(wifi_psk="short", node_id="BAD"))

    assert "short" not in str(raised.value)
    assert "short" not in str(raised.value)


def test_keyfile_is_rendered_with_escaping(store: NodeStore, paths: Paths) -> None:
    store.save(config(wifi_psk=" lead\\ing-space"))

    text = paths.nm_keyfile.read_text()

    assert "psk=\\slead\\\\ing-space" in text
    assert r"ssid=Site WiFi\\; 2.4" in text


def test_failure_while_staging_leaves_everything_as_it_was(paths: Paths) -> None:
    good = NodeStore(paths, chown=lambda path: None)
    good.save(config(hostname="old-name"))
    before = snapshot(paths)

    def refuse(path: Path) -> None:
        raise PermissionError("no such user")

    with pytest.raises(PermissionError):
        NodeStore(paths, chown=refuse).save(config(hostname="new-name"))

    assert snapshot(paths) == before


@pytest.mark.parametrize("step", COMMIT_STEPS)
def test_failure_during_commit_rolls_every_file_back(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    store.save(config(hostname="old-name", wifi_psk="old-wifi-psk"))
    before = snapshot(paths)
    fail_nth_replace(monkeypatch, step, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save(config(hostname="new-name", wifi_psk="new-wifi-psk"))

    assert snapshot(paths) == before


@pytest.mark.parametrize("step", COMMIT_STEPS)
def test_failure_during_a_first_commit_leaves_no_config(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    fail_nth_replace(monkeypatch, step, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save(config())

    assert store.load() is None
    assert snapshot(paths) == {}


def test_failed_save_restores_a_key_it_was_removing(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())
    fail_nth_replace(monkeypatch, 3, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save(config(broker=replace(config().broker, host="10.0.0.3")))

    assert "10.0.0.2" in paths.central_hosts.read_text()


@pytest.mark.parametrize("step", COMMIT_STEPS)
def test_power_cut_during_commit_finishes_the_save_on_recovery(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    store.save(config(hostname="old-name", wifi_psk="old-wifi-psk"))
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, step, SimulatedPowerCut())
        with pytest.raises(SimulatedPowerCut):
            store.save(config(hostname="new-name", wifi_psk="new-wifi-psk"))

    store.recover()

    saved = store.load()
    assert saved is not None
    assert saved.hostname == "new-name"
    assert "psk=new-wifi-psk" in paths.nm_keyfile.read_text()
    names = {Path(name).name for name in snapshot(paths)}
    assert not any(name.endswith(".tmp") or name == "apply.journal" for name in names)


def test_power_cut_while_staging_keeps_the_old_config_on_recovery(
    paths: Paths,
) -> None:
    store = NodeStore(paths, chown=lambda path: None)
    store.save(config(hostname="old-name"))
    before = snapshot(paths)

    def cut(path: Path) -> None:
        raise SimulatedPowerCut()

    with pytest.raises(SimulatedPowerCut):
        NodeStore(paths, chown=cut).save(config(hostname="new-name"))

    store.recover()

    assert snapshot(paths) == before


def test_power_cut_during_rollback_finishes_the_rollback_on_recovery(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config(hostname="old-name", wifi_psk="old-wifi-psk"))
    before = snapshot(paths)
    real = os.replace
    seen = {"staged": 0, "restores": 0}

    def replace(src: Any, dst: Any) -> None:
        name = Path(src).name
        if name.endswith(".staged.tmp"):
            seen["staged"] += 1
            if seen["staged"] == 3:
                raise OSError("I/O error")
        if name.endswith(".prev.tmp"):
            seen["restores"] += 1
            if seen["restores"] == 2:
                raise SimulatedPowerCut()
        real(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", replace)
        with pytest.raises(SimulatedPowerCut):
            # Differs in the first files committed, which the rollback restores last.
            store.save(config(node_id="cam-02", mediamtx_address="10.0.0.9:8554"))

    store.recover()

    assert snapshot(paths) == before


def test_recover_removes_stale_temp_files(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    before = snapshot(paths)
    stale = [
        paths.bridge_config.with_name(".bridge.yaml.abc.tmp"),
        paths.bridge_config.with_name("..bridge.yaml.staged.tmp.abc.tmp"),
        paths.nm_keyfile.with_name(f".{paths.nm_keyfile.name}.abc.tmp"),
        paths.central_hosts.with_name(".central-hosts.nft.abc.tmp"),
        paths.state_file.with_name(".state.json.abc.tmp"),
    ]
    for path in stale:
        path.write_text("partial")
    keep = paths.nm_connections_dir / "someone-elses.nmconnection"
    keep.write_text("not ours")

    store.recover()

    assert all(not path.exists() for path in stale)
    assert keep.exists()
    keep.unlink()
    assert snapshot(paths) == before


def test_recover_removes_a_stray_bridge_config_from_a_blank_node(
    store: NodeStore, paths: Paths
) -> None:
    """Without this the bridge would start on a node that opens the setup AP as blank."""
    paths.bridge_config.parent.mkdir(parents=True)
    paths.bridge_config.write_text("node_id: stray\n")

    store.recover()

    assert not paths.bridge_config.exists()


def test_recover_keeps_the_bridge_config_of_a_saved_node(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    before = snapshot(paths)

    store.recover()

    assert snapshot(paths) == before


def test_recover_keeps_the_bridge_config_while_a_journal_remains(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If settling failed, a later start can still finish the commit, and needs this file."""
    store.save(config())
    paths.journal.write_text("{}")
    paths.node_file.unlink()

    def fail() -> None:
        raise OSError("disk trouble")

    monkeypatch.setattr(store, "_settle_interrupted_save", fail)

    store.recover()

    assert paths.bridge_config.exists()


def test_recover_creates_the_state_directory(store: NodeStore, paths: Paths) -> None:
    store.recover()

    assert paths.state_dir.is_dir()
    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700


def test_chown_gets_the_staged_bridge_config_only(paths: Paths) -> None:
    seen: list[Path] = []

    NodeStore(paths, chown=seen.append).save(config())

    assert len(seen) == 1
    assert seen[0].parent == paths.bridge_config.parent
    assert "bridge.yaml" in seen[0].name and seen[0].name.endswith(".tmp")


def test_staged_keyfile_has_a_dot_name(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, store: NodeStore
) -> None:
    # Dot-names are meant to keep NetworkManager's keyfile plugin from loading a half
    # transaction. That NetworkManager really ignores them needs hardware verification:
    # this test only pins the names.
    seen: list[str] = []
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if Path(dst).parent == paths.nm_connections_dir:
            seen.extend(p.name for p in paths.nm_connections_dir.iterdir())
        real(src, dst)

    monkeypatch.setattr(store_module.os, "replace", replace)
    store.save(config())

    extra = [name for name in seen if name != paths.nm_keyfile.name]
    assert extra and all(name.startswith(".") for name in extra)


# --- WiFi repair ------------------------------------------------------------------------


def test_save_wifi_changes_only_the_keyfile_and_the_saved_ssid(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config(hostname="kept-name"))
    bridge, central, mediamtx = (
        paths.bridge_config.read_bytes(),
        paths.central_hosts.read_bytes(),
        paths.mediamtx_env.read_bytes(),
    )

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    text = paths.nm_keyfile.read_text()
    assert "ssid=Fixed-WiFi" in text and "psk=new-wifi-psk" in text
    assert "dhcp-hostname=kept-name" in text
    assert stat.S_IMODE(paths.nm_keyfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.node_file.stat().st_mode) == 0o600
    assert paths.bridge_config.read_bytes() == bridge
    assert paths.central_hosts.read_bytes() == central
    assert paths.mediamtx_env.read_bytes() == mediamtx
    saved = store.load()
    assert saved is not None and saved.wifi_ssid == "Fixed-WiFi"
    assert "new-wifi-psk" not in paths.node_file.read_text()


def test_i4_save_wifi_works_with_the_bridge_config_missing(store: NodeStore, paths: Paths) -> None:
    store.save(config(hostname="kept-name"))
    paths.bridge_config.unlink()

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    text = paths.nm_keyfile.read_text()
    assert "psk=new-wifi-psk" in text and "dhcp-hostname=kept-name" in text
    assert not paths.bridge_config.exists()


def test_i4_save_wifi_works_with_the_bridge_config_invalid(store: NodeStore, paths: Paths) -> None:
    store.save(config(hostname="kept-name"))
    damaged = paths.bridge_config.read_text().replace("status_interval_s: 30", "status_interval_s: -1")
    paths.bridge_config.write_text(damaged)

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert "psk=new-wifi-psk" in paths.nm_keyfile.read_text()
    assert paths.bridge_config.read_text() == damaged


def test_i4_save_wifi_never_builds_or_validates_a_node_config(
    store: NodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())

    def must_not_run(*args: Any) -> Any:
        raise AssertionError("a WiFi repair went through the NodeConfig path")

    monkeypatch.setattr(store_module, "write_bridge_config", must_not_run)
    monkeypatch.setattr(store_module, "validate", must_not_run)
    monkeypatch.setattr(store_module, "NodeConfig", must_not_run)

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")


def test_i4_save_wifi_needs_a_hostname(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    paths.node_file.write_text('{"wifi_ssid": "x"}')
    before = snapshot(paths)

    with pytest.raises(ValueError):
        store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert snapshot(paths) == before


@pytest.mark.parametrize("ssid,psk", [("", "new-wifi-psk"), ("Fixed-WiFi", "short")])
def test_save_wifi_rejects_bad_fields_before_rendering(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, ssid: str, psk: str
) -> None:
    store.save(config())
    before = snapshot(paths)

    def must_not_run(*args: Any) -> Any:
        raise AssertionError("nm_wifi_profile ran on unvalidated WiFi fields")

    monkeypatch.setattr(store_module, "nm_wifi_profile", must_not_run)

    with pytest.raises(ValueError):
        store.save_wifi(ssid, psk)

    assert snapshot(paths) == before


def test_save_wifi_needs_a_saved_config(store: NodeStore) -> None:
    with pytest.raises(ValueError):
        store.save_wifi("Fixed-WiFi", "new-wifi-psk")


@pytest.mark.parametrize("step", [1, 2])
def test_failed_save_wifi_rolls_back(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    store.save(config())
    before = snapshot(paths)
    fail_nth_replace(monkeypatch, step, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert snapshot(paths) == before


# --- has_config, journal and durability -------------------------------------------------


def test_c1_has_config_needs_only_the_keyfile_and_a_hostname(
    store: NodeStore, paths: Paths
) -> None:
    assert store.has_config() is False
    store.save(config())
    assert store.has_config() is True

    paths.bridge_config.write_text("status_interval_s: -1\n")
    assert store.has_config() is True
    assert store.load() is None

    paths.bridge_config.unlink()
    paths.mediamtx_env.unlink()
    assert store.has_config() is True

    data = paths.nm_keyfile.read_bytes()
    paths.nm_keyfile.unlink()
    assert store.has_config() is False
    paths.nm_keyfile.write_bytes(data)

    paths.node_file.write_text('{"wifi_ssid": "only"}')
    assert store.has_config() is False


@pytest.mark.parametrize(
    "journal",
    [
        "",
        "{not json",
        "[]",
        '{"a": 1}',
        "[1]",
        '[{"target": 1, "write": true}]',
        '[{"target": [], "write": true}]',
        '[{"target": {}, "write": true}]',
        '[{"target": "x"}]',
    ],
)
def test_i1_an_unreadable_journal_counts_as_none_and_restores_backups(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, journal: str
) -> None:
    store.save(config(hostname="old-name", wifi_psk="old-wifi-psk"))
    before = snapshot(paths)
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, 2, SimulatedPowerCut())
        with pytest.raises(SimulatedPowerCut):
            store.save(config(node_id="cam-02", wifi_psk="new-wifi-psk"))
    paths.journal.write_text(journal)

    store.recover()

    assert snapshot(paths) == before


def test_i1_a_journal_cannot_name_a_file_outside_the_store(
    store: NodeStore, paths: Paths, tmp_path: Path
) -> None:
    store.save(config())
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    paths.journal.write_text(json.dumps([{"target": str(victim), "write": False}]))

    store.recover()

    assert victim.read_text() == "keep me"
    assert not paths.journal.exists()


def test_i1_recover_survives_a_directory_it_cannot_change(
    store: NodeStore, paths: Paths, caplog: pytest.LogCaptureFixture
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    store.save(config())
    stale = paths.bridge_config.with_name(".bridge.yaml.abc.tmp")
    stale.write_text("partial")
    paths.bridge_config.parent.chmod(0o500)
    try:
        store.recover()
    finally:
        paths.bridge_config.parent.chmod(0o755)

    assert stale.exists()
    assert "PermissionError" in caplog.text


def test_i1_recover_survives_a_state_dir_it_cannot_create(paths: Paths, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    parent = paths.state_dir.parent
    parent.mkdir(parents=True)
    parent.chmod(0o500)
    try:
        NodeStore(paths, chown=lambda path: None).recover()
    finally:
        parent.chmod(0o755)

    assert not paths.state_dir.exists()


def test_i1_a_failed_roll_forward_keeps_the_journal_for_next_time(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config(hostname="old-name"))
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, 1, SimulatedPowerCut())
        with pytest.raises(SimulatedPowerCut):
            store.save(config(hostname="new-name"))
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, 1, OSError("I/O error"))
        store.recover()
    assert paths.journal.exists()

    store.recover()

    assert not paths.journal.exists()
    saved = store.load()
    assert saved is not None and saved.hostname == "new-name"


def test_m2_recover_sweeps_stale_journal_temp_files(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    stale = paths.journal.with_name(".apply.journal.abc.tmp")
    stale.write_text("partial")

    store.recover()

    assert not stale.exists()


def test_m3_target_directories_are_synced_before_the_journal_goes(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    real_sync = store_module._fsync_dir
    real_unlink = Path.unlink

    def sync(path: Path) -> None:
        events.append(("sync", str(path)))
        real_sync(path)

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".prev.tmp"):
            events.append(("backup", str(self.parent)))
        if self == paths.journal:
            events.append(("journal", str(self.parent)))
        real_unlink(self, missing_ok=missing_ok)

    store.save(config())
    monkeypatch.setattr(store_module, "_fsync_dir", sync)
    monkeypatch.setattr(Path, "unlink", unlink)

    store.save(config(hostname="again"))

    journal_at = events.index(("journal", str(paths.state_dir)))
    for directory in {paths.bridge_config.parent, paths.nm_connections_dir, paths.state_dir}:
        last_backup = max(
            i for i, event in enumerate(events) if event == ("backup", str(directory))
        )
        synced = ("sync", str(directory))
        assert synced in events[last_backup:journal_at], directory
    assert events[-1] == ("sync", str(paths.state_dir))


def test_m3_a_rollback_makes_the_journal_removal_durable_first(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config(hostname="old-name"))
    events: list[str] = []
    real_sync = store_module._fsync_dir
    real_restore = store_module._restore

    def sync(path: Path) -> None:
        events.append(f"sync:{path}")
        real_sync(path)

    def restore(target: Path) -> None:
        events.append("restore")
        real_restore(target)

    monkeypatch.setattr(store_module, "_fsync_dir", sync)
    monkeypatch.setattr(store_module, "_restore", restore)
    fail_nth_replace(monkeypatch, 1, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save(config(hostname="new-name"))

    assert events[events.index("restore") - 1] == f"sync:{paths.state_dir}"


def test_a_failure_tidying_up_after_the_commit_still_counts_as_saved(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config(hostname="old-name"))

    def fails(self: NodeStore, entries: Any) -> None:
        raise OSError("I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(NodeStore, "_finish_commit", fails)
        store.save(config(hostname="new-name"))

    store.recover()

    saved = store.load()
    assert saved is not None and saved.hostname == "new-name"
    assert not paths.journal.exists()


def test_m5_the_saved_profile_names_the_interface(paths: Paths) -> None:
    NodeStore(paths, chown=lambda path: None, interface="wlan7").save(config())

    assert "interface-name=wlan7" in paths.nm_keyfile.read_text()


def test_m5_a_wifi_repair_keeps_naming_the_interface(paths: Paths) -> None:
    store = NodeStore(paths, chown=lambda path: None, interface="wlan7")
    store.save(config())

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert "interface-name=wlan7" in paths.nm_keyfile.read_text()


# --- M3: a removed stray bridge config is reported ----------------------------------


def test_m3_removing_a_stray_bridge_config_logs_a_warning(
    store: NodeStore, paths: Paths, caplog: pytest.LogCaptureFixture
) -> None:
    paths.bridge_config.parent.mkdir(parents=True)
    paths.bridge_config.write_text("node_id: stray\nbroker:\n  password: stray-s3cret\n")

    store.recover()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bridge" in warnings[0].getMessage() and "removed" in warnings[0].getMessage()
    assert "stray-s3cret" not in caplog.text


def test_m3_nothing_is_logged_when_there_is_no_stray_bridge_config(
    store: NodeStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.recover()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_m3_a_saved_nodes_bridge_config_is_not_reported_as_stray(
    store: NodeStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.save(config())

    store.recover()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_m3_the_example_config_warns_that_a_hand_placed_file_is_removed() -> None:
    example = Path(__file__).parents[2] / "bridge" / "config" / "bridge.example.yaml"

    assert "blank node" in example.read_text() and "removed" in example.read_text()


# --- tracker: a failed settle leaves the backups for the next start -----------------


def test_a_failed_settle_keeps_the_backups_a_later_start_needs(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())
    backup = paths.bridge_config.with_name(".bridge.yaml.prev.tmp")
    backup.write_text("the previous config")

    def fail() -> None:
        raise OSError("disk trouble")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_settle_interrupted_save", fail)
        store.recover()

    assert backup.read_text() == "the previous config"

    # With the disk back, the next start finishes the rollback and tidies up.
    store.recover()

    assert paths.bridge_config.read_text() == "the previous config"
    assert not backup.exists()


def test_a_successful_settle_still_sweeps(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    stale = paths.bridge_config.with_name(".bridge.yaml.abc.tmp")
    stale.write_text("partial")

    store.recover()

    assert not stale.exists()


# --- owner decision: the NTP drop-in is part of the atomic save ----------------------


def test_save_writes_the_ntp_dropin_defaulting_to_the_broker(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())

    assert paths.ntp_dropin.read_text() == "[Time]\nNTP=10.0.0.2\n"
    assert stat.S_IMODE(paths.ntp_dropin.stat().st_mode) == 0o644


def test_save_writes_an_explicit_ntp_server(store: NodeStore, paths: Paths) -> None:
    store.save(config(ntp_server="time.site.local"))

    assert paths.ntp_dropin.read_text() == "[Time]\nNTP=time.site.local\n"
    saved = store.load()
    assert saved is not None and saved.ntp_server == "time.site.local"


def test_the_ntp_dropin_is_not_given_to_the_service_user(paths: Paths) -> None:
    chowned: list[Path] = []

    NodeStore(paths, chown=chowned.append).save(config())

    assert all(path.parent != paths.ntp_dropin.parent for path in chowned)


def test_a_failed_save_keeps_the_old_ntp_dropin(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config(ntp_server="old.site.local"))
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, 5, OSError("disk trouble"))
        with pytest.raises(OSError):
            store.save(config(ntp_server="new.site.local"))

    assert paths.ntp_dropin.read_text() == "[Time]\nNTP=old.site.local\n"


@pytest.mark.parametrize("step", range(1, 7))
def test_a_power_cut_during_commit_finishes_the_ntp_dropin_on_recovery(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    store.save(config(ntp_server="old.site.local"))
    with monkeypatch.context() as patch:
        fail_nth_replace(patch, step, SimulatedPowerCut())
        with pytest.raises(SimulatedPowerCut):
            store.save(config(ntp_server="new.site.local"))

    store.recover()

    assert paths.ntp_dropin.read_text() == "[Time]\nNTP=new.site.local\n"
    assert stat.S_IMODE(paths.ntp_dropin.stat().st_mode) == 0o644
    assert not list(paths.ntp_dropin.parent.glob(".*.tmp"))


def test_a_power_cut_while_staging_keeps_the_old_ntp_dropin(paths: Paths) -> None:
    store = NodeStore(paths, chown=lambda path: None)
    store.save(config(ntp_server="old.site.local"))

    def cut(path: Path) -> None:
        raise SimulatedPowerCut()

    with pytest.raises(SimulatedPowerCut):
        NodeStore(paths, chown=cut).save(config(ntp_server="new.site.local"))

    store.recover()

    assert paths.ntp_dropin.read_text() == "[Time]\nNTP=old.site.local\n"


def test_a_wifi_repair_changes_neither_the_ntp_dropin_nor_the_saved_value(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config(ntp_server="time.site.local"))
    before = paths.ntp_dropin.read_bytes()

    store.save_wifi("Other-WiFi", "another-psk")

    assert paths.ntp_dropin.read_bytes() == before
    saved = store.load()
    assert saved is not None and saved.ntp_server == "time.site.local"


def test_a_config_saved_before_the_ntp_field_loads_as_following_the_broker(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())
    node = json.loads(paths.node_file.read_text())
    del node["ntp_server"]
    paths.node_file.write_text(json.dumps(node))

    saved = store.load()

    assert saved is not None and effective_ntp_server(saved) == "10.0.0.2"


# --- central-only access -----------------------------------------------------------


def test_save_writes_central_hosts_nft_atomically_with_the_config(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())

    assert paths.central_hosts in paths.targets()
    assert "central_hosts { 10.0.0.2 }" in paths.central_hosts.read_text()

    store.save(config(broker=replace(config().broker, host="10.0.0.9")))

    assert "central_hosts { 10.0.0.9 }" in paths.central_hosts.read_text()
    assert yaml.safe_load(paths.bridge_config.read_text())["broker"]["host"] == "10.0.0.9"


def test_a_failed_commit_keeps_the_old_firewall_set_with_the_old_config(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())
    before = snapshot(paths)
    fail_nth_replace(monkeypatch, 4, OSError("I/O error"))

    with pytest.raises(OSError):
        store.save(config(broker=replace(config().broker, host="10.0.0.9")))

    assert snapshot(paths) == before


def test_a_power_cut_mid_commit_finishes_the_firewall_set_too(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())
    fail_nth_replace(monkeypatch, 4, SimulatedPowerCut())
    with pytest.raises(SimulatedPowerCut):
        store.save(config(broker=replace(config().broker, host="10.0.0.9")))
    monkeypatch.undo()

    store.recover()

    assert "central_hosts { 10.0.0.9 }" in paths.central_hosts.read_text()
    assert yaml.safe_load(paths.bridge_config.read_text())["broker"]["host"] == "10.0.0.9"


def test_save_wifi_leaves_the_firewall_set_alone(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    before = paths.central_hosts.read_bytes()

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert paths.central_hosts.read_bytes() == before


# --- the picture flip --------------------------------------------------------------


def test_the_upside_down_box_is_saved_where_camera_link_reads_it(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config(camera_upside_down=True))

    assert json.loads(paths.node_file.read_text())["camera_upside_down"] is True
    saved = store.load()
    assert saved is not None and saved.camera_upside_down is True


def test_a_wifi_repair_keeps_the_upside_down_box(store: NodeStore, paths: Paths) -> None:
    store.save(config(camera_upside_down=True))

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert json.loads(paths.node_file.read_text())["camera_upside_down"] is True


# --- configs saved by an older image ----------------------------------------------


def old_image_files(store: NodeStore, paths: Paths) -> None:
    """A node as the Tailscale-era store left it."""
    store.save(config())
    node = json.loads(paths.node_file.read_text())
    del node["camera_upside_down"]
    node["vpn_requested"] = True
    paths.node_file.write_text(json.dumps(node))


def test_an_old_config_with_tailscale_keys_still_loads(store: NodeStore, paths: Paths) -> None:
    old_image_files(store, paths)

    saved = store.load()

    assert saved is not None
    assert saved.node_id == "cam-01"
    assert saved.camera_upside_down is False
    assert not hasattr(saved, "vpn_requested")


def test_an_old_config_that_no_longer_passes_a_save_still_loads(
    store: NodeStore, paths: Paths
) -> None:
    """The form must show it, and the node must not count as damaged, until someone saves."""
    store.save(config())
    bridge = yaml.safe_load(paths.bridge_config.read_text())
    bridge["broker"]["host"] = "broker.lan"
    bridge["broker"]["password"] = "pa$$ word"
    bridge["covered_zones"] = ["front door"]
    paths.bridge_config.write_text(yaml.safe_dump(bridge))

    saved = store.load()

    assert saved is not None and saved.broker.host == "broker.lan"
    with pytest.raises(ValueError, match="broker.host"):
        store.save(replace(saved, wifi_psk="a-real-psk-1"))


def test_an_old_tailscale_key_is_deleted_on_load(store: NodeStore, paths: Paths) -> None:
    old_image_files(store, paths)
    key = paths.state_dir / "tailscale.key"
    key.write_text("tskey-auth-s3cret")
    copies = [
        paths.state_dir / name
        for name in (
            ".tailscale.key.prev.tmp",
            ".tailscale.key.staged.tmp",
            "..tailscale.key.staged.tmp.abc123.tmp",
        )
    ]
    for copy in copies:
        copy.write_text("tskey-auth-s3cret")

    assert store.load() is not None

    assert not key.exists()
    assert [copy for copy in copies if copy.exists()] == []


def test_an_old_tailscale_key_is_deleted_even_with_no_usable_config(
    store: NodeStore, paths: Paths
) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    key = paths.state_dir / "tailscale.key"
    key.write_text("tskey-auth-s3cret")

    assert store.load() is None

    assert not key.exists()


def test_a_key_that_cannot_be_deleted_does_not_stop_the_load(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_image_files(store, paths)
    key = paths.state_dir / "tailscale.key"
    key.write_text("tskey-auth-s3cret")
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == key:
            raise PermissionError("nope")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    assert store.load() is not None


def test_a_wifi_repair_drops_the_old_vpn_key_from_the_node_file(
    store: NodeStore, paths: Paths
) -> None:
    old_image_files(store, paths)

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert "vpn_requested" not in json.loads(paths.node_file.read_text())


def test_a_save_writes_no_tailscale_key_at_all(store: NodeStore, paths: Paths) -> None:
    old_image_files(store, paths)

    store.save(config())

    assert "vpn_requested" not in json.loads(paths.node_file.read_text())
    assert not (paths.state_dir / "tailscale.key").exists()


@pytest.mark.parametrize("value", ["yes", 1, "true", None, []])
def test_only_a_plain_true_flips_the_picture(
    store: NodeStore, paths: Paths, value: Any
) -> None:
    store.save(config())
    node = json.loads(paths.node_file.read_text())
    node["camera_upside_down"] = value
    paths.node_file.write_text(json.dumps(node))

    saved = store.load()

    assert saved is not None and saved.camera_upside_down is False


def test_a_journal_lists_only_targets_to_finish(store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    store.save(config())
    fail_nth_replace(monkeypatch, 1, SimulatedPowerCut())
    with pytest.raises(SimulatedPowerCut):
        store.save(config(hostname="again"))
    monkeypatch.undo()

    entries = json.loads(paths.journal.read_text())

    assert [set(entry) for entry in entries] == [{"target"}] * len(entries)
