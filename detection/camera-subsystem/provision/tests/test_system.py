"""Tests for ``cameraprovision.system``: argv shape, timeouts and output parsing.

No real command ever runs: ``subprocess.run`` is replaced for every test here. The nmcli
text comes from ``fixtures/nmcli``, recorded from nmcli 1.52.1 on a development laptop and
redacted (see its README). The target image's nmcli has not been recorded.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cameraprovision.daemon import Provisioner, Settings
from cameraprovision.netprofile import tailscale_up_args
from cameraprovision.state import ProvisionState as S
from cameraprovision.store import Paths
from cameraprovision.system import RealSystem, SystemCommandError, parse_ssids

FIXTURES = Path(__file__).parent / "fixtures" / "nmcli"
SITE_ACTIVE = (FIXTURES / "site_active.txt").read_text()
SITE_INACTIVE = (FIXTURES / "site_inactive.txt").read_text()
STATE_ACTIVATED = (FIXTURES / "state_activated.txt").read_text()
STATE_INACTIVE = (FIXTURES / "state_inactive.txt").read_text()
SCAN = (FIXTURES / "scan.txt").read_text()

# Edited from the recording, for states it could not catch.
SITE_ACTIVE_NO_LEASE = "".join(
    line for line in SITE_ACTIVE.splitlines(keepends=True) if "ip_address" not in line
)
SITE_ACTIVATING = SITE_ACTIVE_NO_LEASE.replace("activated", "activating")
SITE_NO_MODE = "".join(
    line for line in SITE_ACTIVE.splitlines(keepends=True) if "wireless.mode" not in line
)


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.results: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        for marker, outcome in self.results.items():
            if marker in " ".join(argv):
                if isinstance(outcome, Exception):
                    raise outcome
                return subprocess.CompletedProcess(argv, 0, stdout=outcome, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def system() -> RealSystem:
    return RealSystem(button_line="GPIO17", service_user="camerabridge")


def every_adapter(system: RealSystem, tmp_path: Path) -> None:
    system.scan_ssids()
    system.start_ap()
    system.stop_ap()
    system.reload_connections()
    system.join_uplink()
    system.wifi_associated()
    system.ipv4_lease()
    system.tailscale_up(tmp_path / "key", "edge-7")
    system.gpio_reprovision_held()
    system.ap_active()
    system.set_hostname("edge-7")
    system.restart_timesync()


def test_every_adapter_uses_an_argv_list_with_a_timeout_and_no_shell(
    run: FakeRun, system: RealSystem, tmp_path: Path
) -> None:
    run.results["gpiofind"] = "gpiochip0 17\n"
    run.results["gpioget"] = "0\n"

    every_adapter(system, tmp_path)

    assert len(run.calls) >= 9
    for argv, kwargs in run.calls:
        assert isinstance(argv, list) and all(isinstance(part, str) for part in argv)
        assert kwargs["timeout"] > 0
        assert not kwargs.get("shell")
        assert kwargs["env"]["LC_ALL"] == "C"


def test_the_tailscale_key_is_passed_by_file_only(
    run: FakeRun, system: RealSystem, tmp_path: Path
) -> None:
    key_file = tmp_path / "tailscale.key"
    key_file.write_text("tskey-auth-s3cret")

    system.tailscale_up(key_file, "edge-7")

    assert run.calls[0][0] == tailscale_up_args(key_file, "edge-7")
    assert "--force-reauth" not in run.calls[0][0]
    assert "tskey-auth-s3cret" not in " ".join(run.calls[0][0])


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["tailscale"], stderr="tskey-auth-s3cret rejected"),
        subprocess.TimeoutExpired(["tailscale", "tskey-auth-s3cret"], 1),
        FileNotFoundError("tailscale tskey-auth-s3cret"),
    ],
)
def test_failures_become_system_errors_with_no_output_or_argv(
    run: FakeRun, system: RealSystem, tmp_path: Path, error: Exception
) -> None:
    run.results["tailscale"] = error

    with pytest.raises(SystemCommandError) as raised:
        system.tailscale_up(tmp_path / "key", "edge-7")

    assert "tskey" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "output,associated,lease",
    [
        (SITE_ACTIVE, True, True),
        (SITE_ACTIVE_NO_LEASE, True, False),
        (SITE_ACTIVATING, False, False),
        (SITE_INACTIVE, False, False),
        (SITE_NO_MODE, False, False),
        ("", False, False),
    ],
)
def test_uplink_probe_parses_the_site_profile(
    run: FakeRun, system: RealSystem, output: str, associated: bool, lease: bool
) -> None:
    run.results["connection show"] = output

    assert system.wifi_associated() is associated
    assert system.ipv4_lease() is lease


def test_uplink_probe_asks_about_the_site_profile(run: FakeRun, system: RealSystem) -> None:
    system.wifi_associated()

    argv = run.calls[0][0]
    assert argv[0] == "nmcli"
    assert argv[-3:] == ["connection", "show", "black-relay-uplink"]


def test_join_does_not_block_and_names_the_site_profile(run: FakeRun, system: RealSystem) -> None:
    system.join_uplink()

    assert run.calls[0][0] == [
        "nmcli", "--wait", "0", "connection", "up", "black-relay-uplink"
    ]


def test_start_ap_waits_for_the_profile_to_be_active(run: FakeRun, system: RealSystem) -> None:
    system.start_ap()

    argv, kwargs = run.calls[0]
    assert argv[-3:] == ["connection", "up", "black-relay-setup-ap"]
    wait = int(argv[argv.index("--wait") + 1])
    assert wait > 0
    assert kwargs["timeout"] > wait


def test_stopping_an_ap_that_is_not_up_is_not_an_error(run: FakeRun, system: RealSystem) -> None:
    run.results["connection down"] = subprocess.CalledProcessError(10, ["nmcli"])

    system.stop_ap()


def test_stop_ap_reports_any_other_failure(run: FakeRun, system: RealSystem) -> None:
    run.results["connection down"] = subprocess.CalledProcessError(8, ["nmcli"])

    with pytest.raises(SystemCommandError):
        system.stop_ap()


def test_scan_uses_the_wifi_interface_and_parses_ssids(run: FakeRun, system: RealSystem) -> None:
    run.results["wifi list"] = "Site-WiFi\n\nCafe\\: Free\nSite-WiFi\nback\\\\slash\n"

    assert system.scan_ssids() == ["Site-WiFi", "Cafe: Free", "back\\slash"]
    assert run.calls[0][0][-2:] == ["ifname", "wlan0"]


def test_parse_ssids_keeps_undecodable_names_encodable_later() -> None:
    assert parse_ssids("caf\udce9\n") == ["caf\udce9"]


def test_gpio_reads_by_line_name_through_libgpiod(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = "gpiochip4 17\n"
    run.results["gpioget"] = "1\n"

    assert system.gpio_reprovision_held() is True

    assert run.calls[0][0] == ["gpiofind", "GPIO17"]
    argv = run.calls[1][0]
    assert argv[0] == "gpioget"
    assert argv[-2:] == ["gpiochip4", "17"]
    assert "--active-low" in argv


def test_gpio_released_reads_false(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = "gpiochip4 17\n"
    run.results["gpioget"] = "0\n"

    assert system.gpio_reprovision_held() is False


def test_gpio_with_nonsense_output_is_an_error(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = "what\n"

    with pytest.raises(SystemCommandError):
        system.gpio_reprovision_held()


def test_boot_id_is_read_from_proc(tmp_path: Path) -> None:
    source = tmp_path / "boot_id"
    source.write_text("abc-123\n")

    assert RealSystem(boot_id_path=source).boot_id() == "abc-123"


def test_recorded_scan_parses_to_unique_names_with_no_blanks(
    run: FakeRun, system: RealSystem
) -> None:
    run.results["wifi list"] = SCAN

    names = system.scan_ssids()

    assert names and all(name.startswith("Redacted-") for name in names)
    assert len(names) == len(set(names))


def test_ap_active_reads_the_recorded_states(run: FakeRun, system: RealSystem) -> None:
    run.results["connection show"] = STATE_ACTIVATED
    assert system.ap_active() is True
    assert run.calls[-1][0][-3:] == ["connection", "show", "black-relay-setup-ap"]
    assert "GENERAL.STATE" in run.calls[-1][0]

    run.results["connection show"] = STATE_INACTIVE
    assert STATE_INACTIVE == ""
    assert system.ap_active() is False


def test_set_hostname_uses_hostnamectl(run: FakeRun, system: RealSystem) -> None:
    system.set_hostname("edge-7")

    assert run.calls[0][0] == ["hostnamectl", "set-hostname", "edge-7"]


AVAHI_RESTART = ["systemctl", "--no-block", "try-restart", "avahi-daemon.service"]


@pytest.fixture
def avahi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/sbin/{name}" if name == "avahi-daemon" else None
    )


def test_m2_set_hostname_restarts_avahi_after_hostnamectl(
    run: FakeRun, system: RealSystem, avahi: None
) -> None:
    system.set_hostname("edge-7")

    assert [argv for argv, _ in run.calls] == [
        ["hostnamectl", "set-hostname", "edge-7"],
        AVAHI_RESTART,
    ]


def test_m2_set_hostname_leaves_avahi_alone_when_it_is_absent(
    run: FakeRun, system: RealSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    system.set_hostname("edge-7")

    assert [argv for argv, _ in run.calls] == [["hostnamectl", "set-hostname", "edge-7"]]


def test_m2_a_failed_avahi_restart_is_logged_and_ignored(
    run: FakeRun, system: RealSystem, avahi: None, caplog: pytest.LogCaptureFixture
) -> None:
    run.results["try-restart"] = subprocess.CalledProcessError(5, "systemctl")

    system.set_hostname("edge-7")

    assert "restarting avahi failed" in caplog.text


def test_m2_a_failed_hostnamectl_raises_and_does_not_touch_avahi(
    run: FakeRun, system: RealSystem, avahi: None
) -> None:
    run.results["set-hostname"] = subprocess.CalledProcessError(1, "hostnamectl")

    with pytest.raises(SystemCommandError):
        system.set_hostname("edge-7")

    assert AVAHI_RESTART not in [argv for argv, _ in run.calls]


# --- I1: read_serial --------------------------------------------------------------


def test_i1_read_serial_strips_the_device_tree_nul(tmp_path: Path) -> None:
    serial = tmp_path / "serial-number"
    serial.write_bytes(b"10000000ABCDEF12\x00")

    assert RealSystem(serial_path=serial).read_serial() == "10000000ABCDEF12"


def test_i1_read_serial_raises_when_unreadable(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        RealSystem(serial_path=tmp_path / "absent").read_serial()


@pytest.mark.parametrize(
    "content",
    [b"", b"\x00", b"1234567", b"12345678901234567", b"not-a-serial", b"1000000\xff", b"12345678 "],
)
def test_i1_read_serial_rejects_what_seed_kit_rejects(tmp_path: Path, content: bytes) -> None:
    serial = tmp_path / "serial-number"
    serial.write_bytes(content)

    with pytest.raises(ValueError):
        RealSystem(serial_path=serial).read_serial()


def test_i1_read_serial_uses_the_same_source_as_seed_kit(system: RealSystem) -> None:
    script = (Path(__file__).parents[1] / "ops" / "seed-kit.sh").read_text()

    assert f"SERIAL_FILE:-{system._serial_path}}}" in script


def missing(program: str) -> FileNotFoundError:
    return FileNotFoundError(program)


def test_m6_gpio_v2_reads_by_name_without_gpiofind(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = missing("gpiofind")
    run.results["gpioget"] = "1\n"

    assert system.gpio_reprovision_held() is True

    argv = run.calls[-1][0]
    assert argv[0] == "gpioget" and argv[-1] == "GPIO17"
    assert "--numeric" in argv and "--active-low" in argv and "--bias=pull-up" in argv


def test_m6_gpio_v2_released_reads_false(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = missing("gpiofind")
    run.results["gpioget"] = "0\n"

    assert system.gpio_reprovision_held() is False


def test_m6_a_real_gpiofind_failure_is_not_taken_for_v2(run: FakeRun, system: RealSystem) -> None:
    """An unknown line name on 1.x must stay an error, not fall through."""
    run.results["gpiofind"] = subprocess.CalledProcessError(1, ["gpiofind"])

    with pytest.raises(SystemCommandError):
        system.gpio_reprovision_held()

    assert len(run.calls) == 1


def test_m6_no_libgpiod_is_an_error(run: FakeRun, system: RealSystem) -> None:
    run.results["gpiofind"] = missing("gpiofind")
    run.results["gpioget"] = missing("gpioget")

    with pytest.raises(SystemCommandError):
        system.gpio_reprovision_held()


def test_chown_uses_the_service_user(
    system: RealSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr("shutil.chown", lambda *a, **k: seen.append((*a, k)))

    system.chown_to_service_user(tmp_path / "f")

    assert seen == [(tmp_path / "f", {"user": "camerabridge"})]


def test_the_module_never_asks_for_a_shell() -> None:
    source = Path(__file__).parent.parent / "src/cameraprovision/system.py"

    assert "shell=" not in source.read_text()


# --- RealSystem wired to Provisioner, faked only at the process boundary ----------------


class Machine:
    """Answers commands with recorded nmcli output, tracking what NetworkManager would do."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.ap_up = False
        self.site_up = False
        self.site_connects = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] > 0 and not kwargs.get("shell")
        self.argv.append(argv)
        line = " ".join(argv)
        stdout = ""
        if argv[0] in ("gpiofind", "gpioget"):
            raise FileNotFoundError(argv[0])
        if "wifi list" in line:
            stdout = SCAN
        elif "connection show black-relay-setup-ap" in line:
            stdout = STATE_ACTIVATED if self.ap_up else STATE_INACTIVE
        elif "connection show black-relay-uplink" in line:
            stdout = SITE_ACTIVE if self.site_up else SITE_INACTIVE
        elif "connection up black-relay-setup-ap" in line:
            self.ap_up, self.site_up = True, False
        elif "connection down black-relay-setup-ap" in line:
            if not self.ap_up:
                raise subprocess.CalledProcessError(10, argv)
            self.ap_up = False
        elif "connection up black-relay-uplink" in line:
            self.site_up = self.site_connects
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class NoSocket:
    def __init__(self, app: Any, ip: str, port: int, interface: str) -> None:
        self.closed = False

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self.closed = True


def test_real_system_drives_the_daemon_through_recorded_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    machine = Machine()
    monkeypatch.setattr(subprocess, "run", machine)
    monkeypatch.setattr("shutil.chown", lambda *a, **k: None)
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-1\n")
    paths = Paths(
        bridge_config=tmp_path / "etc/camerabridge/bridge.yaml",
        mediamtx_env=tmp_path / "etc/camerabridge/mediamtx.env",
        nm_connections_dir=tmp_path / "etc/NetworkManager/system-connections",
        state_dir=tmp_path / "var/lib/camera-provision",
        ntp_dropin=tmp_path / "etc/systemd/timesyncd.conf.d/black-relay.conf",
    )
    now = [1000.0]
    daemon = Provisioner(
        system=RealSystem(boot_id_path=boot_id),
        paths=paths,
        clock=lambda: now[0],
        settings=Settings(),
        server_factory=NoSocket,
    )
    paths.nm_connections_dir.mkdir(parents=True)
    paths.nm_keyfile.write_text("[connection]\n")
    paths.state_dir.mkdir(parents=True)
    paths.node_file.write_text('{"hostname": "edge-7", "vpn_requested": false}')

    def tick(seconds: float) -> None:
        now[0] += seconds
        daemon.tick()

    daemon.start()
    try:
        # No libgpiod here: reads as not held, and says so.
        assert "setup button" in caplog.text
        # Recorded inactive output means no uplink, so this times out to the setup AP.
        assert daemon.state is S.JOINING
        assert ["hostnamectl", "set-hostname", "edge-7"] in machine.argv
        assert ["nmcli", "--wait", "0", "connection", "up", "black-relay-uplink"] in machine.argv
        tick(120)
        assert daemon.state is S.FALLBACK_AP and machine.ap_up

        # Recorded "activated" keeps the AP alone; losing it restarts it.
        starts = sum("black-relay-setup-ap" in a and "up" in a for a in machine.argv)
        tick(10)
        assert sum("black-relay-setup-ap" in a and "up" in a for a in machine.argv) == starts
        machine.ap_up = False
        tick(10)
        assert machine.ap_up

        # Back to joining, where the recorded active output with a lease means uplink.
        machine.site_connects = True
        tick(300)
        assert daemon.state is S.JOINING and not machine.ap_up and machine.site_up
        tick(1)
        assert daemon.state is S.PROVISIONED
    finally:
        daemon.shutdown()


# --- owner decision: the NTP drop-in and timesyncd ---------------------------------


def test_restart_timesync_does_not_wait_for_the_restart(run: FakeRun, system: RealSystem) -> None:
    system.restart_timesync()

    assert run.calls[0][0] == ["systemctl", "--no-block", "restart", "systemd-timesyncd.service"]


def test_restart_timesync_reports_a_failure(run: FakeRun, system: RealSystem) -> None:
    run.results["restart"] = subprocess.CalledProcessError(1, "systemctl")

    with pytest.raises(SystemCommandError):
        system.restart_timesync()


def test_the_unit_lets_the_daemon_write_the_ntp_dropin_with_a_readable_directory() -> None:
    unit = (Path(__file__).parents[1] / "ops" / "camera-provision.service").read_text()
    directories = next(
        line for line in unit.splitlines() if line.startswith("ConfigurationDirectory=")
    )

    # Under UMask=0077 the store's own mkdir would leave a directory timesyncd cannot read.
    assert "systemd/timesyncd.conf.d" in directories
    assert "ConfigurationDirectoryMode=0755" in unit
    assert Paths().ntp_dropin == Path("/etc/systemd/timesyncd.conf.d/black-relay.conf")
