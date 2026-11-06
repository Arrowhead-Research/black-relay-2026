import pytest
from conftest import STATUS, heartbeat


def nodes(relay):
    return {node["node_id"]: node for node in relay.http.get("/nodes").json()["nodes"]}


def test_heartbeat_registers_a_node_and_its_address(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.21"


def test_a_replayed_old_heartbeat_does_not_move_the_address(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.21"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z", address="192.168.8.99"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.21"
    assert nodes(relay)["cam-01"]["timestamp"] == "2026-11-05T12:00:00Z"
    assert relay.dropped()["out_of_order_heartbeat"] == 1


def test_an_old_replay_after_an_api_restart_does_not_move_the_address(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.21"))
    relay.restart()
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z", address="192.168.8.99"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.21"


def test_a_newer_heartbeat_moves_the_address(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z", address="192.168.8.21"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.22"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.22"


def test_sub_second_order_is_respected(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00.5Z", address="10.0.0.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00.25Z", address="10.0.0.6"))
    assert nodes(relay)["cam-01"]["node_address"] == "10.0.0.5"


def test_a_heartbeat_stamped_far_ahead_cannot_pin_the_node(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="10.0.0.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="9999-12-31T23:59:59Z", address="10.0.0.6"))
    assert nodes(relay)["cam-01"]["node_address"] == "10.0.0.5"
    assert relay.dropped()["future_heartbeat"] == 1
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="10.0.0.7"))
    assert nodes(relay)["cam-01"]["node_address"] == "10.0.0.7"


def test_a_heartbeat_without_an_address_keeps_the_last_known_one(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="10.0.0.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address=None))
    node = nodes(relay)["cam-01"]
    assert node["node_address"] == "10.0.0.5"
    assert node["timestamp"] == "2026-11-05T12:00:05Z"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "169.254.1.1",
        "fe80::1",
        "fe80::1%eth0",
        "224.0.0.1",
        "evil.example.com",
        "192.168.8.21:1234",
        "192.168.8.21/x",
        "user:pw@192.168.8.21",
        "",
        12345,
        None,
        ["192.168.8.21"],
    ],
)
def test_an_address_that_is_not_a_node_is_refused(relay, address):
    message = heartbeat(address=None, attributes={"node_address": address})
    relay.mqtt.deliver(STATUS, message)
    node = nodes(relay)["cam-01"]
    assert node["node_address"] is None
    assert node["camera_online"] is True


def test_fields_are_reported_as_sent(relay):
    message = heartbeat(
        camera_online=False,
        recording_ok=False,
        detector_ok=False,
        attributes={"camera_link_state": "adopted", "reason": None, "retry_after": None},
    )
    relay.mqtt.deliver(STATUS, message)
    node = nodes(relay)["cam-01"]
    assert node["camera_online"] is False
    assert node["link_online"] is True
    assert node["recording_ok"] is False
    assert node["detector_ok"] is False
    # False with no reason means "not yet confirmed". No state is invented for it.
    assert node["reason"] is None
    assert node["attributes"]["camera_link_state"] == "adopted"
    assert "status" not in node and "state" not in node


def test_a_reason_is_surfaced(relay):
    relay.mqtt.deliver(
        STATUS, heartbeat(detector_ok=False, attributes={"reason": "the camera needs a reset"})
    )
    assert nodes(relay)["cam-01"]["reason"] == "the camera needs a reset"


def test_a_node_that_stops_reporting_turns_stale_with_its_values_marked(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z"))
    fresh = nodes(relay)["cam-01"]
    assert fresh["heartbeat_age_s"] == 10.0
    assert fresh["stale"] is False

    relay.now += 3600
    dead = nodes(relay)["cam-01"]
    assert dead["heartbeat_age_s"] == 3610.0
    assert dead["stale"] is True
    assert dead["camera_online"] is True


def test_a_late_replay_of_the_only_heartbeat_is_stale_on_arrival(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T09:00:00Z"))
    assert nodes(relay)["cam-01"]["stale"] is True


def test_age_never_goes_negative_with_a_slightly_ahead_clock(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:30Z"))
    node = nodes(relay)["cam-01"]
    assert node["heartbeat_age_s"] == 0.0
    assert node["stale"] is False


def test_the_stale_limit_is_configurable(make_relay):
    relay = make_relay(stale_after_s=5.0)
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z"))
    assert nodes(relay)["cam-01"]["stale"] is True


def test_listing_nodes_never_reaches_a_node_or_medimtx(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    relay.mediamtx.calls.clear()
    for _ in range(5):
        relay.http.get("/nodes")
    assert relay.node.requests == []
    assert relay.mediamtx.calls == []


def test_the_number_of_nodes_is_bounded(make_relay):
    relay = make_relay(max_nodes=2)
    for index in range(4):
        relay.mqtt.deliver(STATUS, heartbeat(node_id=f"cam-{index}"))
    assert set(nodes(relay)) == {"cam-0", "cam-1"}
    assert relay.dropped()["too_many_nodes"] == 2
    relay.mqtt.deliver(
        STATUS, heartbeat(node_id="cam-0", timestamp="2026-11-05T12:00:05Z", address="10.0.0.9")
    )
    assert nodes(relay)["cam-0"]["node_address"] == "10.0.0.9"
