"""Keeps one on-demand MediaMTX path per node, through MediaMTX's control API.

MediaMTX forgets these on a restart, so they are checked again on a timer. Only configuration
is read here. A path's config never opens a connection to the node: MediaMTX dials a node only
when a reader asks for its path, and that is the whole point of ``sourceOnDemand``.

A source URL holds the broker's credentials. MediaMTX logs a source it refuses, password and
all, so a source that is not exactly what is meant is never sent, and a refusal backs off. Neither
a source nor a response from MediaMTX is ever logged here.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit

import httpx

from relayapi.nodes import AddressPolicy, node_id_ok
from relayapi.settings import Settings
from relayapi.store import Store

logger = logging.getLogger(__name__)

NODE_RTSP_PORT = 8554
CLOSE_AFTER = "10s"
_RETRY_S = 5.0
_FIRST_BACKOFF_S = 5.0
_PAGE_SIZE = 500


def source_url(user: str, password: str, address: str, node_id: str) -> str:
    """Raises ValueError, with no value in the message, unless the URL says exactly this."""
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    url = f"rtsp://{credentials}@{address}:{NODE_RTSP_PORT}/{node_id}"
    parts = urlsplit(url)
    if (
        parts.hostname != address
        or parts.path != f"/{node_id}"
        or parts.query
        or parts.fragment
        or unquote(parts.username or "") != user
        or unquote(parts.password or "") != password
    ):
        raise ValueError("this source would not mean what it says")
    return url


def path_config(source: str) -> dict:
    return {
        "source": source,
        "sourceOnDemand": True,
        "sourceOnDemandCloseAfter": CLOSE_AFTER,
        "rtspTransport": "tcp",
    }


class _Refused(Exception):
    def __init__(self, status: int):
        super().__init__(str(status))
        self.status = status


class PathSync:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        policy: AddressPolicy,
        clock: Callable[[], float],
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._store = store
        self._policy = policy
        self._clock = clock
        self._client = httpx.AsyncClient(
            base_url=settings.mediamtx_api_url,
            auth=(settings.mediamtx_api_user, settings.mediamtx_api_password),
            timeout=5.0,
            trust_env=False,
            transport=transport,
        )
        # What the control API last confirmed for each node.
        self._applied: dict[str, str] = {}
        # After a refusal: node -> (seconds to wait, not before).
        self._backoff: dict[str, tuple[float, float]] = {}
        self._failing: set[str] = set()
        self._last_sync_at: float | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    def wake(self) -> None:
        """Called from the MQTT thread when a heartbeat holds an address."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def status(self) -> dict:
        return {"last_sync_at": self._last_sync_at, "failing": len(self._failing)}

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        next_full = 0.0
        while True:
            full = time.monotonic() >= next_full
            try:
                await self.sync_once(full)
            except Exception:
                logger.exception("the path sync failed; it will run again")
            if full:
                next_full = time.monotonic() + self._settings.path_sync_interval_s
            try:
                async with asyncio.timeout(_RETRY_S):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()

    def _desired(self) -> dict[str, str]:
        desired = {}
        for node_id, stored in self._store.node_addresses().items():
            # Again, as a database can be older than today's rule, or the site's subnet changed.
            address = self._policy.check(stored)
            if address is None or not node_id_ok(node_id):
                continue
            try:
                desired[node_id] = source_url(
                    self._settings.broker_user, self._settings.broker_password, address, node_id
                )
            except ValueError:
                logger.warning("path %s not created: its source would be malformed", node_id)
        return desired

    async def sync_once(self, full: bool) -> None:
        """Bring MediaMTX into line. ``full`` also prunes nodes long unseen, reads back paths
        thought to be right, and removes paths nobody reports. A write happens only on a real
        difference, because a write cuts off anyone watching."""
        async with self._lock:
            if full:
                cutoff = self._clock() - self._settings.node_retention_s
                pruned = self._store.prune_nodes(cutoff)
                if pruned:
                    logger.info("removed %d nodes unseen for the retention time", pruned)
            desired = self._desired()
            for node_id in set(self._applied) - set(desired):
                self._applied.pop(node_id, None)
            for node_id in set(self._backoff) - set(desired):
                self._backoff.pop(node_id, None)
            self._failing &= set(desired)
            for node_id, source in desired.items():
                if not full and self._applied.get(node_id) == source:
                    continue
                await self._sync_node(node_id, source)
            if full:
                await self._remove_unreported(desired)
            self._last_sync_at = self._clock()

    async def _sync_node(self, node_id: str, source: str) -> None:
        waiting = self._backoff.get(node_id)
        if waiting is not None and time.monotonic() < waiting[1]:
            return
        try:
            await self._apply(node_id, source)
        except Exception as error:
            # Anything at all: one node's trouble must not end this task and every later path.
            self._applied.pop(node_id, None)
            self._failing.add(node_id)
            logger.warning("path %s not updated: %s", node_id, _describe(error))
            if isinstance(error, _Refused) and error.status < 500:
                wait = (
                    _FIRST_BACKOFF_S
                    if waiting is None
                    else min(waiting[0] * 2, self._settings.path_backoff_max_s)
                )
                self._backoff[node_id] = (wait, time.monotonic() + wait)
        else:
            self._applied[node_id] = source
            self._failing.discard(node_id)
            self._backoff.pop(node_id, None)

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

    async def _remove_unreported(self, desired: dict[str, str]) -> None:
        """mediamtx.yml lists no paths, so any path not wanted here is this API's own leftover."""
        try:
            page = 0
            names: list[str] = []
            while True:
                response = await self._client.get(
                    "/v3/config/paths/list",
                    params={"page": page, "itemsPerPage": _PAGE_SIZE},
                )
                self._check(response)
                body = response.json()
                names += [item["name"] for item in body["items"]]
                page += 1
                if page >= body["pageCount"]:
                    break
            for name in names:
                if name not in desired:
                    self._check(await self._client.delete(f"/v3/config/paths/delete/{name}"))
                    logger.info("path %s removed", name)
        except Exception as error:
            logger.warning("could not check for leftover paths: %s", _describe(error))

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code >= 300:
            raise _Refused(response.status_code)


def _describe(error: Exception) -> str:
    """Type and status only: a message could quote what was sent."""
    if isinstance(error, _Refused):
        return f"the control API answered {error.status}"
    return type(error).__name__
