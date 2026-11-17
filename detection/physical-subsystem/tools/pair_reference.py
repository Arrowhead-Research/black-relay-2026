#!/usr/bin/env python3
#
# REFERENCE COPY, NOT PART OF THE BRIDGE.
#
# What this is: a copy of /mnt/project-files/pairing/blackrelay_pair.py, the read-only pairing / snapshot / watch tool
# built on the third-party `qolsys-controller` library (see tools/pair_reference.requirements.txt; Python 3.12+).
#
# Why it is here: it is the FALLBACK if `br2 pair` (src/br2/pairing.py, our own implementation of the same exchange)
# fails on real IQ5 hardware. Our implementation has only been tested against a simulator and an in-process fake panel;
# this tool is the library's own, known-to-pair code path. Pair with it (`--pki-dir D`: the library stores the identity
# in D/pki/<12 hex>/), set iq_remote.pki_dir to D/pki (the bridge expects <12 hex>/<id>.key|.secure|.qolsys, which is
# that layout; confirm the three files exist), then run `br2 snapshot`, which also records the panel certificate pin
# (README, "Panel identity").
#
# What it is NOT: it BYPASSES the bridge's write guard. Every panel write in the bridge goes through GuardedPanel /
# PairGuard (exact allow-lists, tested); this script uses the library's own client directly and is only as read-only as
# its author's discipline. It is not used at runtime, not imported by any br2 module, not installed in any image, and
# is excluded from the packages (tests/test_round4_low_fixes.py checks the wheel and every Dockerfile). Do not add it
# to a Dockerfile.
#
# WARNING about the library it imports: the `qolsys-controller` library contains functions that ARM and DISARM the
# panel (and other state-changing commands). THIS script never calls any of them: it only pairs, reads the database and
# watches. Anyone editing this file must keep it that way; reading the code is the only guarantee, because the library
# is not behind the bridge's write guard.
#
"""Read-only pairing, snapshot and live-watch tool for a Qolsys IQ Panel 5.

Uses the qolsys-controller library (pinned in requirements.txt) to act as a
virtual IQ Remote on the LAN. This script never arms, disarms, bypasses or
sends any other command that changes panel state, and it never asks for a
user code. Pairing does need the INSTALLER or DEALER code typed on the panel.

  pair      one-time pairing; press Pair on the panel when told
  snapshot  connect, print and save a JSON list of partitions and zones
  watch     connect and log zone/partition changes for --minutes (default 10)

Requires Python 3.12 or newer.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qolsys_controller.controller import QolsysController
from qolsys_controller.enum_qolsys import QolsysNotification, ZoneStatus
from qolsys_controller.errors import (
    QolsysConfigError,
    QolsysMqttError,
    QolsysOperationTimeoutError,
    QolsysSqlError,
    QolsysSslError,
)

PANEL_TLS_PORT = 8883  # MQTT over TLS on the panel
DEFAULT_PAIRING_TIMEOUT = 300  # seconds; the library default is 180
DEFAULT_CONNECT_TIMEOUT = 60  # seconds
PKI_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PKI_SUFFIXES = (".key", ".csr", ".cer", ".secure", ".qolsys")

NETWORK_HINT = (
    "Check that this computer and the panel are on the same subnet, the panel is powered on, "
    "and the firewall allows outbound TCP 8883 to the panel."
)
PAIRING_HINT = (
    "Check that Pair was pressed on the panel, the installer/dealer code was entered there, "
    "this computer and the panel share a subnet, and the firewall allows inbound TCP from the panel "
    "on the pairing port (random 50000-55000 unless --pairing-port is given) plus multicast DNS (UDP 5353)."
)


class ScriptError(Exception):
    """A problem the user can act on; printed without a traceback."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_ip(value: str, label: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError as err:
        raise ScriptError(f"{label} '{value}' is not a valid IP address") from err
    return value


# ---------------------------------------------------------------------------
# PKI folder layout
# ---------------------------------------------------------------------------


def library_pki_dir(pki_dir: Path) -> Path:
    # qolsys-controller stores PKI folders at <config_directory>/pki/<12-hex-id>/.
    # --pki-dir is used as the library's config_directory.
    return pki_dir / "pki"


def list_pki_folders(pki_dir: Path) -> list[Path]:
    root = library_pki_dir(pki_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and PKI_ID_RE.fullmatch(p.name))


def is_complete(folder: Path) -> bool:
    return all((folder / (folder.name + suffix)).is_file() for suffix in PKI_SUFFIXES)


def preflight_pair(pki_dir: Path) -> None:
    folders = list_pki_folders(pki_dir)
    if len(folders) > 1:
        raise ScriptError(
            f"Found {len(folders)} PKI folders in {library_pki_dir(pki_dir)}. "
            "Keep only the one you want, then rerun."
        )
    if folders and is_complete(folders[0]):
        raise ScriptError(
            f"Already paired: {folders[0]} holds a complete pairing. Use 'snapshot' or 'watch'. "
            "Do not delete or move this folder: the panel issues only one client certificate per "
            "virtual IQ Remote, so re-pairing may not be possible."
        )
    if folders:
        print(f"Found an unfinished pairing in {folders[0]}; resuming it with the same identity.")


def find_paired_folder(pki_dir: Path) -> Path:
    folders = list_pki_folders(pki_dir)
    if not folders:
        raise ScriptError(f"No pairing found under {library_pki_dir(pki_dir)}. Run 'pair' first.")
    if len(folders) > 1:
        raise ScriptError(
            f"Found {len(folders)} PKI folders in {library_pki_dir(pki_dir)}. "
            "Keep only the one you want, then rerun."
        )
    if not is_complete(folders[0]):
        raise ScriptError(f"Pairing in {folders[0]} is incomplete. Run 'pair' again to finish it.")
    return folders[0]


# ---------------------------------------------------------------------------
# Controller setup
# ---------------------------------------------------------------------------


def detect_plugin_ip(panel_ip: str) -> str:
    # connect() on a UDP socket sends nothing; it only selects the local
    # interface that routes to the panel.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((panel_ip, PANEL_TLS_PORT))
            return str(sock.getsockname()[0])
    except OSError as err:
        raise ScriptError(
            f"Could not find this computer's LAN address toward {panel_ip} ({err}). Pass --plugin-ip."
        ) from err


def make_controller(args: argparse.Namespace) -> QolsysController:
    controller = QolsysController()
    settings = controller.settings

    settings.config_directory = str(args.pki_dir)
    settings.panel_ip = validate_ip(args.panel_ip, "--panel-ip")
    settings.plugin_ip = validate_ip(args.plugin_ip, "--plugin-ip") if args.plugin_ip else detect_plugin_ip(args.panel_ip)

    settings.auto_discover_pki = True  # use the single PKI folder under --pki-dir
    settings.pairing_resume = True  # continue an interrupted pairing with the same folder
    settings.pairing_timeout = getattr(args, "pairing_timeout", DEFAULT_PAIRING_TIMEOUT)
    settings.pairing_port = getattr(args, "pairing_port", 0) or 0  # 0 = random high port

    settings.mqtt_bridge_enabled = False  # do not start the embedded MQTT broker/bridge
    settings.check_user_code_on_arm = False
    settings.check_user_code_on_disarm = False
    settings.log_mqtt_messages = False
    return controller


async def connect_once(controller: QolsysController, timeout: int) -> None:
    """Connect, load the panel database, then return. Never retries."""
    try:
        await asyncio.wait_for(
            controller.run_forever(reconnect=False, run_once=True, start_pairing=False),
            timeout=timeout,
        )
    except TimeoutError as err:
        raise ScriptError(f"No response from the panel within {timeout} s. {NETWORK_HINT}") from err


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _sort_key(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def zone_row(zone: Any) -> dict[str, Any]:
    status = zone.sensorstatus
    return {
        "id": zone.zone_id,
        "name": zone.sensorname,
        "zone_type": zone.zone_type,
        "sensor_type": zone.sensortype.value,
        "sensor_group": zone.sensorgroup,
        "physical_type": zone.zone_physical_type,
        "partition_id": zone.partition_id,
        "status": status.value,
        "state": zone.sensorstate,
        "tampered": status == ZoneStatus.TAMPERED,
        "trouble": zone.is_trouble(),
        "battery_status": zone.battery_status or None,
        "battery_reported": zone.is_battery_enabled(),
        "powerg_battery_level": zone.powerg_battery_level,  # PowerG devices only, else None
        "powerg_battery_voltage": zone.powerg_battery_voltage,
    }


def partition_row(partition: Any) -> dict[str, Any]:
    return {
        "id": partition.id,
        "name": partition.name,
        "system_status": partition.system_status.value,
        "alarm_state": partition.alarm_state.value,
        "alarm_types": [alarm.value for alarm in partition.alarm_type_array],
        "open_zones": list(partition.open_zones),
        "last_error": partition.last_error.value,
        "status_changed_time": partition.system_status_changed_time,
    }


def panel_row(panel: Any) -> dict[str, Any]:
    return {
        "ac_status": panel.AC_STATUS,
        "battery_status": panel.BATTERY_STATUS,
        "tamper_state": panel.PANEL_TAMPER_STATE,
        "fail_to_communicate": panel.FAIL_TO_COMMUNICATE,
    }


def build_snapshot(controller: QolsysController) -> dict[str, Any]:
    zones = sorted(controller.state.zones, key=lambda z: _sort_key(z.zone_id))
    partitions = sorted(controller.state.partitions, key=lambda p: _sort_key(p.id))
    return {
        "captured_at": utc_now(),
        "panel": panel_row(controller.panel),
        "partitions": [partition_row(p) for p in partitions],
        "zones": [zone_row(z) for z in zones],
    }


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Watch
# ---------------------------------------------------------------------------


def describe_event(event: Any) -> str:
    data = event.data or {}
    if event.type == QolsysNotification.PANEL_STATUS_UPDATE:
        return f"connected={data.get('connected')}"
    name = (data.get("attributes") or {}).get("name", "")
    state = data.get("state") or {}
    return f"id={data.get('id')} name={name!r} state={json.dumps(state, separators=(',', ':'), default=str)}"


class EventLog:
    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")
        self.count = 0

    def write(self, event: Any) -> None:
        record = {"ts": utc_now(), "type": event.type.value, "data": event.data}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        self.count += 1
        print(f"{record['ts']} {event.type.value:<16} {describe_event(event)}", flush=True)

    def close(self) -> None:
        self._fh.close()


def attach_observers(controller: QolsysController, log: EventLog) -> None:
    state = controller.state

    def on_object_update(event: Any) -> None:
        log.write(event)

    def hook_objects() -> None:
        # Zones and partitions emit their own *_UPDATE events, so each object needs an observer.
        for zone in state.zones:
            zone.register(QolsysNotification.ZONE_UPDATE, on_object_update)
        for partition in state.partitions:
            partition.register(QolsysNotification.PARTITION_UPDATE, on_object_update)

    def on_state_event(event: Any) -> None:
        log.write(event)
        if event.type in (QolsysNotification.ZONE_ADD, QolsysNotification.PARTITION_ADD):
            hook_objects()

    hook_objects()
    for notification in (
        QolsysNotification.ZONE_ADD,
        QolsysNotification.ZONE_DELETE,
        QolsysNotification.PARTITION_ADD,
        QolsysNotification.PARTITION_DELETE,
        QolsysNotification.PANEL_STATUS_UPDATE,
    ):
        state.register(notification, on_state_event)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def pair_instructions(panel_ip: str, timeout: int) -> str:
    return f"""
Pairing this computer with the panel at {panel_ip} as a virtual IQ Remote.

On the PANEL you will need the INSTALLER or DEALER code (not a user code):
  Settings > Advanced Settings > enter installer/dealer code >
  Wi-Fi devices > IQ Remote > Pair
  (some firmware labels this Installation > Devices > Wi-Fi Devices > IQ Remote Devices)

This script starts its pairing server now and waits up to {timeout} seconds.
Press Pair on the panel now.
"""


async def run_pair(args: argparse.Namespace) -> int:
    preflight_pair(args.pki_dir)
    controller = make_controller(args)
    print(pair_instructions(args.panel_ip, args.pairing_timeout), flush=True)
    try:
        await asyncio.wait_for(
            controller.run_forever(reconnect=False, run_once=True, start_pairing=True),
            timeout=args.pairing_timeout + DEFAULT_CONNECT_TIMEOUT,
        )
    except TimeoutError as err:
        raise ScriptError(f"Pairing did not finish in time. {PAIRING_HINT}") from err
    except QolsysConfigError as err:
        raise ScriptError(f"{err}. {PAIRING_HINT}") from err

    folder = find_paired_folder(args.pki_dir)
    for item in folder.iterdir():
        if item.is_file():
            with contextlib.suppress(OSError):
                os.chmod(item, 0o600)
    print(f"\nPaired. PKI folder: {folder.resolve()}")
    print("Back up this whole folder somewhere safe. It holds the private key, and the panel")
    print("will not necessarily issue a new certificate if it is lost.")
    return 0


async def run_snapshot(args: argparse.Namespace) -> int:
    find_paired_folder(args.pki_dir)
    controller = make_controller(args)
    print(f"Connecting to panel {args.panel_ip} ...", file=sys.stderr, flush=True)
    await connect_once(controller, args.connect_timeout)

    snapshot = build_snapshot(controller)
    path = ensure_dir(args.out) / "snapshot.json"
    write_json_atomic(path, snapshot)

    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print(
        f"Saved snapshot: {path.resolve()} "
        f"({len(snapshot['partitions'])} partitions, {len(snapshot['zones'])} zones)",
        file=sys.stderr,
    )
    return 0


async def run_watch(args: argparse.Namespace) -> int:
    find_paired_folder(args.pki_dir)
    if args.minutes <= 0:
        raise ScriptError("--minutes must be greater than 0")

    controller = make_controller(args)
    out_dir = ensure_dir(args.out)
    events_path = out_dir / "events.jsonl"

    run_task = asyncio.create_task(
        controller.run_forever(reconnect=True, run_once=False, start_pairing=False),
        name="qolsys-controller",
    )
    connected = asyncio.create_task(controller.wait_until_connected(), name="wait-connected")
    log: EventLog | None = None
    try:
        done, _ = await asyncio.wait(
            {run_task, connected},
            timeout=args.connect_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task in done:
            run_task.result()  # re-raises the failure, e.g. QolsysConfigError
        if connected not in done:
            raise ScriptError(f"No connection to the panel within {args.connect_timeout} s. {NETWORK_HINT}")

        log = EventLog(events_path)
        attach_observers(controller, log)
        print(
            f"Connected. Logging to {events_path.resolve()} for {args.minutes:g} minute(s). "
            "Press Ctrl-C to stop early.",
            flush=True,
        )
        await asyncio.wait({run_task}, timeout=args.minutes * 60)
        if run_task.done():
            run_task.result()
    finally:
        connected.cancel()
        run_task.cancel()
        await asyncio.gather(connected, run_task, return_exceptions=True)
        if log is not None:
            print(f"Stopped. {log.count} event(s) written to {events_path.resolve()}")
            log.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only pairing, snapshot and live-watch for a Qolsys IQ Panel 5 (no arming or disarming)."
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging from qolsys-controller (no keys are printed)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--panel-ip", required=True, help="LAN IP of the IQ Panel 5, for example 192.168.1.50")
        sp.add_argument(
            "--pki-dir",
            type=Path,
            default=Path("./pki"),
            help="folder for pairing certificates; the library writes <this>/pki/<id>/ (default: ./pki)",
        )
        sp.add_argument("--plugin-ip", default="", help="this computer's LAN IP (auto-detected if omitted)")

    sp = sub.add_parser("pair", help="pair once as a virtual IQ Remote (needs the panel's Pair button)")
    add_common(sp)
    sp.add_argument("--pairing-timeout", type=int, default=DEFAULT_PAIRING_TIMEOUT, help="seconds to wait for Pair on the panel")
    sp.add_argument(
        "--pairing-port",
        type=int,
        default=0,
        help="fixed TCP port for the pairing server (0 = random 50000-55000); allow it inbound in your firewall",
    )
    sp.set_defaults(func=run_pair)

    sp = sub.add_parser("snapshot", help="connect, print and save partitions and zones as JSON")
    add_common(sp)
    sp.add_argument("--out", type=Path, default=Path("./capture"), help="output folder (default: ./capture)")
    sp.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT, help="seconds to wait for the panel")
    sp.set_defaults(func=run_snapshot)

    sp = sub.add_parser("watch", help="connect and log zone/partition changes to stdout and JSONL")
    add_common(sp)
    sp.add_argument("--out", type=Path, default=Path("./capture"), help="output folder (default: ./capture)")
    sp.add_argument("--minutes", type=float, default=10.0, help="how long to watch (default: 10)")
    sp.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT, help="seconds to wait for the panel")
    sp.set_defaults(func=run_watch)

    args = parser.parse_args(argv)
    if getattr(args, "pairing_port", 0) and not (1024 <= args.pairing_port <= 65535):
        parser.error("--pairing-port must be between 1024 and 65535 (or omitted)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # The library logs full tracebacks at ERROR level; this script prints its own short errors instead.
    library_level = logging.DEBUG if args.verbose else logging.CRITICAL
    for name in ("qolsys_controller", "aiomqtt", "zeroconf"):
        logging.getLogger(name).setLevel(library_level)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\nStopped. The script made no changes to the panel.", file=sys.stderr)
        return 130
    except ScriptError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    except (QolsysSslError, ssl.SSLError) as err:
        print(f"ERROR: TLS problem talking to the panel: {err}", file=sys.stderr)
        return 1
    except QolsysConfigError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    except (QolsysMqttError, QolsysOperationTimeoutError, QolsysSqlError) as err:
        print(f"ERROR: connection to the panel failed ({err}). {NETWORK_HINT}", file=sys.stderr)
        return 1
    except OSError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
