"""The ``camera-link`` command. ``run`` is what the systemd unit starts."""

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from cameralink.daemon import Daemon, Paths, Systemctl, clear_limits


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="camera-link")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the camera link daemon")
    clear = commands.add_parser(
        "clear-limits",
        help="let the camera link try the latest camera again now. Never touches its password.",
    )
    clear.add_argument("--store", type=Path, default=Paths.store)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "clear-limits":
        return clear_limits(args.store)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    Daemon(Paths(), Systemctl()).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
