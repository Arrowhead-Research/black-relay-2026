"""The real side effects: thin adapters over nmcli, hostnamectl, tailscale and libgpiod.

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
# The same rule seed-kit.sh applies, since this is the same value.
_SERIAL_RE = re.compile(r"[0-9a-fA-F]{8,16}")


class SystemCommandError(Exception):
    """A command failed. The message never holds its output or arguments."""

    def __init__(
        self, message: str, returncode: int | None = None, *, not_installed: bool = False
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.not_installed = not_installed


def _run(argv: list[str], *, timeout: float = _QUICK_TIMEOUT_S) -> str:
    program = argv[0]
    not_installed = False
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
        not_installed = isinstance(exc, FileNotFoundError)
        message = f"{program} could not run: {type(exc).__name__}"
    else:
        return done.stdout
    logger.warning("%s", message)
    raise SystemCommandError(message, returncode, not_installed=not_installed)


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
        serial_path: Path = Path("/sys/firmware/devicetree/base/serial-number"),
    ) -> None:
        self._interface = interface
        self._button_line = button_line
        self._service_user = service_user
        self._boot_id_path = boot_id_path
        self._serial_path = serial_path

    def boot_id(self) -> str:
        return self._boot_id_path.read_text(encoding="ascii").strip()

    def read_serial(self) -> str:
        """Return the Pi's serial, from the file ``seed-kit.sh`` names the AP after.

        The device tree value ends in a NUL. Raises ``OSError`` or ``ValueError`` if it
        cannot be read or is not a serial.
        """
        serial = self._serial_path.read_bytes().replace(b"\0", b"").decode("ascii").rstrip("\n")
        if not _SERIAL_RE.fullmatch(serial):
            raise ValueError("unexpected serial format")
        return serial

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

    def ap_active(self) -> bool:
        """True only if the AP profile is activated. An inactive profile prints nothing."""
        return (
            _run(
                [
                    "nmcli", "--terse", "--mode", "multiline", "--fields", "GENERAL.STATE",
                    "connection", "show", AP_PROFILE_ID,
                ]
            ).strip()
            == "GENERAL.STATE:activated"
        )

    def set_hostname(self, hostname: str) -> None:
        """Set the hostname, then restart avahi so ``<hostname>.local`` follows it.

        A failed restart only costs mDNS until the next boot, so it is logged and ignored.
        ``--no-block`` keeps it from holding up the join.
        """
        _run(["hostnamectl", "set-hostname", hostname])
        if shutil.which("avahi-daemon") is None:
            return
        try:
            _run(["systemctl", "--no-block", "try-restart", "avahi-daemon.service"])
        except SystemCommandError as exc:
            logger.warning("restarting avahi failed: %s", type(exc).__name__)

    def restart_timesync(self) -> None:
        """Make timesyncd read the saved time server. ``--no-block`` keeps the join moving."""
        _run(["systemctl", "--no-block", "restart", "systemd-timesyncd.service"])

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
        # No --force-reauth: a retry after a killed login would re-authenticate with
        # a possibly spent one-off key and could lose a working login.
        _run(tailscale_up_args(key_file, hostname), timeout=_TAILSCALE_TIMEOUT_S)

    def gpio_reprovision_held(self) -> bool:
        """Read the active-low button by line name, with libgpiod 1.x or 2.x.

        ``gpiofind`` exists only in 1.x. If neither version is installed the second
        command raises, and the caller must not take that for a released button.
        """
        options = ["--active-low", "--bias=pull-up"]
        try:
            found = _run(["gpiofind", self._button_line]).split()
        except SystemCommandError as exc:
            if not exc.not_installed:
                raise
            argv = ["gpioget", "--numeric", *options, self._button_line]
        else:
            if len(found) != 2:
                raise SystemCommandError("gpiofind gave unexpected output")
            argv = ["gpioget", *options, *found]
        return _run(argv).strip() == "1"

    def chown_to_service_user(self, path: Path) -> None:
        shutil.chown(path, user=self._service_user)
