"""src/pytak_gateway/tak_connection.py

The connection to TAK Server: it connects with PyTAK over mTLS and puts CoT bytes on PyTAK's transmit queue.
It builds no CoT and knows nothing about node state. cot_publisher.py (the http and mqtt services) hands it
the CoT to send, and http_monitor.py sends the Fusion path's CoT through it.
"""

import asyncio
import logging
import os
import tempfile
from configparser import ConfigParser
from datetime import datetime, timezone
from pathlib import Path

import pytak

from . import cot
from .mission_package import GATEWAY_UID

logger = logging.getLogger(__name__)


CERTIFICATE_SETTINGS = ("PYTAK_TLS_CLIENT_CERT", "PYTAK_TLS_CLIENT_KEY")


def with_private_certificate(config: dict[str, str], directory: str | Path | None = None) -> dict[str, str]:
    """The same settings, with the client certificate (and key) pointing at a private copy.

    PyTAK opens the .p12 for reading and writing (crypto_functions.load_cert, mode "br+"). The
    stack keeps that file owner-read-only on a volume mounted read-only, so PyTAK cannot open it
    where it is. The copy is the gateway's alone (0600 in a 0700 directory, made new for this
    process), which keeps the original as locked down as start.sh left it.
    """
    sources = {config[name] for name in CERTIFICATE_SETTINGS if config.get(name) and os.path.isfile(config[name])}
    if not sources:
        return config
    if directory is None:
        directory = tempfile.mkdtemp(prefix="pytak-gateway-")  # 0700, ours
    else:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    copies = {}
    for number, source in enumerate(sorted(sources)):
        target = os.path.join(directory, f"{number}-{os.path.basename(source)}")
        with open(source, "rb") as original:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as copy:
                copy.write(original.read())
        os.chmod(target, 0o600)
        copies[source] = target
    return {name: copies.get(value, value) if name in CERTIFICATE_SETTINGS else value for name, value in config.items()}


IDENTIFY_SECONDS = 60  # how often the connection names itself again (cot.identify_event)


class TakConnection:
    """One PyTAK client connected to TAK Server. `send_many` is the `sender` a CotPublisher is given.

    The first thing it sends, and again every IDENTIFY_SECONDS, is cot.identify_event as `uid`: without it TAK
    drops every event addressed to a mission package (<marti><dest mission>) that came over this connection.
    Re-sending it is cheap and names the connection again should TAK forget it."""

    def __init__(self, config: dict[str, str], uid: str = GATEWAY_UID):
        self.config = config
        self.uid = uid
        self.clitool: pytak.CLITool | None = None
        self.running = False
        self._identify_task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Initializes connection to TAK server and spins up a queue used
        to send messages.

        Returns: bool based on start success
        """
        parser = ConfigParser()
        parser.add_section("pytak")

        for key, value in with_private_certificate(self.config).items():
            parser.set("pytak", key, value)

        self.clitool = pytak.CLITool(parser["pytak"])

        try:
            await self.clitool.setup()

        except (OSError, SyntaxError) as err:
            logger.error(f"Failed to connect to TAK server. {err}.")
            return False

        await self.clitool.tx_queue.put(cot.identify_event(self.uid, datetime.now(timezone.utc)))  # before anything
        asyncio.create_task(self.clitool.run())
        self._identify_task = asyncio.create_task(self._identify_loop())

        self.running = True
        logger.info(f"TAK connection started. Connected to {self.config.get('COT_URL')} as {self.uid}")
        return True

    async def _identify_loop(self, every: float = IDENTIFY_SECONDS) -> None:
        while True:
            await asyncio.sleep(every)
            await self.clitool.tx_queue.put(cot.identify_event(self.uid, datetime.now(timezone.utc)))

    async def send(self, data: str | bytes):
        if not self.running:
            raise RuntimeError("TAK connection not started")

        await self.clitool.tx_queue.put(data)

    async def send_many(self, *events: bytes) -> None:
        """The `sender` for a CotPublisher: queue each CoT for the PyTAK writer."""
        for ev in events:
            await self.send(ev)
