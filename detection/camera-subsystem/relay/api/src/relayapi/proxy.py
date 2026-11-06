"""Fetches a node's Frigate for a logged-in user.

The target is built from an address learned from a heartbeat and a checked path, never from
anything else in the request. Frigate has its login off and trusts this proxy through
``proxy.auth_secret``. Pinned to Frigate 0.18.2:

- ``frigate/api/auth.py:642-643`` compares the request's ``x-proxy-secret`` header with
  ``proxy.auth_secret`` (``frigate/config/proxy.py:38-42`` names it ``X-Proxy-Secret``).
- ``frigate/api/auth.py:652-664``: with its login off, Frigate gives a request the role in
  ``proxy.header_map.role`` or else ``proxy.default_role``, which is ``viewer``
  (``frigate/config/proxy.py:43-44``). This proxy sends no role at all. The node's rendered
  config maps no role header, and an allowlist keeps client headers such as ``Remote-Role`` out.
"""

import asyncio
import hashlib
import re
import unicodedata
from urllib.parse import unquote_to_bytes

import httpx
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

from relayapi.settings import Settings

FRIGATE_PORT = 8971
PROXY_SECRET_HEADER = "X-Proxy-Secret"
_MAX_DECODE_ROUNDS = 5
_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*:")
_ENCODED_SLASH_RE = re.compile(r"%(2f|5c)", re.IGNORECASE)
_OK_LOCATION_RE = re.compile(r"/(?![/\\])[^\\]*")
# Only these pass from a client to a node. Authorization, cookies, Remote-*, X-Forwarded-*,
# and everything else an auth header could be spelled as, stay here.
_FORWARDED_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "range",
    "x-csrf-token",
}
_DROPPED_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "www-authenticate",
    PROXY_SECRET_HEADER.lower(),
}


def proxy_secret(broker_password: str) -> str:
    """Must stay equal to ``cameralink.frigate_config.proxy_secret``, which a test checks. The
    API's image does not include cameralink, so this is its own copy of the formula."""
    if not broker_password:
        raise ValueError("no proxy secret can be derived from an empty broker password")
    return hashlib.sha256(b"black-relay-frigate-proxy:" + broker_password.encode()).hexdigest()


def make_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
    return httpx.AsyncClient(
        # Frigate's certificate on port 8971 is self-signed and generated on the node, so there
        # is nothing to verify it against. This turns verification off for connections to
        # nodes and for nothing else: no other client in this service uses these settings.
        # Who may connect is decided by the node's firewall, which admits only this host.
        verify=False,
        # The relay's host may set HTTP_PROXY for its own reasons. A node is never reached
        # through one, because the proxy secret would pass through it.
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(
            settings.proxy_read_timeout_s, connect=settings.proxy_connect_timeout_s
        ),
        transport=transport,
    )


def _refuse_path() -> HTTPException:
    return HTTPException(status_code=400, detail="this path is not allowed")


def _check_layer(text: str) -> None:
    for form in (text, unicodedata.normalize("NFKC", text)):
        if "\\" in form or any(ord(c) < 0x20 or ord(c) == 0x7F for c in form):
            raise _refuse_path()
        if _ENCODED_SLASH_RE.search(form):
            raise _refuse_path()
        segments = form.split("/")
        if _SCHEME_RE.match(segments[0]):
            raise _refuse_path()
        for index, segment in enumerate(segments):
            if segment == "" and index != len(segments) - 1:
                raise _refuse_path()
            if segment.split(";", 1)[0] in (".", ".."):
                raise _refuse_path()


def checked_path(raw: bytes) -> bytes:
    """Return ``raw`` unchanged if no decoding of it could leave the node's root, else raise.

    ``raw`` is the request's undecoded path after ``/frigate/``. It is checked after every
    round of percent-decoding, because a server behind the proxy may decode once more, and
    refused when it has not settled after a few rounds, as nothing legitimate is encoded so
    deep. A slash or backslash cannot be sent encoded at all. Invalid UTF-8 is refused, which
    takes care of overlong forms such as ``%c0%ae``, and NFKC takes care of fullwidth dots.
    """
    try:
        layer = raw.decode("ascii")
    except UnicodeDecodeError:
        raise _refuse_path() from None
    for _ in range(_MAX_DECODE_ROUNDS):
        _check_layer(layer)
        try:
            decoded = unquote_to_bytes(layer).decode("utf-8")
        except UnicodeDecodeError:
            raise _refuse_path() from None
        if decoded == layer:
            return raw
        layer = decoded
    raise _refuse_path()


def frigate_target(request: Request) -> bytes:
    """The checked path after ``/nodes/<id>/frigate``, with its query. Taken from the raw
    request, because the router's copy is already decoded."""
    raw = request.scope.get("raw_path") or request.scope["path"].encode("latin-1", "replace")
    parts = raw.split(b"/", 4)
    if len(parts) < 4 or unquote_to_bytes(parts[3]) != b"frigate":
        raise _refuse_path()
    path = checked_path(parts[4] if len(parts) == 5 else b"")
    query = request.scope.get("query_string", b"")
    return b"/" + path + (b"?" + query if query else b"")


class _Scrubber:
    """Replaces the secret in a stream of chunks, including across a chunk boundary, with the
    same number of bytes, so a Content-Length from the node stays true."""

    def __init__(self, secret: bytes):
        self._secret = secret
        self._mask = b"*" * len(secret)
        self._keep = len(secret) - 1
        self._tail = b""

    def feed(self, chunk: bytes) -> bytes:
        buffer = (self._tail + chunk).replace(self._secret, self._mask)
        if len(buffer) <= self._keep:
            self._tail = buffer
            return b""
        self._tail = buffer[-self._keep :]
        return buffer[: -self._keep]

    def finish(self) -> bytes:
        tail, self._tail = self._tail, b""
        return tail


class StreamAborted(Exception):
    """Raised mid-body. The server then closes the connection, so the client sees a short
    response and not a complete-looking one."""


class FrigateProxy:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._client = make_client(settings, transport)
        self._secret = proxy_secret(settings.broker_password)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read_body(self, request: Request) -> bytes:
        limit = self._settings.proxy_max_request_bytes
        too_large = HTTPException(status_code=413, detail="the request body is too large")
        declared = request.headers.get("content-length", "0")
        if len(declared) > 20 or (declared.isdigit() and int(declared) > limit):
            raise too_large
        body = bytearray()
        try:
            async with asyncio.timeout(self._settings.proxy_total_timeout_s):
                async for chunk in request.stream():
                    body += chunk
                    if len(body) > limit:
                        raise too_large
        except TimeoutError:
            raise HTTPException(status_code=408, detail="the request body took too long") from None
        return bytes(body)

    async def forward(
        self,
        *,
        address: str,
        node_id: str,
        target: bytes,
        method: str,
        request: Request,
        body: bytes = b"",
        max_bytes: int | None = None,
    ) -> StreamingResponse:
        limit = self._settings.proxy_max_response_bytes if max_bytes is None else max_bytes
        total = self._settings.proxy_total_timeout_s
        base = f"/nodes/{node_id}/frigate"
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _FORWARDED_REQUEST_HEADERS
        }
        headers.update(
            {
                PROXY_SECRET_HEADER: self._secret,
                # So that the body can be checked for the secret.
                "Accept-Encoding": "identity",
            }
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total
        try:
            url = httpx.URL(scheme="https", host=address, port=FRIGATE_PORT)
            url = url.copy_with(raw_path=target)
            outgoing = self._client.build_request(method, url, headers=headers, content=body)
            async with asyncio.timeout(total):
                upstream = await self._client.send(outgoing, stream=True)
        except (TimeoutError, httpx.TimeoutException):
            raise HTTPException(status_code=504, detail=f"node {node_id} did not answer") from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise HTTPException(
                status_code=502, detail=f"node {node_id} could not be reached"
            ) from None
        try:
            response_headers = self._response_headers(upstream, node_id, base, limit)
        except HTTPException:
            await upstream.aclose()
            raise

        scrubber = _Scrubber(self._secret.encode())

        async def stream():
            sent = 0
            chunks = upstream.aiter_raw()
            try:
                while True:
                    try:
                        async with asyncio.timeout_at(deadline):
                            chunk = await anext(chunks)
                    except StopAsyncIteration:
                        break
                    except (TimeoutError, httpx.HTTPError):
                        raise StreamAborted(f"node {node_id} stalled or broke off") from None
                    sent += len(chunk)
                    if sent > limit:
                        raise StreamAborted(f"node {node_id} sent too much")
                    clean = scrubber.feed(chunk)
                    if clean:
                        yield clean
                tail = scrubber.finish()
                if tail:
                    yield tail
            finally:
                await upstream.aclose()

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1", "replace"))
            for name, value in response_headers
        ]
        return response

    def _response_headers(
        self, upstream: httpx.Response, node_id: str, base: str, limit: int
    ) -> list[tuple[str, str]]:
        if upstream.status_code == 401:
            # Not passed on as a 401, which would read as the client's own token being wrong.
            raise HTTPException(
                status_code=502,
                detail=(
                    f"node {node_id} refused the proxy secret: the broker password here and on "
                    "the node must be the same"
                ),
            )
        encoding = upstream.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise HTTPException(
                status_code=502, detail=f"node {node_id} sent a compressed body against the request"
            )
        declared = upstream.headers.get("content-length", "0")
        if len(declared) > 20 or (declared.isdigit() and int(declared) > limit):
            raise HTTPException(status_code=502, detail=f"node {node_id}'s response is too large")
        kept = []
        for name, value in upstream.headers.multi_items():
            lowered = name.lower()
            if lowered in _DROPPED_RESPONSE_HEADERS or lowered.startswith("remote-"):
                continue
            value = value.replace(self._secret, "*" * len(self._secret))
            if lowered == "location":
                value = self._location(value, node_id, base)
            kept.append((name, value))
        return kept

    @staticmethod
    def _location(value: str, node_id: str, base: str) -> str:
        if _OK_LOCATION_RE.fullmatch(value):
            return base + value
        if _SCHEME_RE.match(value) or value.startswith(("/", "\\")):
            # Off the node, or the node's own address, which users have no route to.
            raise HTTPException(
                status_code=502, detail=f"node {node_id} redirected away from the proxy"
            )
        return value
