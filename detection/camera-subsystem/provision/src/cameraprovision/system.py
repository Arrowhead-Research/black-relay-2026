"""The real side effects: thin adapters over nmcli, tailscale and libgpiod.

Every command is an argv list with a timeout, and never a shell. Secrets reach commands
by file only. A failure raises ``SystemCommandError``, built only from the program name
and exit status: stderr and argv are left out, and the original exception is not chained.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from cameraprovision.netprofile import UPLINK_PROFILE_ID, tailscale_up_args

__all__ = ["AP_PROFILE_ID", "RealSystem", "SystemCommandError", "parse_ssids"]

logger = logging.getLogger(__name__)

# Created when the image is seeded (Task 7), holding the kit's SSID and passphrase.
AP_PROFILE_ID = "black-relay-setup-ap"

_NMCLI_NOT_FOUND = 10
_AP_WAIT_S = 30
_QUICK_TIMEOUT_S = 15.0
_SCAN_TIMEOUT_S = 30.0
_TAILSCALE_TIMEOUT_S = 60.0


class SystemCommandError(Exception):
    """A command failed. The message never holds its output or arguments."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def _run(argv: list[str], *, timeout: float = _QUICK_TIMEOUT_S) -> str:
    program = argv[0]
    # C locale: nmcli translates the states parsed below.
    env = {**os.environ, "LC_ALL": "C"}
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            check=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        returncode = exc.returncode
        message = f"{program} exited with status {returncode}"
    except subprocess.TimeoutExpired:
        returncode = None
        message = f"{program} timed out after {timeout:g} s"
    except OSError as exc:
        returncode = None
        message = f"{program} could not run: {type(exc).__name__}"
    else:
        return done.stdout
    logger.warning("%s", message)
    raise SystemCommandError(message, returncode)


def parse_ssids(output: str) -> list[str]:
    """Return the unique SSIDs in nmcli's terse output, unescaped, in order, without blanks."""
    names = (re.sub(r"\\(.)", r"\1", line) for line in output.splitlines())
    return list(dict.fromkeys(name for name in names if name))


def _parse_uplink(output: str) -> tuple[bool, bool]:
    """Return (site profile active in infrastructure mode, holds a DHCP address)."""
    active = infrastructure = lease = False
    for line in output.splitlines():
        name, _, value = line.partition(":")
        if name == "GENERAL.STATE":
            active = value == "activated"
        elif name == "802-11-wireless.mode":
            infrastructure = value == "infrastructure"
        elif name.startswith("DHCP4.OPTION") and value.startswith("ip_address ="):
            lease = True
    associated = active and infrastructure
    return associated, associated and lease


class RealSystem:
    def __init__(
        self,
        *,
        interface: str = "wlan0",
        button_line: str = "GPIO17",
        service_user: str = "camerabridge",
        boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
    ) -> None:
        self._interface = interface
        self._button_line = button_line
        self._service_user = service_user
        self._boot_id_path = boot_id_path

    def boot_id(self) -> str:
        return self._boot_id_path.read_text(encoding="ascii").strip()

    def scan_ssids(self) -> list[str]:
        return parse_ssids(
            _run(
                [
                    "nmcli", "--terse", "--fields", "SSID", "device", "wifi", "list",
                    "--rescan", "yes", "ifname", self._interface,
                ],
                timeout=_SCAN_TIMEOUT_S,
            )
        )

    def start_ap(self) -> None:
        """Return once the profile is active, so the AP's address exists to bind to."""
        _run(
            ["nmcli", "--wait", str(_AP_WAIT_S), "connection", "up", AP_PROFILE_ID],
            timeout=_AP_WAIT_S + _QUICK_TIMEOUT_S,
        )

    def stop_ap(self) -> None:
        try:
            _run(["nmcli", "connection", "down", AP_PROFILE_ID], timeout=_AP_WAIT_S)
        except SystemCommandError as exc:
            if exc.returncode != _NMCLI_NOT_FOUND:  # not active is already what we want
                raise

    def reload_connections(self) -> None:
        _run(["nmcli", "connection", "reload"])

    def join_uplink(self) -> None:
        """Ask for the join without waiting. The uplink probe decides whether it worked."""
        _run(["nmcli", "--wait", "0", "connection", "up", UPLINK_PROFILE_ID])

    def _uplink(self) -> tuple[bool, bool]:
        return _parse_uplink(
            _run(
                [
                    "nmcli", "--terse", "--mode", "multiline", "--fields",
                    "GENERAL.STATE,802-11-wireless.mode,DHCP4",
                    "connection", "show", UPLINK_PROFILE_ID,
                ]
            )
        )

    def wifi_associated(self) -> bool:
        return self._uplink()[0]

    def ipv4_lease(self) -> bool:
        return self._uplink()[1]

    def tailscale_up(self, key_file: Path, hostname: str) -> None:
        _run(tailscale_up_args(key_file, hostname), timeout=_TAILSCALE_TIMEOUT_S)

    def gpio_reprovision_held(self) -> bool:
        """Read the active-low button by line name, with libgpiod 1.6 as on Bookworm."""
        found = _run(["gpiofind", self._button_line]).split()
        if len(found) != 2:
            raise SystemCommandError("gpiofind gave unexpected output")
        return _run(["gpioget", "--active-low", "--bias=pull-up", *found]).strip() == "1"

    def chown_to_service_user(self, path: Path) -> None:
        shutil.chown(path, user=self._service_user)
