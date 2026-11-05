"""Tests for ``cameraprovision.system``: argv shape, timeouts and output parsing.

No real command ever runs: ``subprocess.run`` is replaced for every test here. The
nmcli text below follows nmcli(1)'s terse format, and has NOT been recorded from a
target device, because this suite must not run nmcli on a development machine. Task 7's
checklist confirms the formats on Bookworm.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cameraprovision.netprofile import tailscale_up_args
from cameraprovision.system import RealSystem, SystemCommandError, parse_ssids

SITE_ACTIVE = """\
GENERAL.STATE:activated
802-11-wireless.mode:infrastructure
DHCP4.OPTION[1]:dhcp_lease_time = 86400
DHCP4.OPTION[2]:ip_address = 192.168.1.50
"""
SITE_ACTIVE_NO_LEASE = """\
GENERAL.STATE:activated
802-11-wireless.mode:infrastructure
"""
SITE_ACTIVATING = """\
GENERAL.STATE:activating
802-11-wireless.mode:infrastructure
"""
SITE_INACTIVE = """\
802-11-wireless.mode:infrastructure
"""
AP_MODE = """\
GENERAL.STATE:activated
802-11-wireless.mode:ap
DHCP4.OPTION[2]:ip_address = 10.42.0.1
"""
ADHOC_MODE = """\
GENERAL.STATE:activated
802-11-wireless.mode:adhoc
DHCP4.OPTION[2]:ip_address = 10.42.0.1
"""


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
        (AP_MODE, False, False),
        (ADHOC_MODE, False, False),
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
