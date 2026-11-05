"""The captive portal: the setup form, the submit handler, and a stdlib WSGI app.

``render_form``, ``handle_submit`` and ``handle_wifi_submit`` hold the logic and touch
nothing; ``make_app`` adds HTTP around them, with the scanner, the ``apply`` side effect,
and the AP-window hook injected. Task 6 owns what those three do.

``PortalMode`` follows the provisioning state. ``AP_UP`` serves ``FULL``. ``FALLBACK_AP``
serves ``WIFI_ONLY``, because changing a broker or an identity takes the physical button.

Two rules run through this module. Every value reaching a page is escaped, because
SSIDs are chosen by whoever is broadcasting nearby. And no secret is ever echoed or
logged: the template has no placeholder for one, and exceptions from ``apply`` are
reduced to their type name, since their messages can quote what they were given.
"""

from __future__ import annotations

import enum
import html
import ipaddress
import logging
import re
import socket
import sys
import threading
from dataclasses import dataclass, field, replace
from importlib import resources
from socketserver import ThreadingMixIn
from string import Template
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

from cameraprovision.config import (
    BrokerConfig,
    FrigateConfig,
    NodeConfig,
    effective_ntp_server,
    validate,
    validate_wifi,
)

__all__ = [
    "PortalMode",
    "SubmitResult",
    "WifiChange",
    "handle_submit",
    "handle_wifi_submit",
    "identity_values",
    "make_app",
    "make_server",
    "render_form",
    "saved_values",
]

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024
# Well above the form's own field count, and stops a 64 KiB body parsing into thousands.
_MAX_FORM_FIELDS = 64
# Bounded because int() raises on very long digit strings, and accepts "1_883" and "١٨٨٣".
_PORT_RE = re.compile(r"[0-9]{1,5}")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,18}")

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_REDIRECT_BODY = "Redirecting."


class PortalMode(enum.Enum):
    """What the portal offers: everything, or only a WiFi repair."""

    FULL = "full"
    WIFI_ONLY = "wifi_only"


_FULL_ONLY_REDISPLAYED_FIELDS = (
    "hostname",
    "node_id",
    "broker_host",
    "broker_port",
    "broker_username",
    "mediamtx_address",
    "ntp_server",
    "frigate_base_url",
    "covered_zones",
)

_APPLY_FAILED_ERROR = "The settings could not be saved. Check them and try again."
_REENTER_NOTE = "Passwords are never shown back, so enter them again."

_WIFI_ONLY_INTRO = (
    "<p>Only the WiFi settings can be changed here. "
    "To change anything else, use the setup button.</p>"
)


def identity_values(serial: str) -> dict[str, str]:
    """What a blank node's form starts with: an identity unique to this kit, and editable.

    The node_id is also the MQTT client ID, so two kits sharing one disconnect each other.
    """
    name = f"cam-{serial[-6:].lower()}"
    return {
        "node_id": name,
        "hostname": name,
        "frigate_base_url": f"https://{name}.local:8971",
        "broker_port": str(BrokerConfig.port),
    }


def saved_values(cfg: NodeConfig) -> dict[str, str]:
    """What a re-provisioning form starts with: the saved config, leaving out every secret."""
    broker = cfg.broker
    values = {
        "wifi_ssid": cfg.wifi_ssid,
        "hostname": cfg.hostname,
        "node_id": cfg.node_id,
        "broker_host": broker.host,
        "broker_port": str(broker.port),
        "broker_username": broker.username,
        "mediamtx_address": cfg.mediamtx_address,
        "ntp_server": effective_ntp_server(cfg),
        "frigate_base_url": cfg.frigate.base_url,
        "covered_zones": ", ".join(cfg.covered_zones),
    }
    boxes = {
        "broker_tls": broker.tls,
        "broker_anonymous": cfg.broker_anonymous,
        "camera_upside_down": cfg.camera_upside_down,
    }
    return values | {name: "on" for name, ticked in boxes.items() if ticked}


def _load_template(name: str) -> Template:
    return Template(
        resources.files(__package__).joinpath("templates", name).read_text(encoding="utf-8")
    )


_FORM_TEMPLATE = _load_template("index.html")
_FULL_FIELDS_TEMPLATE = _load_template("full_fields.html")

_SAVED_PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings saved</title>
<style>body { margin: 0 auto; max-width: 30rem; padding: 1rem; font: 16px/1.4 system-ui, sans-serif; }</style>
</head>
<body>
<h1>Settings saved</h1>
<p>The node is now joining the site WiFi, and this setup network will close.</p>
<p>If the setup network comes back after a few minutes, the node could not join.
Reconnect to it and check the WiFi name and password.</p>
$time_server
</body>
</html>
""")

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _saved_page(ntp_server: str) -> str:
    """The page for a saved form. A WiFi repair leaves the time server alone, so has none."""
    line = f"<p>Time server: {_escape(ntp_server)}</p>" if ntp_server else ""
    return _SAVED_PAGE.substitute(time_server=line)


def _errors_html(errors: Sequence[str]) -> str:
    if not errors:
        return ""
    items = "".join(f"<li>{_escape(error)}</li>" for error in errors)
    return f'<div class="errors"><ul>{items}</ul><p>{_escape(_REENTER_NOTE)}</p></div>'


def _full_fields_html(values: Mapping[str, str] | None) -> str:
    """The fields only ``PortalMode.FULL`` offers, escaped and ready to insert."""
    shown = {"broker_port": str(BrokerConfig.port)} if values is None else values
    # Absent from a first render's values too, so every box starts unticked.
    boxes = values or {}
    return _FULL_FIELDS_TEMPLATE.substitute(
        {name: _escape(shown.get(name, "")) for name in _FULL_ONLY_REDISPLAYED_FIELDS},
        upside_down_checked="checked" if "camera_upside_down" in boxes else "",
        tls_checked="checked" if "broker_tls" in boxes else "",
        anonymous_checked="checked" if "broker_anonymous" in boxes else "",
    )


def render_form(
    ssids: Iterable[str],
    *,
    values: Mapping[str, str] | None = None,
    errors: Sequence[str] = (),
    mode: PortalMode = PortalMode.FULL,
) -> str:
    """Return the setup page, listing ``ssids`` and showing any ``errors``.

    ``values`` is a previous submission to redisplay, or a prefill from
    :func:`identity_values` or :func:`saved_values`. Only the non-secret fields the ``mode``
    offers are read from it, so passing a whole submitted form is safe.
    """
    wifi_only = mode is PortalMode.WIFI_ONLY
    # Each substitution is a single pass, so a value holding "$errors" stays literal text.
    return _FORM_TEMPLATE.substitute(
        wifi_ssid=_escape((values or {}).get("wifi_ssid", "")),
        errors=_errors_html(errors),
        intro=_WIFI_ONLY_INTRO if wifi_only else "",
        full_fields="" if wifi_only else _full_fields_html(values),
        # A scan reports one row per access point, so a site network repeats.
        ssid_options="".join(f'<option value="{_escape(s)}">' for s in dict.fromkeys(ssids)),
    )


@dataclass(frozen=True)
class SubmitResult:
    """The outcome of a submission. ``errors`` is safe to show and to log."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    # The time server a full save will use, for the page to confirm.
    ntp_server: str = field(default="", compare=False)


@dataclass(frozen=True, kw_only=True)
class WifiChange:
    """What ``WIFI_ONLY`` hands to ``apply``.

    Task 6 applies this by re-rendering the WiFi keyfile from these two fields and the
    saved hostname; it does not rebuild or revalidate a NodeConfig.
    """

    wifi_ssid: str
    wifi_psk: str = field(repr=False)


def _parse_port(text: str) -> int | str:
    """Return the port as an int, or unchanged for ``validate`` to reject by name."""
    return int(text) if _PORT_RE.fullmatch(text) else text


def _ntp_server(form: Mapping[str, str], previous_broker: str | None) -> str:
    """The time server a form means. Blank, or still the old broker, follows the broker.

    The form has no script to update the field when the broker changes, so a value left
    at the old broker must not strand the node on a server that has moved.
    """
    typed = form.get("ntp_server", "").strip()
    broker = form.get("broker_host", "").strip()
    return broker if not typed or typed == previous_broker else typed


def _build_config(form: Mapping[str, str], previous_broker: str | None = None) -> NodeConfig:
    def raw(name: str) -> str:
        return form.get(name, "")

    # Phone keyboards append spaces, and validate would reject the form over them.
    # Identity fields stay raw so "cam\n" is rejected, and passwords and SSIDs stay raw
    # because a space is legal in them.
    def trimmed(name: str) -> str:
        return raw(name).strip()

    return NodeConfig(
        node_id=raw("node_id"),
        hostname=raw("hostname"),
        wifi_ssid=raw("wifi_ssid"),
        wifi_psk=raw("wifi_psk"),
        camera_upside_down="camera_upside_down" in form,
        mediamtx_address=trimmed("mediamtx_address"),
        ntp_server=_ntp_server(form, previous_broker),
        covered_zones=[z.strip() for z in raw("covered_zones").split(",") if z.strip()],
        broker=BrokerConfig(
            host=trimmed("broker_host"),
            port=_parse_port(raw("broker_port")),
            username=trimmed("broker_username"),
            password=raw("broker_password"),
            tls="broker_tls" in form,
        ),
        broker_anonymous="broker_anonymous" in form,
        frigate=FrigateConfig(base_url=trimmed("frigate_base_url")),
    )


def _apply_safely(apply: Callable[[Any], None], change: NodeConfig | WifiChange) -> SubmitResult:
    try:
        apply(change)
    except Exception as exc:
        # No exc_info: the traceback would print the message, which may quote a secret.
        logger.error("applying the submitted config failed: %s", type(exc).__name__)
        return SubmitResult(ok=False, errors=[_APPLY_FAILED_ERROR])
    return SubmitResult(ok=True)


def handle_submit(
    form: Mapping[str, str],
    *,
    apply: Callable[[NodeConfig], None],
    previous_broker: str | None = None,
) -> SubmitResult:
    """Validate a submitted form, and call ``apply`` once only if it is valid.

    ``previous_broker`` is the saved broker host the form was filled with, if any.
    """
    cfg = _build_config(form, previous_broker)
    errors = validate(cfg)
    if errors:
        return SubmitResult(ok=False, errors=errors)
    result = _apply_safely(apply, cfg)
    return replace(result, ntp_server=effective_ntp_server(cfg)) if result.ok else result


def handle_wifi_submit(
    form: Mapping[str, str], *, apply: Callable[[WifiChange], None]
) -> SubmitResult:
    """Like :func:`handle_submit`, reading only the WiFi fields and ignoring all others."""
    change = WifiChange(wifi_ssid=form.get("wifi_ssid", ""), wifi_psk=form.get("wifi_psk", ""))
    errors = validate_wifi(change.wifi_ssid, change.wifi_psk)
    if errors:
        return SubmitResult(ok=False, errors=errors)
    return _apply_safely(apply, change)


# --- HTTP -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Response:
    status: str
    body: str
    content_type: str = "text/plain; charset=utf-8"
    extra_headers: tuple[tuple[str, str], ...] = ()


def _html_response(status: str, page: str) -> _Response:
    return _Response(
        status,
        page,
        "text/html; charset=utf-8",
        (("Content-Security-Policy", _CSP),),
    )


class _Refused(Exception):
    """A POST turned away before it counts as a submission."""

    def __init__(self, response: _Response) -> None:
        super().__init__(response.status)
        self.response = response


def _netloc(url: str) -> str | None:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return None


def _same_origin(environ: WSGIEnvironment, host: str) -> bool:
    """False if a present ``Origin`` or ``Referer`` is not on ``host``."""
    return all(
        _netloc(environ[header]) == host
        for header in ("HTTP_ORIGIN", "HTTP_REFERER")
        if header in environ
    )


def _is_portal_host(environ: WSGIEnvironment, portal_host: str) -> bool:
    return environ.get("HTTP_HOST", "").lower() == portal_host.lower()


def _media_types(header: str) -> list[str]:
    """The media types in a ``Content-Type`` or ``Accept`` value, without parameters."""
    return [part.split(";")[0].strip().lower() for part in header.split(",")]


def _read_form(environ: WSGIEnvironment, portal_host: str) -> dict[str, str]:
    """Return the POSTed form, or raise ``_Refused``. Reads nothing it will refuse."""
    # Origin alone cannot stop DNS rebinding: a rebound page's Origin matches its Host.
    if not _is_portal_host(environ, portal_host) or not _same_origin(environ, portal_host.lower()):
        raise _Refused(_Response("403 Forbidden", "This form must be sent from its own page."))

    if _media_types(environ.get("CONTENT_TYPE", "")) != [_FORM_CONTENT_TYPE]:
        raise _Refused(_Response("415 Unsupported Media Type", "This form type is not accepted."))

    # Reading without a valid length would park this thread until the socket times out.
    declared = environ.get("CONTENT_LENGTH", "")
    if not _CONTENT_LENGTH_RE.fullmatch(declared):
        raise _Refused(_Response("400 Bad Request", "The request has no usable length."))
    length = int(declared)
    if length > _MAX_BODY_BYTES:
        raise _Refused(_Response("413 Content Too Large", "The form is too large."))

    body = environ["wsgi.input"].read(length)
    # A dropped connection must not submit whichever fields happened to arrive.
    if len(body) != length:
        raise _Refused(_Response("400 Bad Request", "The form did not arrive complete."))

    try:
        pairs = parse_qsl(
            body.decode("utf-8"),
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_FORM_FIELDS,
        )
    except ValueError:
        # Not chained or logged: decode errors quote the bytes, which may be a password.
        raise _Refused(_Response("400 Bad Request", "The form could not be read.")) from None

    form: dict[str, str] = {}
    for name, value in pairs:
        form.setdefault(name, value)
    return form


def _scan(scan_ssids: Callable[[], list[str]]) -> list[str]:
    """Return scanned SSIDs. A failure gives none: the operator can still type the name."""
    try:
        return scan_ssids()
    except Exception as exc:
        logger.warning("listing WiFi networks failed: %s", type(exc).__name__)
        return []


def _guarded_hook(hook: Callable[[], None] | None) -> Callable[[], None]:
    """Serialize ``hook`` across request threads, and stop its failure reaching a request.

    The AP window is housekeeping: losing one restart must not cost a provisioning.
    """
    lock = threading.Lock()

    def notify() -> None:
        if hook is None:
            return
        with lock:
            try:
                hook()
            except Exception as exc:
                logger.warning("the AP-window hook failed: %s", type(exc).__name__)

    return notify


def make_app(
    *,
    scan_ssids: Callable[[], list[str]],
    apply: Callable[[Any], None],
    portal_host: str,
    mode: PortalMode,
    on_form_request: Callable[[], None] | None = None,
    prefill: Mapping[str, str] | None = None,
) -> WSGIApplication:
    """Return the portal's WSGI app.

    ``mode`` is fixed for the app's life, and decides what ``apply`` receives: a whole
    ``NodeConfig`` in ``FULL``, a ``WifiChange`` in ``WIFI_ONLY``. ``apply`` runs under a
    lock, one submission at a time.

    ``portal_host`` is the ``Host`` the portal is served as, such as the AP's IP. A GET
    under any other name is sent there, and a POST under any other name is refused.

    ``on_form_request`` is the AP-window hook. It runs for form GETs and for POSTs that
    reach validation, before ``apply``, and only when the request accepts ``text/html``.
    It never runs for a redirect, a HEAD, or a refused POST, so a phone's connectivity
    checks cannot hold the AP open. ``scan_ssids`` may be called from several threads
    at once.

    ``prefill`` is what a GET's form starts with. A rejected POST shows what was typed.
    """
    notify = _guarded_hook(on_form_request)
    submit_lock = threading.Lock()
    previous_broker = (prefill or {}).get("broker_host")

    def submit(form: Mapping[str, str]) -> SubmitResult:
        if mode is PortalMode.WIFI_ONLY:
            return handle_wifi_submit(form, apply=apply)
        return handle_submit(form, apply=apply, previous_broker=previous_broker)

    def notify_if_browser(environ: WSGIEnvironment) -> None:
        if "text/html" in _media_types(environ.get("HTTP_ACCEPT", "")):
            notify()

    def route_post(environ: WSGIEnvironment) -> _Response:
        if environ.get("PATH_INFO") != "/":
            return _Response("404 Not Found", "Not found.")
        try:
            form = _read_form(environ, portal_host)
        except _Refused as refused:
            return refused.response
        notify_if_browser(environ)
        with submit_lock:
            result = submit(form)
        if result.ok:
            return _html_response("200 OK", _saved_page(result.ntp_server))
        page = render_form(_scan(scan_ssids), values=form, errors=result.errors, mode=mode)
        return _html_response("400 Bad Request", page)

    def route(environ: WSGIEnvironment) -> _Response:
        method = environ.get("REQUEST_METHOD")
        if method == "POST":
            return route_post(environ)
        if method not in ("GET", "HEAD"):
            return _Response(
                "405 Method Not Allowed",
                "Method not allowed.",
                extra_headers=(("Allow", "GET, HEAD, POST"),),
            )
        if not _is_portal_host(environ, portal_host):
            location = f"http://{portal_host}/"
            return _Response("302 Found", _REDIRECT_BODY, extra_headers=(("Location", location),))
        if environ.get("PATH_INFO", "/") != "/":
            return _Response("302 Found", _REDIRECT_BODY, extra_headers=(("Location", "/"),))
        if method == "GET":
            notify_if_browser(environ)
        return _html_response("200 OK", render_form(_scan(scan_ssids), values=prefill, mode=mode))

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        response = route(environ)
        # "replace": a scanner using surrogateescape can return text UTF-8 cannot encode.
        body = response.body.encode("utf-8", "replace")
        start_response(
            response.status,
            [
                ("Content-Type", response.content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                *response.extra_headers,
            ],
        )
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]

    return app


class _QuietRequestHandler(WSGIRequestHandler):
    # Each connection has its own thread, and this bounds how long a silent one lives.
    timeout = 10

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing: request lines carry query strings, which could hold a secret."""


class _PortalServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log the type only. The default prints a traceback, for a silent socket too."""
        logger.warning("a portal connection failed: %s", sys.exc_info()[0].__name__)


def make_server(app: WSGIApplication, ip: str, port: int, interface: str = "wlan0") -> WSGIServer:
    """Return a threaded wsgiref server for ``app``, listening and ready for ``serve_forever``.

    It accepts connections only on ``ip``, and only arriving through ``interface``, so
    the portal stays off ``eth0``. Raises ``ValueError`` for anything
    but one specific IP address or for an empty interface, and ``OSError`` if the
    interface does not exist.
    """
    if not interface:
        # An empty name would clear the device binding instead of setting it.
        raise ValueError("the portal must be bound to a named interface")
    if ipaddress.ip_address(ip).is_unspecified:  # also raises ValueError for a non-address
        raise ValueError("the portal must be bound to one specific IP address")

    server = _PortalServer((ip, port), _QuietRequestHandler, bind_and_activate=False)
    try:
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.set_app(app)
    return server
