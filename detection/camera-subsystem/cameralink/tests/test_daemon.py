"""The link daemon: what it decides each tick, what it persists, and what it never does."""

import configparser
import fcntl
import functools
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cameralink.__main__ import main
from cameralink.adopt import (
    FAILURE_CAP,
    FAILURE_WINDOW_SECONDS,
    Lease,
    LinkState,
    step,
)
from cameralink.daemon import (
    TICK_SECONDS,
    Daemon,
    Paths,
    Systemctl,
    SystemdError,
    clear_limits,
    read_lease,
)
from cameralink.frigate_config import proxy_secret
from cameralink.reolink import ApiError, AuthFailed
from cameralink.store import CameraRecord, CameraStore, StoreError

MAC = "ec:71:db:8d:a0:40"
OTHER_MAC = "ec:71:db:00:00:01"
IP = "10.77.0.10"
NOW = 1_800_000_000
CAMERA_PASSWORD = "cam-S3cret-password"
OTHER_PASSWORD = "other-S3cret-password"
RELAY_PASSWORD = "relay-S3cret-password"
UNITS = Path(__file__).parents[1] / "ops"
NFT = Path(__file__).parents[2] / "provision" / "ops" / "black-relay.nft"


class FakeSystemd:
    """Records every request. Sees the config and env as they were at each restart."""

    def __init__(self, paths):
        self.paths = paths
        self.calls = []
        self.running = set()
        self.broken = False
        self.seen_at_restart = []

    def _call(self, action, unit):
        if self.broken:
            raise SystemdError(f"{action} {unit}: no")
        self.calls.append((action, unit))

    def is_running(self, unit):
        if self.broken:
            raise SystemdError(f"is-active {unit}: no")
        return unit in self.running

    def start(self, unit):
        self._call("start", unit)
        self.running.add(unit)

    def restart(self, unit):
        self._call("restart", unit)
        self.running.add(unit)
        self.seen_at_restart.append(
            (self.paths.frigate_config.read_text(), self.paths.frigate_env.read_text())
        )

    def stop(self, unit):
        self._call("stop", unit)
        self.running.discard(unit)

    def stop_and_wait(self, unit):
        self._call("stop_and_wait", unit)
        self.running.discard(unit)

    def frigate_calls(self):
        return [action for action, unit in self.calls if unit == "frigate.service"]

    def dhcp_calls(self):
        return [action for action, unit in self.calls if unit == "camera-link-dhcp.service"]


class FakeStep:
    """Stands in for adopt.step and leaves the record a real run would have left."""

    def __init__(self):
        self.state = LinkState.ADOPTED
        self.passwords = {MAC: CAMERA_PASSWORD, OTHER_MAC: OTHER_PASSWORD}
        self.calls = []
        self.raises = None
        self.during = None

    def __call__(self, lease, generation, store, client_factory, now, *, node_id=None, flipped=None):
        self.calls.append(
            SimpleNamespace(lease=lease, generation=generation, node_id=node_id, flipped=flipped)
        )
        if self.during is not None:
            self.during()
        if self.raises is not None:
            raise self.raises
        if lease is None:
            return LinkState.NO_CAMERA
        with store.lock():
            record = store.load(lease.mac) or CameraRecord(lease.mac, "", None, None)
            store.save(replace(record, password=self.passwords.get(lease.mac, "")))
        return self.state

    @property
    def last(self):
        return self.calls[-1]


class Node:
    """A node's files under tmp_path, with a daemon that can be restarted."""

    def __init__(self, root: Path):
        self.root = root
        self.paths = Paths(
            lease_file=root / "camera-link" / "dnsmasq.leases",
            store=root / "camera-link" / "cameras.json",
            link_state=root / "camera-link" / "link.json",
            carrier=root / "sys" / "carrier",
            carrier_changes=root / "sys" / "carrier_changes",
            boot_id=root / "proc" / "boot_id",
            bridge_config=root / "etc" / "bridge.yaml",
            node_file=root / "provision" / "node.json",
            frigate_config=root / "frigate" / "config.yml",
            frigate_env=root / "etc" / "frigate.env",
            state_file=root / "run" / "state.json",
        )
        for directory in ("camera-link", "sys", "proc", "etc", "provision", "frigate", "run"):
            (root / directory).mkdir()
        self.clock_epoch = NOW
        self.systemd = FakeSystemd(self.paths)
        self.step = FakeStep()
        self.set_carrier(True)
        self.set_boot("boot-a")
        self.daemon = self.start_daemon()
        # The first tick of a boot resets DHCP. Most tests start after that.

    def clock(self):
        return datetime.fromtimestamp(self.clock_epoch, UTC)

    def start_daemon(self, step_fn=None):
        return Daemon(self.paths, self.systemd, step_fn=step_fn or self.step, clock=self.clock)

    def restart_daemon(self):
        self.daemon = self.start_daemon()

    def set_carrier(self, up):
        self.paths.carrier.write_text("1\n" if up else "0\n")

    def set_boot(self, boot_id):
        self.paths.boot_id.write_text(boot_id + "\n")

    def set_lease(self, mac=MAC, ip=IP, expiry=None):
        expiry = NOW + 3600 if expiry is None else expiry
        self.paths.lease_file.write_text(f"{expiry} {mac} {ip} * 01:{mac}\n")

    def provision(self, **overrides):
        config = {
            "node_id": "cam-8da040",
            "broker": {
                "host": "10.0.0.5",
                "port": 1883,
                "username": "kit1",
                "password": RELAY_PASSWORD,
                "tls": False,
            },
            "covered_zones": ["driveway", "gate"],
        }
        config.update(overrides)
        self.paths.bridge_config.write_text(yaml.safe_dump(config))

    def settle_boot(self):
        """Run the boot's DHCP reset. It deletes the lease file, so a lease goes in after."""
        self.daemon.tick()
        self.systemd.calls.clear()

    def state(self):
        return json.loads(self.paths.state_file.read_text())

    def seed(self, **fields):
        store = CameraStore(self.paths.store)
        with store.lock():
            record = store.load(fields.get("mac", MAC)) or CameraRecord(
                fields.get("mac", MAC), "", None, None
            )
            store.save(replace(record, **fields))


@pytest.fixture
def node(tmp_path):
    return Node(tmp_path)


@pytest.fixture
def live(node):
    """Provisioned, with a camera on a lease, and the boot's DHCP reset done."""
    node.provision()
    node.settle_boot()
    node.set_lease()
    return node


def all_files_text(root):
    return "\n".join(
        path.read_text(errors="replace") for path in root.rglob("*") if path.is_file()
    )


# Leases


def test_lease_file_is_parsed_and_stale_leases_are_ignored(tmp_path):
    leases = tmp_path / "leases"
    leases.write_text(f"{NOW + 100} EC:71:DB:8D:A0:40 {IP} * 01:ec\n")
    assert read_lease(leases, NOW) == Lease(MAC, IP)
    leases.write_text(f"{NOW - 1} {MAC} {IP} * 01:ec\n")
    assert read_lease(leases, NOW) is None
    leases.write_text(f"{NOW} {MAC} {IP} * 01:ec\n")
    assert read_lease(leases, NOW) is None


def test_the_newest_lease_wins_and_never_expiring_counts(tmp_path):
    leases = tmp_path / "leases"
    leases.write_text(f"{NOW + 100} {OTHER_MAC} {IP} * *\n{NOW + 200} {MAC} 10.77.0.11 * *\n")
    assert read_lease(leases, NOW) == Lease(MAC, "10.77.0.11")
    leases.write_text(f"0 {OTHER_MAC} {IP} * *\n{NOW + 200} {MAC} {IP} * *\n")
    assert read_lease(leases, NOW) == Lease(OTHER_MAC, IP)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        f"soon {MAC} {IP} * *",
        f"{NOW + 9} not-a-mac {IP} * *",
        f"{NOW + 9} {MAC} not-an-ip * *",
        f"{NOW + 9} {MAC}",
    ],
)
def test_a_damaged_lease_line_is_no_lease(tmp_path, line):
    leases = tmp_path / "leases"
    leases.write_text(line + "\n")
    assert read_lease(leases, NOW) is None


def test_a_missing_lease_file_is_no_lease(tmp_path):
    assert read_lease(tmp_path / "absent", NOW) is None


def test_no_carrier_means_no_camera_whatever_the_lease_file_says(live):
    live.daemon.tick()
    live.set_carrier(False)
    live.daemon.tick()
    assert live.step.last.lease is None
    assert live.state()["mac"] is None
    assert live.state()["state"] == "no_camera"


def test_an_unreadable_carrier_file_means_no_carrier(live):
    live.paths.carrier.unlink()
    live.daemon.tick()
    assert live.step.last.lease is None


# Link generation


def test_carrier_change_bumps_the_link_generation(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.daemon.tick()
    assert live.step.last.generation == first
    live.set_carrier(False)
    live.daemon.tick()
    assert live.step.last.generation == first + 1
    live.set_carrier(True)
    live.daemon.tick()
    assert live.step.last.generation == first + 2


def test_a_flap_between_ticks_bumps_though_the_carrier_looks_the_same(live):
    """A tick can block for two minutes. The carrier is up again by the next look."""
    live.paths.carrier_changes.write_text("4\n")
    live.daemon.tick()
    first = live.step.last.generation
    live.daemon.tick()
    assert live.step.last.generation == first
    live.paths.carrier_changes.write_text("6\n")
    live.daemon.tick()
    assert live.step.last.generation == first + 1
    assert live.systemd.dhcp_calls() == ["stop_and_wait", "start"]


def test_a_flap_count_appearing_or_going_is_not_a_flap(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.paths.carrier_changes.write_text("4\n")
    live.daemon.tick()
    live.paths.carrier_changes.unlink()
    live.daemon.tick()
    assert live.step.last.generation == first


def test_lease_ip_change_bumps_the_generation(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.set_lease(ip="10.77.0.11")
    live.daemon.tick()
    assert live.step.last.generation == first + 1
    assert live.step.last.lease.ip == "10.77.0.11"


def test_a_lease_that_goes_and_comes_back_at_the_same_ip_is_the_same_generation(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.paths.lease_file.unlink()
    live.daemon.tick()
    live.set_lease()
    live.daemon.tick()
    assert live.step.last.generation == first


def test_a_daemon_restart_keeps_the_generation(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.generation == first


def test_a_change_while_the_daemon_was_down_still_bumps(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.set_carrier(False)
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.generation == first + 1


def test_every_boot_bumps_the_generation_and_it_never_falls(live):
    live.daemon.tick()
    seen = [live.step.last.generation]
    for boot in ("boot-b", "boot-c"):
        live.set_boot(boot)
        live.restart_daemon()
        live.daemon.tick()
        seen.append(live.step.last.generation)
    assert seen == [seen[0], seen[0] + 1, seen[0] + 2]


def test_a_boot_and_a_carrier_change_together_bump_once(live):
    live.daemon.tick()
    first = live.step.last.generation
    live.set_boot("boot-b")
    live.set_carrier(False)
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.generation == first + 1


def test_a_reboot_repeats_the_settle_with_the_real_step(live):
    """The tracker's stale settle: a since from before a reboot must not count as elapsed."""
    live.daemon.tick()
    generation = live.step.last.generation
    logins = []
    real = functools.partial(
        step, probe=lambda host, port: port == 9000, enable=lambda *a: logins.append(a)
    )
    live.set_boot("boot-b")
    live.daemon = live.start_daemon(step_fn=real)
    live.daemon.tick()  # the boot's DHCP reset removes the lease
    live.set_lease()
    # A settle stamped under the generation the old boot would have reached is not this one.
    live.seed(settle_key=f"{MAC}/{generation}", settle_since=-1000.0)
    live.daemon.tick()
    assert live.state()["state"] == "adopting"
    assert logins == []


def test_a_damaged_link_file_jumps_past_any_counter_and_forgets_no_camera(live):
    live.daemon.tick()
    live.paths.link_state.write_text("{not json")
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.generation >= NOW


@pytest.mark.parametrize(
    "content",
    [
        '{"generation": true}',
        "{}",
        '{"generation": -1, "boot_id": "b", "carrier": true}',
        '{"generation": 1.5, "boot_id": "b", "carrier": true}',
        "[]",
    ],
)
def test_a_link_file_of_the_wrong_shape_counts_as_damaged(live, content):
    live.paths.link_state.write_text(content)
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.generation >= NOW


def test_a_generation_is_on_disk_before_any_step_can_use_it(live):
    live.daemon.tick()
    live.set_carrier(False)
    seen = []
    live.step.during = lambda: seen.append(
        json.loads(live.paths.link_state.read_text())["generation"]
    )
    live.daemon.tick()
    assert seen == [live.step.last.generation]


def test_no_step_runs_when_the_generation_cannot_be_saved(live, monkeypatch):
    live.daemon.tick()
    calls = len(live.step.calls)
    live.set_carrier(False)
    real = os.replace

    def refuse(source, target):
        if Path(target) == live.paths.link_state:
            raise OSError(28, "No space left on device")
        real(source, target)

    monkeypatch.setattr(os, "replace", refuse)
    live.daemon.tick()
    assert len(live.step.calls) == calls
    assert live.state()["state"] == "failed"
    assert live.state()["reason"]
    monkeypatch.setattr(os, "replace", real)
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.daemon.tick()
    assert live.step.last.generation == generation + 1


def test_an_unreadable_boot_id_runs_no_step_and_survives(live):
    live.daemon.tick()
    calls = len(live.step.calls)
    live.paths.boot_id.unlink()
    live.daemon.tick()
    assert len(live.step.calls) == calls
    assert live.state()["state"] == "failed"


# The DHCP server holds its one address for the old MAC until the lease ends, measured:
# "no address available" for a swapped camera until the lease is wiped and dnsmasq restarted.


def test_a_rising_carrier_wipes_the_lease_and_restarts_dhcp(live):
    live.daemon.tick()
    live.set_carrier(False)
    live.daemon.tick()
    assert live.systemd.dhcp_calls() == []
    live.set_carrier(True)
    live.daemon.tick()
    assert live.systemd.dhcp_calls() == ["stop_and_wait", "start"]
    assert live.step.last.lease is None
    assert not live.paths.lease_file.exists()


def test_a_boot_wipes_the_lease_before_it_is_trusted(node):
    node.provision()
    node.set_lease()
    node.daemon.tick()
    assert node.systemd.dhcp_calls() == ["stop_and_wait", "start"]
    assert node.step.last.lease is None
    assert not node.paths.lease_file.exists()


def test_a_boot_with_no_carrier_waits_for_the_carrier_to_reset_dhcp(node):
    node.set_carrier(False)
    node.daemon.tick()
    assert node.systemd.dhcp_calls() == []
    node.set_carrier(True)
    node.daemon.tick()
    assert node.systemd.dhcp_calls() == ["stop_and_wait", "start"]


def test_a_daemon_restart_does_not_reset_dhcp(live):
    live.daemon.tick()
    live.systemd.calls.clear()
    live.restart_daemon()
    live.daemon.tick()
    assert live.systemd.dhcp_calls() == []
    assert live.step.last.lease == Lease(MAC, IP)


def test_a_failed_dhcp_reset_is_retried_and_its_lease_is_not_trusted(live):
    live.daemon.tick()
    live.set_carrier(False)
    live.daemon.tick()
    live.set_carrier(True)
    live.systemd.broken = True
    live.daemon.tick()
    assert live.step.last.lease is None
    assert live.state()["reason"]
    live.restart_daemon()
    live.systemd.broken = False
    live.daemon.tick()
    assert live.systemd.dhcp_calls() == ["stop_and_wait", "start"]


# Starting and restarting Frigate


def test_adopted_with_config_renders_and_restarts_frigate_once(live):
    live.daemon.tick()
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]
    config = yaml.safe_load(live.paths.frigate_config.read_text())
    assert list(config["cameras"]) == ["cam-8da040"]
    env = live.paths.frigate_env.read_text()
    assert f"FRIGATE_CAMERA_PASSWORD={CAMERA_PASSWORD}\n" in env
    assert f"FRIGATE_RELAY_PASSWORD={RELAY_PASSWORD}\n" in env
    assert f"FRIGATE_PROXY_SECRET={proxy_secret(RELAY_PASSWORD)}\n" in env


def test_both_files_are_written_before_the_restart(live):
    live.daemon.tick()
    config, env = live.systemd.seen_at_restart[0]
    assert config == live.paths.frigate_config.read_text()
    assert CAMERA_PASSWORD in env


def test_the_env_file_is_root_only_and_the_config_holds_no_secret(live):
    live.daemon.tick()
    assert stat.S_IMODE(live.paths.frigate_env.stat().st_mode) == 0o600
    config = live.paths.frigate_config.read_text()
    for secret in (CAMERA_PASSWORD, RELAY_PASSWORD, proxy_secret(RELAY_PASSWORD)):
        assert secret not in config


def test_no_staged_file_is_left_behind(live):
    live.daemon.tick()
    live.daemon.tick()
    assert sorted(path.name for path in live.paths.frigate_env.parent.iterdir()) == [
        "bridge.yaml",
        "frigate.env",
    ]
    assert [path.name for path in live.paths.frigate_config.parent.iterdir()] == ["config.yml"]


def test_bridge_yaml_change_rerenders_and_restarts_frigate(live):
    live.daemon.tick()
    live.provision(node_id="cam-renamed")
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    assert list(yaml.safe_load(live.paths.frigate_config.read_text())["cameras"]) == ["cam-renamed"]
    assert live.step.last.node_id == "cam-renamed"


def test_unchanged_config_does_not_restart_frigate(live):
    live.daemon.tick()
    before = live.paths.frigate_config.stat().st_mtime_ns, live.paths.frigate_env.stat().st_mtime_ns
    live.restart_daemon()
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]
    assert before == (
        live.paths.frigate_config.stat().st_mtime_ns,
        live.paths.frigate_env.stat().st_mtime_ns,
    )


def test_a_changed_env_alone_restarts_frigate(live):
    live.daemon.tick()
    config = live.paths.frigate_config.read_text()
    live.provision(broker={"username": "kit1", "password": "a-new-relay-password"})
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    assert live.paths.frigate_config.read_text() == config
    assert "a-new-relay-password" in live.paths.frigate_env.read_text()


def test_a_changed_camera_password_alone_restarts_frigate(live):
    live.daemon.tick()
    live.step.passwords[MAC] = "a-new-camera-password"
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    assert "a-new-camera-password" in live.paths.frigate_env.read_text()


def test_a_changed_config_alone_restarts_frigate_once_and_keeps_the_drawing(live):
    """A Frigate UI edit shows as a changed file: one restart at the next render."""
    live.daemon.tick()
    config = yaml.safe_load(live.paths.frigate_config.read_text())
    drawn = "0.1,0.1,0.9,0.1,0.9,0.9"
    config["cameras"]["cam-8da040"]["zones"]["gate"]["coordinates"] = drawn
    live.paths.frigate_config.write_text(yaml.safe_dump(config))
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    rendered = yaml.safe_load(live.paths.frigate_config.read_text())
    assert rendered["cameras"]["cam-8da040"]["zones"]["gate"]["coordinates"] == drawn


def test_a_stopped_frigate_is_started_with_no_rewrite(live):
    live.daemon.tick()
    live.systemd.running.clear()
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "start"]


def test_a_restart_lost_to_a_crash_is_still_owed(live):
    live.daemon.tick()
    live.provision(node_id="cam-renamed")
    live.systemd.broken = True
    live.daemon.tick()
    assert live.state()["reason"]
    assert "cam-renamed" in live.paths.frigate_config.read_text()
    # Both files already match, so only the persisted flag can still ask for the restart.
    live.restart_daemon()
    live.systemd.broken = False
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    assert live.state()["reason"] is None


def test_adopted_pending_settings_runs_frigate(live):
    live.step.state = LinkState.ADOPTED_PENDING_SETTINGS
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]


def test_no_bridge_yaml_adopts_but_does_not_start_frigate(node):
    node.settle_boot()
    node.set_lease()
    node.daemon.tick()
    assert node.state()["state"] == "adopted"
    assert node.systemd.frigate_calls() == []
    assert not node.paths.frigate_config.exists()
    assert not node.paths.frigate_env.exists()
    assert node.step.last.node_id is None
    assert node.step.last.flipped is None


def test_a_lost_bridge_yaml_leaves_a_running_frigate_alone(live):
    live.daemon.tick()
    live.paths.bridge_config.unlink()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]
    assert "frigate.service" in live.systemd.running


# Errors from render


def assert_frigate_kept(live, config, env, reason_part):
    assert live.systemd.frigate_calls() == ["restart"]
    assert "frigate.service" in live.systemd.running
    assert live.paths.frigate_config.read_text() == config
    assert live.paths.frigate_env.read_text() == env
    state = live.state()
    assert state["state"] == "adopted"
    assert reason_part in state["reason"]
    for secret in (CAMERA_PASSWORD, RELAY_PASSWORD, "bad$password", proxy_secret(RELAY_PASSWORD)):
        assert secret not in state["reason"]


@pytest.mark.parametrize(
    ("change", "reason_part"),
    [
        ({"covered_zones": ["bad zone!"]}, "zone"),
        ({"node_id": "bad name"}, "node_id"),
        ({"broker": {"username": "bad:user", "password": "relay-pass-2"}}, "relay_user"),
        ({"broker": {"username": "kit1", "password": "bad$password"}}, "relay password"),
    ],
)
def test_a_render_error_keeps_frigate_and_both_files_and_reports_it(live, change, reason_part):
    live.daemon.tick()
    config = live.paths.frigate_config.read_text()
    env = live.paths.frigate_env.read_text()
    live.provision(**change)
    live.daemon.tick()
    assert_frigate_kept(live, config, env, reason_part)
    assert sorted(p.name for p in live.paths.frigate_env.parent.iterdir()) == [
        "bridge.yaml",
        "frigate.env",
    ]


def test_a_render_error_with_no_files_yet_starts_nothing_and_writes_nothing(live):
    live.provision(covered_zones=["bad zone!"])
    live.daemon.tick()
    assert live.systemd.frigate_calls() == []
    assert not live.paths.frigate_config.exists()
    assert not live.paths.frigate_env.exists()
    assert "zone" in live.state()["reason"]


def test_a_password_pasted_into_a_rejected_field_is_not_reported(live):
    live.provision(broker={"username": "pasted secret!", "password": "pasted secret!"})
    live.daemon.tick()
    assert "pasted secret!" not in live.paths.state_file.read_text()
    assert live.state()["reason"]


def test_an_unparseable_saved_config_is_kept_since_it_may_hold_drawings(live):
    live.daemon.tick()
    live.paths.frigate_config.write_text("cameras: [unclosed\n")
    env = live.paths.frigate_env.read_text()
    live.daemon.tick()
    assert_frigate_kept(live, "cameras: [unclosed\n", env, "config")


@pytest.mark.parametrize(
    "content",
    ["broker: [unclosed\n  password: leaked-from-yaml", "just a string\n", "broker: 5\nnode_id: a\n"],
)
def test_a_damaged_bridge_yaml_is_reported_with_none_of_its_text(live, content):
    live.daemon.tick()
    live.paths.bridge_config.write_text(content)
    live.daemon.tick()
    state = live.state()
    assert live.systemd.frigate_calls() == ["restart"]
    assert state["state"] == "adopted"
    assert "bridge.yaml" in state["reason"]
    assert "leaked-from-yaml" not in state["reason"]
    assert live.step.last.node_id is None


def test_an_unwritable_config_is_reported_and_retried(live, monkeypatch):
    real = os.replace

    def refuse(source, target):
        if Path(target) == live.paths.frigate_config:
            raise OSError(30, "Read-only file system")
        real(source, target)

    monkeypatch.setattr(os, "replace", refuse)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == []
    assert live.state()["reason"]
    monkeypatch.setattr(os, "replace", real)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]


# When Frigate must stop, and when it must not


@pytest.mark.parametrize(
    "state", [LinkState.FAILED, LinkState.ADOPTING, LinkState.NEEDS_RESET]
)
def test_the_same_camera_with_a_known_password_keeps_frigate(live, state):
    live.daemon.tick()
    live.step.state = state
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]


def test_an_unplugged_camera_keeps_frigate(live):
    live.daemon.tick()
    live.set_carrier(False)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]
    assert "frigate.service" in live.systemd.running


def test_needs_reset_with_no_known_password_stops_frigate(live):
    live.daemon.tick()
    live.step.state = LinkState.NEEDS_RESET
    live.step.passwords[MAC] = ""
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]


def test_needs_reset_with_no_known_password_does_not_start_frigate(live):
    live.step.state = LinkState.NEEDS_RESET
    live.step.passwords[MAC] = ""
    live.daemon.tick()
    assert live.systemd.frigate_calls() == []


@pytest.mark.parametrize(
    "state",
    [LinkState.ADOPTING, LinkState.NEEDS_RESET, LinkState.FAILED],
)
def test_a_new_mac_stops_frigate_until_it_is_adopted(live, state):
    live.daemon.tick()
    live.step.state = state
    live.set_lease(mac=OTHER_MAC)
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    live.step.state = LinkState.ADOPTED
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop", "restart"]
    assert OTHER_PASSWORD in live.paths.frigate_env.read_text()


def test_a_new_mac_stays_stopped_across_a_daemon_restart(live):
    live.daemon.tick()
    live.step.state = LinkState.ADOPTING
    live.set_lease(mac=OTHER_MAC)
    live.systemd.broken = True
    live.daemon.tick()
    live.systemd.broken = False
    live.restart_daemon()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]


def test_a_new_mac_adopted_with_a_render_error_still_stops_frigate(live):
    live.daemon.tick()
    live.provision(covered_zones=["bad zone!"])
    live.set_lease(mac=OTHER_MAC)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    assert CAMERA_PASSWORD in live.paths.frigate_env.read_text()


def test_a_new_mac_with_a_damaged_bridge_yaml_still_stops_frigate(live):
    live.daemon.tick()
    live.paths.bridge_config.write_text("{broken")
    live.set_lease(mac=OTHER_MAC)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]


def test_a_new_mac_adopted_at_once_restarts_with_its_own_password(live):
    live.daemon.tick()
    live.set_lease(mac=OTHER_MAC)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "restart"]
    assert OTHER_PASSWORD in live.paths.frigate_env.read_text()
    assert CAMERA_PASSWORD not in live.paths.frigate_env.read_text()


def test_a_damaged_link_file_treats_the_camera_as_new_until_adopted(live):
    live.daemon.tick()
    live.paths.link_state.write_text("{not json")
    live.restart_daemon()
    live.step.state = LinkState.ADOPTING
    live.set_lease()
    # The file's reset of DHCP removes the lease on the first tick; the lease then returns.
    live.daemon.tick()
    live.set_lease()
    live.daemon.tick()
    assert live.systemd.frigate_calls()[-1] == "stop"


# What reaches adopt.step


def test_node_id_and_flip_come_from_provisioning(live):
    live.daemon.tick()
    assert live.step.last.node_id == "cam-8da040"
    assert live.step.last.flipped is False
    live.paths.node_file.write_text(json.dumps({"hostname": "x", "camera_upside_down": True}))
    live.daemon.tick()
    assert live.step.last.flipped is True


@pytest.mark.parametrize(
    "content", ["{broken", '{"hostname": "x"}', '{"camera_upside_down": "yes"}', "[]"]
)
def test_flip_defaults_to_false(live, content):
    live.paths.node_file.write_text(content)
    live.daemon.tick()
    assert live.step.last.flipped is False


def test_the_store_is_locked_around_each_step(live):
    locked = []

    def probe_lock():
        fd = os.open(f"{live.paths.store}.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked.append(True)
        else:
            locked.append(False)
        finally:
            os.close(fd)

    live.step.during = probe_lock
    live.daemon.tick()
    assert locked == [True]


def test_the_daemon_uses_the_timeout_and_clock_step_defaults(live):
    """No override of enable_timeout or monotonic reaches step."""
    live.daemon.tick()  # FakeStep takes neither keyword, so passing one would raise
    assert live.state()["state"] == "adopted"
    assert TICK_SECONDS == 5


def test_a_real_adoption_from_factory_to_a_running_frigate(live):
    """The real step, against a fake camera, on the factory signature."""
    live.daemon.tick()
    live.daemon = None
    live.systemd.calls.clear()
    live.systemd.running.clear()
    live.paths.frigate_env.unlink()
    live.paths.frigate_config.unlink()

    class Camera:
        def __init__(self, ip):
            pass

        def login(self, user, password):
            pass

        def __getattr__(self, name):
            return lambda *args: None

    open_ports = {9000}
    real = functools.partial(
        step, probe=lambda host, port: port in open_ports, enable=_enable(open_ports)
    )
    daemon = Daemon(
        live.paths, live.systemd, step_fn=real, clock=live.clock, client_factory=Camera
    )
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.seed(mac=OTHER_MAC, settle_key=f"{OTHER_MAC}/{generation}", settle_since=-1000.0)
    live.set_lease(mac=OTHER_MAC)
    daemon.tick()
    assert live.state()["state"] == "adopted"
    assert live.systemd.frigate_calls() == ["restart"]
    record = CameraStore(live.paths.store).load(OTHER_MAC)
    assert record.password
    assert f"FRIGATE_CAMERA_PASSWORD={record.password}\n" in live.paths.frigate_env.read_text()
    assert record.password not in live.paths.state_file.read_text()


def _enable(open_ports):
    async def enable(host, password):
        open_ports.add(80)

    return enable


# The state file


def test_state_file_holds_no_secret(live):
    live.daemon.tick()
    live.daemon.tick()
    text = live.paths.state_file.read_text()
    for secret in (CAMERA_PASSWORD, RELAY_PASSWORD, proxy_secret(RELAY_PASSWORD)):
        assert secret not in text
    assert json.loads(text) == {
        "state": "adopted",
        "mac": MAC,
        "updated": NOW,
        "retry_after": None,
        "reason": None,
    }


def test_state_file_is_world_readable_even_under_a_tight_umask(live):
    old = os.umask(0o077)
    try:
        live.daemon.tick()
    finally:
        os.umask(old)
    assert stat.S_IMODE(live.paths.state_file.stat().st_mode) == 0o644


def test_a_state_file_that_cannot_be_written_does_not_stop_the_tick(live, monkeypatch):
    real = os.replace

    def refuse(source, target):
        if Path(target) == live.paths.state_file:
            raise OSError(28, "No space left on device")
        real(source, target)

    monkeypatch.setattr(os, "replace", refuse)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart"]


def test_no_provisioning_is_named_in_the_state_file(node):
    node.settle_boot()
    node.set_lease()
    node.daemon.tick()
    assert "provision" in node.state()["reason"]


def test_retry_after_reports_the_signature_limit(live):
    stamp = NOW - 1000.0
    live.seed(signature_failures=(stamp,))
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["retry_after"] == stamp + FAILURE_WINDOW_SECONDS


def test_retry_after_reports_the_rolling_cap_at_its_oldest_counted_stamp(live):
    stamps = tuple(NOW - 100.0 * (i + 1) for i in range(FAILURE_CAP))
    live.seed(failures=stamps)
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["retry_after"] == min(stamps) + FAILURE_WINDOW_SECONDS


def test_retry_after_takes_the_later_of_the_limits(live):
    stamps = tuple(NOW - 100.0 * (i + 1) for i in range(FAILURE_CAP))
    live.seed(failures=stamps, signature_failures=(NOW - 1.0,))
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["retry_after"] == NOW - 1.0 + FAILURE_WINDOW_SECONDS


def test_retry_after_is_null_under_the_cap_and_after_the_window(live):
    live.seed(
        failures=(NOW - 100.0,) * (FAILURE_CAP - 1),
        signature_failures=(NOW - FAILURE_WINDOW_SECONDS - 1.0,),
    )
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["retry_after"] is None


def test_retry_after_is_null_once_adopted(live):
    live.seed(signature_failures=(NOW - 100.0,))
    live.daemon.tick()
    assert live.state()["retry_after"] is None


def test_a_locked_camera_waits_for_a_reset_and_not_for_the_rolling_cap(live):
    live.seed(failures=(NOW - 100.0,) * FAILURE_CAP, locked=True)
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["retry_after"] is None


def test_retry_after_is_reported_with_the_real_step(live):
    stamp = NOW - 50.0
    live.daemon.tick()
    live.systemd.calls.clear()
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.seed(
        settle_key=f"{MAC}/{generation}",
        settle_since=-1000.0,
        attempted=None,
        phase=None,
        signature_failures=(stamp,),
    )
    real = functools.partial(step, probe=lambda host, port: port == 9000)
    live.daemon = live.start_daemon(step_fn=real)
    live.daemon.tick()
    state = live.state()
    assert state["state"] == "needs_reset"
    assert state["retry_after"] == stamp + FAILURE_WINDOW_SECONDS


# Survival


@pytest.mark.parametrize(
    "error",
    [StoreError("store is corrupt"), ApiError("boom"), AuthFailed("no"), SystemdError("no")],
    ids=lambda err: type(err).__name__,
)
def test_daemon_survives_camera_errors(live, error):
    live.daemon.tick()
    live.step.raises = error
    if isinstance(error, SystemdError):
        live.step.raises = None
        live.systemd.broken = True
    live.daemon.tick()
    live.daemon.tick()
    live.step.raises = None
    live.systemd.broken = False
    live.daemon.tick()
    assert live.state()["state"] == "adopted"
    assert "frigate.service" in live.systemd.running


def test_a_store_error_reports_failed_and_keeps_frigate(live):
    live.daemon.tick()
    live.step.raises = StoreError("store is corrupt")
    live.daemon.tick()
    state = live.state()
    assert state["state"] == "failed"
    assert state["mac"] == MAC
    assert "corrupt" in state["reason"]
    assert live.systemd.frigate_calls() == ["restart"]


def test_a_corrupt_store_survives_the_real_step(live):
    live.daemon.tick()
    live.paths.store.write_text("{corrupt")
    live.daemon = live.start_daemon(step_fn=step)
    live.daemon.tick()
    assert live.state()["state"] == "failed"
    assert "frigate.service" in live.systemd.running


def test_an_unexpected_error_is_not_swallowed(live):
    """Only the errors named are handled. A bug crashes to systemd's Restart=always, and the
    state file goes with the runtime directory, so no stale state is left to be believed."""
    live.step.raises = TypeError("a bug")
    with pytest.raises(TypeError):
        live.daemon.tick()


def test_run_ticks_until_stopped(live):
    stop = threading.Event()
    waits = []

    class Clock:
        def is_set(self):
            return stop.is_set()

        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stop.set()

    before = len(live.step.calls)
    live.daemon.run(Clock())
    assert len(live.step.calls) == before + 3
    assert waits == [TICK_SECONDS] * 3


# clear-limits


def seed_limits(path, **extra):
    store = CameraStore(path)
    with store.lock():
        return store.save(
            CameraRecord(
                MAC,
                CAMERA_PASSWORD,
                None,
                f"{MAC}/7",
                phase="needs_reset",
                failures=(NOW - 1.0,) * FAILURE_CAP,
                signature_failures=(NOW - 2.0,),
                **extra,
            )
        )


def test_clear_limits_clears_both_limits_and_nothing_else(tmp_path, capsys):
    path = tmp_path / "cameras.json"
    before = seed_limits(path, locked=True)
    assert clear_limits(path) == 0
    after = CameraStore(path).load()
    assert after.failures == ()
    assert after.signature_failures == ()
    assert after.password == before.password
    assert after.pending_password == before.pending_password
    assert after.locked is True
    assert after.attempted == before.attempted
    out = capsys.readouterr().out
    assert CAMERA_PASSWORD not in out
    assert MAC in out


def test_clear_limits_keeps_a_pending_password(tmp_path):
    path = tmp_path / "cameras.json"
    store = CameraStore(path)
    with store.lock():
        store.save(CameraRecord(MAC, "", "pending-secret", None, signature_failures=(1.0,)))
    assert clear_limits(path) == 0
    after = CameraStore(path).load()
    assert after.pending_password == "pending-secret"
    assert after.password == ""
    assert after.signature_failures == ()


def test_clear_limits_says_it_is_waiting_while_blocked(tmp_path):
    path = tmp_path / "cameras.json"
    seed_limits(path)
    holder = CameraStore(path)
    printed = threading.Event()
    lines = []

    def output(line):
        lines.append(line)
        printed.set()

    with holder.lock():
        worker = threading.Thread(target=clear_limits, args=(path,), kwargs={"out": output})
        worker.start()
        assert printed.wait(10)
        assert "waiting" in lines[0].lower()
        assert CameraStore(path).load().failures  # it has written nothing yet
    worker.join(10)
    assert not worker.is_alive()
    assert CameraStore(path).load().failures == ()


def test_clear_limits_with_no_record_is_fine(tmp_path, capsys):
    assert clear_limits(tmp_path / "cameras.json") == 0
    assert "no camera" in capsys.readouterr().out.lower()


def test_clear_limits_reports_a_store_error_with_no_traceback(tmp_path, capsys):
    path = tmp_path / "cameras.json"
    path.write_text("{corrupt")
    assert clear_limits(path) == 1
    assert "corrupt" in capsys.readouterr().err


def test_the_command_line_runs_clear_limits(tmp_path):
    path = tmp_path / "cameras.json"
    seed_limits(path)
    assert main(["clear-limits", "--store", str(path)]) == 0
    assert CameraStore(path).load().failures == ()


def test_the_console_script_is_declared():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert 'camera-link = "cameralink.__main__:main"' in pyproject


# The proxy secret


def test_proxy_secret_is_the_labelled_hash_of_the_broker_password():
    expected = hashlib.sha256(b"black-relay-frigate-proxy:" + b"pass\xc3\xa9").hexdigest()
    assert proxy_secret("passé") == expected
    assert (
        proxy_secret("relay")
        == hashlib.sha256(b"black-relay-frigate-proxy:relay").hexdigest()
    )


# systemctl


class Runner:
    def __init__(self, stdout="active\n", returncode=0, raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["timeout"] > 0
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(stdout=self.stdout, stderr="", returncode=self.returncode)


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("active\n", 0, True),
        ("activating\n", 3, True),
        ("reloading\n", 0, True),
        ("inactive\n", 3, False),
        ("failed\n", 3, False),
        ("deactivating\n", 3, False),
    ],
)
def test_systemctl_reads_is_active(stdout, returncode, expected):
    runner = Runner(stdout, returncode)
    assert Systemctl(run=runner).is_running("frigate.service") is expected
    assert runner.calls[0][-2:] == ["is-active", "frigate.service"]


@pytest.mark.parametrize(
    "runner",
    [
        Runner("", 1),
        Runner("nonsense\n", 0),
        Runner(raises=subprocess.TimeoutExpired("systemctl", 30)),
        Runner(raises=OSError(2, "gone")),
    ],
    ids=["empty", "unknown", "timeout", "missing"],
)
def test_systemctl_trouble_is_a_systemd_error(runner):
    with pytest.raises(SystemdError):
        Systemctl(run=runner).is_running("frigate.service")


def test_frigate_control_does_not_wait_on_the_job():
    """Frigate's container can take a minute to stop; a tick must not wait for it."""
    runner = Runner("", 0)
    control = Systemctl(run=runner)
    control.start("frigate.service")
    control.restart("frigate.service")
    control.stop("frigate.service")
    assert all("--no-block" in argv for argv in runner.calls)
    control.stop_and_wait("camera-link-dhcp.service")
    assert "--no-block" not in runner.calls[-1]


def test_a_refused_action_is_a_systemd_error():
    with pytest.raises(SystemdError):
        Systemctl(run=Runner("", 5)).restart("frigate.service")


# Units


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string((UNITS / name).read_text().replace("\\\n", " "))
    return parser


def words(text):
    return text.split()


def test_frigate_unit_runs_the_pinned_image_with_only_what_it_needs():
    frigate = unit("frigate.service")
    argv = words(frigate["Service"]["ExecStart"])
    assert argv[:2] == ["/usr/bin/docker", "run"]
    assert argv[-1] == (
        "ghcr.io/blakeblackshear/frigate:0.18.2-standard-arm64"
        "@sha256:4a854373c0fd4a8f550518758e34b929be0b423085bb569bb232390d128fa4ef"
    )
    for wanted in (
        "--network host",
        "--device /dev/hailo0",
        "--shm-size=128m",
        "--tmpfs /tmp/cache:size=256m",
        "-v /var/lib/frigate/config:/config",
        "-v /var/lib/frigate/media:/media/frigate",
        "-v /etc/localtime:/etc/localtime:ro",
        "--env-file /etc/camerabridge/frigate.env",
    ):
        assert wanted in " ".join(argv)
    assert "--privileged" not in argv
    assert sum(item == "--device" for item in argv) == 1


def test_frigate_unit_needs_its_firewall_and_is_only_started_by_the_daemon():
    frigate = unit("frigate.service")
    for field in ("Requires", "After"):
        assert set(words(frigate["Unit"][field])) >= {"nftables.service", "docker.service"}
    assert frigate["Unit"]["StartLimitIntervalSec"] == "0"
    assert frigate["Service"]["Restart"] == "always"
    assert "rm -f frigate" in frigate["Service"]["ExecStartPre"]
    assert "Install" not in frigate


def test_dhcp_unit_runs_the_sequence_proven_on_kit_1():
    dhcp = unit("camera-link-dhcp.service")
    argv = words(dhcp["Service"]["ExecStart"])
    assert argv[0] == "/usr/sbin/dnsmasq"
    for flag in (
        "--conf-file=/dev/null",
        "--port=0",
        "--bind-interfaces",
        "--interface=eth0",
        "--except-interface=lo",
        "--dhcp-range=10.77.0.10,10.77.0.10,255.255.255.0,1h",
        "--dhcp-option=3",
        "--dhcp-option=6",
        "--dhcp-leasefile=/var/lib/camera-link/dnsmasq.leases",
        "--keep-in-foreground",
    ):
        assert flag in argv
    assert "wlan0" not in argv
    assert dhcp["Service"]["StateDirectory"] == "camera-link"
    assert dhcp["Service"]["StateDirectoryMode"] == "0700"
    assert dhcp["Service"]["Restart"] == "always"
    assert "AF_PACKET" in dhcp["Service"]["RestrictAddressFamilies"]


def test_link_unit_is_ordered_and_can_read_bridge_yaml_and_reach_systemd():
    link = unit("camera-link.service")
    after = words(link["Unit"]["After"])
    assert {"NetworkManager.service", "nftables.service", "camera-link-dhcp.service"} <= set(after)
    service = link["Service"]
    assert service["Restart"] == "always"
    assert link["Unit"]["StartLimitIntervalSec"] == "0"
    assert service["StateDirectory"] == "camera-link"
    assert service["StateDirectoryMode"] == "0700"
    assert service["RuntimeDirectory"] == "camera-link"
    assert service["RuntimeDirectoryMode"] == "0755"
    assert "User" not in service
    # bridge.yaml is 0600 and owned by camerabridge, so root needs this to read it.
    assert "CAP_DAC_READ_SEARCH" in service["CapabilityBoundingSet"]
    # systemctl reaches systemd over /run/systemd/private, a unix socket.
    assert "AF_UNIX" in service["RestrictAddressFamilies"]
    assert "PrivateNetwork" not in service
    assert "InaccessiblePaths" not in service
    assert "IPAddressDeny" not in service
    # Everything it writes outside its state and runtime directories.
    assert set(words(service["ReadWritePaths"])) == {"/etc/camerabridge", "/var/lib/frigate/config"}
    assert service["ExecStart"].endswith("camera-link run")
    # Long enough to finish a tick with the 120 s enable_services timeout in it.
    assert int(service["TimeoutStopSec"]) >= 130


def test_units_do_not_tie_the_daemon_to_frigate():
    text = (UNITS / "camera-link.service").read_text()
    assert not re.search(r"^(Requires|BindsTo|PartOf|Wants)=.*frigate", text, re.MULTILINE)
    assert not re.search(r"^(BindsTo|PartOf)=.*", (UNITS / "frigate.service").read_text(), re.MULTILINE)


def test_camera_profile_is_static_with_no_route():
    profile = configparser.ConfigParser(interpolation=None)
    profile.read_string((UNITS / "black-relay-camera.nmconnection").read_text())
    assert profile["connection"]["type"] == "ethernet"
    assert profile["connection"]["interface-name"] == "eth0"
    assert profile["ipv4"]["method"] == "manual"
    assert profile["ipv4"]["address1"] == "10.77.0.1/24"
    assert profile["ipv4"]["never-default"] == "true"
    assert profile["ipv6"]["method"] == "disabled"


def test_docker_and_hailo_settings():
    assert json.loads((UNITS / "daemon.json").read_text()) == {
        "bridge": "none",
        "iptables": False,
        "ip6tables": False,
    }
    assert (UNITS / "hailo_pci.conf").read_text().strip().splitlines()[-1] == (
        "options hailo_pci force_desc_page_size=4096"
    )


def test_firewall_accepts_dhcp_on_the_camera_link_ahead_of_the_drop():
    lines = [line.strip() for line in NFT.read_text().splitlines()]
    accept = lines.index("iifname $CAMERA_IF udp sport 68 udp dport 67 counter accept")
    drop = lines.index("iifname $CAMERA_IF counter drop")
    assert accept < drop
    assert sum("$CAMERA_IF" in line and "accept" in line for line in lines) == 1
