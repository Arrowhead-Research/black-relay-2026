import logging

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
    url = source_url("a b:c", "p@ss/w%rd#?", "10.0.0.5", "cam-01")
    assert url == "rtsp://a%20b%3Ac:p%40ss%2Fw%25rd%23%3F@10.0.0.5:8554/cam-01"


def test_an_ipv6_address_is_bracketed():
    assert source_url("u", "p", "fd00::5", "cam-01") == "rtsp://u:p@[fd00::5]:8554/cam-01"


def test_a_new_address_updates_the_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="10.0.0.5"))
    relay.sync_paths()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="10.0.0.6"))
    relay.sync_paths()
    assert "@10.0.0.6:8554/" in relay.mediamtx.paths["cam-01"]["source"]


def test_an_old_replay_does_not_touch_the_path(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="10.0.0.5"))
    relay.sync_paths()
    relay.mediamtx.calls.clear()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z", address="10.0.0.6"))
    relay.sync_paths()
    assert relay.mediamtx.writes == []
    assert "@10.0.0.5:8554/" in relay.mediamtx.paths["cam-01"]["source"]


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
    import httpx

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
