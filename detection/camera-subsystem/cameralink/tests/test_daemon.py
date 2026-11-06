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
import time
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
from cameralink import daemon as daemon_module
from cameralink.daemon import (
    DETECTOR_ERROR_RESTART_SECONDS,
    DETECTOR_RESTART_LIMIT_SECONDS,
    FRIGATE_DOWN_TICKS,
    FRIGATE_UNIT,
    LOG_SCAN_SECONDS,
    RUNNING_STATES,
    TICK_SECONDS,
    Daemon,
    FrigateLog,
    Paths,
    Systemctl,
    SystemdError,
    clear_limits,
    read_lease,
)
from cameralink.frigate_config import FrigateConfigError, proxy_secret
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


class FakeFrigateLog:
    """Frigate's container as docker reports it: when it started, and what it has logged."""

    def __init__(self):
        self.started = "2026-11-05T10:00:00.000000000Z"
        self.text = ""
        self.since = []

    def started_at(self):
        return self.started

    def since_time(self, since):
        self.since.append(since)
        return self.text


@pytest.fixture(autouse=True)
def frigate_log(monkeypatch):
    """No test here may reach a real docker."""
    log = FakeFrigateLog()
    monkeypatch.setattr(daemon_module, "FrigateLog", lambda: log)
    return log


class FakeSystemd:
    """Records every request. Sees the config and env as they were at each restart."""

    def __init__(self, paths):
        self.paths = paths
        self.calls = []
        self.running = set()
        self.states = {}
        self.failed = set()
        self.broken = False
        self.seen_at_restart = []

    def _call(self, action, unit):
        if self.broken:
            raise SystemdError(f"{action} {unit}: no")
        self.calls.append((action, unit))

    def state(self, unit):
        if self.broken:
            raise SystemdError(f"is-active {unit}: no")
        if unit in self.states:
            return self.states[unit]
        return "active" if unit in self.running else "inactive"

    def is_failed(self, unit):
        if self.broken:
            raise SystemdError(f"is-failed {unit}: no")
        return unit in self.failed

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
            frigate_cache=root / "frigate-cache",
            frigate_media=root / "frigate",
            local_proxy_secret=root / "camera-link" / "proxy.secret",
        )
        for directory in (
            "camera-link", "sys", "proc", "etc", "provision", "frigate", "run", "frigate-cache",
        ):
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


def test_a_clock_step_does_not_expire_a_lease_seen_in_this_generation(live):
    """A node with no RTC boots on an old clock, and NTP then steps it past the lease's expiry."""
    live.daemon.tick()
    generation = live.step.last.generation
    live.clock_epoch = NOW + 6 * 3600
    live.daemon.tick()
    assert live.step.last.lease == Lease(MAC, IP)
    assert live.step.last.generation == generation
    assert live.state()["state"] == "adopted"


def test_a_held_lease_survives_a_daemon_restart(live):
    live.daemon.tick()
    live.clock_epoch = NOW + 6 * 3600
    live.restart_daemon()
    live.daemon.tick()
    assert live.step.last.lease == Lease(MAC, IP)


def test_a_lease_dnsmasq_dropped_after_a_clock_step_is_still_held(live):
    live.daemon.tick()
    live.clock_epoch = NOW + 6 * 3600
    live.paths.lease_file.write_text("")
    live.daemon.tick()
    assert live.step.last.lease == Lease(MAC, IP)


def test_an_expired_lease_never_seen_in_this_generation_is_not_believed(live):
    live.set_lease(expiry=NOW - 1)
    live.daemon.tick()
    assert live.step.last.lease is None


def test_a_held_lease_ends_when_the_file_names_another_mac_even_if_expired(live):
    live.daemon.tick()
    live.set_lease(mac=OTHER_MAC, expiry=NOW - 1)
    live.daemon.tick()
    assert live.step.last.lease is None


def test_a_held_lease_gives_way_to_another_macs_live_lease(live):
    live.daemon.tick()
    live.set_lease(mac=OTHER_MAC)
    live.daemon.tick()
    assert live.step.last.lease == Lease(OTHER_MAC, IP)
    live.clock_epoch = NOW + 6 * 3600
    live.daemon.tick()
    assert live.step.last.lease == Lease(OTHER_MAC, IP)


def test_a_carrier_change_ends_a_held_lease(live):
    live.daemon.tick()
    live.set_carrier(False)
    live.daemon.tick()
    live.set_carrier(True)
    live.daemon.tick()  # the rising carrier's reset
    live.set_lease(expiry=NOW - 1)
    live.daemon.tick()
    assert live.step.last.lease is None


def test_a_flap_ends_a_held_lease(live):
    live.paths.carrier_changes.write_text("4\n")
    live.daemon.tick()
    live.paths.carrier_changes.write_text("6\n")
    live.daemon.tick()  # the reset
    live.set_lease(expiry=NOW - 1)
    live.daemon.tick()
    assert live.step.last.lease is None


def test_a_boot_ends_a_held_lease(live):
    live.daemon.tick()
    live.set_boot("boot-b")
    live.restart_daemon()
    live.daemon.tick()  # the boot's reset
    live.set_lease(expiry=NOW - 1)
    live.daemon.tick()
    assert live.step.last.lease is None


def test_read_lease_with_no_clock_ignores_expiry(tmp_path):
    leases = tmp_path / "leases"
    leases.write_text(f"{NOW - 100} {MAC} {IP} * *\n")
    assert read_lease(leases, None) == Lease(MAC, IP)


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


def test_an_undecodable_flap_count_or_carrier_is_survived(live):
    live.paths.carrier_changes.write_bytes(b"\xff\xfe")
    live.daemon.tick()
    assert live.step.last.lease == Lease(MAC, IP)
    live.paths.carrier.write_bytes(b"\xff")
    live.daemon.tick()
    assert live.step.last.lease is None


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


@pytest.mark.parametrize("held_mac", [5, True, ["x"], {"a": 1}])
def test_a_held_mac_of_the_wrong_type_counts_as_damaged(live, held_mac):
    """It reaches adopt.step as a lease's MAC, where it would crash every tick."""
    live.daemon.tick()
    link = json.loads(live.paths.link_state.read_text())
    live.paths.link_state.write_text(json.dumps({**link, "held_mac": held_mac}))
    live.clock_epoch = NOW + 2 * 3600  # so that only the hold could give a lease
    live.restart_daemon()
    live.daemon.tick()
    live.daemon.tick()
    assert live.step.last.generation >= NOW
    assert live.step.last.lease is None


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


def test_the_lease_is_wiped_while_dnsmasq_is_stopped(node):
    """dnsmasq rewrites its file while it runs, so a wipe only counts between stop and start."""
    node.set_lease()
    seen = {}
    real_stop, real_start = node.systemd.stop_and_wait, node.systemd.start

    def stop_and_wait(unit):
        seen["at_stop"] = node.paths.lease_file.exists()
        real_stop(unit)

    def start(unit):
        seen["at_start"] = node.paths.lease_file.exists()
        real_start(unit)

    node.systemd.stop_and_wait, node.systemd.start = stop_and_wait, start
    node.daemon.tick()
    assert seen == {"at_stop": True, "at_start": False}


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


def counted_fsyncs(monkeypatch):
    count = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (count.append(os.readlink(f"/proc/self/fd/{fd}")), real(fd))[1])
    return count


def test_a_steady_tick_syncs_and_replaces_nothing_on_the_card(live, monkeypatch):
    """The card's wear: a kit ticks 17,280 times a day, and only the tmpfs state file changes."""
    live.daemon.tick()
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.seed(
        attempted=f"{MAC}/{generation}",
        phase="adopted",
        applied={
            "disable_p2p": True,
            "enable_rtsp": True,
            "disable_onvif": True,
            "set_clock": True,
            "set_name": "cam-8da040",
            "set_streams": True,
            "set_rotation": False,
        },
    )
    live.daemon._step = step  # the real one, so that its writes count too
    live.daemon.tick()
    assert live.state()["state"] == "adopted"
    synced = counted_fsyncs(monkeypatch)
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append(Path(b).name), real(a, b))[1])
    on_card = [
        path
        for directory in (live.paths.frigate_env.parent, live.paths.store.parent, live.paths.frigate_config.parent)
        for path in directory.iterdir()
    ]
    before = {path: path.stat().st_mtime_ns for path in on_card}

    for _ in range(3):
        live.clock_epoch += TICK_SECONDS
        live.daemon.tick()

    assert live.state()["state"] == "adopted"
    assert set(replaced) == {live.paths.state_file.name}
    state_dir = str(live.paths.state_file.parent)
    assert all(path.startswith(state_dir) for path in synced), synced
    assert before == {path: path.stat().st_mtime_ns for path in on_card}


def test_a_changed_password_still_rewrites_the_env_and_restarts(live):
    live.daemon.tick()
    live.daemon.tick()
    live.step.passwords[MAC] = "a-new-camera-password"
    live.systemd.calls.clear()
    live.daemon.tick()
    assert "a-new-camera-password" in live.paths.frigate_env.read_text()
    assert live.systemd.frigate_calls() == ["restart"]


def test_a_stale_staged_file_is_removed_on_a_steady_tick(live):
    live.daemon.tick()
    stale = live.paths.frigate_env.with_name("frigate.env.new")
    stale.write_text("left by a power cut")
    live.daemon.tick()
    assert not stale.exists()


def test_a_write_that_fails_changes_neither_file_and_is_reported(live, monkeypatch):
    live.daemon.tick()
    config = live.paths.frigate_config.read_bytes()
    env = live.paths.frigate_env.read_bytes()
    live.provision(node_id="cam-renamed")
    monkeypatch.setattr(
        "cameralink.frigate_config._replace_file",
        lambda path, body: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    live.daemon.tick()
    assert live.paths.frigate_config.read_bytes() == config
    assert live.paths.frigate_env.read_bytes() == env
    assert "cannot write" in live.state()["reason"]


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


def test_the_restart_is_owed_on_disk_before_the_files_change(live, monkeypatch):
    live.daemon.tick()
    live.provision(node_id="cam-renamed")

    def refuse(source, target):
        raise OSError(30, "Read-only file system")

    real = os.replace
    monkeypatch.setattr(
        os,
        "replace",
        lambda source, target: refuse(source, target)
        if Path(target) == live.paths.frigate_config
        else real(source, target),
    )
    live.daemon.tick()
    assert json.loads(live.paths.link_state.read_text())["restart_owed"] is True


def test_the_config_is_world_readable_and_replaced_whole(live):
    live.daemon.tick()
    assert stat.S_IMODE(live.paths.frigate_config.stat().st_mode) == 0o644


def test_a_stopped_frigate_is_started_with_no_rewrite(live):
    live.daemon.tick()
    live.systemd.running.clear()
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "start"]


def test_a_frigate_wanted_and_down_is_reported_after_six_ticks(live):
    live.daemon.tick()
    live.systemd.running.clear()
    live.systemd.states["frigate.service"] = "failed"
    for _ in range(FRIGATE_DOWN_TICKS - 1):
        live.daemon.tick()
        assert live.state()["reason"] is None
    live.daemon.tick()
    state = live.state()
    assert state["state"] == "adopted"
    assert "Frigate is wanted but not running" in state["reason"]
    assert FRIGATE_DOWN_TICKS == 6
    del live.systemd.states["frigate.service"]
    live.systemd.running.add("frigate.service")
    live.daemon.tick()
    assert live.state()["reason"] is None


def test_a_crash_loop_with_short_active_spells_is_reported(live):
    live.daemon.tick()
    reasons = []
    for _ in range(FRIGATE_DOWN_TICKS):
        live.systemd.states["frigate.service"] = "activating"
        live.daemon.tick()
        reasons.append(live.state()["reason"])
        live.systemd.states["frigate.service"] = "active"
        live.daemon.tick()
    assert "Frigate is wanted but not running" in reasons[-1]
    assert all(reason is None for reason in reasons[:-1])


def test_a_recovery_that_lasts_starts_the_down_count_again(live):
    live.daemon.tick()
    live.systemd.states["frigate.service"] = "activating"
    for _ in range(FRIGATE_DOWN_TICKS - 1):
        live.daemon.tick()
    live.systemd.states["frigate.service"] = "active"
    for _ in range(FRIGATE_DOWN_TICKS):
        live.daemon.tick()
    live.systemd.states["frigate.service"] = "activating"
    live.daemon.tick()
    assert live.state()["reason"] is None


def test_a_frigate_stuck_activating_is_reported_and_not_started_again(live):
    """A missing /dev/hailo0 never gets past activating."""
    live.daemon.tick()
    live.systemd.states["frigate.service"] = "activating"
    for _ in range(FRIGATE_DOWN_TICKS):
        live.daemon.tick()
    assert "Frigate is wanted but not running" in live.state()["reason"]
    assert live.systemd.frigate_calls() == ["restart"]


def test_a_failed_firewall_is_named_as_the_cause(live):
    live.daemon.tick()
    live.systemd.states["frigate.service"] = "failed"
    live.systemd.failed.add("nftables.service")
    for _ in range(FRIGATE_DOWN_TICKS):
        live.daemon.tick()
    assert "nftables.service" in live.state()["reason"]
    assert live.state()["state"] == "adopted"


def test_the_down_count_starts_again_after_a_restart_request(live):
    live.daemon.tick()
    live.systemd.states["frigate.service"] = "failed"
    for _ in range(FRIGATE_DOWN_TICKS - 1):
        live.daemon.tick()
    live.provision(node_id="cam-renamed")
    live.daemon.tick()
    assert live.state()["reason"] is None
    live.daemon.tick()
    assert live.state()["reason"] is None


def test_the_down_count_starts_again_when_frigate_is_no_longer_wanted(live):
    live.daemon.tick()
    live.systemd.states["frigate.service"] = "failed"
    for _ in range(FRIGATE_DOWN_TICKS - 1):
        live.daemon.tick()
    live.step.state = LinkState.ADOPTING
    live.daemon.tick()
    live.step.state = LinkState.ADOPTED
    live.daemon.tick()
    assert live.state()["reason"] is None


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


def test_relay_user_is_named_and_never_quoted(live):
    live.provision(broker={"username": "pa\\ss word1", "password": "other-pass"})
    live.daemon.tick()
    reason = live.state()["reason"]
    assert "relay_user" in reason
    assert "ss word1" not in reason


def test_the_camera_password_in_a_rejected_zone_name_is_not_reported(live):
    live.provision(covered_zones=[f"a b {CAMERA_PASSWORD}"])
    live.daemon.tick()
    reason = live.state()["reason"]
    assert CAMERA_PASSWORD not in reason
    assert "Frigate's config" in reason


@pytest.mark.parametrize(
    "password",
    ["pa\\ss-word-1", "pa'ss\\word-1", 'pa"ss\\word-1', "pa'ss\"word\\1", "pa\nss-word-1"],
    ids=repr,
)
@pytest.mark.parametrize("field", ["zone", "node_id"])
def test_a_password_escaped_by_the_quoting_is_still_not_reported(live, password, field):
    pasted = {"covered_zones": [password]} if field == "zone" else {"node_id": password}
    live.provision(broker={"username": "kit1", "password": password}, **pasted)
    live.daemon.tick()
    text = live.paths.state_file.read_text()
    assert "word" not in text and "ss-" not in text
    assert "Frigate's config" in live.state()["reason"]


def test_a_pending_camera_password_in_a_rejected_zone_name_is_not_reported(live):
    live.step.passwords[MAC] = ""
    live.provision(covered_zones=["a b pending-S3cret-1"])
    real_step = live.daemon._step

    def keep_pending(lease, generation, store, *args, **kwargs):
        state = real_step(lease, generation, store, *args, **kwargs)
        with store.lock():
            store.save(replace(store.load(lease.mac), pending_password="pending-S3cret-1"))
        return state

    live.daemon._step = keep_pending
    live.daemon.tick()
    reason = live.state()["reason"]
    assert "pending-S3cret-1" not in live.paths.state_file.read_text()
    assert "Frigate's config" in reason


def test_the_derived_proxy_secret_in_a_rejected_zone_name_is_not_reported(live):
    derived = proxy_secret(RELAY_PASSWORD)
    live.provision(covered_zones=[f"a b {derived}"])
    live.daemon.tick()
    assert derived not in live.paths.state_file.read_text()
    assert "Frigate's config" in live.state()["reason"]


def test_a_very_short_password_hides_a_quoted_reason_but_says_what_failed(live):
    live.provision(
        covered_zones=["bad zone!"], broker={"username": "kit1", "password": "a"}
    )
    live.daemon.tick()
    assert "Frigate's config" in live.state()["reason"]


def test_a_very_short_password_does_not_hide_reasons_with_no_settings_in_them(live):
    live.provision(broker={"username": "kit1", "password": "e"})
    live.daemon.tick()
    live.seed(phase="needs_reset")
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["reason"] == "the camera needs a factory reset"


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


@pytest.mark.parametrize("state", [LinkState.FAILED, LinkState.ADOPTING])
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


def test_a_refusal_stops_frigate_even_with_a_saved_password(live):
    live.daemon.tick()
    live.seed(phase="needs_reset")
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    assert CAMERA_PASSWORD == CameraStore(live.paths.store).load(MAC).password


def test_a_locked_camera_stops_frigate(live):
    live.daemon.tick()
    live.seed(locked=True)
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    assert live.state()["reason"] == "the camera needs a factory reset"


def _real_step_daemon(live, seed, *, probe=lambda host, port: True):
    """The real step in a new generation's first look, settled, with a camera that accepts the
    saved password if it is ever asked. Returns the logins it was asked for."""
    live.daemon.tick()
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.seed(settle_key=f"{MAC}/{generation}", settle_since=-1000.0, attempted=None, **seed)
    logins = []

    class Camera:
        def __init__(self, ip):
            pass

        def login(self, user, password):
            logins.append(password)

        def logout(self):
            pass

    live.daemon = Daemon(
        live.paths,
        live.systemd,
        step_fn=functools.partial(step, probe=probe),
        clock=live.clock,
        client_factory=Camera,
    )
    return logins


CAPPED = (NOW - 100.0,) * FAILURE_CAP


def test_a_healthy_camera_at_the_rolling_cap_keeps_frigate(live):
    """Unknown login outcomes filled the cap, so no login is sent, and nothing has said that
    the password is wrong. A day without detection on a working camera would be the outage."""
    logins = _real_step_daemon(live, {"phase": "failed", "failures": CAPPED})
    live.daemon.tick()
    state = live.state()
    assert state["state"] == "needs_reset"
    assert logins == []
    assert live.systemd.frigate_calls() == ["restart"]
    assert "frigate.service" in live.systemd.running
    assert "factory reset" not in state["reason"]
    assert "clear-limits" in state["reason"]
    assert state["retry_after"] == NOW - 100.0 + FAILURE_WINDOW_SECONDS


def test_a_spent_signature_limit_on_a_healthy_camera_keeps_frigate(live):
    """Port 80 refusing once on a working camera must not bounce Frigate."""
    logins = _real_step_daemon(
        live,
        {"phase": "adopted", "signature_failures": (NOW - 100.0,)},
        probe=lambda host, port: port == 9000,
    )
    live.daemon.tick()
    assert live.state()["state"] == "needs_reset"
    assert logins == []
    assert live.systemd.frigate_calls() == ["restart"]
    assert "factory reset" not in live.state()["reason"]


def test_a_refusal_that_then_reaches_the_cap_still_stops_frigate(live):
    logins = _real_step_daemon(live, {"phase": "needs_reset", "failures": CAPPED})
    live.daemon.tick()
    assert logins == []
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    assert live.state()["reason"] == "the camera needs a factory reset"


def test_a_locked_camera_at_the_cap_still_stops_frigate(live):
    _real_step_daemon(live, {"phase": "failed", "failures": CAPPED, "locked": True})
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop"]
    assert live.state()["reason"] == "the camera needs a factory reset"


def test_frigate_comes_back_after_needs_reset_with_no_rewrite(live):
    live.daemon.tick()
    live.seed(phase="needs_reset")
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    live.step.state = LinkState.ADOPTED
    live.daemon.tick()
    assert live.systemd.frigate_calls() == ["restart", "stop", "start"]


@pytest.mark.parametrize("remain_times", [7, 2])
def test_a_refused_saved_password_stops_frigate_with_the_real_step(live, remain_times):
    """Frigate's ffmpeg would go on retrying the password that adoption just stopped using."""
    live.daemon.tick()
    generation = json.loads(live.paths.link_state.read_text())["generation"]
    live.seed(settle_key=f"{MAC}/{generation}", settle_since=-1000.0, attempted=None, phase=None)
    logins = []

    class Camera:
        def __init__(self, ip):
            pass

        def login(self, user, password):
            logins.append(password)
            raise AuthFailed("no", remain_times=remain_times)

        def logout(self):
            pass

    live.daemon = Daemon(
        live.paths,
        live.systemd,
        step_fn=functools.partial(step, probe=lambda host, port: True),
        clock=live.clock,
        client_factory=Camera,
    )
    live.daemon.tick()
    assert live.state()["state"] == "needs_reset"
    assert logins == [CAMERA_PASSWORD]
    assert live.systemd.frigate_calls() == ["restart", "stop"]


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
        "detector_error": False,
        "log_read_at": NOW,
        "record_writing": False,
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
    assert node.state()["reason"] == "not provisioned, so Frigate's config is not updated"


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


def test_retry_after_with_more_stamps_than_the_cap_waits_for_enough_to_age_out(live):
    stamps = tuple(NOW - 100.0 * (i + 1) for i in range(FAILURE_CAP + 2))
    live.seed(failures=stamps)
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    counted = sorted(stamps)[-FAILURE_CAP]
    assert live.state()["retry_after"] == counted + FAILURE_WINDOW_SECONDS


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
        # A copy read before the lock would now be stale, and its save refused.
        time.sleep(0.2)
        holder.save(replace(holder.load(), phase="failed"))
    worker.join(10)
    assert not worker.is_alive()
    after = CameraStore(path).load()
    assert after.failures == ()
    assert after.phase == "failed"


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


def test_there_is_no_proxy_secret_for_an_empty_password():
    with pytest.raises(FrigateConfigError):
        proxy_secret("")


# An anonymous broker: nothing to derive from, and the label alone is a public constant.

LABEL_ONLY = hashlib.sha256(b"black-relay-frigate-proxy:").hexdigest()


def provision_anonymous(node):
    node.provision(broker={"username": "", "password": ""})


def env_secret(node):
    for line in node.paths.frigate_env.read_text().splitlines():
        if line.startswith("FRIGATE_PROXY_SECRET="):
            return line.split("=", 1)[1]
    raise AssertionError("no proxy secret")


def test_an_anonymous_broker_gets_a_random_secret_stored_root_only(live):
    provision_anonymous(live)
    live.daemon.tick()
    secret = env_secret(live)
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert live.paths.local_proxy_secret.read_text().strip() == secret
    assert stat.S_IMODE(live.paths.local_proxy_secret.stat().st_mode) == 0o600
    assert live.systemd.frigate_calls() == ["restart"]


def test_the_label_only_constant_never_appears(live):
    provision_anonymous(live)
    live.daemon.tick()
    live.daemon.tick()
    assert LABEL_ONLY not in all_files_text(live.root)


def test_the_random_secret_is_reused_with_no_restart(live):
    provision_anonymous(live)
    live.daemon.tick()
    secret = env_secret(live)
    live.daemon.tick()
    live.restart_daemon()
    live.daemon.tick()
    assert env_secret(live) == secret
    assert live.systemd.frigate_calls() == ["restart"]


def test_two_nodes_get_different_random_secrets(tmp_path):
    secrets = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        node = Node(root)
        provision_anonymous(node)
        node.settle_boot()
        node.set_lease()
        node.daemon.tick()
        secrets.append(env_secret(node))
    assert secrets[0] != secrets[1]


@pytest.mark.parametrize("content", ["", "short\n", "g" * 64])
def test_a_damaged_random_secret_is_replaced_with_a_new_random_one(live, content):
    live.paths.local_proxy_secret.write_text(content)
    provision_anonymous(live)
    live.daemon.tick()
    secret = env_secret(live)
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert secret != LABEL_ONLY
    assert live.paths.local_proxy_secret.read_text().strip() == secret


def test_a_broker_with_a_password_writes_no_random_secret(live):
    live.daemon.tick()
    assert not live.paths.local_proxy_secret.exists()
    assert env_secret(live) == proxy_secret(RELAY_PASSWORD)


def test_a_random_secret_that_cannot_be_saved_starts_nothing(live, monkeypatch):
    provision_anonymous(live)
    real = os.replace

    def refuse(source, target):
        if Path(target) == live.paths.local_proxy_secret:
            raise OSError(28, "No space left on device")
        real(source, target)

    monkeypatch.setattr(os, "replace", refuse)
    live.daemon.tick()
    assert live.systemd.frigate_calls() == []
    assert not live.paths.frigate_env.exists()
    assert live.state()["reason"] != "central_proxy_unavailable"


def test_an_anonymous_broker_is_reported_when_nothing_matters_more(live):
    provision_anonymous(live)
    live.daemon.tick()
    assert live.state()["reason"] == "central_proxy_unavailable"
    assert live.state()["state"] == "adopted"
    secret = env_secret(live)
    assert secret not in live.paths.state_file.read_text()


def test_more_important_reasons_come_before_the_anonymous_one(live):
    provision_anonymous(live)
    live.daemon.tick()
    live.seed(phase="needs_reset")
    live.step.state = LinkState.NEEDS_RESET
    live.daemon.tick()
    assert live.state()["reason"] == "the camera needs a factory reset"
    live.step.state = LinkState.ADOPTED
    live.provision(broker={"username": "", "password": ""}, covered_zones=["bad zone!"])
    live.daemon.tick()
    assert "zone" in live.state()["reason"]


def test_a_broker_with_a_password_never_says_the_proxy_is_unavailable(live):
    live.daemon.tick()
    assert live.state()["reason"] is None


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
        ("refreshing\n", 0, True),
        ("inactive\n", 3, False),
        ("failed\n", 3, False),
        ("deactivating\n", 3, False),
    ],
)
def test_systemctl_reads_is_active(stdout, returncode, expected):
    runner = Runner(stdout, returncode)
    assert (Systemctl(run=runner).state("frigate.service") in RUNNING_STATES) is expected
    assert Systemctl(run=runner).state("frigate.service") == stdout.strip()
    assert runner.calls[0][-2:] == ["is-active", "frigate.service"]


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [("failed\n", 0, True), ("active\n", 1, False), ("inactive\n", 1, False)],
)
def test_systemctl_reads_is_failed(stdout, returncode, expected):
    runner = Runner(stdout, returncode)
    assert Systemctl(run=runner).is_failed("nftables.service") is expected
    assert runner.calls[0][-2:] == ["is-failed", "nftables.service"]


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
        Systemctl(run=runner).state("frigate.service")


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


def directives(name):
    """Every directive of a unit, by section, with repeats kept in order."""
    sections = {}
    section = None
    text = re.sub(r"\\\n\s*", " ", (UNITS / name).read_text())
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = sections.setdefault(line.strip("[]"), {})
        else:
            key, value = line.split("=", 1)
            section.setdefault(key, []).append(value)
    return sections


SANDBOX = {
    "NoNewPrivileges": ["yes"],
    "ProtectSystem": ["strict"],
    "ProtectHome": ["yes"],
    "PrivateTmp": ["yes"],
    "PrivateDevices": ["yes"],
    "ProtectClock": ["yes"],
    "ProtectControlGroups": ["yes"],
    "ProtectKernelLogs": ["yes"],
    "ProtectKernelModules": ["yes"],
    "ProtectKernelTunables": ["yes"],
    "LockPersonality": ["yes"],
    "MemoryDenyWriteExecute": ["yes"],
    "RestrictNamespaces": ["yes"],
    "RestrictRealtime": ["yes"],
    "RestrictSUIDSGID": ["yes"],
    "SystemCallArchitectures": ["native"],
    "SystemCallFilter": ["@system-service"],
    "SystemCallErrorNumber": ["EPERM"],
}
DOCS = ["file:/opt/black-relay/ops/README.md"]


def test_frigate_unit_is_exactly_what_it_needs():
    frigate = directives("frigate.service")
    service = frigate["Service"]
    assert service.pop("ExecStart")[0].split() == [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--name",
        "frigate",
        "--network",
        "host",
        "--device",
        "/dev/hailo0",
        "--shm-size=128m",
        "-v",
        "/run/frigate-cache:/tmp/cache",
        "-v",
        "/var/lib/frigate/config:/config",
        "-v",
        "/var/lib/frigate/media:/media/frigate",
        "-v",
        "/etc/localtime:/etc/localtime:ro",
        "--env-file",
        "/etc/camerabridge/frigate.env",
        "ghcr.io/blakeblackshear/frigate:0.18.2-standard-arm64"
        "@sha256:4a854373c0fd4a8f550518758e34b929be0b423085bb569bb232390d128fa4ef",
    ]
    assert service == {
        "Type": ["simple"],
        # The table first and with no "-": nftables stays active even with its rules flushed.
        "ExecStartPre": [
            "/bin/sh -c '/usr/sbin/nft list table inet black_relay >/dev/null'",
            "-/usr/bin/docker rm -f frigate",
        ],
        "ExecStop": ["-/usr/bin/docker stop --timeout 30 frigate"],
        "Restart": ["always"],
        "RestartSec": ["5"],
        "TimeoutStopSec": ["45"],
    }
    assert int(service["TimeoutStopSec"][0]) > 30  # longer than docker stop's own timeout
    assert frigate["Unit"] == {
        "Description": ["Frigate NVR for the Black Relay camera node"],
        "Documentation": DOCS,
        "Requires": ["nftables.service docker.service"],
        "After": ["nftables.service docker.service"],
        "RequiresMountsFor": ["/run/frigate-cache"],
        "StartLimitIntervalSec": ["0"],
    }
    assert set(frigate) == {"Unit", "Service"}  # no [Install]: only the daemon starts it


def test_dhcp_unit_is_the_sequence_proven_on_kit_1_with_exact_hardening():
    dhcp = directives("camera-link-dhcp.service")
    service = dhcp["Service"]
    assert service.pop("ExecStart")[0].split() == [
        "/usr/sbin/dnsmasq",
        "--conf-file=/dev/null",
        "--port=0",
        "--bind-interfaces",
        "--interface=eth0",
        "--except-interface=lo",
        "--dhcp-range=10.77.0.10,10.77.0.10,255.255.255.0,1h",
        "--dhcp-authoritative",
        "--dhcp-option=3",
        "--dhcp-option=6",
        "--dhcp-leasefile=/var/lib/camera-link/dnsmasq.leases",
        "--keep-in-foreground",
        "--pid-file=/run/camera-link-dhcp/dnsmasq.pid",
    ]
    assert service == {
        "Type": ["simple"],
        "Restart": ["always"],
        "RestartSec": ["5"],
        "RestartSteps": ["5"],
        "RestartMaxDelaySec": ["60"],
        "UMask": ["0077"],
        "StateDirectory": ["camera-link"],
        "StateDirectoryMode": ["0700"],
        "RuntimeDirectory": ["camera-link-dhcp"],
        "RuntimeDirectoryMode": ["0755"],
        "CapabilityBoundingSet": [
            "CAP_CHOWN CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW CAP_SETGID CAP_SETUID"
        ],
        "RestrictAddressFamilies": ["AF_UNIX AF_INET AF_INET6 AF_NETLINK AF_PACKET"],
        **SANDBOX,
    }
    assert dhcp["Unit"] == {
        "Description": ["Black Relay camera link DHCP (one address on eth0, no gateway, no DNS)"],
        "Documentation": DOCS,
        "After": ["NetworkManager.service nftables.service"],
        "Wants": ["NetworkManager.service nftables.service"],
        "StartLimitIntervalSec": ["0"],
    }
    assert dhcp["Install"] == {"WantedBy": ["multi-user.target"]}


def test_link_unit_has_exact_hardening_and_no_tie_to_frigate():
    link = directives("camera-link.service")
    assert link["Service"] == {
        "Type": ["simple"],
        # "+" because ReadWritePaths needs the directory before the sandbox is built.
        "ExecStartPre": [
            "+/usr/bin/install -d -m 0750 /var/lib/frigate /var/lib/frigate/config "
            "/var/lib/frigate/media"
        ],
        "ExecStart": ["/opt/black-relay/cameralink/bin/camera-link run"],
        "Restart": ["always"],
        "RestartSec": ["5"],
        "TimeoutStopSec": ["140"],  # longer than a tick with its 120 s enable_services timeout
        "UMask": ["0077"],
        # bridge.yaml is 0600 and owned by camerabridge, so root needs this to read it.
        "CapabilityBoundingSet": ["CAP_DAC_READ_SEARCH"],
        "StateDirectory": ["camera-link"],
        "StateDirectoryMode": ["0700"],
        "RuntimeDirectory": ["camera-link"],
        "RuntimeDirectoryMode": ["0755"],
        "ConfigurationDirectory": ["camerabridge"],
        "ConfigurationDirectoryMode": ["0755"],
        "ReadWritePaths": ["/etc/camerabridge /var/lib/frigate/config"],
        # systemctl reaches systemd over /run/systemd/private, a unix socket.
        "RestrictAddressFamilies": ["AF_UNIX AF_INET AF_INET6 AF_NETLINK"],
        **SANDBOX,
    }
    assert link["Unit"] == {
        "Description": [
            "Black Relay camera link (adopts the camera, renders Frigate's config, runs Frigate)"
        ],
        "Documentation": DOCS,
        "After": ["NetworkManager.service nftables.service camera-link-dhcp.service"],
        "Wants": ["NetworkManager.service nftables.service camera-link-dhcp.service"],
        "RequiresMountsFor": ["/run/frigate-cache"],
        "StartLimitIntervalSec": ["0"],
    }
    assert link["Install"] == {"WantedBy": ["multi-user.target"]}


def test_camera_profile_is_static_with_no_route():
    profile = configparser.ConfigParser(interpolation=None)
    profile.read_string((UNITS / "black-relay-camera.nmconnection").read_text())
    assert profile["connection"]["type"] == "ethernet"
    assert profile["connection"]["interface-name"] == "eth0"
    assert profile["ipv4"]["method"] == "manual"
    assert profile["ipv4"]["address1"] == "10.77.0.1/24"
    assert profile["ipv4"]["never-default"] == "true"
    assert profile["ipv6"]["method"] == "disabled"


def test_the_segment_cache_is_a_bounded_private_tmpfs_on_the_host():
    # The file name is the systemd escape of the mount point, or the unit does nothing.
    mount = directives("run-frigate\\x2dcache.mount")
    assert mount["Mount"] == {
        "What": ["tmpfs"],
        "Where": ["/run/frigate-cache"],
        "Type": ["tmpfs"],
        "Options": ["size=256m,mode=0700,nosuid,nodev,noexec"],
    }


def test_camera_link_sees_the_segment_cache_from_the_start():
    # Mounted ahead of its sandbox, so nothing depends on a later mount propagating in.
    assert directives("camera-link.service")["Unit"]["RequiresMountsFor"] == ["/run/frigate-cache"]


def test_docker_and_hailo_settings():
    assert json.loads((UNITS / "daemon.json").read_text()) == {
        "bridge": "none",
        "iptables": False,
        "ip6tables": False,
        # Bounds what the first log read after a daemon restart has to get through.
        "log-driver": "json-file",
        "log-opts": {"max-size": "10m", "max-file": "3"},
    }
    assert (UNITS / "hailo_pci.conf").read_text().strip().splitlines()[-1] == (
        "options hailo_pci force_desc_page_size=4096"
    )


# Frigate's log


# Built from the format strings in Frigate v0.18.2's source, because no line from kit 1 was saved.
HAILO_LINE = "[HailoRT] [error] CHECK_SUCCESS failed with status=HAILO_TIMEOUT(4)\n"
HAILO_EXCEPTION = "hailo_platform.pyhailort.pyhailort.HailoRTException: boom\n"
TIMEOUT_LINE = (
    "frigate.detectors.plugins.hailo8l ERROR : "
    "Timeout waiting for inference results for request 42\n"  # hailo8l.py:345
)
INFERENCE_ERROR = "frigate.detectors.plugins.hailo8l ERROR : Inference error: boom\n"  # :132
THREAD_STOPPED = "RuntimeError: HailoRT inference thread has stopped, restart required.\n"  # :351
STUCK_LINE = "frigate.watchdog INFO : Detection appears to be stuck. Restarting detection process...\n"  # watchdog.py:126
INIT_LINE = (
    "frigate.detectors.plugins.hailo8l ERROR : "
    "[INIT] Failed to initialize HailoAsyncInference: boom\n"  # hailo8l.py:264
)


@pytest.mark.parametrize(
    "text",
    [HAILO_LINE, HAILO_EXCEPTION, TIMEOUT_LINE, INFERENCE_ERROR, THREAD_STOPPED, STUCK_LINE, INIT_LINE],
)
def test_a_hailo_failure_in_frigates_log_is_reported(node, frigate_log, text):
    frigate_log.text = "frigate.app INFO : Starting Frigate\n" + text
    node.daemon.tick()
    assert node.state()["detector_error"] is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "frigate.detectors.plugins.hailo8l INFO : [HailoRT] loaded\n",
        "some other [error] line\n",
    ],
)
def test_a_clean_log_reports_no_detector_error(node, frigate_log, text):
    frigate_log.text = text
    node.daemon.tick()
    assert node.state()["detector_error"] is False


def test_the_log_is_read_once_per_interval(node, frigate_log):
    node.daemon.tick()
    node.daemon.tick()
    assert len(frigate_log.since) == 1
    node.clock_epoch += LOG_SCAN_SECONDS - 1
    node.daemon.tick()
    assert len(frigate_log.since) == 1
    node.clock_epoch += 1
    node.daemon.tick()
    assert len(frigate_log.since) == 2


def test_the_first_read_covers_all_of_this_frigate_and_later_ones_only_the_rest(node, frigate_log):
    node.daemon.tick()
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert frigate_log.since[0] == frigate_log.started
    # With an overlap: a line written during the last read must not fall between two reads.
    assert int(frigate_log.since[1]) < NOW
    assert int(frigate_log.since[1]) >= NOW - LOG_SCAN_SECONDS


def test_a_detector_error_outlives_the_lines_that_showed_it(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.daemon.tick()
    frigate_log.text = ""
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["detector_error"] is True


def test_a_frigate_restart_clears_the_detector_error(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.daemon.tick()
    frigate_log.text = ""
    frigate_log.started = "2026-11-05T11:00:00.000000000Z"
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["detector_error"] is False
    assert frigate_log.since[-1] == frigate_log.started


def test_a_failure_right_after_the_restart_is_still_seen(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.daemon.tick()
    frigate_log.started = "2026-11-05T11:00:00.000000000Z"
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["detector_error"] is True


def test_a_restarted_daemon_finds_the_failure_again(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.daemon.tick()
    node.restart_daemon()
    node.daemon.tick()
    assert node.state()["detector_error"] is True
    assert frigate_log.since[-1] == frigate_log.started


def test_a_clock_stepped_back_reads_from_the_start_again(node, frigate_log):
    node.daemon.tick()
    node.clock_epoch -= 1000
    node.daemon.tick()
    assert frigate_log.since[-1] == frigate_log.started
    assert len(frigate_log.since) == 2


def test_no_docker_answer_changes_nothing_and_does_not_stop_the_tick(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.daemon.tick()
    frigate_log.started = None
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["detector_error"] is True
    assert node.state()["updated"] == node.clock_epoch


def test_an_unreadable_log_is_read_again_from_the_same_place(node, frigate_log):
    frigate_log.since_time = lambda since: frigate_log.since.append(since)  # returns None
    node.daemon.tick()
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["log_read_at"] is None
    assert frigate_log.since == [frigate_log.started, frigate_log.started]


def test_a_successful_read_is_published_with_its_time(node, frigate_log):
    node.daemon.tick()
    assert node.state()["log_read_at"] == NOW
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["log_read_at"] == NOW + LOG_SCAN_SECONDS


def test_a_failed_read_keeps_the_time_of_the_last_good_one(node, frigate_log):
    node.daemon.tick()
    frigate_log.since_time = lambda since: None
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    state = node.state()
    assert state["log_read_at"] == NOW
    assert state["updated"] == NOW + LOG_SCAN_SECONDS


def test_docker_gone_keeps_the_time_of_the_last_good_read(node, frigate_log):
    node.daemon.tick()
    frigate_log.started = None
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["log_read_at"] == NOW


def test_a_daemon_that_has_never_read_the_log_says_so(node, frigate_log):
    frigate_log.started = None
    node.daemon.tick()
    state = node.state()
    assert state["log_read_at"] is None
    assert state["detector_error"] is False


def test_a_new_container_voids_the_old_read_until_its_own_succeeds(node, frigate_log):
    node.daemon.tick()
    frigate_log.started = "2026-11-05T11:00:00.000000000Z"
    frigate_log.since_time = lambda since: None
    node.clock_epoch += LOG_SCAN_SECONDS
    node.daemon.tick()
    assert node.state()["log_read_at"] is None


def test_a_blind_scan_gives_a_reason_while_frigate_is_wanted(live, frigate_log):
    live.daemon.tick()
    frigate_log.started = None
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    assert "log cannot be read" in live.state()["reason"]


# Recovering from a detector failure


def frigate_restarts(node):
    return [call for call in node.systemd.calls if call == ("restart", FRIGATE_UNIT)]


def run_for(node, seconds):
    """Tick the way the daemon does, with Frigate's container following its unit's restarts."""
    for _ in range(seconds // TICK_SECONDS):
        node.clock_epoch += TICK_SECONDS
        node.daemon.tick()


def healthy_frigate(live):
    live.daemon.tick()
    live.daemon.tick()
    live.systemd.calls.clear()


def restarts_new_container(node, frigate_log, then_text):
    """A queued restart gives a new StartedAt, and a log holding ``then_text``."""
    real = node.systemd.restart
    count = [0]

    def restart(unit):
        real(unit)
        count[0] += 1
        frigate_log.started = f"2026-11-05T12:00:{count[0]:02d}.000000000Z"
        frigate_log.text = then_text

    node.systemd.restart = restart


def test_a_detector_error_gives_a_reason(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    assert "detector" in live.state()["reason"]


def test_the_error_is_given_time_before_a_restart(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS - TICK_SECONDS)
    assert frigate_restarts(live) == []

    run_for(live, TICK_SECONDS)
    assert len(frigate_restarts(live)) == 1


def test_the_detector_restart_timing_rules_are_these_numbers():
    """A ruling: restart after 120 s, at most once per 10 minutes. Tests above import these."""
    assert DETECTOR_ERROR_RESTART_SECONDS == 120
    assert DETECTOR_RESTART_LIMIT_SECONDS == 600


def test_the_limit_is_on_disk_before_the_restart_is_asked_for(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    saved_at_restart = []
    real = live.systemd.restart

    def restart(unit):
        saved_at_restart.append(json.loads(live.paths.link_state.read_text())["detector_restart_at"])
        real(unit)

    live.systemd.restart = restart
    run_for(live, DETECTOR_ERROR_RESTART_SECONDS)

    assert saved_at_restart == [live.clock_epoch]


def test_no_restart_for_a_detector_failure_on_an_unprovisioned_node(live, frigate_log):
    healthy_frigate(live)
    live.paths.bridge_config.unlink()
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()

    run_for(live, 2 * DETECTOR_ERROR_RESTART_SECONDS)

    assert frigate_restarts(live) == []
    assert live.state()["detector_error"] is True


def test_one_transient_line_then_a_restart_recovers(live, frigate_log):
    healthy_frigate(live)
    restarts_new_container(live, frigate_log, "")
    frigate_log.text = TIMEOUT_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    assert live.state()["detector_error"] is True
    frigate_log.text = ""  # the line is behind us, and only a restart clears it

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS)
    assert len(frigate_restarts(live)) == 1
    # Only the restart's own detection clears it, at the next read.
    run_for(live, LOG_SCAN_SECONDS)

    state = live.state()
    assert state["detector_error"] is False
    assert state["reason"] is None
    run_for(live, 3600)
    assert len(frigate_restarts(live)) == 1


def test_the_restart_alone_does_not_clear_the_error(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    frigate_log.text = ""  # the same container: its StartedAt never changes

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS + 2 * LOG_SCAN_SECONDS)

    assert len(frigate_restarts(live)) == 1
    assert live.state()["detector_error"] is True


def test_a_permanent_fault_cannot_restart_faster_than_the_limit(live, frigate_log):
    healthy_frigate(live)
    restarts_new_container(live, frigate_log, HAILO_LINE)
    frigate_log.text = HAILO_LINE
    stamps = []
    real = live.systemd.restart

    def timed(unit):
        stamps.append(live.clock_epoch)
        real(unit)

    live.systemd.restart = timed

    run_for(live, 4 * 3600)

    assert len(stamps) >= 2
    assert all(b - a >= DETECTOR_RESTART_LIMIT_SECONDS for a, b in zip(stamps, stamps[1:]))
    assert len(stamps) <= 4 * 3600 / DETECTOR_RESTART_LIMIT_SECONDS + 1


def test_the_limit_outlives_a_daemon_restart(live, frigate_log):
    healthy_frigate(live)
    restarts_new_container(live, frigate_log, HAILO_LINE)
    frigate_log.text = HAILO_LINE
    stamps = []
    real = live.systemd.restart

    def timed(unit):
        stamps.append(live.clock_epoch)
        real(unit)

    live.systemd.restart = timed

    for _ in range(40):
        run_for(live, DETECTOR_ERROR_RESTART_SECONDS + 2 * LOG_SCAN_SECONDS)
        live.restart_daemon()
        live.systemd.restart = timed

    assert len(stamps) >= 2
    assert all(b - a >= DETECTOR_RESTART_LIMIT_SECONDS for a, b in zip(stamps, stamps[1:]))


def test_an_older_link_file_without_the_limit_still_loads(live):
    live.daemon.tick()
    link = json.loads(live.paths.link_state.read_text())
    del link["detector_restart_at"]
    live.paths.link_state.write_text(json.dumps(link))
    generation = link["generation"]

    live.restart_daemon()
    live.daemon.tick()

    assert json.loads(live.paths.link_state.read_text())["generation"] == generation


@pytest.mark.parametrize("value", ["soon", True, [1]])
def test_a_bad_restart_time_counts_as_damaged(live, value):
    live.daemon.tick()
    link = json.loads(live.paths.link_state.read_text())
    live.paths.link_state.write_text(json.dumps({**link, "detector_restart_at": value}))

    live.restart_daemon()
    live.daemon.tick()

    assert json.loads(live.paths.link_state.read_text())["generation"] >= NOW


@pytest.mark.parametrize(
    "raw",
    ["1" + "0" * 400, "1e999", "-1e999", "NaN", "Infinity", "1e300", "-1", "99999999999"],
)
def test_an_impossible_restart_time_is_damage_and_not_a_crash(live, frigate_log, raw):
    healthy_frigate(live)
    link = json.loads(live.paths.link_state.read_text())
    text = json.dumps({**link, "detector_restart_at": None}).replace(
        '"detector_restart_at": null', f'"detector_restart_at": {raw}'
    )
    live.paths.link_state.write_text(text)
    frigate_log.text = HAILO_LINE

    live.restart_daemon()
    live.daemon.tick()
    run_for(live, DETECTOR_ERROR_RESTART_SECONDS + TICK_SECONDS)

    saved = json.loads(live.paths.link_state.read_text())
    assert saved["generation"] >= NOW
    assert saved["detector_restart_at"] is None or 0 <= saved["detector_restart_at"] <= live.clock_epoch


def test_a_real_restart_time_is_not_damage(live):
    live.daemon.tick()
    link = json.loads(live.paths.link_state.read_text())
    live.paths.link_state.write_text(json.dumps({**link, "detector_restart_at": NOW - 5.5}))

    live.restart_daemon()
    live.daemon.tick()

    assert json.loads(live.paths.link_state.read_text())["generation"] == link["generation"]


def test_no_restart_for_a_frigate_that_is_not_running(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    live.systemd.running.discard(FRIGATE_UNIT)
    live.systemd.states[FRIGATE_UNIT] = "activating"
    live.systemd.calls.clear()

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS + TICK_SECONDS)

    assert frigate_restarts(live) == []


def test_no_restart_while_the_camera_link_holds_frigate_back(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()
    live.step.state = LinkState.ADOPTING
    live.systemd.calls.clear()

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS + TICK_SECONDS)

    assert frigate_restarts(live) == []


def test_a_failed_restart_does_not_stop_the_tick(live, frigate_log):
    healthy_frigate(live)
    frigate_log.text = HAILO_LINE
    live.clock_epoch += LOG_SCAN_SECONDS
    live.daemon.tick()

    def broken(unit):
        raise SystemdError("no")

    live.systemd.restart = broken

    run_for(live, DETECTOR_ERROR_RESTART_SECONDS + TICK_SECONDS)

    assert live.state()["updated"] == live.clock_epoch


# Whether segments are really being written


@pytest.fixture
def disk(monkeypatch):
    """The media disk's free space, as a fraction of the whole."""
    free = {"fraction": 0.5, "error": None}

    def statvfs(path):
        if free["error"]:
            raise free["error"]
        return SimpleNamespace(f_blocks=1000, f_bavail=int(1000 * free["fraction"]))

    monkeypatch.setattr(daemon_module.os, "statvfs", statvfs)
    return free


def segment(node, name, age):
    path = node.paths.frigate_cache / name
    path.write_bytes(b"mp4")
    stamp = node.clock_epoch - age
    os.utime(path, (stamp, stamp))
    return path


def test_a_fresh_segment_with_room_on_disk_is_writing(live, disk):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    live.daemon.tick()
    assert live.state()["record_writing"] is True


def test_a_dead_main_stream_leaves_only_an_old_segment(live, disk):
    # Detection's sub stream is fine, so stats and camera_fps say nothing about this.
    segment(live, "cam-8da040@20261105120000+0000.mp4", 60)
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_the_newest_segment_decides(live, disk):
    segment(live, "cam-8da040@20261105110000+0000.mp4", 3000)
    segment(live, "cam-8da040@20261105120000+0000.mp4", 59)
    live.daemon.tick()
    assert live.state()["record_writing"] is True


def test_a_full_disk_is_not_writing_however_new_the_segment(live, disk):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    disk["fraction"] = 0.049
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_five_percent_free_is_enough(live, disk):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    disk["fraction"] = 0.05
    live.daemon.tick()
    assert live.state()["record_writing"] is True


def test_a_disk_that_cannot_be_measured_is_not_writing(live, disk):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    disk["error"] = OSError("gone")
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_an_empty_disk_report_is_not_writing(live, monkeypatch):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    monkeypatch.setattr(
        daemon_module.os, "statvfs", lambda path: SimpleNamespace(f_blocks=0, f_bavail=0)
    )
    live.daemon.tick()
    assert live.state()["record_writing"] is False


@pytest.mark.parametrize(
    "name",
    [
        "other-cam@20261105120000+0000.mp4",
        "cam-8da040-x@20261105120000+0000.mp4",
        "preview_cam-8da040@20261105120000+0000.mp4",
        "cam-8da040@20261105120000+0000.tmp",
        "cam-8da040",
    ],
)
def test_only_this_cameras_segments_count(live, disk, name):
    segment(live, name, 2)
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_a_segment_dated_in_the_future_proves_nothing(live, disk):
    segment(live, "cam-8da040@20261105120000+0000.mp4", -120)
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_a_missing_cache_is_not_writing_and_does_not_stop_the_tick(live, disk):
    live.paths.frigate_cache.rmdir()
    live.daemon.tick()
    assert live.state()["record_writing"] is False


def test_a_segment_removed_mid_scan_is_skipped(live, disk, monkeypatch):
    segment(live, "cam-8da040@20261105120000+0000.mp4", 2)
    real = daemon_module.os.scandir

    class Vanishing:
        name = "cam-8da040@20261105110000+0000.mp4"

        def is_file(self):
            return True

        def stat(self):
            raise FileNotFoundError(self.name)

    def scandir(path):
        return [Vanishing(), *real(path)]

    monkeypatch.setattr(daemon_module.os, "scandir", scandir)
    live.daemon.tick()
    assert live.state()["record_writing"] is True


def test_unprovisioned_is_not_writing(node, disk):
    segment(node, "cam-8da040@20261105120000+0000.mp4", 2)
    node.daemon.tick()
    assert node.state()["record_writing"] is False


def test_a_docker_that_does_not_answer_is_not_asked_every_tick(node, frigate_log):
    asked = []
    frigate_log.started_at = lambda: asked.append(1)
    node.daemon.tick()
    node.daemon.tick()
    assert len(asked) == 1


def test_a_failed_link_reading_still_reports_the_detector_error(node, frigate_log):
    frigate_log.text = HAILO_LINE
    node.paths.boot_id.unlink()
    node.daemon.tick()
    state = node.state()
    assert state["state"] == "failed"
    assert state["detector_error"] is True


def _fake_run(calls, *, stdout="", returncode=0, raises=None):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    return run


def test_frigate_log_asks_docker_for_the_containers_start():
    calls = []
    log = FrigateLog(run=_fake_run(calls, stdout="2026-11-05T10:00:00.123456789Z\n"))
    assert log.started_at() == "2026-11-05T10:00:00.123456789Z"
    assert calls[0][0] == [
        "/usr/bin/docker", "inspect", "--format", "{{.State.StartedAt}}", "frigate",
    ]
    assert calls[0][1]["timeout"] > 0
    # A CLI warning here must not end up as the time handed to --since.
    assert calls[0][1]["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "answer",
    [
        "WARNING: Error loading config file\n2026-11-05T10:00:00Z\n",
        "true\n",
        "--since\n",
        "2026-11-05\n",
    ],
)
def test_frigate_log_refuses_a_start_that_is_not_a_time(answer):
    assert FrigateLog(run=_fake_run([], stdout=answer)).started_at() is None


def test_frigate_log_reads_both_output_streams():
    # HailoRT writes its own lines from native code, not through Frigate's logger.
    calls = []
    log = FrigateLog(run=_fake_run(calls, stdout=HAILO_LINE))
    assert log.since_time("1800000000") == HAILO_LINE
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/docker", "logs", "--since", "1800000000", "frigate"]
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize(
    "fake",
    [
        {"returncode": 1, "stdout": "Error: No such container: frigate\n"},
        {"raises": subprocess.TimeoutExpired("docker", 1)},
        {"raises": FileNotFoundError("docker")},
        {"raises": PermissionError("docker")},
    ],
)
def test_frigate_log_gives_none_when_docker_does_not_answer(fake):
    log = FrigateLog(run=_fake_run([], **fake))
    assert log.started_at() is None
    assert log.since_time("0") is None


def test_frigate_log_gives_no_start_for_an_empty_answer():
    assert FrigateLog(run=_fake_run([], stdout="\n")).started_at() is None


def test_firewall_accepts_dhcp_on_the_camera_link_ahead_of_the_drop():
    lines = [line.strip() for line in NFT.read_text().splitlines()]
    accept = lines.index("iifname $CAMERA_IF udp sport 68 udp dport 67 counter accept")
    drop = lines.index("iifname $CAMERA_IF counter drop")
    assert accept < drop
    assert sum("$CAMERA_IF" in line and "accept" in line for line in lines) == 1
