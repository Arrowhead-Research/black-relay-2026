import ipaddress
import json

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
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00.5Z", address="192.168.8.5"))
    relay.mqtt.deliver(
        STATUS, heartbeat(timestamp="2026-11-05T12:00:00.25Z", address="192.168.8.6")
    )
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.5"


def test_a_heartbeat_stamped_far_ahead_cannot_pin_the_node(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="9999-12-31T23:59:59Z", address="192.168.8.6"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.5"
    assert relay.dropped()["future_heartbeat"] == 1
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="192.168.8.7"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.7"


def test_a_heartbeat_without_an_address_keeps_the_last_known_one(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address=None))
    node = nodes(relay)["cam-01"]
    assert node["node_address"] == "192.168.8.5"
    assert node["timestamp"] == "2026-11-05T12:00:05Z"


REFUSED_ADDRESSES = [
    # Each of the review's measured attacks, whole.
    "8.8.8.8",
    "2606:4700::1111",
    "100.100.100.100",
    "172.31.250.2",
    "172.17.0.1",
    "192.168.8.255",
    "2002:7f00:1::",
    "::ffff:8.8.8.8",
    "::ffff:192.168.8.21",
    "::ffff:192.168.8.21%25x]:1883?",
    "::ffff:172.31.244.2%25x]:9997?",
    "fd00::1%25x]:9997?",
    "fd00::1%\n",
    "fd00::1%",
    "192.168.8.21\n",
    "\n192.168.8.21",
    " 192.168.8.21",
    "192.168.8.21 ",
    "192.168.8.21:1883",
    "192.168.8.21:9997",
    "192.168.8.21:8554",
    "192.168.8.21%eth0",
    "192.168.8.021",
    "192.168.008.21",
    "0xc0.0xa8.8.21",
    "3232237589",
    "192.168.8",
    "\u0661\u0669\u0662.168.8.21",  # Arabic-Indic digits
    # The site's own network and broadcast addresses.
    "192.168.8.0",
    # Loopback, link-local and the rest.
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "169.254.1.1",
    "fe80::1",
    "fe80::1%eth0",
    "224.0.0.1",
    "240.0.0.1",
    "255.255.255.255",
    # Not addresses.
    "evil.example.com",
    "192.168.8.21/x",
    "user:pw@192.168.8.21",
    "",
    12345,
    3232237589,
    True,
    ["192.168.8.21"],
    {"a": "192.168.8.21"},
]


@pytest.mark.parametrize("address", REFUSED_ADDRESSES, ids=repr)
def test_an_address_that_is_not_a_node_is_refused_and_counted(relay, address):
    relay.mqtt.deliver(STATUS, heartbeat(address=None, attributes={"node_address": address}))
    node = nodes(relay)["cam-01"]
    assert node["node_address"] is None
    assert node["camera_online"] is True
    assert relay.dropped()["refused_address"] == 1
    relay.sync_paths()
    assert relay.mediamtx.paths == {}
    response = relay.http.get("/nodes/cam-01/frigate/api/stats")
    assert response.status_code == 503
    assert relay.node.requests == []


@pytest.mark.parametrize("address", ["172.31.250.2", "172.31.251.9"])
def test_the_bundles_own_networks_are_refused(make_relay, address):
    # 172.31.250.2 and 172.31.251.9 lie on the site here only because a stored-rule test needs
    # them to, so the bundle's networks are what refuses them.
    relay = make_relay(
        site_subnet=ipaddress.ip_network("172.31.0.0/16"),
        internal_subnets=(
            ipaddress.ip_network("172.31.250.0/29"),
            ipaddress.ip_network("172.31.251.0/24"),
        ),
    )
    relay.mqtt.deliver(STATUS, heartbeat(address=address))
    assert nodes(relay)["cam-01"]["node_address"] is None


def test_the_central_hosts_own_ip_is_refused(make_relay):
    relay = make_relay(central_ip=ipaddress.ip_address("192.168.8.10"))
    relay.mqtt.deliver(STATUS, heartbeat(address="192.168.8.10"))
    assert nodes(relay)["cam-01"]["node_address"] is None
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="192.168.8.11"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.11"


def test_a_refused_address_leaves_the_known_one_in_place(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:05Z", address="8.8.8.8"))
    node = nodes(relay)["cam-01"]
    assert node["node_address"] == "192.168.8.5"
    assert node["timestamp"] == "2026-11-05T12:00:05Z"


def test_every_usable_host_of_the_site_is_accepted(relay):
    for index, address in enumerate(("192.168.8.1", "192.168.8.254")):
        relay.mqtt.deliver(STATUS, heartbeat(node_id=f"cam-{index}", address=address))
        assert nodes(relay)[f"cam-{index}"]["node_address"] == address


def test_a_stored_address_that_breaks_the_rule_never_reaches_a_node_or_mediamtx(relay):
    # A database from before this rule, or a site subnet that has since changed.
    relay.app.state.store.add_heartbeat(
        "old-row",
        1,
        relay.now,
        "8.8.8.8",
        json.dumps(heartbeat(node_id="old-row", address="8.8.8.8")),
        100,
    )
    relay.sync_paths()
    assert relay.mediamtx.paths == {}
    assert relay.http.get("/nodes/old-row/frigate/x").status_code == 503
    assert relay.http.get("/nodes/old-row/snapshot/abc.jpg").status_code == 503
    assert relay.node.requests == []
    assert nodes(relay)["old-row"]["node_address"] is None


NODE_IDS_REFUSED = [
    "all",
    "all_others",
    "~^.*$",
    "~all",
    "a" * 64,
    "a" * 5000,
    "-cam",
    "Cam-01",
    "cam_01",
    "cam.01",
    "cam/01",
    "cam 01",
    "cam-01\n",
    "..",
    "\u0661",
]


@pytest.mark.parametrize("node_id", NODE_IDS_REFUSED, ids=lambda v: repr(v)[:20])
def test_a_node_id_the_portal_or_mediamtx_would_not_take_is_refused(relay, node_id):
    relay.mqtt.deliver(STATUS, heartbeat(node_id=node_id))
    relay.sync_paths()
    assert nodes(relay) == {}
    assert relay.mediamtx.paths == {}
    assert sum(relay.dropped().values()) == 1


@pytest.mark.parametrize("node_id", ["a", "0", "a" * 63, "cam-01", "all-cameras", "allx", "a-"])
def test_a_node_id_within_the_rule_is_accepted(relay, node_id):
    relay.mqtt.deliver(STATUS, heartbeat(node_id=node_id))
    relay.sync_paths()
    assert node_id in nodes(relay)
    assert node_id in relay.mediamtx.paths


def test_a_stored_reserved_node_id_never_becomes_a_path(relay):
    relay.app.state.store.add_heartbeat(
        "all",
        1,
        relay.now,
        "192.168.8.5",
        json.dumps(heartbeat(node_id="all")),
        100,
    )
    relay.sync_paths()
    assert relay.mediamtx.paths == {}


def deep_message(depth):
    text = json.dumps(heartbeat(node_id="deep-row", address=None))
    return text.replace('"attributes": {}', '"attributes": ' + '{"a":' * depth + "1" + "}" * depth)


def test_stored_rows_that_cannot_be_read_never_break_the_listing(relay):
    store = relay.app.state.store
    relay.mqtt.deliver(STATUS, heartbeat(node_id="good", address="192.168.8.5"))
    # Depths are computed at run time: where this fails depends on the interpreter and the
    # platform. Every depth up to the point where the parser itself gives up is stored.
    deepest = 1
    while deepest < 200_000:
        try:
            json.loads(deep_message(deepest))
        except RecursionError:
            break
        deepest *= 2
    low, high = deepest // 2, deepest
    while high - low > 1:
        middle = (low + high) // 2
        try:
            json.loads(deep_message(middle))
            low = middle
        except RecursionError:
            high = middle
    rows = {f"deep-{depth}": deep_message(depth) for depth in range(max(1, low - 12), low + 3)}
    rows |= {
        "not-json": "{nope",
        "not-object": "[1,2]",
        "no-fields": "{}",
        "bad-attributes": json.dumps({**heartbeat(), "attributes": [1]}),
    }
    for node_id, text in rows.items():
        store.add_heartbeat(node_id, 1, relay.now, None, text, 10_000)
    for _ in range(2):
        response = relay.http.get("/nodes")
        assert response.status_code == 200
        body = response.json()
        assert "good" in {node["node_id"] for node in body["nodes"]}
        assert body["unreadable_nodes"] >= 4
        relay.restart()


def test_a_stored_unreadable_row_does_not_break_its_own_routes(relay):
    store = relay.app.state.store
    store.add_heartbeat("bad-row", 1, relay.now, "192.168.8.5", "{nope", 100)
    assert relay.http.get("/nodes/bad-row/frigate/x").status_code == 503
    assert relay.http.get("/nodes/bad-row/snapshot/abc.jpg").status_code == 503
    assert relay.node.requests == []


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
    assert fresh["heartbeat_age_s"] == 0.0
    assert fresh["stale"] is False

    relay.now += 3600
    dead = nodes(relay)["cam-01"]
    assert dead["heartbeat_age_s"] == 3600.0
    assert dead["stale"] is True
    assert dead["camera_online"] is True


def test_age_counts_from_receipt_and_an_old_replay_does_not_refresh_it(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z"))
    relay.now += 50
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T11:00:00Z"))
    assert nodes(relay)["cam-01"]["heartbeat_age_s"] == 50.0


def test_stale_flips_only_past_the_limit(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.now += 90
    assert nodes(relay)["cam-01"]["stale"] is False
    relay.now += 0.001
    assert nodes(relay)["cam-01"]["stale"] is True


def test_an_equal_stamp_is_ignored(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.5"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z", address="192.168.8.6"))
    assert nodes(relay)["cam-01"]["node_address"] == "192.168.8.5"
    assert relay.dropped()["out_of_order_heartbeat"] == 1


def test_the_reviews_299_second_stamp_is_dropped_and_the_truth_is_kept(relay):
    ahead = "2026-11-05T12:05:09Z"  # now + 299 s
    relay.mqtt.deliver(STATUS, heartbeat(timestamp=ahead, address="192.168.8.66"))
    assert relay.dropped()["future_heartbeat"] == 1
    assert nodes(relay) == {}
    relay.now += 100
    relay.mqtt.deliver(
        STATUS,
        heartbeat(timestamp="2026-11-05T12:01:50Z", camera_online=False, address="192.168.8.5"),
    )
    node = nodes(relay)["cam-01"]
    assert node["camera_online"] is False
    assert node["node_address"] == "192.168.8.5"
    relay.now += 91
    assert nodes(relay)["cam-01"]["stale"] is True


def test_the_future_allowance_is_60_seconds(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:01:10Z", node_id="at-limit"))
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:01:10.001Z", node_id="past"))
    assert set(nodes(relay)) == {"at-limit"}
    assert relay.dropped()["future_heartbeat"] == 1


def test_a_stamp_at_the_limit_cannot_keep_a_dead_node_fresh(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:01:10Z"))
    relay.now += 90.001
    node = nodes(relay)["cam-01"]
    assert node["stale"] is True
    assert node["heartbeat_age_s"] == 90.001


def test_a_truthful_heartbeat_after_a_stamp_at_the_limit_is_accepted(relay):
    # Stamped 60 s ahead, then the truth from the same node, 10 s on: its stamp is behind the
    # bad one, and ahead of when the bad one really arrived.
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:01:10Z", address="192.168.8.66"))
    relay.now += 10
    relay.mqtt.deliver(
        STATUS,
        heartbeat(timestamp="2026-11-05T12:00:20Z", camera_online=False, address="192.168.8.5"),
    )
    node = nodes(relay)["cam-01"]
    assert node["camera_online"] is False
    assert node["node_address"] == "192.168.8.5"
    assert node["heartbeat_age_s"] == 0.0


def test_a_slightly_ahead_clock_gains_no_time(relay):
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:30Z"))
    node = nodes(relay)["cam-01"]
    assert node["heartbeat_age_s"] == 0.0
    relay.now += 91
    assert nodes(relay)["cam-01"]["stale"] is True


def test_the_stale_limit_is_configurable(make_relay):
    relay = make_relay(stale_after_s=5.0)
    relay.mqtt.deliver(STATUS, heartbeat(timestamp="2026-11-05T12:00:00Z"))
    assert nodes(relay)["cam-01"]["stale"] is False
    relay.now += 6
    assert nodes(relay)["cam-01"]["stale"] is True


def test_listing_nodes_never_reaches_a_node_or_medimtx(relay):
    relay.mqtt.deliver(STATUS, heartbeat())
    relay.sync_paths()
    relay.mediamtx.calls.clear()
    for _ in range(5):
        relay.http.get("/nodes")
    assert relay.node.requests == []
    assert relay.mediamtx.calls == []


def test_a_full_table_evicts_the_longest_silent_and_never_locks_out_a_new_node(make_relay):
    relay = make_relay(max_nodes=2)
    for index in range(2):
        relay.mqtt.deliver(STATUS, heartbeat(node_id=f"cam-{index}"))
        relay.now += 1
    relay.mqtt.deliver(STATUS, heartbeat(node_id="cam-0", timestamp="2026-11-05T12:00:05Z"))
    relay.now += 1
    relay.mqtt.deliver(STATUS, heartbeat(node_id="real-node"))
    assert set(nodes(relay)) == {"cam-0", "real-node"}
    assert relay.dropped()["evicted_node"] == 1


def test_a_full_table_of_made_up_ids_does_not_stop_a_real_node(make_relay):
    relay = make_relay(max_nodes=5)
    for index in range(50):
        relay.mqtt.deliver(STATUS, heartbeat(node_id=f"made-up-{index}"))
        relay.now += 1
    relay.mqtt.deliver(STATUS, heartbeat(node_id="real-node", address="192.168.8.5"))
    assert nodes(relay)["real-node"]["node_address"] == "192.168.8.5"
    assert len(nodes(relay)) == 5


def test_a_node_unseen_for_the_retention_time_is_pruned_with_its_path(make_relay):
    relay = make_relay(node_retention_s=1000.0)
    relay.mqtt.deliver(STATUS, heartbeat(node_id="gone", address="192.168.8.5"))
    relay.sync_paths()
    assert "gone" in relay.mediamtx.paths
    relay.now += 500
    relay.mqtt.deliver(STATUS, heartbeat(node_id="here", address="192.168.8.6"))
    relay.sync_paths()
    assert set(nodes(relay)) == {"gone", "here"}
    relay.now += 501
    relay.sync_paths()
    assert set(nodes(relay)) == {"here"}
    assert set(relay.mediamtx.paths) == {"here"}
