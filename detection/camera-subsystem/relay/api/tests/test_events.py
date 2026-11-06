import asyncio

import pytest
from conftest import CUE_ACKS, DETECTIONS, cue_ack, detection

from relayapi.app import event_stream


def events(relay, **params):
    return relay.http.get("/events", params=params).json()["events"]


def ids(rows):
    return [row["message"]["event_id"] for row in rows]


def test_a_redelivered_event_is_kept_once(relay):
    message = detection()
    relay.mqtt.deliver(DETECTIONS, message)
    relay.mqtt.deliver(DETECTIONS, message)
    assert ids(events(relay)) == [message["event_id"]]
    assert relay.dropped()["duplicate_event"] == 1


def test_dedupe_survives_a_restart(relay):
    message = detection()
    relay.mqtt.deliver(DETECTIONS, message)
    relay.restart()
    relay.mqtt.deliver(DETECTIONS, message)
    assert ids(events(relay)) == [message["event_id"]]


def test_since_is_a_cursor_that_cannot_miss_a_late_replay(relay):
    first = detection(timestamp="2026-11-05T12:00:00Z")
    relay.mqtt.deliver(DETECTIONS, first)
    cursor = events(relay)[-1]["seq"]
    # Replayed from a node's outbox: it arrives after, stamped before.
    late = detection(timestamp="2026-11-05T08:00:00Z")
    relay.mqtt.deliver(DETECTIONS, late)
    assert ids(events(relay, since=cursor)) == [late["event_id"]]


def test_since_takes_a_timestamp(relay):
    old = detection(timestamp="2026-11-05T08:00:00Z")
    new = detection(timestamp="2026-11-05T12:00:00.5Z")
    relay.mqtt.deliver(DETECTIONS, old)
    relay.mqtt.deliver(DETECTIONS, new)
    assert ids(events(relay, since="2026-11-05T09:00:00Z")) == [new["event_id"]]


def test_limit_and_order(relay):
    messages = [detection() for _ in range(5)]
    for message in messages:
        relay.mqtt.deliver(DETECTIONS, message)
    # With no cursor: the newest, oldest of them first.
    assert ids(events(relay, limit=2)) == [m["event_id"] for m in messages[-2:]]
    # With a cursor: the next page, so paging misses nothing.
    assert ids(events(relay, since=0, limit=2)) == [m["event_id"] for m in messages[:2]]
    assert ids(events(relay, since="", limit=2)) == [m["event_id"] for m in messages[-2:]]


@pytest.mark.parametrize(
    "params",
    [
        {"since": "9" * 40},
        {"since": "9" * 5000},
        {"since": "-1"},
        {"since": "yesterday"},
        {"since": "2026-13-45T00:00:00Z"},
        {"limit": "0"},
        {"limit": "-5"},
        {"limit": "100000"},
        {"limit": "9" * 5000},
        {"limit": "many"},
    ],
)
def test_a_bad_query_is_a_clean_refusal(relay, params):
    assert 400 <= relay.http.get("/events", params=params).status_code < 500


def test_retention_thins_updates_before_start_and_end(make_relay):
    relay = make_relay(retain_updates=3, retain_durable=5, retention_check_every=1)
    start = detection("object_start")
    end = detection("object_end")
    relay.mqtt.deliver(DETECTIONS, start)
    updates = [detection("object_update") for _ in range(10)]
    for update in updates:
        relay.mqtt.deliver(DETECTIONS, update)
    relay.mqtt.deliver(DETECTIONS, end)
    relay.mqtt.deliver(CUE_ACKS, cue_ack())

    kept = ids(events(relay, since=0, limit=1000))
    assert start["event_id"] in kept
    assert end["event_id"] in kept
    assert [u["event_id"] for u in updates if u["event_id"] in kept] == [
        u["event_id"] for u in updates[-3:]
    ]


def test_start_and_end_have_a_limit_too(make_relay):
    relay = make_relay(retain_updates=3, retain_durable=2, retention_check_every=1)
    messages = [detection("object_start") for _ in range(5)]
    for message in messages:
        relay.mqtt.deliver(DETECTIONS, message)
    assert ids(events(relay, since=0, limit=1000)) == [m["event_id"] for m in messages[-2:]]


def test_stream_sends_new_events_in_order_with_their_cursor(relay):
    first = detection()
    relay.mqtt.deliver(DETECTIONS, first)
    cursor = events(relay)[-1]["seq"]
    second = detection()
    third = detection("object_end")
    relay.mqtt.deliver(DETECTIONS, second)
    relay.mqtt.deliver(DETECTIONS, third)

    async def take(count):
        stream = event_stream(relay.app.state.store, cursor, poll_s=0.01)
        chunks = [await anext(stream) for _ in range(count)]
        await stream.aclose()
        return chunks

    chunks = asyncio.run(take(2))
    assert second["event_id"] in chunks[0] and chunks[0].startswith(f"id: {cursor + 1}\n")
    assert third["event_id"] in chunks[1] and chunks[1].endswith("\n\n")
    assert "event: detection\n" in chunks[0]


def test_stream_sends_a_keepalive_while_idle(relay):
    async def take():
        stream = event_stream(relay.app.state.store, 0, poll_s=0.01, keepalive_s=0.02)
        chunk = await anext(stream)
        await stream.aclose()
        return chunk

    assert asyncio.run(take()).startswith(":")


def test_stream_route_resumes_from_last_event_id(relay, monkeypatch):
    seen = {}

    async def fake(store, after, **kwargs):
        seen["after"] = after
        yield "id: 1\n\n"

    monkeypatch.setattr("relayapi.app.event_stream", fake)
    response = relay.http.get("/events/stream", headers={"Last-Event-ID": "7"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert seen["after"] == 7


@pytest.mark.parametrize("value", ["9" * 40, "-1", "x"])
def test_stream_ignores_a_bad_last_event_id(relay, monkeypatch, value):
    seen = {}

    async def fake(store, after, **kwargs):
        seen["after"] = after
        yield "id: 1\n\n"

    monkeypatch.setattr("relayapi.app.event_stream", fake)
    relay.mqtt.deliver(DETECTIONS, detection())
    response = relay.http.get("/events/stream", headers={"Last-Event-ID": value})
    assert response.status_code == 200
    assert seen["after"] == 1
