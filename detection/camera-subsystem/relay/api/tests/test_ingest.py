import json
import sqlite3

import pytest
from conftest import (
    API_BROKER_PASSWORD,
    API_BROKER_USER,
    BROKER_USER,
    CUE_ACKS,
    DETECTIONS,
    STATUS,
    cue_ack,
    detection,
    heartbeat,
)

from relayapi import mqtt as relay_mqtt


def events(relay, **params):
    return relay.http.get("/events", params=params).json()["events"]


def test_detection_payload_is_validated_against_the_v0_schema(relay):
    good = detection()
    relay.mqtt.deliver(DETECTIONS, good)
    assert [e["message"]["event_id"] for e in events(relay)] == [good["event_id"]]

    missing = detection()
    del missing["label"]
    extra = detection(surprise=1)
    wrong_range = detection(confidence=1.5)
    wrong_event = detection(event="object_gone")
    wrong_schema = detection(schema="camera.detection.v1")
    for bad in (missing, extra, wrong_range, wrong_event, wrong_schema):
        relay.mqtt.deliver(DETECTIONS, bad)
    assert len(events(relay)) == 1
    assert relay.dropped()["schema"] == 5


def test_each_topic_takes_only_its_own_schema(relay):
    relay.mqtt.deliver(STATUS, detection())
    relay.mqtt.deliver(DETECTIONS, heartbeat())
    relay.mqtt.deliver(DETECTIONS, cue_ack())
    assert relay.dropped()["schema"] == 3
    assert events(relay) == []
    assert relay.http.get("/nodes").json()["nodes"] == []


def test_a_cue_ack_is_kept_as_an_event(relay):
    ack = cue_ack()
    relay.mqtt.deliver(CUE_ACKS, ack)
    assert events(relay)[0]["message"]["rf_cue_id"] == "cue-1"
    assert events(relay)[0]["kind"] == "cue_ack"


def nested(depth):
    return b'{"a":' * depth + b"1" + b"}" * depth


def with_attributes(attributes: bytes, make=detection):
    text = json.dumps(make()).replace('"attributes": {}', '"attributes": @')
    return text.encode().replace(b"@", attributes)


BAD = {
    "not json": b"{nope",
    "empty": b"",
    "binary": b"\xff\xfe\x00\x01",
    "array": b"[1,2,3]",
    "number": b"7",
    "null": b"null",
    # Inside the size limit, so that they reach the JSON parser.
    "deep object": nested(25_000),
    "deep array": b"[" * 100_000 + b"]" * 100_000,
    "deep in attributes": with_attributes(nested(25_000)),
    "just over the depth limit": with_attributes(nested(32)),
    "huge integer": json.dumps(detection()).replace("0.87", "1" + "0" * 5000).encode(),
    "large integer confidence": json.dumps(detection()).replace("0.87", "1" + "0" * 400).encode(),
    "huge float": json.dumps(detection()).replace("0.87", "1e999").encode(),
    "nan": json.dumps(detection()).replace("0.87", "NaN").encode(),
    "infinity in attributes": (
        json.dumps(detection()).replace('"attributes": {}', '"attributes": {"x": Infinity}')
    ).encode(),
    "huge float in attributes": (
        json.dumps(detection()).replace('"attributes": {}', '"attributes": {"x": -1e999}')
    ).encode(),
    "impossible date": json.dumps(detection(timestamp="2026-13-45T25:61:61Z")).encode(),
    "year zero": json.dumps(detection(timestamp="0000-01-01T00:00:00Z")).encode(),
    "newline after node id": json.dumps(detection(node_id="cam-01\n")).encode(),
    "newline after timestamp": json.dumps(detection(timestamp="2026-11-05T12:00:00Z\n")).encode(),
    "oversize": json.dumps(detection(attributes={"x": "y" * 17_000})).encode(),
}


@pytest.mark.parametrize("name", BAD)
def test_bad_payload_is_counted_and_dropped_not_fatal(relay, name):
    before = sum(relay.dropped().values())
    relay.mqtt.deliver(DETECTIONS, BAD[name])
    relay.mqtt.deliver(STATUS, BAD[name])
    assert sum(relay.dropped().values()) == before + 2
    assert events(relay) == []
    assert relay.http.get("/nodes").json()["nodes"] == []
    # The service still takes a good message.
    good = detection()
    relay.mqtt.deliver(DETECTIONS, good)
    assert [e["message"]["event_id"] for e in events(relay)] == [good["event_id"]]


@pytest.mark.parametrize("depth", [100, 5_000, 9_000, 9_990, 9_992, 9_995, 10_000, 30_000])
def test_deep_nesting_past_the_size_limit_is_refused_inside_the_parser_too(make_relay, depth):
    relay = make_relay(max_payload_bytes=10_000_000)
    relay.mqtt.deliver(DETECTIONS, with_attributes(nested(depth)))
    relay.mqtt.deliver(STATUS, with_attributes(nested(depth), make=lambda: heartbeat(address=None)))
    assert events(relay) == []
    response = relay.http.get("/nodes")
    assert response.status_code == 200
    assert response.json()["nodes"] == []
    assert sum(relay.dropped().values()) == 2


def test_the_depth_limit_is_exact(relay):
    # The message is depth 1, so 31 levels in attributes reach the limit of 32.
    good = detection()
    relay.mqtt.deliver(
        DETECTIONS,
        json.dumps(good)
        .replace('"attributes": {}', '"attributes": ' + nested(31).decode())
        .encode(),
    )
    assert [e["message"]["event_id"] for e in events(relay)] == [good["event_id"]]
    relay.mqtt.deliver(DETECTIONS, with_attributes(nested(32)))
    relay.mqtt.deliver(DETECTIONS, with_attributes(b"[" * 32 + b"]" * 32))
    assert relay.dropped()["too_deep"] == 2


def test_the_size_limit_is_exact(relay):
    good = detection()
    filler = 16 * 1024 - len(json.dumps({**good, "attributes": {"x": ""}}))
    good["attributes"] = {"x": "y" * filler}
    assert len(json.dumps(good)) == 16 * 1024
    relay.mqtt.deliver(DETECTIONS, good)
    assert len(events(relay)) == 1
    bad = detection(attributes={"x": "y" * (filler + 1)})
    relay.mqtt.deliver(DETECTIONS, bad)
    assert relay.dropped()["oversize"] == 1


def test_a_camera_id_with_a_trailing_newline_is_refused(relay):
    relay.mqtt.deliver(DETECTIONS, detection(camera_id="cam-01\n"))
    assert relay.dropped()["bad_node_id"] == 1


def test_a_lone_surrogate_in_a_message_is_stored_and_served(relay):
    raw = json.dumps(detection(attributes={"sub_label": "x"})).replace('"x"', '"\\ud800"')
    relay.mqtt.deliver(DETECTIONS, raw.encode())
    response = relay.http.get("/events")
    assert response.status_code == 200
    assert len(response.json()["events"]) == 1


def test_a_lone_surrogate_in_a_heartbeat_is_served(relay):
    raw = json.dumps(heartbeat(attributes={"reason": "x"})).replace('"x"', '"\\ud800"')
    relay.mqtt.deliver(STATUS, raw.encode())
    response = relay.http.get("/nodes")
    assert response.status_code == 200
    assert response.json()["nodes"][0]["reason"] == "\ud800"


def test_a_big_but_legal_integer_in_attributes_is_served(relay):
    raw = json.dumps(detection()).replace(
        '"attributes": {}', '"attributes": {"n": ' + "9" * 400 + "}"
    )
    relay.mqtt.deliver(DETECTIONS, raw.encode())
    assert relay.http.get("/events").status_code == 200


def test_nesting_below_the_parser_limit_cannot_break_a_read(make_relay):
    # Whichever way the JSON handling goes at this depth, no read may fail on it.
    relay = make_relay(max_payload_bytes=10_000_000)
    for depth in (400, 900, 990, 2000):
        raw = json.dumps(detection()).replace(
            '"attributes": {}', '"attributes": ' + '{"a":' * depth + "1" + "}" * depth
        )
        relay.mqtt.deliver(DETECTIONS, raw.encode())
        heart = json.dumps(heartbeat(address=None)).replace(
            '"attributes": {}', '"attributes": ' + '{"a":' * depth + "1" + "}" * depth
        )
        relay.mqtt.deliver(STATUS, heart.encode())
        assert relay.http.get("/events").status_code == 200
        assert relay.http.get("/nodes").status_code == 200


def test_an_unknown_topic_is_counted(relay):
    relay.mqtt.deliver("detection/other", detection())
    assert relay.dropped()["unknown_topic"] == 1


def test_a_failing_store_cannot_kill_the_mqtt_thread(relay, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(relay.app.state.store, "add_event", boom)
    relay.mqtt.deliver(DETECTIONS, detection())
    assert relay.dropped()["internal_error"] == 1


def test_the_consumer_subscribes_to_all_three_topics_at_qos_1(relay):
    subscribed = dict(relay.mqtt.subscriptions[-1])
    assert subscribed == {DETECTIONS: 1, CUE_ACKS: 1, STATUS: 1}
    # Its own login, not the one nodes hold.
    assert relay.mqtt.credentials == (API_BROKER_USER, API_BROKER_PASSWORD)
    assert relay.mqtt.credentials[0] != BROKER_USER
    assert relay.mqtt.connect_target == ("broker.test", 1883)


def test_a_real_client_keeps_its_session_across_restarts(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(relay_mqtt.mqtt, "Client", Client)
    settings = type("S", (), {"mqtt_client_id": "relay-api-secret"})()
    relay_mqtt.make_client(settings)
    assert captured["client_id"] == "relay-api-secret"
    assert captured["clean_session"] is False
    # Acknowledged only once durably stored.
    assert captured["manual_ack"] is True


def test_an_event_is_acknowledged_only_after_it_is_stored(relay, monkeypatch):
    seen = []
    real = relay.app.state.store.add_event

    def spy(*args, **kwargs):
        seen.append(list(relay.mqtt.acks))
        return real(*args, **kwargs)

    monkeypatch.setattr(relay.app.state.store, "add_event", spy)
    mid = relay.mqtt.deliver(DETECTIONS, detection())
    assert seen == [[]]
    assert relay.mqtt.acks == [mid]


def full_disk(*args, **kwargs):
    raise sqlite3.OperationalError("database or disk is full")


def test_a_full_disk_acknowledges_nothing_and_stores_on_retry(relay, monkeypatch):
    message = detection()
    real = relay.app.state.store.add_event
    monkeypatch.setattr(relay.app.state.store, "add_event", full_disk)
    mid = relay.mqtt.deliver(DETECTIONS, message)
    assert relay.mqtt.acks == []
    broker = relay.http.get("/nodes").json()["broker"]
    assert broker["store_full"] == 1
    assert broker["unacked"] == 1
    assert "internal_error" not in broker["dropped"]
    # Still full: still not acknowledged.
    relay.app.state.consumer.retry_pending()
    assert relay.mqtt.acks == []
    # Space again.
    monkeypatch.setattr(relay.app.state.store, "add_event", real)
    relay.app.state.consumer.retry_pending()
    assert relay.mqtt.acks == [mid]
    assert [e["message"]["event_id"] for e in events(relay)] == [message["event_id"]]
    assert relay.http.get("/nodes").json()["broker"]["unacked"] == 0


def test_a_heartbeat_on_a_full_disk_is_not_acknowledged_either(relay, monkeypatch):
    monkeypatch.setattr(relay.app.state.store, "add_heartbeat", full_disk)
    relay.mqtt.deliver(STATUS, heartbeat())
    assert relay.mqtt.acks == []


def test_a_reconnect_forgets_unacked_messages_as_the_broker_sends_them_again(relay, monkeypatch):
    # Their packet ids mean nothing on the new connection.
    monkeypatch.setattr(relay.app.state.store, "add_event", full_disk)
    relay.mqtt.deliver(DETECTIONS, detection())
    relay.mqtt.drop()
    assert relay.http.get("/nodes").json()["broker"]["unacked"] == 0
    relay.mqtt.connect()
    monkeypatch.undo()
    relay.app.state.consumer.retry_pending()
    assert relay.mqtt.acks == []


def test_a_message_that_can_never_be_stored_is_acknowledged_so_it_cannot_block_the_queue(relay):
    mid = relay.mqtt.deliver(DETECTIONS, b"{nope")
    assert relay.mqtt.acks == [mid]


def test_a_failing_store_on_qos_0_holds_nothing(relay, monkeypatch):
    monkeypatch.setattr(relay.app.state.store, "add_event", full_disk)
    relay.mqtt.deliver(DETECTIONS, detection("object_update"), qos=0)
    assert relay.http.get("/nodes").json()["broker"]["unacked"] == 0


def test_flapping_backs_off_and_a_stable_connection_resets_it(relay, monkeypatch):
    moment = {"now": 1000.0}
    monkeypatch.setattr("relayapi.mqtt.time.monotonic", lambda: moment["now"])
    relay.mqtt.drop()  # the harness connected just before
    start = len(relay.mqtt.delays)
    for _ in range(7):
        relay.mqtt.connect()
        moment["now"] += 1
        relay.mqtt.drop()
    assert relay.mqtt.delays[start:] == [4, 8, 16, 30, 30, 30, 30]
    relay.mqtt.connect()
    moment["now"] += 120
    relay.mqtt.drop()
    assert relay.mqtt.delays[-1] == 1


def test_reconnects_are_counted(relay):
    before = relay.http.get("/nodes").json()["broker"]["reconnects"]
    relay.mqtt.drop()
    relay.mqtt.connect()
    relay.mqtt.drop()
    assert relay.http.get("/nodes").json()["broker"]["reconnects"] == before + 2


def test_broker_down_leaves_reads_working(relay):
    good = detection()
    relay.mqtt.deliver(DETECTIONS, good)
    relay.mqtt.deliver(STATUS, heartbeat())
    assert relay.http.get("/nodes").json()["broker"]["connected"] is True

    relay.mqtt.drop()

    body = relay.http.get("/nodes").json()
    assert body["broker"]["connected"] is False
    assert [n["node_id"] for n in body["nodes"]] == ["cam-01"]
    assert [e["message"]["event_id"] for e in events(relay)] == [good["event_id"]]


def test_a_reconnect_subscribes_again(relay):
    count = len(relay.mqtt.subscriptions)
    relay.mqtt.drop()
    relay.mqtt.connect()
    assert len(relay.mqtt.subscriptions) == count + 1
    assert relay.http.get("/nodes").json()["broker"]["connected"] is True


def test_an_api_that_starts_with_no_broker_still_serves(make_relay):
    relay = make_relay()
    relay.mqtt.drop()
    assert relay.http.get("/nodes").status_code == 200
    assert relay.http.get("/events").status_code == 200


def test_a_refused_login_reads_as_disconnected(relay):
    import types

    relay.mqtt.drop()
    relay.mqtt.on_connect(
        relay.mqtt, None, types.SimpleNamespace(), types.SimpleNamespace(is_failure=True), None
    )
    assert relay.http.get("/nodes").json()["broker"]["connected"] is False
    assert relay.mqtt.subscriptions == relay.mqtt.subscriptions[:1]
