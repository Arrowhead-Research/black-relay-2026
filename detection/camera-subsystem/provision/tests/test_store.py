"""Tests for ``cameraprovision.store``: all-or-nothing saves across several files."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from cameraprovision import store as store_module
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig
from cameraprovision.store import NodeStore, Paths

WIFI_PSK = "w1fi-s3cret-Psk"
BROKER_PASSWORD = "br0ker-s3cret-Pw"
TAILSCALE_KEY = "tskey-auth-s3cret"


def config(**overrides: Any) -> NodeConfig:
    values: dict[str, Any] = {
        "node_id": "cam-01",
        "hostname": "edge-7",
        "wifi_ssid": "Site WiFi; 2.4",
        "wifi_psk": WIFI_PSK,
        "tailscale_auth_key": TAILSCALE_KEY,
        "vpn_requested": True,
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
    )


@pytest.fixture
def store(paths: Paths) -> NodeStore:
    return NodeStore(paths, chown=lambda path: None)


def snapshot(paths: Paths) -> dict[str, bytes]:
    roots = {
        paths.bridge_config.parent,
        paths.nm_connections_dir,
        paths.state_dir,
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


COMMIT_STEPS = [1, 2, 3, 4, 5]


def test_save_writes_every_file_with_the_right_mode(store: NodeStore, paths: Paths) -> None:
    store.save(config())

    assert paths.nm_keyfile == paths.nm_connections_dir / "black-relay-uplink.nmconnection"
    for path in (paths.bridge_config, paths.nm_keyfile, paths.node_file, paths.tailscale_key):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(paths.mediamtx_env.stat().st_mode) == 0o644
    assert paths.tailscale_key.read_text() == TAILSCALE_KEY
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
        "tailscale.key",
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
    assert saved.vpn_requested is True
    assert saved.tailscale_auth_key == TAILSCALE_KEY


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


def test_the_tailscale_key_is_only_in_its_file(store: NodeStore, paths: Paths) -> None:
    store.save(config())

    holders = [name for name, data in snapshot(paths).items() if TAILSCALE_KEY.encode() in data]

    assert holders == [str(paths.tailscale_key)]


def test_after_the_key_is_deleted_the_config_stays_valid_and_owes_no_login(
    store: NodeStore, paths: Paths
) -> None:
    store.save(config())

    store.delete_tailscale_key()

    saved = store.load()
    assert saved is not None
    assert saved.vpn_requested is False
    assert not paths.tailscale_key.exists()


def test_a_config_without_vpn_removes_a_pending_key(store: NodeStore, paths: Paths) -> None:
    store.save(config())

    store.save(config(vpn_requested=False, tailscale_auth_key=""))

    assert not paths.tailscale_key.exists()


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
        store.save(config(wifi_psk="short", node_id="BAD", tailscale_auth_key=" " + TAILSCALE_KEY))

    assert "short" not in str(raised.value)
    assert TAILSCALE_KEY not in str(raised.value)


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
        store.save(config(vpn_requested=False, tailscale_auth_key=""))

    assert paths.tailscale_key.read_text() == TAILSCALE_KEY


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
        paths.tailscale_key.with_name(".tailscale.key.abc.tmp"),
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


def test_staged_keyfile_is_hidden_from_networkmanager(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, store: NodeStore
) -> None:
    """NM's keyfile plugin ignores dot-files, so it can never load half a transaction."""
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
    bridge, key, mediamtx = (
        paths.bridge_config.read_bytes(),
        paths.tailscale_key.read_bytes(),
        paths.mediamtx_env.read_bytes(),
    )

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    text = paths.nm_keyfile.read_text()
    assert "ssid=Fixed-WiFi" in text and "psk=new-wifi-psk" in text
    assert "dhcp-hostname=kept-name" in text
    assert stat.S_IMODE(paths.nm_keyfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.node_file.stat().st_mode) == 0o600
    assert paths.bridge_config.read_bytes() == bridge
    assert paths.tailscale_key.read_bytes() == key
    assert paths.mediamtx_env.read_bytes() == mediamtx
    saved = store.load()
    assert saved is not None and saved.wifi_ssid == "Fixed-WiFi"
    assert "new-wifi-psk" not in paths.node_file.read_text()


def test_save_wifi_works_once_the_tailscale_key_is_gone(store: NodeStore, paths: Paths) -> None:
    store.save(config())
    store.delete_tailscale_key()

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")

    assert "psk=new-wifi-psk" in paths.nm_keyfile.read_text()
    assert not paths.tailscale_key.exists()


def test_save_wifi_never_rewrites_the_bridge_config_or_revalidates_it(
    store: NodeStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(config())

    def must_not_run(*args: Any) -> None:
        raise AssertionError("a WiFi repair rewrote the bridge config")

    monkeypatch.setattr(store_module, "write_bridge_config", must_not_run)

    store.save_wifi("Fixed-WiFi", "new-wifi-psk")


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
