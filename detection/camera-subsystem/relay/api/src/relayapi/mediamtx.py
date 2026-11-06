"""Keeps one on-demand MediaMTX path per node, through MediaMTX's control API.

MediaMTX forgets these on a restart, so they are checked again on a timer. Only configuration
is read here. A path's config never opens a connection to the node: MediaMTX dials a node only
when a reader asks for its path, and that is the whole point of ``sourceOnDemand``.

A source URL holds the broker's credentials, so neither it nor a response from MediaMTX is ever
logged.
"""

import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from relayapi.settings import Settings
from relayapi.store import Store

logger = logging.getLogger(__name__)

NODE_RTSP_PORT = 8554
CLOSE_AFTER = "10s"
_RETRY_S = 5.0


def source_url(user: str, password: str, address: str, node_id: str) -> str:
    host = f"[{address}]" if ":" in address else address
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return f"rtsp://{credentials}@{host}:{NODE_RTSP_PORT}/{node_id}"


def path_config(source: str) -> dict:
    return {
        "source": source,
        "sourceOnDemand": True,
        "sourceOnDemandCloseAfter": CLOSE_AFTER,
        "rtspTransport": "tcp",
    }


class _Refused(Exception):
    pass


class PathSync:
    def __init__(
        self, settings: Settings, store: Store, transport: httpx.BaseTransport | None = None
    ):
        self._settings = settings
        self._store = store
        self._client = httpx.AsyncClient(
            base_url=settings.mediamtx_api_url,
            auth=(settings.mediamtx_api_user, settings.mediamtx_api_password),
            timeout=5.0,
            trust_env=False,
            transport=transport,
        )
        # What the control API last confirmed for each node.
        self._applied: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    def wake(self) -> None:
        """Called from the MQTT thread when a heartbeat holds an address."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        next_full = 0.0
        while True:
            full = time.monotonic() >= next_full
            await self.sync_once(full)
            if full:
                next_full = time.monotonic() + self._settings.path_sync_interval_s
            try:
                async with asyncio.timeout(_RETRY_S):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()

    async def sync_once(self, full: bool) -> None:
        """Bring MediaMTX into line. ``full`` also reads back paths thought to be right. A write
        happens only on a real difference, because a write cuts off anyone watching."""
        async with self._lock:
            for node_id, address in self._store.node_addresses().items():
                source = source_url(
                    self._settings.broker_user, self._settings.broker_password, address, node_id
                )
                if not full and self._applied.get(node_id) == source:
                    continue
                try:
                    await self._apply(node_id, source)
                except (httpx.HTTPError, httpx.InvalidURL, _Refused) as error:
                    self._applied.pop(node_id, None)
                    logger.warning(
                        "path %s not updated, will retry: %s", node_id, type(error).__name__
                    )
                    if isinstance(error, _Refused):
                        logger.warning("MediaMTX's control API answered %s", error)
                else:
                    self._applied[node_id] = source

    async def _apply(self, node_id: str, source: str) -> None:
        desired = path_config(source)
        current = await self._client.get(f"/v3/config/paths/get/{node_id}")
        if current.status_code == 404:
            self._check(await self._client.post(f"/v3/config/paths/add/{node_id}", json=desired))
            logger.info("path %s created", node_id)
            return
        self._check(current)
        held = current.json()
        if all(held.get(key) == value for key, value in desired.items()):
            return
        self._check(await self._client.patch(f"/v3/config/paths/patch/{node_id}", json=desired))
        logger.info("path %s updated", node_id)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code >= 300:
            raise _Refused(str(response.status_code))
