"""Tests for ``cameraprovision.daemon``: the wiring of the pure cores to the system.

Only the operating system is faked: nmcli, systemctl, GPIO and the listening socket.
The state machine, the watchdog decision, the portal's WSGI app and the on-disk store
are all real, so each test drives what a node would do. Time comes from a fake clock.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest
import yaml
from camerabridge import cli as bridge_cli

from cameraprovision import __main__ as cli
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig
from cameraprovision.daemon import Provisioner, ProvisionerFailed, Settings
from cameraprovision.portal import make_server
from cameraprovision.state import ProvisionState as S
from cameraprovision.store import NodeStore, Paths
from cameraprovision.system import SystemCommandError

WIFI_PSK = "w1fi-s3cret-Psk"
NEW_WIFI_PSK = "n3w-w1fi-s3cret"
BROKER_PASSWORD = "br0ker-s3cret-Pw"
SECRETS = (WIFI_PSK, NEW_WIFI_PSK, BROKER_PASSWORD)
AP_IP = "10.42.0.1"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
FORM_TYPE = "application/x-www-form-urlencoded"
JOIN_TIMEOUT = 120.0
AP_WINDOW = 300.0
AP_CAP = 1800.0
AP_CHECK = 10.0
JOIN_TICKS_PAST_EXIT_LIMIT = 61


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
    }
    form.update(overrides)
    return form


def node_config(**overrides: Any) -> NodeConfig:
    values: dict[str, Any] = {
        "node_id": "cam-01",
        "hostname": "edge-7",
        "wifi_ssid": "Site-WiFi",
        "wifi_psk": WIFI_PSK,
        "mediamtx_address": "10.0.0.2:8554",
        "covered_zones": ["perimeter-north"],
        "broker": BrokerConfig(host="10.0.0.2", username="camera-cam-01", password=BROKER_PASSWORD),
        "frigate": FrigateConfig(base_url="http://cam-01.local:5000"),
    }
    values.update(overrides)
    return NodeConfig(**values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSystem:
    """Records every side effect, in order, and fails on request."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.boot = "boot-1"
        self.button = False
        self.associated = False
        self.lease = False
        self.ssids = ["Site-WiFi", "Neighbour"]
        self.scan_failures = 0
        self.serial = "10000000ABCDEF12"
        self.failing: dict[str, Exception] = {}
        self.chowned: list[Path] = []
        self.hostnames: list[str] = []
        self.ap_alive = False
        self.on_start_ap: Callable[[], None] = lambda: None

    def _do(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failing:
            raise self.failing[name]

    def boot_id(self) -> str:
        self._do("boot_id")
        return self.boot

    def gpio_reprovision_held(self) -> bool:
        self._do("gpio")
        return self.button

    def scan_ssids(self) -> list[str]:
        self._do("scan")
        if self.scan_failures:
            self.scan_failures -= 1
            raise SystemCommandError("nmcli failed")
        return list(self.ssids)

    def restart_timesync(self) -> None:
        self._do("restart_timesync")

    def read_serial(self) -> str:
        self._do("read_serial")
        return self.serial

    def start_ap(self) -> None:
        self._do("start_ap")
        self.on_start_ap()
        self.ap_alive = True

    def stop_ap(self) -> None:
        self._do("stop_ap")
        self.ap_alive = False

    def ap_active(self) -> bool:
        self._do("ap_active")
        return self.ap_alive

    def set_hostname(self, hostname: str) -> None:
        self.hostnames.append(hostname)
        self._do("set_hostname")

    def reload_connections(self) -> None:
        self._do("reload")

    def join_uplink(self) -> None:
        self._do("join")

    def wifi_associated(self) -> bool:
        self._do("wifi_associated")
        return self.associated

    def ipv4_lease(self) -> bool:
        self._do("ipv4_lease")
        return self.lease

    def reload_firewall(self) -> None:
        self._do("reload_firewall")

    def chown_to_service_user(self, path: Path) -> None:
        self.chowned.append(path)
        self._do("chown")

    def link_up(self) -> None:
        self.associated = self.lease = True

    def link_down(self) -> None:
        self.associated = self.lease = False

    def count(self, name: str) -> int:
        return self.calls.count(name)


class FakeListener:
    """Stands in for the socket. The WSGI app it holds is the real portal."""

    def __init__(self, app: Any, ip: str, port: int, interface: str) -> None:
        self.app, self.ip, self.port, self.interface = app, ip, port, interface
        self.closed = False
        self.was_shut_down = threading.Event()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.was_shut_down.wait()

    def shutdown(self) -> None:
        self.was_shut_down.set()

    def server_close(self) -> None:
        self.closed = True


class Response:
    def __init__(self, status: str, headers: dict[str, str], body: str, closer: Any) -> None:
        self.status, self.headers, self.body = status, headers, body
        self._closer = closer
        self._finished = False

    @property
    def code(self) -> int:
        return int(self.status.split()[0])

    def finish(self) -> None:
        """What wsgiref does once the response is on the wire."""
        if not self._finished:
            self._finished = True
            self._closer.close()


class Node:
    """One simulated kit: a disk, a clock and a radio that survive daemon restarts."""

    def __init__(self, root: Path) -> None:
        self.calls: list[str] = []
        self.clock = FakeClock()
        self.system = FakeSystem(self.calls)
        self.paths = Paths(
            bridge_config=root / "etc/camerabridge/bridge.yaml",
            mediamtx_env=root / "etc/camerabridge/mediamtx.env",
            nm_connections_dir=root / "etc/NetworkManager/system-connections",
            state_dir=root / "var/lib/camera-provision",
            ntp_dropin=root / "etc/systemd/timesyncd.conf.d/black-relay.conf",
            central_hosts=root / "black-relay" / "central-hosts.nft",
        )
        self.listeners: list[FakeListener] = []
        self.daemon: Provisioner | None = None
        self.responses: list[Response] = []

    def _listen(self, app: Any, ip: str, port: int, interface: str) -> FakeListener:
        self.calls.append("listen")
        listener = FakeListener(app, ip, port, interface)
        self.listeners.append(listener)
        return listener

    def store(self) -> NodeStore:
        return NodeStore(self.paths, chown=lambda path: None)

    def seed_config(self, cfg: NodeConfig | None = None) -> None:
        self.store().save(cfg or node_config())

    def boot(self, *, server_factory: Any = None, **settings: Any) -> Provisioner:
        """Start a daemon process."""
        self.daemon = Provisioner(
            system=self.system,
            paths=self.paths,
            clock=self.clock,
            settings=Settings(
                join_timeout_s=JOIN_TIMEOUT, ap_window_s=AP_WINDOW, ap_cap_s=AP_CAP, **settings
            ),
            server_factory=server_factory or self._listen,
        )
        self.daemon.start()
        return self.daemon

    def tick(self, seconds: float = 0.0) -> Provisioner:
        self.clock.advance(seconds)
        assert self.daemon is not None
        self.daemon.tick()
        return self.daemon

    @property
    def state(self) -> S:
        assert self.daemon is not None
        return self.daemon.state

    def open_listeners(self) -> list[FakeListener]:
        return [listener for listener in self.listeners if not listener.closed]

    def listener(self) -> FakeListener:
        open_now = self.open_listeners()
        assert len(open_now) == 1
        return open_now[0]

    def request(
        self,
        method: str = "GET",
        form: dict[str, str] | None = None,
        *,
        host: str = AP_IP,
        accept: str = BROWSER_ACCEPT,
    ) -> Response:
        """Make a real WSGI call on the open listener's app. Leaves the response unfinished."""
        response = self.request_app(self.listener().app, method, form, host=host, accept=accept)
        self.responses.append(response)
        return response

    @staticmethod
    def request_app(
        app: Any,
        method: str = "GET",
        form: dict[str, str] | None = None,
        *,
        host: str = AP_IP,
        accept: str = BROWSER_ACCEPT,
    ) -> Response:
        body = urlencode(form or {}).encode()
        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ.update(
            REQUEST_METHOD=method,
            QUERY_STRING="",
            PATH_INFO="/",
            HTTP_HOST=host,
            HTTP_ACCEPT=accept,
            CONTENT_LENGTH=str(len(body)),
        )
        environ["wsgi.input"] = io.BytesIO(body)
        if method == "POST":
            environ["CONTENT_TYPE"] = FORM_TYPE
        started: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            started.update(status=status, headers=dict(headers))
            return lambda data: None

        result = validator(app)(environ, start_response)
        text = b"".join(result).decode()
        return Response(started["status"], started["headers"], text, result)

    def submit(self, form: dict[str, str] | None = None) -> Response:
        """POST the form, and let the response finish."""
        response = self.request("POST", valid_form() if form is None else form)
        response.finish()
        return response

    def disk(self) -> dict[str, bytes]:
        """Every file the store owns, to prove a failure changed nothing."""
        files = [
            self.paths.bridge_config,
            self.paths.mediamtx_env,
            self.paths.nm_keyfile,
            self.paths.node_file,
            self.paths.central_hosts,
            self.paths.ntp_dropin,
        ]
        return {str(path): path.read_bytes() for path in files if path.exists()}

    def bridge_yaml(self) -> dict[str, Any]:
        return yaml.safe_load(self.paths.bridge_config.read_text())


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    kit = Node(tmp_path)
    yield kit
    if kit.daemon is not None:
        kit.daemon.shutdown()
    for response in kit.responses:
        response.finish()


def after(calls: list[str], first: str, then: str) -> bool:
    return first in calls and then in calls and calls.index(first) < calls.index(then)


# --- required behaviours ----------------------------------------------------------------


def test_blank_boot_opens_the_ap_with_the_full_portal(node: Node) -> None:
    node.boot()

    assert node.state is S.AP_UP
    assert node.system.count("start_ap") == 1
    listener = node.listener()
    assert (listener.ip, listener.port, listener.interface) == (AP_IP, 80, "wlan0")
    page = node.request().body
    assert 'name="broker_host"' in page and 'name="wifi_psk"' in page


def test_scan_runs_before_the_ap_and_the_listener_after_it(node: Node) -> None:
    node.boot()

    assert after(node.calls, "scan", "start_ap")
    assert after(node.calls, "start_ap", "listen")


def test_the_portal_shows_the_cached_scan_without_rescanning(node: Node) -> None:
    node.boot()
    scans = node.system.count("scan")

    page = node.request().body

    assert 'value="Neighbour"' in page
    assert node.system.count("scan") == scans


def test_the_scan_cache_gives_each_reader_its_own_copy(node: Node) -> None:
    daemon = node.boot()

    daemon._cached_ssids().clear()

    assert daemon._cached_ssids() == ["Site-WiFi", "Neighbour"]


def test_an_empty_scan_keeps_the_last_good_list(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.system.ssids = []

    node.tick(AP_WINDOW)  # to JOINING
    node.tick(JOIN_TIMEOUT)  # to FALLBACK_AP, scanning again
    node.tick()

    assert node.state is S.FALLBACK_AP
    assert 'value="Neighbour"' in node.request().body


def test_a_failed_scan_still_opens_the_ap(node: Node) -> None:
    node.system.failing["scan"] = SystemCommandError("nmcli failed")

    node.boot()

    assert node.state is S.AP_UP
    assert node.listener() is not None


def test_portal_host_is_the_ap_ip_clients_send(node: Node) -> None:
    """Served on port 80, so the Host a browser sends is the bare IP. Anything else is 403."""
    node.boot()

    assert node.request("GET", host=AP_IP).code == 200
    assert node.request("POST", valid_form(), host=f"{AP_IP}:80").code == 403


def test_submit_writes_config_and_profile_then_joins(node: Node) -> None:
    node.boot()

    response = node.request("POST", valid_form())
    assert response.code == 200
    # Saved, but the join must wait for the response to reach the operator.
    assert node.bridge_yaml()["node_id"] == "cam-01"
    keyfile = node.paths.nm_keyfile.read_text()
    assert "ssid=Site-WiFi" in keyfile and f"psk={WIFI_PSK}" in keyfile
    assert node.paths.nm_keyfile.name == "black-relay-uplink.nmconnection"
    assert node.paths.nm_keyfile.stat().st_mode & 0o777 == 0o600
    node.tick()
    assert node.state is S.AP_UP
    assert node.system.count("join") == 0

    response.finish()
    node.tick()

    assert node.state is S.JOINING
    calls = node.calls
    assert after(calls, "stop_ap", "reload") and after(calls, "reload", "join")
    assert node.open_listeners() == []


def test_a_double_tap_submit_is_idempotent(node: Node) -> None:
    node.boot()
    first = node.request("POST", valid_form())
    saved = node.disk()
    second = node.request("POST", valid_form())
    assert (first.code, second.code) == (200, 200)
    assert node.disk() == saved

    first.finish()
    second.finish()
    node.tick()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1


def test_config_received_is_not_fired_for_a_get_finishing_after_a_post(node: Node) -> None:
    """Only the POST's own response going out may start the join."""
    node.boot()
    get = node.request("GET")
    post = node.request("POST", valid_form())

    get.finish()
    node.tick()

    assert node.state is S.AP_UP
    post.finish()
    node.tick()
    assert node.state is S.JOINING


def test_rejected_submit_writes_nothing_and_stays_up(node: Node) -> None:
    node.boot()

    response = node.request("POST", valid_form(node_id="BAD"))
    response.finish()
    node.tick()

    assert response.code == 400
    assert node.disk() == {}
    assert node.state is S.AP_UP
    assert node.listener() is not None


def test_joining_past_the_timeout_falls_back_to_the_ap(node: Node) -> None:
    node.seed_config()
    node.boot()
    assert node.state is S.JOINING

    node.tick(JOIN_TIMEOUT - 1)
    assert node.state is S.JOINING
    assert node.system.count("start_ap") == 0

    node.tick(1)

    assert node.state is S.FALLBACK_AP
    assert node.system.count("start_ap") == 1
    assert after(node.calls, "start_ap", "listen")
    page = node.request().body
    assert 'name="wifi_psk"' in page
    assert 'name="broker_host"' not in page


def test_a_second_join_after_the_ap_window_succeeds(node: Node) -> None:
    """Power cut: the node boots before the router, and rejoins when WiFi returns."""
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP

    node.tick(AP_WINDOW - 1)
    assert node.state is S.FALLBACK_AP
    node.tick(1)
    assert node.state is S.JOINING
    assert node.system.count("join") == 2
    assert node.open_listeners() == []

    # Most of a timeout has passed since boot, but this attempt gets a full one.
    node.system.link_up()
    node.tick(JOIN_TIMEOUT - 1)

    assert node.state is S.PROVISIONED
    assert node.system.count("start_ap") == 1


def test_each_join_attempt_gets_a_full_timeout(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    node.tick(AP_WINDOW)
    assert node.state is S.JOINING

    node.tick(JOIN_TIMEOUT - 1)
    assert node.state is S.JOINING

    node.tick(1)
    assert node.state is S.FALLBACK_AP


def test_association_without_a_lease_is_not_uplink(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.associated = True

    node.tick(1)
    assert node.state is S.JOINING

    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP


def test_provisioned_link_loss_does_not_open_the_ap(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED

    node.system.link_down()
    for _ in range(5):
        node.tick(JOIN_TIMEOUT + AP_WINDOW + AP_CAP)

    assert node.state is S.PROVISIONED
    assert node.system.count("start_ap") == 0
    assert node.listeners == []


def test_restart_on_the_same_boot_restores_state_without_firing_boot(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    node.daemon.shutdown()
    node.system.link_down()
    node.system.button = True  # only read at a real boot
    joins = node.system.count("join")

    node.boot()

    assert node.state is S.PROVISIONED  # BOOT would give JOINING, the button AP_UP
    assert node.system.count("join") == joins
    assert node.system.count("gpio") == 1
    assert node.system.count("start_ap") == 0


def test_restart_in_fallback_reopens_the_wifi_only_portal(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    node.daemon.shutdown()

    node.boot()

    assert node.state is S.FALLBACK_AP
    assert len(node.open_listeners()) == 1
    assert 'name="broker_host"' not in node.request().body


def test_restart_during_joining_keeps_the_original_join_timer(node: Node) -> None:
    """Otherwise a crash loop would keep a node with bad credentials from ever falling back."""
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT - 10)
    node.daemon.shutdown()

    node.boot()
    assert node.state is S.JOINING
    node.tick(10)

    assert node.state is S.FALLBACK_AP




def test_restart_keeps_the_ap_cap(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    for _ in range(6):
        node.tick(AP_WINDOW - 1)
        node.request("GET")
    node.daemon.shutdown()  # 1794 s in

    node.boot()
    assert node.state is S.AP_UP
    node.tick(6)

    assert node.state is S.JOINING


def test_new_boot_id_fires_boot(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    node.daemon.shutdown()
    node.system.link_down()
    joins = node.system.count("join")

    node.system.boot = "boot-2"
    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("join") == joins + 1


def test_corrupt_state_file_fires_boot(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.daemon.shutdown()
    node.paths.state_file.write_text("{not json")

    node.boot()

    assert node.state is S.JOINING


@pytest.mark.parametrize(
    "content",
    [
        '{"boot_id": "boot-1", "state": "nonsense", "entered_at": 1, "window_started": 1}',
        '{"boot_id": "boot-1", "state": "joining", "entered_at": 1}',
        '{"boot_id": "boot-1", "state": "ap_up", "entered_at": 1, "window_started": "x"}',
        '{"boot_id": "boot-1", "state": "joining"}',
        '{"boot_id": "boot-1", "state": "joining", "entered_at": "soon"}',
        '{"boot_id": "boot-1", "state": "unprovisioned", "entered_at": 1, "window_started": 1}',
        "[]",
    ],
)
def test_a_malformed_state_file_fires_boot(node: Node, content: str) -> None:
    node.seed_config()
    node.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    node.paths.state_file.write_text(content)

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("gpio") == 1


def test_button_at_boot_opens_the_ap_even_with_config(node: Node) -> None:
    node.seed_config()
    node.system.button = True

    node.boot()

    assert node.state is S.AP_UP
    assert node.system.count("join") == 0
    assert 'name="broker_host"' in node.request().body  # the full form, not the WiFi repair


def test_ap_window_restarts_on_the_hook_and_stops_at_the_cap(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    elapsed = 0.0

    # Each form GET restarts the 300 s window, up to the 1800 s cap.
    while elapsed + AP_WINDOW - 1 < AP_CAP:
        node.tick(AP_WINDOW - 1)
        elapsed += AP_WINDOW - 1
        assert node.state is S.AP_UP
        node.request("GET")

    node.tick(AP_CAP - elapsed)

    assert node.state is S.JOINING


def test_ap_window_elapses_without_a_form_request(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()

    node.tick(AP_WINDOW - 1)
    assert node.state is S.AP_UP
    node.tick(1)

    assert node.state is S.JOINING


def test_connectivity_probes_do_not_restart_the_window(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()

    node.tick(AP_WINDOW - 1)
    node.request("GET", accept="*/*")  # a phone's probe, no text/html
    node.request("GET", host="connectivitycheck.gstatic.com")  # the redirect
    node.tick(1)

    assert node.state is S.JOINING


def test_a_blank_node_keeps_its_ap_open_past_the_cap(node: Node) -> None:
    node.boot()

    node.tick(AP_CAP + AP_WINDOW)
    node.tick(AP_CAP + AP_WINDOW)

    assert node.state is S.AP_UP
    assert node.system.count("start_ap") == 1
    assert len(node.open_listeners()) == 1


def test_has_config_is_read_at_each_event(node: Node) -> None:
    """A WiFi profile removed while the AP is up: nothing to join with, so the AP stays."""
    node.seed_config()
    node.system.button = True
    node.boot()
    node.paths.nm_keyfile.unlink()

    node.tick(AP_WINDOW)

    assert node.state is S.AP_UP


def test_listener_is_down_outside_ap_states(node: Node) -> None:
    node.seed_config()
    node.boot()
    assert node.state is S.JOINING
    assert node.listeners == []

    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    assert len(node.open_listeners()) == 1

    node.tick(AP_WINDOW)
    assert node.state is S.JOINING
    assert node.open_listeners() == []

    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    assert node.open_listeners() == []
    assert all(listener.was_shut_down.is_set() for listener in node.listeners)


def test_the_listener_stops_before_the_ap(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    listener = node.listener()
    node.calls.clear()
    seen: list[bool] = []
    original = node.system.stop_ap
    node.system.stop_ap = lambda: (seen.append(listener.closed), original())[1]  # type: ignore[method-assign]

    node.tick(AP_WINDOW)

    assert seen == [True]


# --- apply ------------------------------------------------------------------------------


def test_failed_apply_leaves_the_old_config_and_the_portal_up(node: Node) -> None:
    node.seed_config(node_config(hostname="old-name"))
    node.system.button = True
    node.boot()
    before = node.disk()
    node.system.failing["chown"] = PermissionError("nope")

    response = node.request("POST", valid_form(hostname="new-name"))
    response.finish()
    node.tick()

    assert response.code == 400
    assert "could not be saved" in response.body
    assert node.disk() == before
    assert node.state is S.AP_UP
    assert node.listener() is not None
    assert node.store().load().hostname == "old-name"

    # The operator can try again and succeed.
    del node.system.failing["chown"]
    node.submit(valid_form(hostname="new-name"))
    node.tick()
    assert node.state is S.JOINING
    assert node.store().load().hostname == "new-name"


def test_the_bridge_config_is_chowned_to_the_service_user_before_it_lands(node: Node) -> None:
    node.boot()

    node.submit()

    assert len(node.system.chowned) == 1
    assert node.system.chowned[0].parent == node.paths.bridge_config.parent
    assert node.system.chowned[0] != node.paths.bridge_config


def test_the_written_config_loads_through_the_real_bridge(node: Node) -> None:
    node.boot()
    node.submit()

    loaded = bridge_cli.load_config(str(node.paths.bridge_config))

    assert loaded["node_id"] == "cam-01"


def test_fallback_repair_changes_only_the_wifi(node: Node) -> None:
    node.seed_config(node_config(hostname="kept-name", covered_zones=["kept-zone"]))
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    bridge = node.paths.bridge_config.read_bytes()

    # Everything but the WiFi fields must be ignored, whatever a client posts.
    node.submit(
        valid_form(
            wifi_ssid="Fixed-WiFi",
            wifi_psk=NEW_WIFI_PSK,
            broker_host="evil.example",
            node_id="evil",
            hostname="evil",
        )
    )
    node.tick()

    assert node.state is S.JOINING
    assert node.paths.bridge_config.read_bytes() == bridge
    saved = node.store().load()
    assert saved.wifi_ssid == "Fixed-WiFi"
    assert saved.hostname == "kept-name"
    keyfile = node.paths.nm_keyfile.read_text()
    assert f"psk={NEW_WIFI_PSK}" in keyfile and "ssid=Fixed-WiFi" in keyfile
    assert "dhcp-hostname=kept-name" in keyfile


def test_fallback_with_no_config_gets_a_new_full_server(node: Node) -> None:
    """Nothing to repair, so a WiFi-only form would leave the node stuck."""
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    wifi_only = node.listener()
    node.paths.node_file.unlink()
    before = node.disk()

    # The old server has not been replaced yet, and refuses without writing.
    refused = Node.request_app(
        wifi_only.app, "POST", {"wifi_ssid": "Fixed-WiFi", "wifi_psk": NEW_WIFI_PSK}
    )
    refused.finish()
    assert refused.code == 400
    assert node.disk() == before

    node.tick()

    assert node.state is S.FALLBACK_AP
    assert wifi_only.closed
    assert node.listener() is not wifi_only
    assert node.system.count("start_ap") == 1  # the same AP stays up
    assert 'name="broker_host"' in node.request().body

    node.submit()
    node.tick()
    assert node.state is S.JOINING


def test_a_full_save_in_fallback_is_not_cut_off_by_the_mode_change(node: Node) -> None:
    """The save makes a config exist, which would pick WIFI_ONLY before the response is out."""
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    node.paths.node_file.unlink()
    node.tick()
    full = node.listener()
    response = node.request("POST", valid_form())
    assert response.code == 200

    node.tick()

    assert node.listener() is full
    response.finish()
    node.tick()
    assert node.state is S.JOINING


def test_a_saved_config_restores_wifi_only_mode_in_fallback(node: Node) -> None:
    """A config that returns while the full form is up, with nothing submitted to it."""
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    content = node.paths.node_file.read_bytes()
    node.paths.node_file.unlink()
    node.tick()
    assert 'name="broker_host"' in node.request().body

    node.paths.node_file.write_bytes(content)
    node.tick()

    assert 'name="broker_host"' not in node.request().body
    assert len(node.open_listeners()) == 1


def test_an_invalid_wifi_repair_writes_nothing(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    before = node.disk()

    response = node.request("POST", {"wifi_ssid": "Fixed-WiFi", "wifi_psk": "short"})
    response.finish()
    node.tick()

    assert response.code == 400
    assert node.disk() == before
    assert node.state is S.FALLBACK_AP


def test_post_in_flight_at_shutdown_cannot_apply(node: Node) -> None:
    """A real socket: the body is still arriving when the node leaves the AP state."""
    node.seed_config(node_config(hostname="old-name"))
    node.system.button = True
    in_handler = threading.Event()
    bound: list[Any] = []

    def factory(app: Any, ip: str, port: int, interface: str) -> Any:
        def noting(environ: Any, start_response: Any) -> Any:
            in_handler.set()
            return app(environ, start_response)

        server = make_server(noting, "127.0.0.1", 0, "lo")
        bound.append(server)
        return server

    node.boot(server_factory=factory)
    before = node.disk()
    body = urlencode(valid_form(hostname="new-name")).encode()

    with socket.create_connection(bound[0].server_address, timeout=5) as client:
        client.sendall(
            b"POST / HTTP/1.1\r\n"
            + f"Host: {AP_IP}\r\nContent-Type: {FORM_TYPE}\r\n".encode()
            + f"Accept: {BROWSER_ACCEPT}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body[:10]
        )
        assert in_handler.wait(5)

        node.tick(AP_WINDOW)  # the window elapses, so the portal closes
        assert node.state is S.JOINING

        client.sendall(body[10:])
        reply = b""
        while chunk := client.recv(65536):
            reply += chunk

    assert reply.startswith(b"HTTP/1.0 400") or reply.startswith(b"HTTP/1.1 400")
    assert node.disk() == before
    node.tick()
    assert node.state is S.JOINING


def test_response_finishing_after_the_portal_closed_fires_nothing(node: Node) -> None:
    """A stale config_received must not pull a later fallback AP back into JOINING."""
    node.seed_config()
    node.system.button = True
    node.boot()
    response = node.request("POST", valid_form())
    assert response.code == 200

    node.tick(AP_WINDOW)
    assert node.state is S.JOINING
    response.finish()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP

    node.tick()

    assert node.state is S.FALLBACK_AP


def test_a_late_form_request_on_a_closed_portal_does_not_move_the_new_window(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    stale_app = node.listener().app
    node.tick(AP_WINDOW)
    assert node.state is S.JOINING
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP

    node.tick(AP_WINDOW - 1)
    Node.request_app(stale_app, "GET").finish()
    node.tick(1)

    assert node.state is S.JOINING


# --- crash safety -----------------------------------------------------------------------


def test_a_failed_join_is_retried(node: Node) -> None:
    node.seed_config()
    node.system.failing["join"] = SystemCommandError("nmcli failed")

    node.boot()
    assert node.system.count("join") == 1
    node.tick(1)
    assert node.system.count("join") == 2

    del node.system.failing["join"]
    node.tick(1)
    node.tick(1)
    assert node.system.count("join") == 3  # no more once one succeeded


def test_a_failed_ap_stop_is_retried_and_the_join_still_happens(node: Node) -> None:
    node.seed_config()
    node.system.failing["stop_ap"] = SystemCommandError("nmcli failed")
    node.boot()
    assert node.system.count("join") == 0

    del node.system.failing["stop_ap"]
    node.tick(1)

    assert node.system.count("join") == 1


def test_a_failing_join_still_reaches_fallback(node: Node) -> None:
    node.seed_config()
    node.system.failing["join"] = SystemCommandError("nmcli failed")
    node.boot()

    node.tick(JOIN_TIMEOUT)

    assert node.state is S.FALLBACK_AP
    assert len(node.open_listeners()) == 1


def test_a_failing_probe_counts_as_no_uplink(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.failing["wifi_associated"] = SystemCommandError("nmcli failed")

    node.tick(JOIN_TIMEOUT)

    assert node.state is S.FALLBACK_AP


def test_ap_start_failure_is_retried_and_no_listener_opens_early(node: Node) -> None:
    node.system.failing["start_ap"] = SystemCommandError("nmcli failed")
    node.boot()
    assert node.listeners == []

    node.tick(1)
    assert node.system.count("start_ap") == 2
    assert node.listeners == []

    del node.system.failing["start_ap"]
    node.tick(1)
    assert len(node.open_listeners()) == 1
    assert node.system.count("start_ap") == 3


def test_a_listener_that_fails_to_bind_is_retried_without_restarting_the_ap(node: Node) -> None:
    attempts = {"left": 2}

    def flaky(app: Any, ip: str, port: int, interface: str) -> Any:
        if attempts["left"]:
            attempts["left"] -= 1
            raise OSError("cannot assign requested address")
        return node._listen(app, ip, port, interface)

    node.boot(server_factory=flaky)
    assert node.open_listeners() == []
    node.tick(1)
    node.tick(1)

    assert len(node.open_listeners()) == 1
    assert node.system.count("start_ap") == 1


def test_a_dead_portal_thread_is_replaced(node: Node) -> None:
    node.boot()
    first = node.listener()

    first.was_shut_down.set()  # serve_forever returns, as if its thread died
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        node.tick(1)
        if len(node.listeners) == 2:
            break
        time.sleep(0.01)

    assert first.closed
    assert len(node.open_listeners()) == 1
    assert node.listener() is not first


def test_persistent_failure_exits_and_takes_the_ap_down(node: Node) -> None:
    """An AP with no portal is useless: exit nonzero so systemd restarts the daemon."""

    def broken(app: Any, ip: str, port: int, interface: str) -> Any:
        raise OSError("cannot bind")

    node.boot(server_factory=broken, max_consecutive_failures=3)

    with pytest.raises(ProvisionerFailed):
        for _ in range(3):
            node.tick(1)

    node.daemon.shutdown()
    assert node.system.count("stop_ap") >= 1
    assert after(node.calls[::-1], "stop_ap", "start_ap")  # stopped after it started


def test_one_recovery_resets_the_failure_count(node: Node) -> None:
    node.seed_config()
    node.system.failing["join"] = SystemCommandError("nmcli failed")
    node.boot(max_consecutive_failures=3)
    node.tick(1)
    node.tick(1)  # two failed ticks in a row
    del node.system.failing["join"]
    node.tick(1)  # clean

    node.system.failing["start_ap"] = SystemCommandError("nmcli failed")
    node.tick(JOIN_TIMEOUT)
    node.tick(1)  # two more, which would be four without the reset

    assert node.state is S.FALLBACK_AP


def test_shutdown_stops_the_portal_and_the_ap(node: Node) -> None:
    node.boot()
    listener = node.listener()

    node.daemon.shutdown()

    assert listener.closed
    assert node.system.count("stop_ap") == 1
    node.daemon.shutdown()  # idempotent
    assert node.system.count("stop_ap") == 1


def test_shutdown_survives_a_failing_ap_stop(node: Node) -> None:
    node.boot()
    node.system.failing["stop_ap"] = SystemCommandError("nmcli failed")

    node.daemon.shutdown()

    assert node.open_listeners() == []


def test_startup_recovers_an_interrupted_apply_and_clears_stale_temp_files(node: Node) -> None:
    node.seed_config()
    stale = node.paths.bridge_config.parent / ".bridge.yaml.abc123.tmp"
    stale.write_text("half a config")

    node.boot()

    assert not stale.exists()


def test_start_fails_loudly_without_a_boot_id(node: Node) -> None:
    node.system.failing["boot_id"] = OSError("no proc")

    with pytest.raises(OSError):
        node.boot()


def test_a_failing_gpio_read_counts_as_not_held(node: Node) -> None:
    node.seed_config()
    node.system.failing["gpio"] = SystemCommandError("gpiofind failed")

    node.boot()

    assert node.state is S.JOINING


def test_a_state_file_write_failure_does_not_stop_the_join(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    node.seed_config()
    monkeypatch.setattr(
        "cameraprovision.daemon._write_private_atomic",
        lambda path, text: (_ for _ in ()).throw(OSError("disk full")),
    )

    node.boot()

    assert node.system.count("join") == 1


# --- secrets ----------------------------------------------------------------------------


def test_secrets_stay_out_of_logs_and_of_files_that_do_not_need_them(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    node.boot()
    node.submit()
    node.tick()
    node.system.link_up()
    node.tick(1)

    for secret in SECRETS:
        assert secret not in caplog.text
        for path in (node.paths.state_file, node.paths.node_file, node.paths.mediamtx_env):
            assert secret not in path.read_text()


# --- end to end -------------------------------------------------------------------------


def test_end_to_end_blank_boot_to_provisioned(node: Node) -> None:
    """A whole first install, with real WSGI calls and only the OS faked."""
    # First boot, no config: the AP comes up and a phone finds the form.
    node.boot()
    assert node.state is S.AP_UP
    probe = node.request("GET", host="connectivitycheck.gstatic.com", accept="*/*")
    assert probe.code == 302 and probe.headers["Location"] == f"http://{AP_IP}/"
    form_page = node.request("GET")
    assert form_page.code == 200 and 'value="Site-WiFi"' in form_page.body

    # A typo gets a page back, and nothing is written.
    bad = node.request("POST", valid_form(node_id="NOT VALID"))
    assert bad.code == 400 and node.disk() == {}
    assert WIFI_PSK not in bad.body

    # The real submission.
    saved = node.request("POST", valid_form())
    assert saved.code == 200 and "Settings saved" in saved.body
    saved.finish()

    # The wrong password: joining times out and the setup AP returns, WiFi fields only.
    node.tick()
    assert node.state is S.JOINING and node.open_listeners() == []
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    repair_page = node.request("GET")
    assert 'name="broker_host"' not in repair_page.body
    fixed = node.request("POST", {"wifi_ssid": "Site-WiFi", "wifi_psk": NEW_WIFI_PSK})
    assert fixed.code == 200
    fixed.finish()

    # This time it joins.
    node.tick()
    assert node.state is S.JOINING
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    assert node.open_listeners() == []

    # Everything is where the rest of the node expects it.
    assert f"psk={NEW_WIFI_PSK}" in node.paths.nm_keyfile.read_text()
    assert node.bridge_yaml()["broker"]["password"] == BROKER_PASSWORD
    assert bridge_cli.load_config(str(node.paths.bridge_config))["node_id"] == "cam-01"
    assert node.paths.mediamtx_env.read_text() == "MEDIAMTX_ADDRESS=10.0.0.2:8554\n"
    assert node.paths.mediamtx_env.stat().st_mode & 0o777 == 0o644
    assert node.paths.bridge_config.stat().st_mode & 0o777 == 0o600

    # Daemon restart, then a reboot with the router down: it rejoins alone.
    node.daemon.shutdown()
    node.boot()
    assert node.state is S.PROVISIONED
    node.daemon.shutdown()
    node.system.boot = "boot-2"
    node.system.link_down()
    node.boot()
    assert node.state is S.JOINING
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    node.tick(AP_WINDOW)
    assert node.state is S.JOINING
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED


# --- review findings --------------------------------------------------------------------

BAD_BRIDGE_EDITS = {
    "negative status interval": lambda text: text.replace(
        "status_interval_s: 30", "status_interval_s: -1"
    ),
    "unknown broker key": lambda text: text.replace("broker:\n", "broker:\n  surprise: 1\n"),
    "missing cue window": lambda text: re.sub(r"cue_window_s: .*\n", "", text),
}


@pytest.mark.parametrize("edit", BAD_BRIDGE_EDITS.values(), ids=BAD_BRIDGE_EDITS.keys())
def test_c1_a_damaged_bridge_config_still_joins_and_never_opens_the_full_form(
    node: Node, caplog: pytest.LogCaptureFixture, edit: Callable[[str], str]
) -> None:
    node.seed_config()
    original = node.paths.bridge_config.read_text()
    edited = edit(original)
    assert edited != original
    node.paths.bridge_config.write_text(edited)

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1
    assert node.system.count("start_ap") == 0
    assert "bridge config" in caplog.text
    assert json.loads(node.paths.state_file.read_text())["bridge_config_valid"] is False

    node.system.link_up()
    for _ in range(120):  # two hours of fake time
        node.tick(60)

    assert node.state is S.PROVISIONED
    assert node.system.count("start_ap") == 0
    assert node.listeners == []


@pytest.mark.parametrize("edit", BAD_BRIDGE_EDITS.values(), ids=BAD_BRIDGE_EDITS.keys())
def test_c1_a_damaged_bridge_config_offers_only_the_wifi_repair(
    node: Node, edit: Callable[[str], str]
) -> None:
    node.seed_config()
    node.paths.bridge_config.write_text(edit(node.paths.bridge_config.read_text()))
    node.boot()

    node.tick(JOIN_TIMEOUT)

    assert node.state is S.FALLBACK_AP
    assert 'name="broker_host"' not in node.request().body
    node.tick(AP_WINDOW)
    assert node.state is S.JOINING


def test_c1_a_missing_bridge_config_still_joins(node: Node) -> None:
    node.seed_config()
    node.paths.bridge_config.unlink()

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1


@pytest.mark.parametrize("content", ["{not json", "[]", "{}", '{"hostname": ""}', '{"hostname": 7}'])
def test_c1_a_node_file_without_a_hostname_cannot_attempt_a_join(
    node: Node, content: str
) -> None:
    node.seed_config()
    node.paths.node_file.write_text(content)

    node.boot()

    assert node.state is S.AP_UP
    assert 'name="broker_host"' in node.request().body


def test_c1_a_valid_config_reports_as_valid(node: Node) -> None:
    node.seed_config()

    node.boot()

    assert json.loads(node.paths.state_file.read_text())["bridge_config_valid"] is True


@pytest.mark.parametrize("journal", ["", "{not json", "[]", '{"a": 1}', "[1]"])
def test_i1_an_unreadable_journal_does_not_stop_the_daemon(node: Node, journal: str) -> None:
    node.seed_config()
    node.paths.journal.write_text(journal)

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1
    assert not node.paths.journal.exists()


def read_only_state_dir(node: Node) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    node.paths.state_dir.chmod(0o500)


@pytest.fixture
def restore_state_dir(node: Node) -> Iterator[None]:
    yield
    node.paths.state_dir.chmod(0o700)


def test_i2_a_read_only_state_dir_still_reaches_fallback_and_never_exits(
    node: Node, caplog: pytest.LogCaptureFixture, restore_state_dir: None
) -> None:
    """A wrong PSK must reach the AP with the disk full or read-only."""
    node.seed_config()
    read_only_state_dir(node)

    node.boot()
    for _ in range(JOIN_TICKS_PAST_EXIT_LIMIT):
        node.tick(1)
        if node.state is not S.JOINING:
            break
    assert node.state is S.JOINING or node.state is S.FALLBACK_AP
    node.tick(JOIN_TIMEOUT)

    assert node.state is S.FALLBACK_AP
    for _ in range(70):  # past the 60-tick exit limit
        node.tick(1)
    assert node.system.count("gpio") == 1  # one boot, fired once
    assert node.system.count("join") == 1
    assert caplog.text.count("saving state failed") == 1  # not once per tick


def test_i2_a_persist_failure_alone_never_counts_as_a_failed_tick(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    node.seed_config()
    monkeypatch.setattr(
        "cameraprovision.daemon._write_private_atomic",
        lambda path, text: (_ for _ in ()).throw(OSError("disk full")),
    )
    node.boot(max_consecutive_failures=3)

    for _ in range(10):
        node.tick(1)

    assert node.state is S.JOINING


def test_i2_persisting_resumes_once_the_disk_recovers(
    node: Node, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    node.seed_config()
    with monkeypatch.context() as patch:
        patch.setattr(
            "cameraprovision.daemon._write_private_atomic",
            lambda path, text: (_ for _ in ()).throw(OSError("disk full")),
        )
        node.boot()
        node.tick(1)
        assert not node.paths.state_file.exists()

    node.tick(1)

    assert json.loads(node.paths.state_file.read_text())["state"] == "joining"


def test_i3_a_restart_does_not_restart_the_ap_window(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.tick(200)
    node.daemon.shutdown()

    node.boot()
    node.tick(99)
    assert node.state is S.AP_UP
    node.tick(1)

    assert node.state is S.JOINING


def test_i3_a_form_request_survives_a_restart(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.tick(200)
    node.request("GET")
    node.tick()
    node.daemon.shutdown()

    node.boot()
    node.tick(299)
    assert node.state is S.AP_UP
    node.tick(1)

    assert node.state is S.JOINING


def test_i3_a_clean_stop_saves_a_form_request_from_the_last_tick(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.tick(200)
    node.request("GET")
    node.daemon.shutdown()

    node.boot()
    node.tick(299)

    assert node.state is S.AP_UP


def test_i3_failing_to_bind_leaves_after_one_window_not_the_cap(node: Node) -> None:
    def broken(app: Any, ip: str, port: int, interface: str) -> Any:
        raise OSError("cannot bind")

    node.seed_config()
    node.system.button = True
    node.boot(server_factory=broken, max_consecutive_failures=100000)

    for _ in range(29):
        node.tick(10)
        assert node.state is S.AP_UP
    node.tick(10)

    assert node.state is S.JOINING


def test_i3_a_mode_rebuild_does_not_move_the_window(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    content = node.paths.node_file.read_bytes()

    node.tick(200)
    node.paths.node_file.unlink()
    node.tick()  # rebuilds as FULL
    node.paths.node_file.write_bytes(content)
    node.tick()  # rebuilds as WIFI_ONLY
    assert len(node.listeners) == 3
    node.tick(99)
    assert node.state is S.FALLBACK_AP
    node.tick(1)

    assert node.state is S.JOINING


def test_i3_a_replaced_portal_thread_does_not_move_the_window(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.tick(200)
    first = node.listener()
    first.was_shut_down.set()
    deadline = time.monotonic() + 5
    while first.closed is False and time.monotonic() < deadline:
        node.tick()
        time.sleep(0.001)
    assert first.closed

    node.tick(100)

    assert node.state is S.JOINING


def test_i3_the_window_starts_at_the_first_open_after_the_ap_starts(node: Node) -> None:
    """Bringing the AP up can take most of a minute, which must not eat the window."""
    node.seed_config()
    node.system.button = True
    node.system.on_start_ap = lambda: node.clock.advance(40)
    node.boot()

    node.tick(299)
    assert node.state is S.AP_UP
    node.tick(1)

    assert node.state is S.JOINING


def test_i5_entering_joining_sets_the_hostname_not_apply(node: Node) -> None:
    node.boot()
    response = node.request("POST", valid_form())
    assert node.system.hostnames == []

    response.finish()
    node.tick()

    assert node.state is S.JOINING
    assert node.system.hostnames == ["edge-7"]
    node.tick(1)
    assert node.system.hostnames == ["edge-7"]  # once per entry


def test_i5_a_power_cut_after_save_still_applies_the_hostname(node: Node) -> None:
    node.boot()
    node.request("POST", valid_form())
    assert node.system.hostnames == []
    node.daemon.shutdown()  # the response never finished: the power went

    node.system.boot = "boot-2"
    node.boot()

    assert node.state is S.JOINING
    assert node.system.hostnames == ["edge-7"]


def test_i5_a_restart_during_joining_applies_it_again(node: Node) -> None:
    node.seed_config()
    node.system.failing["set_hostname"] = SystemCommandError("hostnamectl failed")
    node.boot()
    node.daemon.shutdown()
    del node.system.failing["set_hostname"]
    node.system.hostnames.clear()

    node.boot()

    assert node.system.hostnames == ["edge-7"]


def test_i5_hostname_failure_retries_without_blocking_or_counting(node: Node) -> None:
    node.seed_config()
    node.system.failing["set_hostname"] = SystemCommandError("hostnamectl failed")
    node.boot(max_consecutive_failures=3)

    assert node.system.count("join") == 1
    attempts = node.system.count("set_hostname")
    assert attempts == 1

    node.tick(1)
    assert node.system.count("set_hostname") == attempts  # backing off
    for _ in range(6):
        node.tick(30)
    assert node.system.count("set_hostname") > attempts
    assert node.state in (S.JOINING, S.FALLBACK_AP)


def test_i5_hostname_retry_continues_after_the_link_comes_up(node: Node) -> None:
    node.seed_config()
    node.system.failing["set_hostname"] = SystemCommandError("hostnamectl failed")
    node.boot()
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    del node.system.failing["set_hostname"]
    failed_attempts = node.system.count("set_hostname")

    node.tick(30)
    assert node.system.count("set_hostname") == failed_attempts + 1
    node.tick(300)

    assert node.system.count("set_hostname") == failed_attempts + 1  # applied, so finished


def test_i5_an_option_like_hostname_never_reaches_the_command(node: Node) -> None:
    """node.json can be hand-edited, and a leading hyphen would read as a flag."""
    node.seed_config()
    data = json.loads(node.paths.node_file.read_text())
    data["hostname"] = "--static"
    node.paths.node_file.write_text(json.dumps(data))

    node.boot()

    assert node.state is S.JOINING
    assert node.system.hostnames == []
    assert node.system.count("join") == 1


def test_i6_an_ap_that_dies_is_restarted_on_a_blank_node(node: Node) -> None:
    node.boot()
    first = node.listener()
    node.system.ap_alive = False

    node.tick(AP_CHECK)

    assert node.system.count("start_ap") == 2
    assert node.system.ap_alive
    assert first.closed
    assert node.listener() is not first
    assert after(node.calls[node.calls.index("ap_active"):], "start_ap", "listen")


def test_i6_a_live_ap_is_left_alone(node: Node) -> None:
    node.boot()
    first = node.listener()

    for _ in range(5):
        node.tick(AP_CHECK)

    assert node.system.count("start_ap") == 1
    assert node.listener() is first
    assert node.system.count("ap_active") == 5


def test_i6_the_check_is_periodic(node: Node) -> None:
    node.boot()

    node.tick(AP_CHECK - 1)
    assert node.system.count("ap_active") == 0
    node.tick(1)

    assert node.system.count("ap_active") == 1


def test_i6_a_failing_check_does_not_restart_the_ap(node: Node) -> None:
    node.boot()
    node.system.failing["ap_active"] = SystemCommandError("nmcli failed")

    node.tick(AP_CHECK)

    assert node.system.count("start_ap") == 1


def test_i6_a_restart_after_the_ap_died_keeps_the_window(node: Node) -> None:
    node.seed_config()
    node.system.button = True
    node.boot()
    node.tick(200)
    node.system.ap_alive = False
    node.tick()  # not due yet
    node.tick(AP_CHECK)
    assert node.system.count("start_ap") == 2

    node.tick(100 - AP_CHECK)

    assert node.state is S.JOINING


def test_i7_a_lost_link_reissues_the_join_every_three_minutes_and_never_opens_the_ap(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED
    node.system.link_down()
    joins = node.system.count("join")

    node.tick(179)
    assert node.system.count("join") == joins
    node.tick(1)
    assert node.system.count("join") == joins + 1
    node.tick(179)
    assert node.system.count("join") == joins + 1
    node.tick(1)
    assert node.system.count("join") == joins + 2

    assert node.state is S.PROVISIONED
    assert node.system.count("start_ap") == 0
    assert node.listeners == []


def test_i7_a_healthy_link_gets_no_join_calls(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    joins = node.system.count("join")

    for _ in range(10):
        node.tick(60)

    assert node.system.count("join") == joins


def test_i7_a_failing_rejoin_is_not_a_daemon_failure(node: Node) -> None:
    node.seed_config()
    node.boot(max_consecutive_failures=3)
    node.system.link_up()
    node.tick(1)
    node.system.link_down()
    node.system.failing["join"] = SystemCommandError("nmcli failed")

    for _ in range(10):
        node.tick(60)

    assert node.state is S.PROVISIONED


def test_i7_a_restart_while_provisioned_keeps_rejoining(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.tick(1)
    node.daemon.shutdown()
    node.system.link_down()
    joins = node.system.count("join")

    node.boot()
    node.tick(180)

    assert node.system.count("join") == joins + 1


def test_m1_a_link_already_up_is_not_reactivated(node: Node) -> None:
    node.seed_config()
    node.system.link_up()

    node.boot()
    assert node.system.count("join") == 0
    node.tick(1)

    assert node.state is S.PROVISIONED
    assert node.system.count("join") == 0


def test_m6_a_button_read_failure_logs_at_error(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    node.seed_config()
    node.system.failing["gpio"] = SystemCommandError("no libgpiod")

    node.boot()

    assert any(
        record.levelno == logging.ERROR and "setup button" in record.message
        for record in caplog.records
    )


# --- run loop and CLI -------------------------------------------------------------------


def test_run_loops_until_stopped_then_shuts_down(node: Node) -> None:
    daemon = Provisioner(
        system=node.system,
        paths=node.paths,
        clock=node.clock,
        settings=Settings(tick_s=0.001),
        server_factory=node._listen,
    )
    stop = threading.Event()
    thread = threading.Thread(target=daemon.run, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 5
    while not node.listeners and time.monotonic() < deadline:
        time.sleep(0.001)

    stop.set()
    thread.join(5)

    assert not thread.is_alive()
    assert node.listeners[0].closed
    assert node.system.count("stop_ap") == 1


def test_run_shuts_down_when_a_tick_fails_for_good(node: Node) -> None:
    def broken(app: Any, ip: str, port: int, interface: str) -> Any:
        raise OSError("cannot bind")

    daemon = Provisioner(
        system=node.system,
        paths=node.paths,
        clock=node.clock,
        settings=Settings(tick_s=0.001, max_consecutive_failures=2),
        server_factory=broken,
    )

    with pytest.raises(ProvisionerFailed):
        daemon.run(threading.Event())

    assert node.system.count("stop_ap") == 1


def run_cli(
    monkeypatch: pytest.MonkeyPatch, run: Callable[[Any, threading.Event], None], *args: str
) -> tuple[int, list[Provisioner]]:
    made: list[Provisioner] = []

    def fake_run(self: Provisioner, stop: threading.Event) -> None:
        made.append(self)
        run(self, stop)

    monkeypatch.setattr(Provisioner, "run", fake_run)
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    return cli.main(["run", *args]), made


def test_cli_run_exits_zero_on_a_clean_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    code, made = run_cli(monkeypatch, lambda daemon, stop: None)

    assert code == 0
    assert len(made) == 1


def test_cli_run_exits_nonzero_on_an_unrecoverable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(daemon: Any, stop: threading.Event) -> None:
        raise ProvisionerFailed("gave up")

    code, _ = run_cli(monkeypatch, fail)

    assert code == 1


def test_cli_run_exits_nonzero_without_logging_an_exception_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(daemon: Any, stop: threading.Event) -> None:
        raise RuntimeError(f"oops {WIFI_PSK}")

    code, _ = run_cli(monkeypatch, fail)

    assert code == 1
    assert "RuntimeError" in caplog.text
    assert WIFI_PSK not in caplog.text


def test_cli_passes_paths_and_settings_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, made = run_cli(
        monkeypatch,
        lambda daemon, stop: None,
        "--bridge-config",
        str(tmp_path / "b.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
        "--ap-ip",
        "10.9.9.1",
        "--join-timeout",
        "77",
        "--ap-window",
        "88",
    )

    assert code == 0
    assert made[0].paths.bridge_config == tmp_path / "b.yaml"
    assert made[0].paths.state_dir == tmp_path / "state"
    assert made[0].settings.ap_ip == "10.9.9.1"
    assert made[0].settings.join_timeout_s == 77
    assert made[0].settings.ap_window_s == 88


def test_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code != 0


# --- M7: a failed scan is retried once ----------------------------------------------


def test_m7_a_failed_scan_is_retried_and_its_result_used(node: Node) -> None:
    node.system.scan_failures = 1

    node.boot()

    assert node.system.count("scan") == 2
    assert 'value="Neighbour"' in node.request().body


def test_m7_a_scan_that_fails_twice_is_not_tried_a_third_time(node: Node) -> None:
    node.system.scan_failures = 2

    node.boot()
    node.tick()

    assert node.system.count("scan") == 2
    assert node.state is S.AP_UP
    assert node.system.count("start_ap") == 1
    assert 'value="Neighbour"' not in node.request().body


def test_m7_a_good_scan_runs_once(node: Node) -> None:
    node.boot()
    node.tick()

    assert node.system.count("scan") == 1


def test_m7_a_retry_happens_before_the_ap_starts(node: Node) -> None:
    node.system.scan_failures = 1

    node.boot()

    last_scan = max(i for i, call in enumerate(node.calls) if call == "scan")
    assert last_scan < node.calls.index("start_ap")


# --- I1: the identity a blank node's form starts with ------------------------------


def field_value(body: str, name: str) -> str | None:
    found = re.search(rf'name="{name}"[^>]*? value="([^"]*)"', body)
    return found.group(1) if found else None


def test_i1_a_blank_nodes_form_is_filled_from_the_serial(node: Node) -> None:
    node.boot()

    body = node.request().body

    assert field_value(body, "node_id") == "cam-cdef12"
    assert field_value(body, "hostname") == "cam-cdef12"
    assert field_value(body, "frigate_base_url") == "https://cam-cdef12.local:8971"


def test_i1_two_kits_get_different_defaults(tmp_path: Path) -> None:
    first, second = Node(tmp_path / "a"), Node(tmp_path / "b")
    second.system.serial = "10000000ABC00001"
    try:
        first.boot()
        second.boot()

        assert field_value(first.request().body, "node_id") != field_value(
            second.request().body, "node_id"
        )
    finally:
        for kit in (first, second):
            assert kit.daemon is not None
            kit.daemon.shutdown()
            for response in kit.responses:
                response.finish()


def test_i1_the_defaults_can_be_changed_before_saving(node: Node) -> None:
    node.boot()

    node.submit(valid_form(node_id="gate-1", hostname="gate", frigate_base_url="http://gate:5000"))
    node.tick()

    assert node.bridge_yaml()["node_id"] == "gate-1"
    assert node.bridge_yaml()["frigate"]["base_url"] == "http://gate:5000"
    assert node.store().hostname() == "gate"


def test_i1_an_unreadable_serial_leaves_the_fields_blank_and_is_logged(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    node.system.failing["read_serial"] = OSError("no such file")

    node.boot()
    body = node.request().body

    assert node.state is S.AP_UP
    assert field_value(body, "node_id") == ""
    assert field_value(body, "hostname") == ""
    assert field_value(body, "frigate_base_url") == ""
    assert field_value(body, "broker_port") == "1883"
    assert "reading the serial failed" in caplog.text and "OSError" in caplog.text


def test_i1_a_malformed_serial_leaves_the_fields_blank_and_is_logged(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    node.system.failing["read_serial"] = ValueError("unexpected serial format")

    node.boot()

    assert field_value(node.request().body, "node_id") == ""
    assert "reading the serial failed" in caplog.text


def test_i1_a_fallback_with_no_config_also_gets_the_defaults(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    node.paths.nm_keyfile.unlink()
    node.tick()

    assert field_value(node.request().body, "node_id") == "cam-cdef12"


# --- M5: a button re-provision starts from what is saved ---------------------------


def test_m5_the_button_form_shows_the_saved_settings_and_no_secret(node: Node) -> None:
    node.seed_config(
        node_config(
            node_id="cam-07",
            hostname="edge-7",
        )
    )
    node.system.button = True
    node.boot()

    body = node.request().body

    assert field_value(body, "wifi_ssid") == "Site-WiFi"
    assert field_value(body, "node_id") == "cam-07"
    assert field_value(body, "hostname") == "edge-7"
    assert field_value(body, "broker_host") == "10.0.0.2"
    assert field_value(body, "broker_username") == "camera-cam-01"
    assert field_value(body, "covered_zones") == "perimeter-north"
    assert field_value(body, "mediamtx_address") == "10.0.0.2:8554"
    assert all(secret not in body for secret in SECRETS)
    assert "read_serial" not in node.calls


def test_m5_the_saved_form_cannot_be_resubmitted_without_its_secrets(node: Node) -> None:
    node.seed_config()
    before = node.disk()
    node.system.button = True
    node.boot()

    response = node.submit(
        valid_form(wifi_psk="", broker_password="")
    )

    assert response.code == 400
    assert "wifi_psk" in response.body and "broker.password" in response.body
    assert node.disk() == before


def test_m5_a_wifi_only_portal_prefills_nothing(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    node.tick()

    response = node.request()

    assert 'name="wifi_ssid" list="scanned-ssids" value=""' in response.body
    assert 'name="node_id"' not in response.body


def test_m5_a_damaged_config_prefills_nothing_rather_than_a_new_identity(node: Node) -> None:
    node.seed_config()
    node.paths.bridge_config.write_text("not: [valid")
    node.system.button = True
    node.boot()

    body = node.request().body

    assert field_value(body, "node_id") == ""
    assert "read_serial" not in node.calls


# --- owner decision: NTP from the portal -------------------------------------------


def test_submit_writes_the_drop_in_and_restarts_timesyncd_on_joining(node: Node) -> None:
    node.boot()
    assert node.system.count("restart_timesync") == 0

    node.submit()
    node.tick()

    assert node.paths.ntp_dropin.read_text() == "[Time]\nNTP=10.0.0.2\n"
    assert node.state is S.JOINING
    assert node.system.count("restart_timesync") == 1
    assert after(node.calls, "restart_timesync", "join")


def test_a_reboot_with_a_saved_config_restarts_timesyncd_once(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.tick()
    node.tick()

    assert node.state is S.JOINING
    assert node.system.count("restart_timesync") == 1


def test_a_failed_timesyncd_restart_does_not_block_the_join_and_is_retried(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    node.seed_config()
    node.system.failing["restart_timesync"] = SystemCommandError("systemctl failed")

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1
    assert "restarting systemd-timesyncd failed" in caplog.text

    node.system.link_up()
    node.tick()
    assert node.state is S.PROVISIONED
    assert node.system.count("start_ap") == 0

    # Spaced out, and carried on after the link came up.
    before = node.system.count("restart_timesync")
    node.tick(1)
    assert node.system.count("restart_timesync") == before
    node.tick(30)
    assert node.system.count("restart_timesync") == before + 1

    del node.system.failing["restart_timesync"]
    node.tick(30)
    settled = node.system.count("restart_timesync")
    node.tick(30)
    assert node.system.count("restart_timesync") == settled


def test_a_failing_timesyncd_restart_never_counts_as_a_daemon_failure(node: Node) -> None:
    node.seed_config()
    node.system.failing["restart_timesync"] = SystemCommandError("systemctl failed")
    node.system.link_up()
    node.boot()

    for _ in range(JOIN_TICKS_PAST_EXIT_LIMIT):
        node.tick(30)

    assert node.state is S.PROVISIONED


def test_a_failed_timesyncd_restart_does_not_itself_open_the_ap(node: Node) -> None:
    node.seed_config()
    node.system.failing["restart_timesync"] = SystemCommandError("systemctl failed")
    node.boot()

    node.tick(JOIN_TIMEOUT - 1)

    assert node.state is S.JOINING
    assert node.system.count("start_ap") == 0


def test_a_daemon_restart_during_joining_still_owes_the_timesyncd_restart(node: Node) -> None:
    node.seed_config()
    node.system.failing["restart_timesync"] = SystemCommandError("systemctl failed")
    node.boot()
    node.daemon.shutdown()
    del node.system.failing["restart_timesync"]
    before = node.system.count("restart_timesync")

    node.boot()

    assert node.state is S.JOINING
    assert node.system.count("restart_timesync") == before + 1


def test_a_wifi_repair_does_not_change_the_ntp_dropin(node: Node) -> None:
    node.seed_config(node_config(ntp_server="time.site.local"))
    before = node.paths.ntp_dropin.read_bytes()
    node.boot()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    node.tick()
    assert 'name="ntp_server"' not in node.request().body

    response = node.submit({"wifi_ssid": "Site-WiFi", "wifi_psk": NEW_WIFI_PSK, "ntp_server": "evil.example"})

    assert response.code == 200
    assert node.paths.ntp_dropin.read_bytes() == before


def test_a_button_move_to_another_broker_moves_a_defaulted_ntp_server(node: Node) -> None:
    node.seed_config()
    assert node.paths.ntp_dropin.read_text() == "[Time]\nNTP=10.0.0.2\n"
    node.system.button = True
    node.boot()
    assert field_value(node.request().body, "ntp_server") == "10.0.0.2"

    saved = node.submit(valid_form(broker_host="10.0.0.9", ntp_server="10.0.0.2"))

    assert "Time server: 10.0.0.9" in saved.body
    assert node.paths.ntp_dropin.read_text() == "[Time]\nNTP=10.0.0.9\n"


def test_a_button_move_to_another_broker_keeps_an_explicit_ntp_server(node: Node) -> None:
    node.seed_config(node_config(ntp_server="time.site.local"))
    node.system.button = True
    node.boot()
    assert field_value(node.request().body, "ntp_server") == "time.site.local"

    node.submit(valid_form(broker_host="10.0.0.9", ntp_server="time.site.local"))

    assert node.paths.ntp_dropin.read_text() == "[Time]\nNTP=time.site.local\n"


# --- central-only access and no node Tailscale --------------------------------------


def test_daemon_never_calls_tailscale(node: Node) -> None:
    node.boot()
    node.submit(valid_form(vpn_requested="on", tailscale_auth_key="tskey-auth-s3cret"))
    node.tick()
    node.system.link_up()
    node.tick(1)
    node.tick(60)

    assert node.state is S.PROVISIONED
    assert not any("tailscale" in call for call in node.system.calls)
    assert not (node.paths.state_dir / "tailscale.key").exists()
    assert "tskey-auth-s3cret" not in "".join(
        data.decode(errors="replace") for data in node.disk().values()
    )


def test_a_start_with_an_old_tailscale_key_deletes_it(node: Node) -> None:
    node.seed_config()
    key = node.paths.state_dir / "tailscale.key"
    key.write_text("tskey-auth-s3cret")

    node.boot()

    assert not key.exists()


def test_save_reloads_nftables_and_a_failure_is_logged_and_retried(
    node: Node, caplog: pytest.LogCaptureFixture
) -> None:
    node.boot()
    start_reloads = node.system.count("reload_firewall")
    node.system.failing["reload_firewall"] = SystemCommandError("systemctl exited with status 1")

    node.submit()
    with caplog.at_level(logging.ERROR):
        node.tick()

    assert node.state is S.JOINING
    assert node.system.count("reload_firewall") == start_reloads + 1
    assert "reloading the firewall failed" in caplog.text

    node.tick(1)
    assert node.system.count("reload_firewall") == start_reloads + 1  # waiting
    node.tick(30)
    assert node.system.count("reload_firewall") == start_reloads + 2

    del node.system.failing["reload_firewall"]
    node.tick(30)
    assert node.system.count("reload_firewall") == start_reloads + 3
    node.tick(60)
    assert node.system.count("reload_firewall") == start_reloads + 3  # done


def test_a_failing_firewall_reload_never_blocks_the_join(node: Node) -> None:
    node.boot()
    node.system.failing["reload_firewall"] = SystemCommandError("systemctl exited with status 1")

    node.submit()
    node.tick()

    assert node.state is S.JOINING
    assert node.system.count("join") == 1
    node.system.link_up()
    node.tick(1)
    assert node.state is S.PROVISIONED


def test_a_failing_firewall_reload_never_counts_as_a_daemon_failure(node: Node) -> None:
    node.seed_config()
    node.boot()
    node.system.link_up()
    node.system.failing["reload_firewall"] = SystemCommandError("systemctl exited with status 1")

    for _ in range(JOIN_TICKS_PAST_EXIT_LIMIT + 5):
        node.tick(1)

    assert node.state is S.PROVISIONED


def test_a_firewall_reload_is_retried_while_the_ap_is_up(node: Node) -> None:
    node.boot()
    node.submit()
    node.system.failing["reload_firewall"] = SystemCommandError("systemctl exited with status 1")
    node.tick()
    node.tick(JOIN_TIMEOUT)
    assert node.state is S.FALLBACK_AP
    del node.system.failing["reload_firewall"]
    before = node.system.count("reload_firewall")

    node.tick(30)

    assert node.system.count("reload_firewall") == before + 1


def test_every_start_loads_the_saved_set_once(node: Node) -> None:
    """Settling an interrupted save can change the set after nftables loaded it at boot."""
    node.seed_config()

    node.boot()

    assert node.system.count("reload_firewall") == 1


def test_a_config_that_no_longer_passes_a_save_is_not_reported_damaged(node: Node) -> None:
    node.seed_config()
    bridge = node.bridge_yaml()
    bridge["broker"]["host"] = "broker.lan"
    node.paths.bridge_config.write_text(yaml.safe_dump(bridge))

    node.boot()

    assert json.loads(node.paths.state_file.read_text())["bridge_config_valid"] is True


def test_upside_down_checkbox_is_saved_and_shown_on_reprovision(node: Node) -> None:
    node.boot()
    node.submit(valid_form(camera_upside_down="on"))
    assert json.loads(node.paths.node_file.read_text())["camera_upside_down"] is True
    node.tick()
    node.daemon.shutdown()

    node.system.button = True
    node.system.boot = "boot-2"
    node.boot()

    assert node.state is S.AP_UP
    assert re.search(r'name="camera_upside_down"[^>]* checked', node.request().body)
