import asyncio
import concurrent.futures
import logging

import httpx
import pytest
from conftest import BROKER_PASSWORD, STATUS, heartbeat

from relayapi.mediamtx import source_url


def test_a_heartbeat_creates_the_on_demand_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    assert relay.mediamtx.paths["cam-01"] == {
        "source": "rtsp://relay:br0ker%252F%2Fp%23w%40rd%3Ax@192.168.8.21:8554/cam-01",
        "sourceOnDemand": True,
        "sourceOnDemandCloseAfter": "10s",
        "rtspTransport": "tcp",
    }


def test_the_control_api_is_called_with_its_own_login_and_no_broker_password(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    for call in relay.mediamtx.calls:
        assert call.headers["authorization"].startswith("Basic ")
        assert BROKER_PASSWORD not in str(call.url)


def test_user_and_password_are_percent_encoded():
    url = source_url("a b:c", "p@ss/w%rd#?", "192.168.8.5", "cam-01")
    assert url == "rtsp://a%20b%3Ac:p%40ss%2Fw%25rd%23%3F@192.168.8.5:8554/cam-01"


@pytest.mark.parametrize(
    "address",
    [
        "fd00::1%\n",
        "fd00::1%25x]:9997?",
        "1.2.3.4:1883",
        "1.2.3.4/x",
        "a@1.2.3.4",
        "1.2.3.4?x",
        "h#x",
        "EVIL",
    ],
)
def test_a_source_that_does_not_round_trip_is_never_built(address):
    # MediaMTX logs a source it refuses, with the password in it, so it must never see one.
    with pytest.raises(ValueError) as error:
        source_url("u", "secret-password", address, "cam-01")
    assert "secret-password" not in str(error.value)


def test_a_refused_source_backs_off_and_does_not_retry_every_pass(relay, monkeypatch):
    moment = {"now": 1000.0}
    monkeypatch.setattr("relayapi.mediamtx.time.monotonic", lambda: moment["now"])

    def refusing(request):
        relay.mediamtx.calls.append(request)
        if request.method == "GET":
            return httpx.Response(404, json={"error": "path not found"})
        return httpx.Response(400, json={"error": "no"})

    relay.mqtt.deliver(STATUS, heartbeat())
    original = type(relay.mediamtx).__call__
    type(relay.mediamtx).__call__ = lambda self, request: refusing(request)
    try:
        counts = []
        relay.mediamtx.calls.clear()
        for _ in range(4):
            relay.sync_paths(full=True)
            counts.append(len(relay.mediamtx.writes))
        assert counts == [1, 1, 1, 1]
        moment["now"] += 5.1
        relay.sync_paths(full=True)
        assert len(relay.mediamtx.writes) == 2
        moment["now"] += 5.1  # the wait has doubled
        relay.sync_paths(full=True)
        assert len(relay.mediamtx.writes) == 2
        moment["now"] += 5.1
        relay.sync_paths(full=True)
        assert len(relay.mediamtx.writes) == 3
    finally:
        type(relay.mediamtx).__call__ = original
    relay.sync_paths(full=True)  # still waiting
    moment["now"] += 600
    relay.sync_paths(full=True)
    assert "cam-01" in relay.mediamtx.paths


def test_a_path_nobody_reports_is_removed(relay):
    relay.mediamtx.paths["ghost"] = {"source": "rtsp://x:y@192.168.8.9:8554/ghost"}
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths(full=True)
    assert set(relay.mediamtx.paths) == {"cam-01"}


def test_several_pages_of_paths_are_read(relay):
    original = type(relay.mediamtx).__call__

    def paged(self, request):
        if request.url.path == "/v3/config/paths/list":
            self.calls.append(request)
            page = int(request.url.params.get("page", "0"))
            items = [[{"name": "ghost-a"}], [{"name": "ghost-b"}]][page]
            return httpx.Response(200, json={"pageCount": 2, "items": items})
        return original(self, request)

    type(relay.mediamtx).__call__ = paged
    relay.mediamtx.paths.update({"ghost-a": {}, "ghost-b": {}})
    try:
        relay.sync_paths(full=True)
    finally:
        type(relay.mediamtx).__call__ = original
    assert relay.mediamtx.paths == {}


def test_a_broken_answer_from_mediamtx_cannot_stop_the_sync(relay):
    original = type(relay.mediamtx).__call__

    def garbage(self, request):
        self.calls.append(request)
        return httpx.Response(200, content=b"not json")

    relay.mqtt.deliver(STATUS, heartbeat())
    type(relay.mediamtx).__call__ = garbage
    try:
        relay.sync_paths(full=True)
    finally:
        type(relay.mediamtx).__call__ = original
    paths = relay.http.get("/nodes").json()["paths"]
    assert paths["failing"] == 1
    relay.sync_paths(full=True)
    assert "cam-01" in relay.mediamtx.paths
    paths = relay.http.get("/nodes").json()["paths"]
    assert paths["failing"] == 0
    assert paths["last_sync_at"] == relay.now


def test_the_sync_loop_survives_an_exception_in_a_pass(relay, monkeypatch):
    calls = []

    async def boom(full):
        calls.append(full)
        if len(calls) == 1:
            raise RuntimeError("not expected")
        raise asyncio.CancelledError

    monkeypatch.setattr(relay.app.state.path_sync, "sync_once", boom)
    monkeypatch.setattr("relayapi.mediamtx._RETRY_S", 0.001)
    with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
        relay.http.portal.call(relay.app.state.path_sync.run)
    assert len(calls) == 2


def test_a_quick_pass_makes_no_call_for_a_path_already_right(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths(full=True)
    relay.mediamtx.calls.clear()
    relay.sync_paths(full=False)
    assert relay.mediamtx.calls == []


def test_a_full_pass_with_nothing_changed_is_one_listing_for_any_number_of_nodes(relay):
    for index in range(1, 5):
        relay.mqtt.deliver(
            STATUS, heartbeat(node_id=f"cam-0{index}", address=f"192.168.8.2{index}")
        )
    relay.sync_paths(full=True)
    relay.mediamtx.calls.clear()
    relay.sync_paths(full=True)
    assert [(call.method, call.url.path) for call in relay.mediamtx.calls] == [
        ("GET", "/v3/config/paths/list")
    ]


def test_a_path_changed_behind_the_api_is_put_right_by_a_full_pass(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths(full=True)
    right = dict(relay.mediamtx.paths["cam-01"])
    relay.mediamtx.paths["cam-01"]["source"] = "rtsp://x:y@192.168.8.99:8554/cam-01"
    relay.sync_paths(full=True)
    assert relay.mediamtx.paths["cam-01"] == right


def test_with_the_list_unreadable_each_path_is_still_checked_and_none_removed(relay):
    original = type(relay.mediamtx).__call__

    def no_list(self, request):
        if request.url.path == "/v3/config/paths/list":
            self.calls.append(request)
            return httpx.Response(500, json={"error": "no"})
        return original(self, request)

    relay.mediamtx.paths["ghost"] = {"source": "rtsp://x:y@192.168.8.9:8554/ghost"}
    relay.mqtt.deliver(STATUS, heartbeat())
    type(relay.mediamtx).__call__ = no_list
    try:
        relay.sync_paths(full=True)
    finally:
        type(relay.mediamtx).__call__ = original
    assert set(relay.mediamtx.paths) == {"cam-01", "ghost"}
    assert relay.http.get("/nodes").json()["paths"]["failing"] == 0


def test_no_path_is_created_until_the_egress_guard_says_it_is_ready(make_relay, tmp_path):
    # Docker's restart at boot ignores depends_on, so MediaMTX can run with the guard's rules
    # not loaded. With no path it has no source, and so nothing to dial.
    ready = tmp_path / "ready"
    relay = make_relay(guard_ready_file=ready)
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    assert relay.mediamtx.writes == []
    assert relay.mediamtx.paths == {}
    assert relay.http.get("/nodes").json()["paths"]["guard_ready"] is False
    ready.touch()
    relay.sync_paths()
    assert "cam-01" in relay.mediamtx.paths
    assert relay.http.get("/nodes").json()["paths"]["guard_ready"] is True


def test_with_no_guard_file_configured_it_counts_as_ready(relay):
    assert relay.http.get("/nodes").json()["paths"]["guard_ready"] is True


def test_a_new_address_updates_the_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.sync_paths()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="192.168.8.6"))
    relay.sync_paths()
    assert "@192.168.8.6:8554/" in relay.mediamtx.paths["cam-01"]["source"]


def test_an_old_replay_does_not_touch_the_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.sync_paths()
    relay.mediamtx.calls.clear()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z", address="192.168.8.6"))
    relay.sync_paths()
    assert relay.mediamtx.writes == []
    assert "@192.168.8.5:8554/" in relay.mediamtx.paths["cam-01"]["source"]


def test_an_unchanged_address_leaves_the_path_alone(relay):
    # A write would cut off anyone watching.
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z"))
    relay.sync_paths()
    relay.mediamtx.calls.clear()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z"))
    relay.sync_paths(full=False)
    relay.sync_paths(full=True)
    assert relay.mediamtx.writes == []


def test_a_restarted_mediamtx_gets_its_paths_back(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    relay.mediamtx.paths.clear()
    relay.sync_paths(full=True)
    assert "cam-01" in relay.mediamtx.paths


def test_mediamtx_down_is_retried_and_not_fatal(relay, caplog):
    relay.mediamtx.down = True
    relay.mqtt.deliver(STATUS, heartbeat())
    with caplog.at_level(logging.DEBUG):
        relay.sync_paths()
    assert BROKER_PASSWORD not in caplog.text
    assert relay.http.get("/nodes").status_code == 200

    relay.mediamtx.down = False
    relay.sync_paths(full=False)
    assert "cam-01" in relay.mediamtx.paths


def test_a_node_with_no_address_gets_no_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat(address=None))
    relay.sync_paths()
    assert relay.mediamtx.paths == {}


@pytest.mark.parametrize("status", [401, 400, 500])
def test_a_refusal_from_the_control_api_is_not_fatal(relay, status, caplog):
    original = relay.mediamtx.__class__.__call__

    def refusing(self, request):
        self.calls.append(request)
        return httpx.Response(status, json={"error": "no"})

    relay.mediamtx.__class__.__call__ = refusing
    try:
        relay.mqtt.deliver(STATUS, heartbeat())
        with caplog.at_level(logging.WARNING):
            relay.sync_paths()
    finally:
        relay.mediamtx.__class__.__call__ = original
    assert BROKER_PASSWORD not in caplog.text
    assert relay.http.get("/nodes").status_code == 200
