"""The central API. Reads go to SQLite, so they keep working with the broker down, and listing
nodes reaches neither a node nor MediaMTX."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from starlette.responses import Response, StreamingResponse

from relayapi.auth import token_checker
from relayapi.mediamtx import PathSync
from relayapi.mqtt import Consumer, Ingest, make_client
from relayapi.nodes import NO_CENTRAL_PROXY, attributes_of, report, timestamp_us
from relayapi.proxy import FrigateProxy, frigate_target
from relayapi.settings import Settings
from relayapi.store import Store

_MAX_SEQ = 2**63 - 1
_PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_STREAM_BATCH = 100


def _event_json(row) -> str:
    # The message is already canonical JSON, so it is embedded, not parsed and encoded again.
    # A deeply nested message cannot then cost a recursion on the way out.
    return (
        f'{{"seq":{row["seq"]},"kind":"{row["kind"]}",'
        f'"received_at":{row["received_at"]!r},"message":{row["message"]}}}'
    )


async def event_stream(
    store: Store, after: int, *, poll_s: float = 0.5, keepalive_s: float = 15.0
) -> AsyncIterator[str]:
    """Server-sent events from ``after`` on. Polled from SQLite, so a slow client holds no queue
    and a client resuming with ``Last-Event-ID`` takes the same path as a live one."""
    seq = after
    last_sent = time.monotonic()
    while True:
        rows = await asyncio.to_thread(store.events_after, seq, _STREAM_BATCH)
        for row in rows:
            seq = row["seq"]
            last_sent = time.monotonic()
            yield f"id: {seq}\nevent: {row['kind']}\ndata: {_event_json(row)}\n\n"
        if rows:
            continue
        if time.monotonic() - last_sent >= keepalive_s:
            last_sent = time.monotonic()
            yield ": keepalive\n\n"
        await asyncio.sleep(poll_s)


def _parse_since(since: str | None) -> tuple[int | None, int | None]:
    """(cursor, timestamp in microseconds). A number is a ``seq``: the safe way to page, since a
    replayed event arrives late and carries an early timestamp."""
    if not since:
        return None, None
    refused = HTTPException(status_code=400, detail="since must be a seq or an RFC 3339 UTC time")
    try:
        if since.isascii() and since.isdigit():
            cursor = int(since)
            if cursor > _MAX_SEQ:
                raise refused
            return cursor, None
        return None, timestamp_us(since)
    except ValueError:
        raise refused from None


def create_app(
    settings: Settings | None = None,
    *,
    mqtt_client_factory: Callable = make_client,
    node_transport=None,
    mediamtx_transport=None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Uvicorn's factory. Raises ``SettingsError`` with no token, so the service cannot run open."""
    logging.basicConfig(
        level=os.environ.get("RELAY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env() if settings is None else settings
    store = Store(
        settings.db_path,
        settings.retain_updates,
        settings.retain_durable,
        settings.retention_check_every,
    )
    path_sync = PathSync(settings, store, mediamtx_transport)
    ingest = Ingest(settings, store, clock, on_address=path_sync.wake)
    consumer = Consumer(settings, ingest, mqtt_client_factory)
    proxy = FrigateProxy(settings, node_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sync_task = asyncio.create_task(path_sync.run())
        consumer.start()
        try:
            yield
        finally:
            consumer.stop()
            sync_task.cancel()
            await asyncio.gather(sync_task, return_exceptions=True)
            await path_sync.aclose()
            await proxy.aclose()
            store.close()

    app = FastAPI(
        title="Black Relay central API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        dependencies=[Depends(token_checker(settings.api_token))],
    )
    app.state.store = store
    app.state.path_sync = path_sync
    app.state.proxy = proxy

    @app.get("/nodes")
    def list_nodes() -> Response:
        now = clock()
        body = {
            "broker": {"connected": consumer.connected, "dropped": ingest.dropped()},
            "nodes": [report(row, now, settings.stale_after_s) for row in store.nodes()],
        }
        # ASCII-only, because a message can hold a lone surrogate, which UTF-8 cannot encode.
        return Response(json.dumps(body, allow_nan=False), media_type="application/json")

    @app.get("/events")
    def list_events(
        since: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> Response:
        cursor, from_time = _parse_since(since)
        if cursor is not None:
            rows = store.events_after(cursor, limit)
        elif from_time is not None:
            rows = store.events_from_time(from_time, limit)
        else:
            rows = store.events_latest(limit)
        body = ",".join(_event_json(row) for row in rows)
        return Response(f'{{"events":[{body}]}}', media_type="application/json")

    @app.get("/events/stream")
    def stream_events(last_event_id: str | None = Header(default=None)) -> Response:
        after = None
        if last_event_id and last_event_id.isascii() and last_event_id.isdigit():
            if len(last_event_id) < 19:
                after = int(last_event_id)
        if after is None:
            after = store.latest_seq()
        return StreamingResponse(
            event_stream(store, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    def reachable_address(node_id: str) -> str:
        """The address from this node's newest heartbeat. Nothing in the request can add to it."""
        row = store.node(node_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no heartbeat from node {node_id}")
        reason = attributes_of(row["message"]).get("reason")
        if isinstance(reason, str) and NO_CENTRAL_PROXY in reason:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"node {node_id} reports {NO_CENTRAL_PROXY}: it has no broker password, so "
                    "it shares no proxy secret and its live view is on loopback only"
                ),
            )
        if row["node_address"] is None:
            raise HTTPException(
                status_code=503, detail=f"node {node_id} has not reported an address"
            )
        return row["node_address"]

    @app.get("/nodes/{node_id}/snapshot/{event_id}.jpg")
    async def snapshot(node_id: str, event_id: str, request: Request) -> Response:
        if not _SNAPSHOT_ID_RE.fullmatch(event_id):
            raise HTTPException(status_code=400, detail="this event id is not allowed")
        address = reachable_address(node_id)
        # A message's event_id, or Frigate's own id, which is what snapshot_url holds.
        track = store.track_of_event(node_id, event_id) or event_id
        if not _SNAPSHOT_ID_RE.fullmatch(track):
            raise HTTPException(status_code=502, detail="this event's track id is not allowed")
        return await proxy.forward(
            address=address,
            node_id=node_id,
            target=f"/api/events/{track}/snapshot.jpg".encode(),
            method="GET",
            request=request,
            max_bytes=settings.proxy_max_snapshot_bytes,
        )

    @app.api_route("/nodes/{node_id}/frigate", methods=_PROXY_METHODS)
    @app.api_route("/nodes/{node_id}/frigate/{path:path}", methods=_PROXY_METHODS)
    async def frigate(node_id: str, request: Request) -> Response:
        target = frigate_target(request)
        address = reachable_address(node_id)
        body = await proxy.read_body(request)
        return await proxy.forward(
            address=address,
            node_id=node_id,
            target=target,
            method=request.method,
            request=request,
            body=body,
        )

    return app
