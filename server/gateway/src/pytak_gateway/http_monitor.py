"""src/pytak_gateway/http_monitor.py

The HTTP monitor polls the Fusion server (fusion_client.py) and does two jobs each cycle:

1. It asks Fusion's /health whether Fusion itself is up, and tells TAK with a `HealthCheck-fusion`
   marker. A Fusion that is down must not look like a quiet site.
2. It reads the event log by cursor and hands each page to `apply`, which turns events into state
   changes (the registry database) and returns the CoT to send. The page's changes and its cursor are
   saved in one transaction: a crash re-reads a page, it never skips one.

Config comes from the environment (FusionConfig). Nothing here is stored in a file with the token.
"""

import asyncio
import logging
import os
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from sqlalchemy.orm import Session

from . import cot
from .db import FusionSourceRow, GapRow
from .fusion_client import (
    CursorAhead,
    CursorExpired,
    Event,
    FusionClient,
    FusionError,
    InvalidRequest,
    Unauthorized,
    Unavailable,
    source_name,
)
from .fusion_mapper import FusionMapper, Policy
from .registry import Registry
from .state import GatewayState
from .tak_connection import TakConnection

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300
EVENTS_STREAM = source_name("events")
HEALTH_STREAM = "health"

# What turns a page of events into state changes, inside the page's transaction. It returns the CoT
# to send once that transaction has committed.
Apply = Callable[[Session, list[Event]], list[bytes]]


def _no_changes(session: Session, events: list[Event]) -> list[bytes]:
    return []


@dataclass(frozen=True)
class FusionConfig:
    url: str
    token: str = ""
    interval: int = 10
    page_limit: int = 500
    cafile: str | None = None

    @classmethod
    def from_env(cls) -> "FusionConfig":
        """FUSION_URL (https: the bearer token rides on it; http only with FUSION_ALLOW_HTTP=true, for a trusted
        network, and the monitor warns at start-up), and the token from FUSION_TOKEN or
        from the file named by FUSION_TOKEN_FILE (not both). Raises ValueError, never with the token."""
        url = os.getenv("FUSION_URL", "").strip()
        scheme = urlparse(url).scheme
        allow_http = os.getenv("FUSION_ALLOW_HTTP", "").lower() in ("1", "true", "yes")
        if scheme not in (("https", "http") if allow_http else ("https",)) or not urlparse(url).hostname:
            raise ValueError("FUSION_URL must be an https:// URL (an http:// one only with FUSION_ALLOW_HTTP=true)")
        token, token_file = os.getenv("FUSION_TOKEN", ""), os.getenv("FUSION_TOKEN_FILE", "")
        if token and token_file:
            raise ValueError("set FUSION_TOKEN or FUSION_TOKEN_FILE, not both")
        if token_file:
            try:
                token = Path(token_file).read_text().strip()
            except OSError as e:
                raise ValueError(f"cannot read FUSION_TOKEN_FILE: {type(e).__name__}") from None
        if not token:
            raise ValueError("FUSION_TOKEN (or FUSION_TOKEN_FILE) is not set")
        try:
            interval = int(os.getenv("FUSION_POLL_INTERVAL", "10"))
            page_limit = int(os.getenv("FUSION_PAGE_LIMIT", "500"))
        except ValueError:
            raise ValueError("FUSION_POLL_INTERVAL and FUSION_PAGE_LIMIT must be whole numbers") from None
        if interval < 1 or not 1 <= page_limit <= 1000:
            raise ValueError("FUSION_POLL_INTERVAL must be at least 1 and FUSION_PAGE_LIMIT 1 to 1000")
        return cls(
            url=url, token=token, interval=interval, page_limit=page_limit, cafile=os.getenv("FUSION_CAFILE") or None
        )


class HTTPMonitor:
    """Polls Fusion and publishes what it finds."""

    def __init__(
        self,
        client: FusionClient,
        registry: Registry,
        tx_queue: asyncio.Queue,
        apply: Apply = _no_changes,
        interval: int = 10,
        page_limit: int = 500,
        mapper: FusionMapper | None = None,
    ):
        """
        Args:
            client: the Fusion client (URL and token)
            registry: the database holding the cursors, and the state `apply` changes
            tx_queue: queue of CoT to send to TAK
            apply: turns a page of events into state changes (see Apply); the mapper's, when there is one
            interval: seconds between cycles, when all is well
            page_limit: events asked for per page
            mapper: also keeps node health in line with /nodes, and marks nodes offline when Fusion is lost
        """
        self.client = client
        self.registry = registry
        self.tx_queue = tx_queue
        self.mapper = mapper
        self.apply = mapper.apply if mapper else apply
        self.interval = interval
        self.unreachable_after = timedelta(seconds=max(3 * interval, 60))
        self.marked_unreachable = False
        self.page_limit = page_limit
        self.halted = False  # the cursor is ahead of Fusion's log: events stop until an operator acts

    # --- Fusion's own health ------------------------------------------------------------------------

    def _to_cot(self, label: str, detail: str | None = None) -> bytes:
        host = urlparse(self.client.base_url).netloc.rsplit("@", 1)[-1]
        remarks = f"Status: {label}, URL: {host}" + (f", Detail: {detail}" if detail else "")
        return cot.health_marker("HealthCheck-fusion", remarks, f"http-monitor:{host}", datetime.now(timezone.utc))

    async def check_health(self) -> bool:
        """Is Fusion up? Sends the marker either way. True if Fusion answered at all, so the caller
        knows whether asking for events is worth a request. A 401 is raised: it will not fix itself."""
        try:
            health = await self.client.health()
        except Unauthorized:
            raise
        except FusionError as e:
            logger.warning("fusion health check failed: %s", e.code)
            self._record(HEALTH_STREAM, error=e.code)
            await self.tx_queue.put(self._to_cot("unreachable", e.code))
            return False
        self._record(HEALTH_STREAM, error=None if health.ok else health.status)
        unregistered = self.registry.count_unregistered()
        await self.tx_queue.put(
            self._to_cot(
                "healthy" if health.ok else health.status,
                f"{unregistered} unregistered node(s)" if unregistered else None,
            )
        )
        return True

    # --- the event log ------------------------------------------------------------------------------

    def cursor(self, stream: str = EVENTS_STREAM) -> int | None:
        with self.registry.begin() as s:
            row = s.get(FusionSourceRow, stream)
            return row.cursor if row else None

    def _record(self, stream: str, error: str | None, session: Session | None = None) -> FusionSourceRow:
        """Note how the last poll of a stream went, in `session` when the caller has a transaction open."""
        if session is None:
            with self.registry.begin() as s:
                return self._record(stream, error, s)
        row = session.get(FusionSourceRow, stream) or FusionSourceRow(name=stream)
        row.last_error_code = error
        if error in (None, "degraded"):  # fusion answered
            row.last_poll_ok_at = datetime.now(timezone.utc)
        session.add(row)
        return row

    def _skip_gap(self, stream: str, expired: CursorExpired) -> None:
        """Retention deleted history we had not read. Resuming accepts the loss (the contract says so
        in so many words), so it is written down, and the cursor moves to where fusion says it can resume."""
        with self.registry.begin() as s:
            row = s.get(FusionSourceRow, stream) or FusionSourceRow(name=stream)
            if row.cursor is not None and expired.resume_after <= row.cursor:
                raise Unavailable("cursor_expired_no_progress")  # fusion offers nothing past where we are
            s.add(
                GapRow(
                    source=stream,
                    from_cursor=row.cursor,
                    resume_after=expired.resume_after,
                    at=datetime.now(timezone.utc),
                )
            )
            row.cursor, row.last_event_id = expired.resume_after, None
            s.add(row)
        logger.error("fusion deleted history we had not read: resuming after cursor %d (a gap)", expired.resume_after)

    async def poll_events(self) -> int:
        """Drains the log: pages until there are no more. Returns how many events were applied."""
        applied = 0
        while True:
            cursor = self.cursor()
            try:
                page = await self.client.events(cursor, self.page_limit)
            except CursorExpired as e:
                self._skip_gap(EVENTS_STREAM, e)
                continue
            with self.registry.begin() as s:  # the page's changes and its cursor, together or not at all
                cots = self.apply(s, page.events)
                row = self._record(EVENTS_STREAM, error=None, session=s)
                row.cursor = page.next_cursor
                if page.events:
                    row.last_event_id = page.events[-1].id
            await self._send(cots)
            applied += len(page.events)
            if not page.has_more:
                return applied

    # --- the loop -----------------------------------------------------------------------------------

    def delay(self, failures: int) -> float:
        """Seconds to wait: the interval, doubling with each failed cycle in a row, up to a cap."""
        return min(self.interval * 2 ** min(failures, 8), MAX_BACKOFF_SECONDS) if failures else self.interval

    async def cycle(self) -> bool:
        """One pass: Fusion's health, then its events, then its nodes. False if Fusion could not be reached
        or could not serve us."""
        if not await self.check_health():
            await self._lose_fusion()
            return False
        self.marked_unreachable = False
        if self.halted:
            return True
        try:
            await self.poll_events()
            await self._reconcile_nodes()
        except CursorAhead:
            # Fusion's database is not the one this cursor came from (replaced or restored). Whether to
            # start over, and from where, is for an operator: guessing could replay or hide history.
            self.halted = True
            self._record(EVENTS_STREAM, error="cursor_ahead")
            logger.error(
                "fusion's log is behind our cursor (database replaced or restored?): not polling "
                "events. Delete the %r row of fusion_sources to start over",
                EVENTS_STREAM,
            )
        except Unavailable as e:
            logger.warning("fusion events: %s", e.code)
            self._record(EVENTS_STREAM, error=e.code)
            return False
        except InvalidRequest as e:
            logger.error("fusion rejected our request (%s): a bug in this client", e.code)
            self._record(EVENTS_STREAM, error=e.code)
            return False
        return True

    async def _send(self, cots: list[bytes]) -> None:
        for event in cots:
            await self.tx_queue.put(event)

    async def _reconcile_nodes(self) -> None:
        """/nodes is the latest status of every node and whether it has gone quiet: the one place silence shows."""
        if self.mapper is None:
            return
        nodes = await self.client.nodes()
        with self.registry.begin() as s:
            cots = self.mapper.reconcile(s, nodes, datetime.now(timezone.utc))
        await self._send(cots)

    async def _lose_fusion(self) -> None:
        """Fusion has not answered. For a while, what it last said still stands; after `unreachable_after`
        it does not, and a site must not read "all quiet" because its source of news went down. Done on every
        cycle Fusion stays out of reach, not once: a site built or updated during the outage is marked too. Each
        node changes once (an offline node is left as it is), and the error is logged once."""
        if self.mapper is None:
            return
        now = datetime.now(timezone.utc)
        with self.registry.begin() as s:
            row = s.get(FusionSourceRow, HEALTH_STREAM)
            if row is None or row.last_poll_ok_at is None or now - row.last_poll_ok_at < self.unreachable_after:
                return  # never reached it, or not out of reach for long
            cots = self.mapper.mark_unreachable(s, now)
        if not self.marked_unreachable:
            logger.error(
                "fusion has been unreachable for over %ds: its nodes are marked offline",
                self.unreachable_after.total_seconds(),
            )
        self.marked_unreachable = True
        await self._send(cots)

    async def run(self):
        """Polls until stopped. Raises Unauthorized (a wrong token will not fix itself); every other
        failure, including a bug in `apply`, is logged and retried with backoff."""
        logger.info("fusion monitor started: %s, every %ds", self.client.base_url, self.interval)
        failures = 0
        while True:
            try:
                failures = 0 if await self.cycle() else failures + 1
            except Unauthorized:
                logger.error("fusion refused the bearer token (401): not retrying")
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not end monitoring
                logger.exception("fusion monitor cycle failed")
                failures += 1
            await asyncio.sleep(self.delay(failures))


async def run_http_monitor(tak_config: dict[str, str]) -> int:
    """Initialize and run the HTTP monitor as a standalone process."""
    try:
        config = FusionConfig.from_env()
    except ValueError as err:
        logger.error(f"Fusion is not configured: {err}")
        return 1

    tak = TakConnection(tak_config)

    if not await tak.start():
        return 1

    if config.url.startswith("http://"):
        logger.warning("FUSION_URL is plain http: the bearer token travels in cleartext. Keep it on a trusted network")
    context = ssl.create_default_context(cafile=config.cafile)
    registry = Registry.load_from_env()
    mapper = FusionMapper(GatewayState(registry), Policy.from_env())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as session:
        monitor = HTTPMonitor(
            FusionClient(config.url, config.token, session),
            registry,
            tak.clitool.tx_queue,
            interval=config.interval,
            page_limit=config.page_limit,
            mapper=mapper,
        )
        try:
            await monitor.run()
        except Unauthorized:
            return 1
        finally:
            registry.close()

    return 0
