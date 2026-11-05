"""The captive portal: the setup form, the submit handler, and a stdlib WSGI app.

``render_form`` and ``handle_submit`` hold the logic and touch nothing; ``make_app``
adds HTTP around them, with the scanner, the ``apply`` side effect, and the AP-window
hook injected. Task 6 owns what those three do.

Two rules run through this module. Every value reaching a page is escaped, because
SSIDs are chosen by whoever is broadcasting nearby. And no secret is ever echoed or
logged: the template has no placeholder for one, and exceptions from ``apply`` are
reduced to their type name, since their messages can quote what they were given.
"""

from __future__ import annotations

import html
import logging
import re
import wsgiref.simple_server
from dataclasses import dataclass, field
from importlib import resources
from string import Template
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig, validate

__all__ = ["SubmitResult", "handle_submit", "make_app", "make_server", "render_form"]

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024
# Well above the form's own field count, and stops a 64 KiB body parsing into thousands.
_MAX_FORM_FIELDS = 64
# Bounded because int() raises on very long digit strings, and accepts "1_883" and "١٨٨٣".
_PORT_RE = re.compile(r"[0-9]{1,5}")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,18}")

_REDISPLAYED_FIELDS = (
    "wifi_ssid",
    "hostname",
    "node_id",
    "broker_host",
    "broker_port",
    "broker_username",
    "mediamtx_address",
    "frigate_base_url",
    "covered_zones",
)

_APPLY_FAILED_ERROR = "The settings could not be saved. Check them and try again."
_REENTER_NOTE = "Passwords and keys are never shown back, so enter them again."

_FORM_TEMPLATE = Template(
    resources.files(__package__).joinpath("templates/index.html").read_text(encoding="utf-8")
)

_SAVED_PAGE = """<!doctype html>
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
</body>
</html>
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _errors_html(errors: Sequence[str]) -> str:
    if not errors:
        return ""
    items = "".join(f"<li>{_escape(error)}</li>" for error in errors)
    return f'<div class="errors"><ul>{items}</ul><p>{_escape(_REENTER_NOTE)}</p></div>'


def render_form(
    ssids: Iterable[str],
    *,
    values: Mapping[str, str] | None = None,
    errors: Sequence[str] = (),
) -> str:
    """Return the setup page, listing ``ssids`` and showing any ``errors``.

    ``values`` is a previous submission to redisplay. Only the non-secret fields are
    read from it, so passing a whole submitted form is safe.
    """
    if values is None:
        shown: Mapping[str, str] = {"broker_port": str(BrokerConfig.port)}
        vpn_ticked = True
    else:
        shown = values
        vpn_ticked = "vpn_requested" in values
    # Absent from a first render's values too, so both boxes start unticked.
    boxes = values or {}

    # Substitution is a single pass, so a value holding "$errors" stays literal text.
    return _FORM_TEMPLATE.substitute(
        {name: _escape(shown.get(name, "")) for name in _REDISPLAYED_FIELDS},
        errors=_errors_html(errors),
        # A scan reports one row per access point, so a site network repeats.
        ssid_options="".join(f'<option value="{_escape(s)}">' for s in dict.fromkeys(ssids)),
        vpn_checked="checked" if vpn_ticked else "",
        tls_checked="checked" if "broker_tls" in boxes else "",
        anonymous_checked="checked" if "broker_anonymous" in boxes else "",
    )


@dataclass(frozen=True)
class SubmitResult:
    """The outcome of a submission. ``errors`` is safe to show and to log."""

    ok: bool
    errors: list[str] = field(default_factory=list)


def _parse_port(text: str) -> int | str:
    """Return the port as an int, or unchanged for ``validate`` to reject by name."""
    return int(text) if _PORT_RE.fullmatch(text) else text


def _build_config(form: Mapping[str, str]) -> NodeConfig:
    def raw(name: str) -> str:
        return form.get(name, "")

    # Phone keyboards append spaces, and validate would reject the form over them.
    # Identity fields stay raw so "cam\n" is rejected, and passwords and SSIDs stay raw
    # because a space is legal in them.
    def trimmed(name: str) -> str:
        return raw(name).strip()

    vpn_requested = "vpn_requested" in form
    return NodeConfig(
        node_id=raw("node_id"),
        hostname=raw("hostname"),
        wifi_ssid=raw("wifi_ssid"),
        wifi_psk=raw("wifi_psk"),
        # A key for a VPN nobody asked for must not end up saved on disk.
        tailscale_auth_key=trimmed("tailscale_auth_key") if vpn_requested else "",
        vpn_requested=vpn_requested,
        mediamtx_address=trimmed("mediamtx_address"),
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


def handle_submit(form: Mapping[str, str], *, apply: Callable[[NodeConfig], None]) -> SubmitResult:
    """Validate a submitted form, and call ``apply`` once only if it is valid."""
    cfg = _build_config(form)
    errors = validate(cfg)
    if errors:
        return SubmitResult(ok=False, errors=errors)
    try:
        apply(cfg)
    except Exception as exc:
        # No exc_info: the traceback would print the message, which may quote a secret.
        logger.error("applying the submitted config failed: %s", type(exc).__name__)
        return SubmitResult(ok=False, errors=[_APPLY_FAILED_ERROR])
    return SubmitResult(ok=True)


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


def _same_origin(environ: WSGIEnvironment) -> bool:
    """False if a present ``Origin`` or ``Referer`` does not match ``Host``."""
    host = environ.get("HTTP_HOST", "").lower()
    for header in ("HTTP_ORIGIN", "HTTP_REFERER"):
        value = environ.get(header)
        if value is not None and (not host or _netloc(value) != host):
            return False
    return True


def _read_form(environ: WSGIEnvironment) -> dict[str, str]:
    """Return the POSTed form, or raise ``_Refused``. Reads nothing it will refuse."""
    if not _same_origin(environ):
        raise _Refused(_Response("403 Forbidden", "This form must be sent from its own page."))

    # Reading without a valid length would block this single-threaded server.
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


def make_app(
    *,
    scan_ssids: Callable[[], list[str]],
    apply: Callable[[NodeConfig], None],
    on_form_request: Callable[[], None] | None = None,
) -> WSGIApplication:
    """Return the portal's WSGI app.

    ``on_form_request`` is the AP-window hook. It runs for form GETs and for POSTs that
    reach validation, before ``apply``. It never runs for a redirected path, so a
    phone's connectivity checks cannot hold the AP open, nor for a HEAD or a refused
    POST.
    """
    notify = on_form_request or (lambda: None)

    def route_post(environ: WSGIEnvironment) -> _Response:
        if environ.get("PATH_INFO") != "/":
            return _Response("404 Not Found", "Not found.")
        try:
            form = _read_form(environ)
        except _Refused as refused:
            return refused.response
        notify()
        result = handle_submit(form, apply=apply)
        if result.ok:
            return _html_response("200 OK", _SAVED_PAGE)
        page = render_form(_scan(scan_ssids), values=form, errors=result.errors)
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
        if environ.get("PATH_INFO", "/") != "/":
            return _Response("302 Found", "Redirecting.", extra_headers=(("Location", "/"),))
        if method == "GET":
            notify()
        return _html_response("200 OK", render_form(_scan(scan_ssids)))

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
    # One thread serves every client, so a silent one must not hold the portal.
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing: request lines carry query strings, which could hold a secret."""


def make_server(app: WSGIApplication, host: str, port: int) -> WSGIServer:
    """Return a wsgiref server for ``app``, bound and ready for ``serve_forever``."""
    return wsgiref.simple_server.make_server(host, port, app, handler_class=_QuietRequestHandler)
