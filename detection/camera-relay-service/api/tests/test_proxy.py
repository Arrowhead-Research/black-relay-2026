import asyncio
import hashlib
import json
import ssl
from urllib.parse import unquote

import httpx
import pytest
from cameralink.frigate_config import proxy_secret as node_proxy_secret
from conftest import BROKER_PASSWORD, DETECTIONS, STATUS, TOKEN, Chunks, detection, heartbeat

from relayapi import proxy

SECRET = node_proxy_secret(BROKER_PASSWORD)


def asgi_request(relay, raw_path, method="GET"):
    """Send exactly these bytes as the request target, as uvicorn would pass them on: no
    client library gets the chance to tidy the path first."""
    if b"?" in raw_path:
        raw, query = raw_path.split(b"?", 1)
    else:
        raw, query = raw_path, b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw.decode("latin-1")),
        "raw_path": raw,
        "query_string": query,
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"host", b"relay")],
        "client": ("127.0.0.1", 1),
        "server": ("relay", 8080),
    }
    result = {"status": None, "body": b""}
    bodies = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if bodies:
            return bodies.pop()
        await asyncio.Event().wait()  # The client stays connected until the response ends.

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    relay.http.portal.call(relay.app, scope, receive, send)
    return result["status"]


@pytest.fixture
def online(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    return relay


def test_the_secret_uses_the_nodes_exact_formula():
    for password in ("x", BROKER_PASSWORD, "pässwörd", "a" * 500):
        assert proxy.proxy_secret(password) == node_proxy_secret(password)
    assert proxy.proxy_secret("x") == (hashlib.sha256(b"black-relay-frigate-proxy:x").hexdigest())


def test_no_secret_comes_from_an_empty_password():
    with pytest.raises(ValueError):
        proxy.proxy_secret("")


def test_proxy_refuses_an_unknown_node(relay):
    response = relay.http.get(
        "/nodes/ghost/frigate/api/stats?address=192.168.8.9",
        headers={
            "X-Forwarded-Host": "192.168.8.9",
            "Host": "192.168.8.9",
            "X-Node-Address": "192.168.8.9",
        },
    )
    assert response.status_code == 404
    assert relay.node.requests == []


def test_proxy_refuses_a_node_with_no_address(relay):
    relay.mqtt.deliver(STATUS, heartbeat(address=None))
    assert relay.http.get("/nodes/cam-01/frigate/api/stats").status_code == 503
    assert relay.node.requests == []


def test_proxy_uses_only_the_heartbeats_address(online):
    response = online.http.get(
        "/nodes/cam-01/frigate/api/stats?host=192.168.8.9",
        headers={"X-Forwarded-Host": "192.168.8.9", "Host": "192.168.8.9"},
    )
    assert response.status_code == 200
    url = online.node.requests[0].url
    assert (url.scheme, url.host, url.port) == ("https", "192.168.8.21", 8971)
    assert url.raw_path == b"/api/stats?host=192.168.8.9"


ESCAPES = [
    b"..",
    b"../",
    b"../../etc/passwd",
    b"api/../../etc/passwd",
    b"api/..",
    b"./api",
    b"api/./x",
    b"%2e%2e/secret",
    b"%2E%2E/secret",
    b".%2e/secret",
    b"%2e./secret",
    b"api/%2e%2e/%2e%2e/secret",
    b"%252e%252e/secret",
    b"%25252e%25252e/secret",
    b"%2525252e%2525252e%2525252fsecret",
    b"..%2fsecret",
    b"..%2Fsecret",
    b"..%252fsecret",
    b"..%5csecret",
    b"..%5Csecret",
    b"..\\secret",
    b"api\\..\\secret",
    b"%c0%ae%c0%ae/secret",
    b"%c0%2e%c0%2e/secret",
    b"%e0%80%ae%e0%80%ae/secret",
    b"%ef%bc%8e%ef%bc%8e/secret",
    b"..;/secret",
    b"api/..;x/secret",
    b"%2e%2e;/secret",
    b"%00/secret",
    b"api%00.jpg",
    b"api%0d%0aHost:%20evil",
    b"/etc/passwd",
    b"//evil.example.com/x",
    b"%2f%2fevil.example.com/x",
    b"%2Fetc/passwd",
    b"api//x",
    b"http://evil.example.com/x",
    b"https:/evil.example.com",
    b"http%3A%2F%2Fevil.example.com/x",
    b"http%3a//evil.example.com",
    b"hTTp://evil.example.com",
    b"\xff/secret",
]


@pytest.mark.parametrize("path", ESCAPES, ids=lambda p: p.decode("latin-1"))
def test_proxy_path_cannot_escape_the_node(online, path):
    # A decoded newline never matches the route at all, hence the 404.
    status = asgi_request(online, b"/nodes/cam-01/frigate/" + path)
    assert status in (400, 404), path
    assert online.node.requests == []


@pytest.mark.parametrize(
    "path, expected",
    [
        (b"", b"/"),
        (b"api/stats", b"/api/stats"),
        (
            b"api/events/1730749200.12-abc/snapshot.jpg",
            b"/api/events/1730749200.12-abc/snapshot.jpg",
        ),
        (b"assets/index-AbC.js", b"/assets/index-AbC.js"),
        (b"api/", b"/api/"),
        (b"a%20b", b"/a%20b"),
        (b"..foo/bar..", b"/..foo/bar.."),
        (b"api/events?limit=5&cameras=all", b"/api/events?limit=5&cameras=all"),
        (b"@evil.example.com/x", b"/@evil.example.com/x"),
    ],
)
def test_ordinary_paths_pass_unchanged_and_stay_on_the_node(online, path, expected):
    assert asgi_request(online, b"/nodes/cam-01/frigate/" + path) == 200
    url = online.node.requests[-1].url
    assert url.host == "192.168.8.21"
    assert url.raw_path == expected


def test_the_node_id_in_the_path_is_not_a_way_out(online):
    assert asgi_request(online, b"/nodes/cam-01%2f..%2fx/frigate/api") in (400, 404)
    assert asgi_request(online, b"/nodes/../frigate/api") in (400, 404)
    assert online.node.requests == []


def test_an_encoded_slash_after_frigate_is_refused(online):
    # The router sees .../frigate/../x. The raw path is what is checked.
    assert asgi_request(online, b"/nodes/cam-01/frigate%2f../x") == 400
    assert asgi_request(online, b"/nodes/cam-01/frigate%2F..%2Fx") in (400, 404)
    assert online.node.requests == []


def test_an_encoded_but_correct_prefix_still_works(online):
    assert asgi_request(online, b"/nodes/cam-01/fri%67ate/api/x") == 200
    assert online.node.requests[0].url.raw_path == b"/api/x"


def test_an_address_httpx_cannot_build_is_a_clean_failure(online, monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.InvalidURL("no")

    monkeypatch.setattr("relayapi.proxy.httpx.URL", boom)
    assert online.http.get("/nodes/cam-01/frigate/x").status_code == 502


def test_the_bare_frigate_path_is_the_ui_root(online):
    assert online.http.get("/nodes/cam-01/frigate").status_code == 200
    assert online.node.requests[0].url.raw_path == b"/"


def test_proxy_sends_the_secret_and_strips_it_from_responses(online):
    def respond(request):
        assert request.headers["x-proxy-secret"] == SECRET
        return httpx.Response(
            200,
            headers={
                "X-Proxy-Secret": SECRET,
                "X-Echo": f"v={SECRET}",
                "Content-Type": "text/plain",
            },
            content=f'{{"proxy": {{"auth_secret": "{SECRET}"}}}}'.encode(),
        )

    online.node.respond = respond
    response = online.http.get("/nodes/cam-01/frigate/api/config")
    assert response.status_code == 200
    assert "x-proxy-secret" not in response.headers
    assert SECRET not in response.headers.get("x-echo", "")
    assert SECRET not in response.text
    assert SECRET not in json.dumps(dict(response.headers))
    # Same length, so a Content-Length from the node stays true.
    assert len(response.content) == len(f'{{"proxy": {{"auth_secret": "{SECRET}"}}}}')


@pytest.mark.parametrize("split", [1, 5, 32, 63, 64, 65])
def test_a_secret_split_across_chunks_is_still_stripped(online, split):
    body = b"before " + SECRET.encode() + b" after"
    cut = len(b"before ") + split
    online.node.respond = lambda request: httpx.Response(
        200, stream=Chunks([body[:cut], body[cut:]])
    )
    response = online.http.get("/nodes/cam-01/frigate/x")
    assert SECRET not in response.text
    assert response.content.startswith(b"before ") and response.content.endswith(b" after")
    assert len(response.content) == len(body)


def test_the_node_is_asked_for_an_uncompressed_body_so_the_strip_can_work(online):
    online.http.get("/nodes/cam-01/frigate/x", headers={"Accept-Encoding": "gzip, br"})
    assert online.node.requests[0].headers["accept-encoding"] == "identity"


def test_a_compressed_body_that_cannot_be_checked_is_refused(online):
    online.node.respond = lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, content=b"\x1f\x8b"
    )
    assert online.http.get("/nodes/cam-01/frigate/x").status_code == 502


def test_client_credentials_never_reach_the_node(online):
    online.http.get(
        "/nodes/cam-01/frigate/api/stats",
        headers={
            "Cookie": "frigate_token=abc; relay=def",
            "X-Proxy-Secret": "client-supplied",
            "Remote-User": "admin",
            "Remote-Role": "admin",
            "Remote-Groups": "admins",
            "X-Forwarded-User": "admin",
            "X-Forwarded-Groups": "admin",
            "X-Forwarded-For": "1.2.3.4",
            "X-Auth-Request-User": "admin",
            "X-authentik-username": "admin",
            "X-Server-Port": "5000",
            "Proxy-Authorization": "Basic abc",
        },
    )
    sent = online.node.requests[0].headers
    assert "authorization" not in sent
    assert TOKEN not in json.dumps(dict(sent))
    assert sent["x-proxy-secret"] == SECRET
    for name in (
        "cookie",
        "remote-user",
        "remote-role",
        "remote-groups",
        "x-forwarded-user",
        "x-forwarded-groups",
        "x-forwarded-for",
        "x-auth-request-user",
        "x-authentik-username",
        "x-server-port",
        "proxy-authorization",
    ):
        assert name not in sent, name


def test_no_admin_role_is_ever_sent(online):
    online.http.get("/nodes/cam-01/frigate/x")
    sent = {k.lower(): v.lower() for k, v in online.node.requests[0].headers.items()}
    assert "admin" not in json.dumps(sent)
    assert "remote-role" not in sent


def test_harmless_request_headers_pass(online):
    online.http.get(
        "/nodes/cam-01/frigate/clip.mp4",
        headers={"Range": "bytes=0-99", "If-None-Match": '"abc"', "X-CSRF-TOKEN": "1"},
    )
    sent = online.node.requests[0].headers
    assert sent["range"] == "bytes=0-99"
    assert sent["if-none-match"] == '"abc"'
    assert sent["x-csrf-token"] == "1"


def test_a_post_body_is_forwarded(online):
    online.http.post("/nodes/cam-01/frigate/api/x", content=b"hello")
    request = online.node.requests[0]
    assert request.method == "POST"
    assert request.content == b"hello"


def test_a_large_request_body_is_refused(make_relay):
    relay = make_relay(proxy_max_request_bytes=10)
    relay.mqtt.deliver(STATUS, heartbeat())
    assert relay.http.post("/nodes/cam-01/frigate/x", content=b"x" * 11).status_code == 413

    def chunked():
        yield b"x" * 6
        yield b"x" * 6

    assert relay.http.post("/nodes/cam-01/frigate/x", content=chunked()).status_code == 413
    assert relay.node.requests == []


def test_a_node_reporting_no_central_proxy_is_a_clear_503(relay):
    relay.mqtt.deliver(STATUS, heartbeat(attributes={"reason": "central_proxy_unavailable"}))
    response = relay.http.get("/nodes/cam-01/frigate/api/stats")
    assert response.status_code == 503
    assert "central_proxy_unavailable" in response.json()["detail"]
    assert relay.node.requests == []


def test_the_reason_is_read_from_the_newest_heartbeat_only(relay):
    relay.mqtt.deliver(
        STATUS,
        heartbeat(
            timestamp="2026-11-05T12:00:00Z", attributes={"reason": "central_proxy_unavailable"}
        ),
    )
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z"))
    assert relay.http.get("/nodes/cam-01/frigate/x").status_code == 200


def test_the_node_refusing_the_secret_is_not_shown_as_the_clients_login_failing(online):
    online.node.respond = lambda request: httpx.Response(401)
    response = online.http.get("/nodes/cam-01/frigate/api/stats")
    assert response.status_code == 502
    assert "secret" in response.json()["detail"]


def test_frigates_own_role_refusal_passes_through(online):
    online.node.respond = lambda request: httpx.Response(403, content=b"viewer may not")
    assert online.http.post("/nodes/cam-01/frigate/api/x").status_code == 403


def test_a_redirect_is_not_followed_and_stays_under_the_proxy(online):
    online.node.respond = lambda request: httpx.Response(302, headers={"Location": "/login"})
    response = online.http.get("/nodes/cam-01/frigate/x", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/nodes/cam-01/frigate/login"
    assert len(online.node.requests) == 1


@pytest.mark.parametrize(
    "location",
    ["http://evil.example.com/", "//evil.example.com/", "https://192.168.8.21:8971/x"],
)
def test_a_redirect_off_the_proxy_is_dropped(online, location):
    online.node.respond = lambda request: httpx.Response(302, headers={"Location": location})
    response = online.http.get("/nodes/cam-01/frigate/x", follow_redirects=False)
    assert response.status_code == 502
    assert "location" not in response.headers


def test_cookies_from_the_node_do_not_pass(online):
    online.node.respond = lambda request: httpx.Response(
        200, headers={"Set-Cookie": "frigate_token=abc", "Content-Type": "text/plain"}
    )
    response = online.http.get("/nodes/cam-01/frigate/x")
    assert "set-cookie" not in response.headers
    assert response.headers["content-type"] == "text/plain"


def test_a_node_that_cannot_be_reached_is_a_502(online):
    def respond(request):
        raise httpx.ConnectError("no route", request=request)

    online.node.respond = respond
    assert online.http.get("/nodes/cam-01/frigate/x").status_code == 502


def test_a_node_that_never_answers_is_a_504(make_relay):
    relay = make_relay(proxy_total_timeout_s=0.1)
    relay.mqtt.deliver(STATUS, heartbeat())

    async def respond(request):
        await asyncio.sleep(30)

    relay.node.respond = respond
    assert relay.http.get("/nodes/cam-01/frigate/x").status_code == 504


def test_a_body_that_drips_is_cut_at_the_deadline(make_relay):
    relay = make_relay(proxy_total_timeout_s=0.2)
    relay.mqtt.deliver(STATUS, heartbeat())
    stream = Chunks([b"x" * 80] * 1000, delay=0.05)
    relay.node.respond = lambda request: httpx.Response(200, stream=stream)
    with pytest.raises(proxy.StreamAborted):
        relay.http.get("/nodes/cam-01/frigate/x")
    assert stream.closed


def test_a_declared_oversize_response_is_refused(make_relay):
    relay = make_relay(proxy_max_response_bytes=100)
    relay.mqtt.deliver(STATUS, heartbeat())
    stream = Chunks([b"x" * 200])
    relay.node.respond = lambda request: httpx.Response(
        200, headers={"Content-Length": "200"}, stream=stream
    )
    assert relay.http.get("/nodes/cam-01/frigate/x").status_code == 502
    assert stream.closed


def test_an_undeclared_oversize_response_is_cut(make_relay):
    relay = make_relay(proxy_max_response_bytes=500)
    relay.mqtt.deliver(STATUS, heartbeat())
    stream = Chunks([b"x" * 200] * 100)
    relay.node.respond = lambda request: httpx.Response(200, stream=stream)
    with pytest.raises(proxy.StreamAborted):
        relay.http.get("/nodes/cam-01/frigate/x")
    assert stream.closed


def test_only_the_node_client_skips_certificate_checks(online):
    # Frigate's certificate is self-signed, so this one client cannot verify it.
    real = proxy.make_client(online.settings())
    other = online.app.state.path_sync._client
    try:
        assert real._transport._pool._ssl_context.verify_mode == ssl.CERT_NONE
        assert real.trust_env is False
        assert real.follow_redirects is False
        assert other.trust_env is False
    finally:
        asyncio.run(real.aclose())


def test_snapshot_fetches_from_the_node_with_the_secret(online):
    online.node.respond = lambda request: httpx.Response(
        200, headers={"Content-Type": "image/jpeg"}, content=b"\xff\xd8jpeg"
    )
    response = online.http.get("/nodes/cam-01/snapshot/1730749200.12-abc.jpg")
    assert response.status_code == 200
    assert response.content == b"\xff\xd8jpeg"
    request = online.node.requests[0]
    assert request.url.raw_path == b"/api/events/1730749200.12-abc/snapshot.jpg"
    assert request.headers["x-proxy-secret"] == SECRET
    assert "authorization" not in request.headers


def test_snapshot_by_message_event_id_uses_that_events_track(online):
    message = detection(object_id="1730749200.99-xyz")
    online.mqtt.deliver(DETECTIONS, message)
    online.http.get(f"/nodes/cam-01/snapshot/{message['event_id']}.jpg")
    assert online.node.requests[0].url.raw_path == b"/api/events/1730749200.99-xyz/snapshot.jpg"


def test_snapshot_never_builds_a_path_from_another_nodes_event(online):
    message = detection(node_id="cam-02", camera_id="cam-02", object_id="other-node-track")
    online.mqtt.deliver(DETECTIONS, message)
    online.http.get(f"/nodes/cam-01/snapshot/{message['event_id']}.jpg")
    assert b"other-node-track" not in online.node.requests[0].url.raw_path


@pytest.mark.parametrize(
    "event_id",
    [b"..", b"%2e%2e", b"..%2f..%2fx", b"a%2fb", b"a%00b", b"a%5cb", b"a%20b", b"%252e%252e"],
)
def test_snapshot_refuses_an_id_that_is_not_an_id(online, event_id):
    status = asgi_request(online, b"/nodes/cam-01/snapshot/" + event_id + b".jpg")
    assert status in (400, 404)
    assert online.node.requests == []


def test_snapshot_keeps_a_bad_track_id_from_a_message_out_of_the_url(online):
    message = detection(object_id="../../evil")
    online.mqtt.deliver(DETECTIONS, message)
    response = online.http.get(f"/nodes/cam-01/snapshot/{message['event_id']}.jpg")
    assert response.status_code == 502
    assert online.node.requests == []


def test_snapshot_refuses_an_unknown_node(relay):
    assert relay.http.get("/nodes/ghost/snapshot/abc.jpg").status_code == 404
    assert relay.node.requests == []


def test_snapshot_of_a_node_with_no_central_proxy_is_a_503(relay):
    relay.mqtt.deliver(STATUS, heartbeat(attributes={"reason": "central_proxy_unavailable"}))
    assert relay.http.get("/nodes/cam-01/snapshot/abc.jpg").status_code == 503
