"""A client for the Fusion JSON / REST contract, v1 (`/api/v1/events`, `/nodes`, `/health`).

Fusion merges the camera, RF and physical-panel feeds into one ordered log. This module only speaks
its wire contract: auth, paging, and what each failure means. What an event means for a site is
fusion_mapper.py's business.

Contract points that shape this code:
- Every route needs `Authorization: Bearer <token>`. The token is never logged or put in an error.
- Consumers ignore unknown fields and unknown event types (additions are not breaking within v1), so
  models here ignore extras and `type` / `subsystem` are plain strings, never enums.
- A cursor belongs to one fusion database history. Poll by cursor, never by source timestamp.
- 410 `cursor_expired` carries `resume_after`: retrying from it accepts the missing history.
  400 `cursor_ahead` means fusion's database was replaced or restored: only an operator can say how
  to recover, so it is its own error and the poller stops on it.
- A 422 carries only a code (it never echoes the rejected value), so there is nothing to parse.
"""

import logging
import re
from datetime import datetime
from typing import Any

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

MAX_LIMIT = 1000
_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")


class FusionError(Exception):
    """Fusion answered, or could not be reached, in a way the caller has to decide about."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


class Unauthorized(FusionError):
    """401: the token is missing or wrong. Retrying cannot fix it."""


class CursorExpired(FusionError):
    """410: retention deleted history past our cursor. `resume_after` is the first cursor still held."""

    def __init__(self, resume_after: int):
        super().__init__("cursor_expired", 410)
        self.resume_after = resume_after


class CursorAhead(FusionError):
    """400: our cursor is above fusion's high watermark, so its database is not the one we read."""

    def __init__(self):
        super().__init__("cursor_ahead", 400)


class InvalidRequest(FusionError):
    """422: a query parameter was rejected. A bug in this client, not in the data."""


class Unavailable(FusionError):
    """503, another server error, or no answer at all. Try again later."""


class Event(BaseModel):
    """The envelope every fusion event shares (contract, "GET /api/v1/events")."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_id: str = Field(alias="schema")
    cursor: int
    id: str
    source_id: str
    source_schema: str
    timestamp: datetime
    received_at: datetime
    site_id: str
    subsystem: str
    node_id: str | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)


class EventsPage(BaseModel):
    events: list[Event]
    next_cursor: int
    has_more: bool
    rejected: int = 0  # events in this page that did not fit the envelope: skipped, the cursor still moves past them


class NodeStatus(BaseModel):
    """One entry of `/nodes`: the latest status event of a (site, subsystem, node) and its freshness."""

    model_config = ConfigDict(extra="ignore")

    site_id: str
    subsystem: str
    node_id: str
    event_id: str
    timestamp: datetime
    received_at: datetime
    source_age_s: float
    receipt_age_s: float
    clock_ahead: bool = False
    stale: bool = False
    data: dict[str, Any] = Field(default_factory=dict)


class Health(BaseModel):
    """`/health`: `ok` when storage is ready and every broker is connected and subscribed. Connection
    health does not prove the nodes are reporting: that is what nodes' `stale` is for."""

    model_config = ConfigDict(extra="ignore")

    status: str
    storage: dict[str, Any] = Field(default_factory=dict)
    brokers: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def source_name(stream: str, **filters: str | None) -> str:
    """The key a stream's cursor is stored under. A cursor only means something for the filters it
    was read with (changing filters with an advanced cursor can skip earlier matches), so each filter
    combination is its own stream."""
    query = "&".join(f"{k}={v}" for k, v in sorted(filters.items()) if v is not None)
    return f"{stream}?{query}" if query else stream


class FusionClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def events(
        self,
        after: int | None = None,
        limit: int = 100,
        *,
        site_id: str | None = None,
        subsystem: str | None = None,
        type: str | None = None,
        node_id: str | None = None,
    ) -> EventsPage:
        """One page of events after `after` (None: from the retained boundary), in cursor order."""
        params = {
            "after": after,
            "limit": min(limit, MAX_LIMIT),
            "site_id": site_id,
            "subsystem": subsystem,
            "type": type,
            "node_id": node_id,
        }
        body = await self._get("/api/v1/events", params)
        try:
            raw = body["events"]
            next_cursor, has_more = int(body["next_cursor"]), bool(body["has_more"])
        except (KeyError, TypeError, ValueError):
            raise Unavailable("bad_events_page") from None
        events, rejected = [], 0
        for item in raw:
            try:
                events.append(Event.model_validate(item))
            except ValidationError:
                rejected += 1  # one bad event must not hold up the rest of the page
        if rejected:
            log.warning("fusion: skipped %d event(s) that did not fit the v1 envelope", rejected)
        return EventsPage(events=events, next_cursor=next_cursor, has_more=has_more, rejected=rejected)

    async def nodes(self, site_id: str | None = None, subsystem: str | None = None) -> list[NodeStatus]:
        body = await self._get("/api/v1/nodes", {"site_id": site_id, "subsystem": subsystem})
        try:
            return [NodeStatus.model_validate(n) for n in body["nodes"]]
        except (KeyError, TypeError, ValidationError):
            raise Unavailable("bad_nodes_response") from None

    async def health(self) -> Health:
        try:
            return Health.model_validate(await self._get("/api/v1/health", {}))
        except ValidationError:
            raise Unavailable("bad_health_response") from None

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {k: str(v) for k, v in params.items() if v is not None}
        try:
            async with self.session.get(
                self.base_url + path, params=query, headers=self._headers, timeout=self.timeout
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                body = await _error_body(response)
                raise _error_for(response.status, body)
        except (TimeoutError, aiohttp.ClientError, ValueError) as e:
            # ValueError: a body that is not JSON. The class name only: a message could carry the URL.
            raise Unavailable(type(e).__name__) from None


async def _error_body(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await response.json(content_type=None)
        return body["error"] if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
    except (aiohttp.ClientError, ValueError):
        return {}


def _error_for(status: int, error: dict[str, Any]) -> FusionError:
    code = str(error.get("code", "unknown"))
    if status == 401:
        return Unauthorized("unauthorized", 401)
    if status == 410 and code == "cursor_expired" and isinstance(error.get("resume_after"), int):
        return CursorExpired(error["resume_after"])
    if status == 400 and code == "cursor_ahead":
        return CursorAhead()
    if status == 422:
        return InvalidRequest(code, 422)
    # Fusion's own code says why (storage_unavailable); it goes in the log, the database and a CoT remark, so
    # only a short plain identifier is taken from a body we do not control.
    return Unavailable(code if _CODE.fullmatch(code) and code != "unknown" else f"http_{status}", status)
