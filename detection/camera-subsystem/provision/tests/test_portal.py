"""Tests for ``cameraprovision.portal`` — the form, the submit handler, the WSGI app.

SSIDs are attacker-controlled and the form carries three secrets, so pages are parsed
with a real HTML parser rather than searched for substrings: a passing test means a
browser would build no injected element.
"""

from __future__ import annotations

import http.client
import io
import logging
import socket
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import Mock
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest
from camerabridge import cli as bridge_cli

from cameraprovision import portal
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig, write_bridge_config
from cameraprovision.portal import SubmitResult, handle_submit, make_app, make_server, render_form

WIFI_PSK = "w1fi-s3cret-Psk"
BROKER_PASSWORD = "br0ker-s3cret-Pw"
TAILSCALE_KEY = "tskey-auth-s3cret"
SECRETS = (WIFI_PSK, BROKER_PASSWORD, TAILSCALE_KEY)
SECRET_FIELDS = ("wifi_psk", "broker_password", "tailscale_auth_key")

REDISPLAYED_FIELDS = (
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

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "'><svg onload=alert(1)>",
    "</option></datalist><script>alert(1)</script>",
    "$errors",  # must not be expanded as a template placeholder
    "&lt;b&gt;",  # must not be un-escaped on the way through
]


def valid_form(**overrides: str) -> dict[str, str]:
    form = {
        "wifi_ssid": "Site-WiFi",
        "wifi_psk": WIFI_PSK,
        "hostname": "edge-7",
        "node_id": "cam-01",
        "broker_host": "10.0.0.2",
        "broker_port": "1883",
        "broker_username": "camera-cam-01",
        "broker_password": BROKER_PASSWORD,
        "mediamtx_address": "10.0.0.2:8554",
        "frigate_base_url": "http://cam-01.local:5000",
        "covered_zones": "perimeter-north, driveway",
        "vpn_requested": "on",
        "tailscale_auth_key": TAILSCALE_KEY,
    }
    form.update(overrides)
    return form


def form_without(name: str) -> dict[str, str]:
    form = valid_form()
    del form[name]
    return form


class Page(HTMLParser):
    """The elements a browser would build from a page."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def tags(self) -> set[str]:
        return {tag for tag, _ in self.elements}

    def controls(self) -> dict[str, dict[str, str | None]]:
        return {
            str(attrs["name"]): attrs
            for tag, attrs in self.elements
            if tag in ("input", "select", "textarea") and "name" in attrs
        }

    def options(self) -> list[str | None]:
        return [attrs.get("value") for tag, attrs in self.elements if tag == "option"]

    def event_handler_attrs(self) -> list[str]:
        return [
            name for _, attrs in self.elements for name in attrs if name.lower().startswith("on")
        ]


# --- render_form ----------------------------------------------------------------


def test_form_lists_the_scanned_ssids() -> None:
    assert Page(render_form(["Alpha", "Beta net"])).options() == ["Alpha", "Beta net"]


def test_form_lists_a_repeated_ssid_once() -> None:
    """A scan reports one row per access point, so a site network repeats."""
    assert Page(render_form(["Alpha", "Beta", "Alpha"])).options() == ["Alpha", "Beta"]


def test_form_works_with_no_scan_results() -> None:
    """The radio may have found nothing, or the network may be hidden: typing must work."""
    page = Page(render_form([]))
    assert page.options() == []
    assert page.controls()["wifi_ssid"].get("type") == "text"


def test_form_collects_exactly_the_portal_fields() -> None:
    """Camera-link settings stay out (constraints.md): the set is exact, not a superset."""
    assert set(Page(render_form([])).controls()) == {
        *REDISPLAYED_FIELDS,
        *SECRET_FIELDS,
        "vpn_requested",
        "broker_tls",
        "broker_anonymous",
    }


def test_form_posts_to_the_root() -> None:
    forms = [attrs for tag, attrs in Page(render_form([])).elements if tag == "form"]
    assert len(forms) == 1
    assert forms[0]["method"] == "post"
    assert forms[0]["action"] == "/"


def test_form_uses_no_external_assets() -> None:
    """The AP has no internet, so anything fetched would hang the page."""
    html = render_form(["Alpha"])
    page = Page(html)

    assert "style" in page.tags()
    assert not page.tags() & {"script", "link", "img", "iframe", "object", "embed"}
    assert all("src" not in attrs and "href" not in attrs for _, attrs in page.elements)
    assert "url(" not in html
    assert "@import" not in html


def test_form_is_mobile_friendly() -> None:
    metas = [attrs for tag, attrs in Page(render_form([])).elements if tag == "meta"]
    assert any(attrs.get("name") == "viewport" for attrs in metas)


def test_first_render_defaults() -> None:
    controls = Page(render_form([])).controls()
    assert controls["broker_port"]["value"] == str(BrokerConfig.port)
    assert "checked" in controls["vpn_requested"]
    assert "checked" not in controls["broker_tls"]
    assert "checked" not in controls["broker_anonymous"]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_malicious_ssid_is_inert(payload: str) -> None:
    page = Page(render_form([payload]))

    assert page.options() == [payload]  # shown literally, exactly once
    assert not page.tags() & {"script", "img", "svg"}
    assert page.event_handler_attrs() == []


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
@pytest.mark.parametrize("field", REDISPLAYED_FIELDS)
def test_malicious_redisplayed_value_is_inert(field: str, payload: str) -> None:
    page = Page(render_form([], values=valid_form(**{field: payload})))

    assert page.controls()[field]["value"] == payload
    assert not page.tags() & {"script", "img", "svg"}
    assert page.event_handler_attrs() == []


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_error_text_is_escaped(payload: str) -> None:
    page = Page(render_form([], errors=[payload]))
    assert not page.tags() & {"script", "img", "svg"}
    assert page.event_handler_attrs() == []


def test_errors_are_shown() -> None:
    assert "node_id must be lowercase" in render_form([], errors=["node_id must be lowercase"])


def test_redisplay_keeps_non_secret_values_and_checkbox_state() -> None:
    values = valid_form()
    del values["vpn_requested"]
    controls = Page(render_form([], values=values)).controls()

    for field in REDISPLAYED_FIELDS:
        assert controls[field]["value"] == values[field]
    assert "checked" not in controls["vpn_requested"]


def test_redisplay_keeps_a_ticked_vpn_box() -> None:
    assert "checked" in Page(render_form([], values=valid_form())).controls()["vpn_requested"]


def test_secrets_are_never_rendered() -> None:
    html = render_form(["Alpha"], values=valid_form(), errors=["node_id is wrong"])
    controls = Page(html).controls()

    for secret in SECRETS:
        assert secret not in html
    for field in SECRET_FIELDS:
        assert controls[field]["type"] == "password"
        assert "value" not in controls[field]


@pytest.mark.parametrize("box", ["broker_tls", "broker_anonymous"])
def test_redisplay_keeps_broker_checkbox_state(box: str) -> None:
    assert "checked" not in Page(render_form([], values=valid_form())).controls()[box]
    assert "checked" in Page(render_form([], values=valid_form(**{box: "on"}))).controls()[box]


def test_broker_credentials_can_be_left_blank_in_the_browser() -> None:
    """A required attribute would stop an anonymous submission reaching the server."""
    controls = Page(render_form([])).controls()
    assert "required" not in controls["broker_username"]
    assert "required" not in controls["broker_password"]


# --- handle_submit: success -----------------------------------------------------


def test_valid_form_applies_the_parsed_config_once() -> None:
    apply = Mock()

    result = handle_submit(valid_form(), apply=apply)

    assert result == SubmitResult(ok=True)
    apply.assert_called_once_with(
        NodeConfig(
            node_id="cam-01",
            hostname="edge-7",
            wifi_ssid="Site-WiFi",
            wifi_psk=WIFI_PSK,
            tailscale_auth_key=TAILSCALE_KEY,
            vpn_requested=True,
            mediamtx_address="10.0.0.2:8554",
            covered_zones=["perimeter-north", "driveway"],
            broker=BrokerConfig(
                host="10.0.0.2",
                port=1883,
                username="camera-cam-01",
                password=BROKER_PASSWORD,
            ),
            frigate=FrigateConfig(base_url="http://cam-01.local:5000"),
        )
    )


def submitted_config(form: dict[str, str]) -> NodeConfig:
    apply = Mock()
    assert handle_submit(form, apply=apply) == SubmitResult(ok=True)
    return apply.call_args.args[0]


def test_tls_box_sets_broker_tls() -> None:
    assert submitted_config(valid_form(broker_tls="on")).broker.tls is True
    assert submitted_config(valid_form()).broker.tls is False


def test_tls_box_reaches_the_bridge_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"
    write_bridge_config(submitted_config(valid_form(broker_tls="on")), path)
    assert bridge_cli.load_config(str(path))["broker"]["tls"] is True


def test_anonymous_box_allows_blank_broker_credentials() -> None:
    cfg = submitted_config(
        valid_form(broker_anonymous="on", broker_username="", broker_password="")
    )
    assert cfg.broker_anonymous is True
    assert (cfg.broker.username, cfg.broker.password) == ("", "")


def test_anonymous_box_with_credentials_is_rejected() -> None:
    apply = Mock()
    result = handle_submit(valid_form(broker_anonymous="on"), apply=apply)
    assert not result.ok
    assert any(e.startswith("broker.username") for e in result.errors)
    apply.assert_not_called()


def test_blank_broker_credentials_need_the_anonymous_box() -> None:
    apply = Mock()
    result = handle_submit(valid_form(broker_username="", broker_password=""), apply=apply)
    assert not result.ok
    apply.assert_not_called()


def test_unticked_vpn_needs_no_key() -> None:
    apply = Mock()
    form = form_without("vpn_requested")
    form["tailscale_auth_key"] = ""

    assert handle_submit(form, apply=apply).ok

    cfg = apply.call_args.args[0]
    assert cfg.vpn_requested is False
    assert cfg.tailscale_auth_key == ""


def test_unticked_vpn_discards_a_typed_key() -> None:
    """The key would otherwise be saved to disk for a VPN nobody asked for."""
    apply = Mock()
    assert handle_submit(form_without("vpn_requested"), apply=apply).ok
    assert apply.call_args.args[0].tailscale_auth_key == ""


@pytest.mark.parametrize(
    ("typed", "zones"),
    [
        ("driveway", ["driveway"]),
        (" a , b ", ["a", "b"]),
        ("a,,b,", ["a", "b"]),
    ],
)
def test_covered_zones_are_split_on_commas(typed: str, zones: list[str]) -> None:
    apply = Mock()
    assert handle_submit(valid_form(covered_zones=typed), apply=apply).ok
    assert apply.call_args.args[0].covered_zones == zones


def test_phone_keyboard_spaces_are_trimmed_from_endpoints() -> None:
    """Forgiven here, because validate would reject the whole form over a stray space."""
    apply = Mock()
    form = valid_form(
        broker_host=" 10.0.0.2 ",
        broker_username="camera-cam-01 ",
        mediamtx_address="10.0.0.2:8554 ",
        frigate_base_url="http://cam-01.local:5000 ",
        tailscale_auth_key=TAILSCALE_KEY + " ",
    )

    assert handle_submit(form, apply=apply).ok

    cfg = apply.call_args.args[0]
    assert cfg.broker.host == "10.0.0.2"
    assert cfg.broker.username == "camera-cam-01"
    assert cfg.mediamtx_address == "10.0.0.2:8554"
    assert cfg.frigate.base_url == "http://cam-01.local:5000"
    assert cfg.tailscale_auth_key == TAILSCALE_KEY


def test_passwords_and_ssids_keep_their_spaces() -> None:
    """Spaces are legal in all three, so trimming would silently change the value."""
    apply = Mock()
    form = valid_form(wifi_psk=" pass word ", broker_password=" pw ", wifi_ssid=" Cafe WiFi ")

    assert handle_submit(form, apply=apply).ok

    cfg = apply.call_args.args[0]
    assert cfg.wifi_psk == " pass word "
    assert cfg.broker.password == " pw "
    assert cfg.wifi_ssid == " Cafe WiFi "


def test_portal_only_fields_stay_out_of_the_bridge_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"
    form = valid_form(wifi_ssid="Site-WiFi-XYZ", hostname="edge-host-7")

    result = handle_submit(form, apply=lambda cfg: write_bridge_config(cfg, path))

    assert result.ok
    text = path.read_text(encoding="utf-8")
    for leaked in ("Site-WiFi-XYZ", WIFI_PSK, TAILSCALE_KEY, "edge-host-7"):
        assert leaked not in text
    assert bridge_cli.load_config(str(path))["broker"]["password"] == BROKER_PASSWORD


# --- handle_submit: rejection ---------------------------------------------------


@pytest.mark.parametrize(
    ("named", "form"),
    [
        ("wifi_ssid", form_without("wifi_ssid")),
        ("wifi_ssid", valid_form(wifi_ssid="")),
        ("wifi_ssid", valid_form(wifi_ssid="   ")),
        ("wifi_ssid", valid_form(wifi_ssid="s" * 33)),
        ("wifi_psk", form_without("wifi_psk")),
        ("wifi_psk", valid_form(wifi_psk="short")),
        ("node_id", form_without("node_id")),
        ("node_id", valid_form(node_id="")),
        ("node_id", valid_form(node_id="CAM")),
        ("node_id", valid_form(node_id="cam\n")),  # must not be trimmed into validity
        ("node_id", valid_form(node_id="cam ")),
        ("node_id", valid_form(node_id="-cam")),
        ("hostname", form_without("hostname")),
        ("hostname", valid_form(hostname="edge.local")),
        ("hostname", valid_form(hostname="edge\n")),
        ("broker.host", form_without("broker_host")),
        ("broker.host", valid_form(broker_host="   ")),
        ("broker.username", form_without("broker_username")),
        ("broker.password", form_without("broker_password")),
        ("broker.port", form_without("broker_port")),
        ("broker.port", valid_form(broker_port="")),
        ("broker.port", valid_form(broker_port="abc")),
        ("broker.port", valid_form(broker_port="0")),
        ("broker.port", valid_form(broker_port="70000")),
        ("broker.port", valid_form(broker_port="-1")),
        ("broker.port", valid_form(broker_port="1_883")),  # int() would accept these three
        ("broker.port", valid_form(broker_port=" 1883")),
        ("broker.port", valid_form(broker_port="١٨٨٣")),
        ("broker.port", valid_form(broker_port="9" * 5000)),  # int() raises past 4300 digits
        ("mediamtx_address", form_without("mediamtx_address")),
        ("frigate.base_url", form_without("frigate_base_url")),
        ("frigate.base_url", valid_form(frigate_base_url="cam-01.local:5000")),
        ("covered_zones", form_without("covered_zones")),
        ("covered_zones", valid_form(covered_zones="")),
        ("covered_zones", valid_form(covered_zones=" , ,")),
        ("tailscale_auth_key", form_without("tailscale_auth_key")),
        ("tailscale_auth_key", valid_form(tailscale_auth_key="")),
        ("tailscale_auth_key", valid_form(tailscale_auth_key="   ")),
    ],
)
def test_bad_form_names_the_field_and_applies_nothing(named: str, form: dict[str, str]) -> None:
    apply = Mock()

    result = handle_submit(form, apply=apply)

    assert result.ok is False
    assert any(error.startswith(named) for error in result.errors), result.errors
    apply.assert_not_called()


def test_empty_form_reports_every_problem_without_raising() -> None:
    apply = Mock()
    result = handle_submit({}, apply=apply)

    assert result.ok is False
    assert len(result.errors) > 5
    apply.assert_not_called()


def test_rejection_carries_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    form = valid_form(node_id="BAD", wifi_psk="short-psk")
    secrets = ("short-psk", BROKER_PASSWORD, TAILSCALE_KEY)

    with caplog.at_level(logging.DEBUG):
        result = handle_submit(form, apply=Mock())

    assert not result.ok
    for secret in secrets:
        assert secret not in " ".join(result.errors)
        assert secret not in repr(result)
        assert secret not in caplog.text


# --- handle_submit: apply failure -----------------------------------------------


def exploding_apply(cfg: NodeConfig) -> None:
    raise OSError(f"could not write {cfg.wifi_psk} {cfg.broker.password} {cfg.tailscale_auth_key}")


def test_apply_failure_is_generic_and_carries_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = handle_submit(valid_form(), apply=exploding_apply)

    assert result.ok is False
    assert result.errors
    text = " ".join(result.errors) + repr(result) + caplog.text
    for secret in SECRETS:
        assert secret not in text
    assert "could not write" not in " ".join(result.errors)


def test_apply_failure_logs_no_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """A traceback prints the exception message, which is where a secret would be."""
    with caplog.at_level(logging.DEBUG):
        handle_submit(valid_form(), apply=exploding_apply)

    assert caplog.records  # the failure is still visible to an operator reading logs
    assert all(record.exc_info is None for record in caplog.records)


def test_apply_is_called_once_even_when_it_fails() -> None:
    apply = Mock(side_effect=OSError("disk full"))
    assert not handle_submit(valid_form(), apply=apply).ok
    apply.assert_called_once()


# --- WSGI harness ---------------------------------------------------------------


class Response:
    def __init__(self, status: str, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.code = int(status.split()[0])
        self.headers = {name.lower(): value for name, value in headers}
        self.body = body
        self.text = body.decode("utf-8")


class ForbiddenInput(io.BytesIO):
    """Fails a test that reads a body the app was meant to refuse unread."""

    def read(self, *_args: Any) -> bytes:
        raise AssertionError("the body must not be read")


def request(
    app: Callable[..., Any],
    method: str = "GET",
    path: str = "/",
    body: bytes | None = None,
    *,
    headers: dict[str, str] | None = None,
    content_length: str | None = None,
    wsgi_input: io.BytesIO | None = None,
    strict: bool = True,
) -> Response:
    """Call the app as a server would; ``strict`` also checks WSGI compliance."""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": "",
    }
    setup_testing_defaults(environ)
    environ["HTTP_HOST"] = "10.42.0.1"
    environ["wsgi.input"] = wsgi_input or io.BytesIO(body or b"")
    if body is not None:
        environ["CONTENT_LENGTH"] = str(len(body))
    if content_length is not None:
        environ["CONTENT_LENGTH"] = content_length
    for name, value in (headers or {}).items():
        environ[name] = value
        if value == "":
            del environ[name]

    captured: dict[str, Any] = {}

    def start_response(status: str, response_headers: list[tuple[str, str]], _exc: Any = None) -> Any:
        captured.update(status=status, headers=response_headers)
        return lambda _data: None

    callable_app = validator(app) if strict else app
    result = callable_app(environ, start_response)
    try:
        payload = b"".join(result)
    finally:
        if hasattr(result, "close"):
            result.close()
    return Response(captured["status"], captured["headers"], payload)


class Harness:
    def __init__(self, **kwargs: Any) -> None:
        self.apply = kwargs.pop("apply", Mock())
        self.scan = kwargs.pop("scan", Mock(return_value=["Alpha", "Beta"]))
        self.activity = Mock()
        self.app = make_app(scan_ssids=self.scan, apply=self.apply, on_form_request=self.activity)

    def post(self, form: dict[str, str] | None = None, **kwargs: Any) -> Response:
        return request(self.app, "POST", "/", encoded(form or valid_form()), **kwargs)


def encoded(form: dict[str, str]) -> bytes:
    return urlencode(form).encode("ascii")


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --- WSGI: pages and redirects --------------------------------------------------


def test_get_root_serves_the_form_with_the_scanned_ssids(harness: Harness) -> None:
    response = request(harness.app)

    assert response.code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert int(response.headers["content-length"]) == len(response.body)
    assert Page(response.text).options() == ["Alpha", "Beta"]


def test_pages_are_not_cached_and_may_load_nothing_external(harness: Harness) -> None:
    response = request(harness.app)
    csp = response.headers["content-security-policy"]

    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in csp
    assert "form-action 'self'" in csp


def test_form_get_restarts_the_ap_window(harness: Harness) -> None:
    request(harness.app)
    harness.activity.assert_called_once_with()


@pytest.mark.parametrize(
    "path",
    [
        "/generate_204",
        "/gen_204",
        "/hotspot-detect.html",
        "/library/test/success.html",
        "/connecttest.txt",
        "/ncsi.txt",
        "/canonical.html",
        "/index.html",
        "//evil.example/",
    ],
)
def test_unknown_get_redirects_home_without_restarting_the_ap_window(
    harness: Harness, path: str
) -> None:
    response = request(harness.app, "GET", path)

    assert response.code == 302
    assert response.headers["location"] == "/"
    harness.activity.assert_not_called()
    harness.scan.assert_not_called()


def test_head_root_has_no_body_and_does_not_restart_the_ap_window(harness: Harness) -> None:
    response = request(harness.app, "HEAD")

    assert response.code == 200
    assert response.body == b""
    harness.activity.assert_not_called()


def test_head_unknown_path_redirects_home(harness: Harness) -> None:
    response = request(harness.app, "HEAD", "/generate_204")

    assert response.code == 302
    assert response.headers["location"] == "/"
    harness.activity.assert_not_called()


def test_other_methods_are_refused(harness: Harness) -> None:
    response = request(harness.app, "PUT", "/", b"x")

    assert response.code == 405
    assert "POST" in response.headers["allow"]
    harness.activity.assert_not_called()


def test_post_to_another_path_is_not_a_submit(harness: Harness) -> None:
    response = request(harness.app, "POST", "/generate_204", encoded(valid_form()))

    assert response.code == 404
    harness.apply.assert_not_called()
    harness.activity.assert_not_called()


def test_failing_scanner_still_serves_a_form() -> None:
    """The operator can type the SSID, so a scan problem must not lock them out."""
    harness = Harness(scan=Mock(side_effect=OSError("nmcli gone")))

    response = request(harness.app)

    assert response.code == 200
    assert Page(response.text).options() == []


def test_undecodable_ssid_does_not_break_the_page() -> None:
    """A scanner using surrogateescape hands back text UTF-8 cannot encode."""
    harness = Harness(scan=Mock(return_value=["caf\udce9", "Alpha"]))

    response = request(harness.app)

    assert response.code == 200
    assert "Alpha" in Page(response.text).options()


# --- WSGI: submit ---------------------------------------------------------------


def test_valid_post_applies_once_and_restarts_the_ap_window(harness: Harness) -> None:
    response = harness.post()

    assert response.code == 200
    harness.apply.assert_called_once()
    assert harness.apply.call_args.args[0].node_id == "cam-01"
    harness.activity.assert_called_once_with()
    for secret in SECRETS:
        assert secret not in response.text


def test_success_page_uses_no_form_and_no_external_assets(harness: Harness) -> None:
    page = Page(harness.post().text)
    assert "form" not in page.tags()
    assert not page.tags() & {"script", "link", "img"}


def test_failed_post_redisplays_values_without_secrets(harness: Harness) -> None:
    response = harness.post(valid_form(node_id="BAD ID", wifi_ssid="Site <b>"))
    controls = Page(response.text).controls()

    assert response.code == 400
    harness.apply.assert_not_called()
    assert "node_id must be" in response.text
    assert controls["node_id"]["value"] == "BAD ID"
    assert controls["wifi_ssid"]["value"] == "Site <b>"
    assert Page(response.text).options() == ["Alpha", "Beta"]
    for secret in SECRETS:
        assert secret not in response.text
    for field in SECRET_FIELDS:
        assert "value" not in controls[field]


def test_failed_post_still_counts_as_operator_activity(harness: Harness) -> None:
    harness.post(valid_form(node_id="BAD"))
    harness.activity.assert_called_once_with()


def test_apply_failure_through_wsgi_shows_nothing_secret() -> None:
    harness = Harness(apply=exploding_apply)

    response = harness.post()

    assert response.code == 400
    assert "could not write" not in response.text
    for secret in SECRETS:
        assert secret not in response.text


def test_first_duplicate_field_wins(harness: Harness) -> None:
    body = encoded(valid_form(node_id="cam-first")) + b"&node_id=cam-second"
    assert request(harness.app, "POST", "/", body).code == 200
    assert harness.apply.call_args.args[0].node_id == "cam-first"


def test_percent_encoded_utf8_values_decode(harness: Harness) -> None:
    assert harness.post(valid_form(wifi_ssid="Café")).code == 200
    assert harness.apply.call_args.args[0].wifi_ssid == "Café"


@pytest.mark.parametrize(
    "body",
    [
        b"wifi_psk=%FF%FE&node_id=cam-01",  # not UTF-8 once decoded
        b"wifi_psk=\xff\xfe",  # not UTF-8 on the wire
        b"&".join(b"f%d=1" % i for i in range(5000)),  # more fields than the form has
    ],
    ids=["percent-encoded", "raw", "too-many-fields"],
)
def test_undecodable_body_is_a_bad_request(harness: Harness, body: bytes) -> None:
    response = request(harness.app, "POST", "/", body)

    assert response.code == 400
    harness.apply.assert_not_called()
    harness.activity.assert_not_called()


# --- WSGI: body limit -----------------------------------------------------------


def body_of_size(size: int) -> bytes:
    body = encoded(valid_form()) + b"&padding="
    return body + b"x" * (size - len(body))


def test_body_of_exactly_64_kib_is_accepted(harness: Harness) -> None:
    response = request(harness.app, "POST", "/", body_of_size(64 * 1024))

    assert response.code == 200
    harness.apply.assert_called_once()


def test_larger_body_is_413_unread(harness: Harness) -> None:
    response = request(
        harness.app,
        "POST",
        "/",
        content_length=str(64 * 1024 + 1),
        wsgi_input=ForbiddenInput(),
    )

    assert response.code == 413
    harness.apply.assert_not_called()
    harness.activity.assert_not_called()


@pytest.mark.parametrize("content_length", ["", "abc", "-1", "1_000"])
def test_unusable_content_length_is_400_unread(harness: Harness, content_length: str) -> None:
    """Reading with no valid length would block the single-threaded server."""
    response = request(
        harness.app,
        "POST",
        "/",
        content_length=content_length,
        wsgi_input=ForbiddenInput(),
        strict=False,  # wsgiref's validator itself refuses these environs
    )

    assert response.code == 400
    harness.apply.assert_not_called()


def test_truncated_body_is_400_and_applies_nothing(harness: Harness) -> None:
    """A dropped connection must not submit whatever fields happened to arrive."""
    body = encoded(valid_form())
    response = request(
        harness.app,
        "POST",
        "/",
        content_length=str(len(body) + 10),
        wsgi_input=io.BytesIO(body),
    )

    assert response.code == 400
    harness.apply.assert_not_called()


# --- WSGI: Origin and Referer ---------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"HTTP_ORIGIN": "http://10.42.0.1"},
        {"HTTP_REFERER": "http://10.42.0.1/"},
        {"HTTP_ORIGIN": "http://10.42.0.1", "HTTP_REFERER": "http://10.42.0.1/?x=1"},
        {"HTTP_ORIGIN": "HTTP://10.42.0.1"},
    ],
    ids=["neither", "origin", "referer", "both", "case-insensitive"],
)
def test_same_origin_post_is_accepted(harness: Harness, headers: dict[str, str]) -> None:
    assert harness.post(headers=headers).code == 200
    harness.apply.assert_called_once()


@pytest.mark.parametrize(
    "headers",
    [
        {"HTTP_ORIGIN": "http://evil.example"},
        {"HTTP_REFERER": "http://evil.example/"},
        {"HTTP_ORIGIN": "null"},
        {"HTTP_ORIGIN": "http://["},
        {"HTTP_ORIGIN": "http://10.42.0.1:8080"},
        {"HTTP_ORIGIN": "http://10.42.0.1.evil.example"},
        {"HTTP_ORIGIN": "http://10.42.0.1", "HTTP_REFERER": "http://evil.example/"},
        {"HTTP_ORIGIN": "http://evil.example", "HTTP_REFERER": "http://10.42.0.1/"},
        {"HTTP_ORIGIN": "http://10.42.0.1", "HTTP_HOST": ""},
    ],
    ids=[
        "origin",
        "referer",
        "null-origin",
        "malformed",
        "other-port",
        "host-as-prefix",
        "bad-referer-good-origin",
        "bad-origin-good-referer",
        "no-host",
    ],
)
def test_cross_origin_post_is_403(harness: Harness, headers: dict[str, str]) -> None:
    response = harness.post(headers=headers)

    assert response.code == 403
    harness.apply.assert_not_called()
    harness.activity.assert_not_called()


# --- the wsgiref server ---------------------------------------------------------


@pytest.fixture
def served(harness: Harness) -> Iterator[tuple[Harness, int]]:
    server = make_server(harness.app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield harness, server.server_address[1]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def http_call(port: int, method: str, path: str, body: bytes | None = None) -> Any:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body)
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read()
    finally:
        connection.close()


def test_server_serves_the_form_redirect_and_submit(served: tuple[Harness, int]) -> None:
    harness, port = served

    status, _, page = http_call(port, "GET", "/")
    assert status == 200
    assert b"Alpha" in page

    assert http_call(port, "GET", "/generate_204")[:2] == (302, "/")

    status, _, _ = http_call(port, "POST", "/", encoded(valid_form()))
    assert status == 200
    harness.apply.assert_called_once()


def test_server_refuses_an_oversized_declared_body(served: tuple[Harness, int]) -> None:
    _, port = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", "/")
        connection.putheader("Content-Length", str(64 * 1024 + 1))
        connection.endheaders()
        assert connection.getresponse().status == 413
    finally:
        connection.close()


def test_server_does_not_log_request_lines(
    served: tuple[Harness, int], capfd: pytest.CaptureFixture[str]
) -> None:
    """A hand-typed query string could hold a secret, and the default log prints it."""
    _, port = served

    http_call(port, "GET", f"/generate_204?wifi_psk={WIFI_PSK}")
    http_call(port, "GET", f"/?wifi_psk={WIFI_PSK}")

    captured = capfd.readouterr()
    assert WIFI_PSK not in captured.out + captured.err


def test_stalled_client_does_not_hold_the_server(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One thread serves everyone, so a silent client must time out."""
    assert 0 < portal._QuietRequestHandler.timeout <= 60  # the shipped value is bounded
    monkeypatch.setattr(portal._QuietRequestHandler, "timeout", 0.1)
    server = make_server(harness.app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            assert http_call(port, "GET", "/")[0] == 200
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
