"""The ``camera-provision`` command. ``run`` is what the systemd unit starts."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Sequence

from cameraprovision.daemon import Provisioner, Settings
from cameraprovision.store import Paths
from cameraprovision.system import RealSystem

logger = logging.getLogger("cameraprovision")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="camera-provision")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the provisioning daemon")
    run.add_argument("--bridge-config", type=Path, default=Paths.bridge_config)
    run.add_argument("--state-dir", type=Path, default=Paths.state_dir)
    run.add_argument("--ap-ip", default=Settings.ap_ip)
    run.add_argument("--interface", default=Settings.interface)
    run.add_argument("--button-line", default="GPIO17", help="libgpiod line name")
    run.add_argument("--service-user", default="camerabridge", help="owner of the bridge config")
    run.add_argument("--join-timeout", type=float, default=Settings.join_timeout_s)
    run.add_argument("--ap-window", type=float, default=Settings.ap_window_s)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    daemon = Provisioner(
        system=RealSystem(
            interface=args.interface,
            button_line=args.button_line,
            service_user=args.service_user,
        ),
        paths=Paths(
            bridge_config=args.bridge_config,
            state_dir=args.state_dir,
        ),
        settings=Settings(
            ap_ip=args.ap_ip,
            interface=args.interface,
            join_timeout_s=args.join_timeout,
            ap_window_s=args.ap_window,
        ),
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        daemon.run(stop)
    except Exception as exc:
        # The type only: a message can quote a secret. systemd restarts on nonzero.
        logger.critical("exiting after an unrecoverable failure: %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
