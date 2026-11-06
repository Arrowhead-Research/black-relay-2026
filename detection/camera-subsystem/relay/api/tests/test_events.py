import asyncio

import pytest
from conftest import CUE_ACKS, DETECTIONS, cue_ack, detection

from relayapi.app import event_stream
from relayapi.store import Store, StoreLocked


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


def test_a_commit_reaches_the_disk_before_it_returns(relay):
    # An acknowledgement follows the commit, and the node then deletes its copy.
    db = relay.app.state.store._db
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_retention_holds_a_byte_limit_too_taking_updates_first(make_relay):
    relay = make_relay(
        retain_bytes=6000, retain_updates=1000, retain_durable=1000, retention_check_every=1
    )
    start = detection("object_start", attributes={"x": "y" * 1000})
    relay.mqtt.deliver(DETECTIONS, start)
    updates = [detection("object_update", attributes={"x": "y" * 1000}) for _ in range(8)]
    for update in updates:
        relay.mqtt.deliver(DETECTIONS, update)
    kept = ids(events(relay, since=0, limit=1000))
    assert start["event_id"] in kept
    assert updates[-1]["event_id"] in kept
    assert updates[0]["event_id"] not in kept
    assert relay.app.state.store.stored_bytes() <= 6000


def test_when_only_start_and_end_are_left_the_byte_limit_still_holds(make_relay):
    relay = make_relay(retain_bytes=3000, retention_check_every=1)
    messages = [detection("object_start", attributes={"x": "y" * 1000}) for _ in range(6)]
    for message in messages:
        relay.mqtt.deliver(DETECTIONS, message)
    kept = ids(events(relay, since=0, limit=1000))
    assert messages[-1]["event_id"] in kept
    assert messages[0]["event_id"] not in kept
    assert relay.app.state.store.stored_bytes() <= 3000


def test_byte_accounting_survives_a_restart(make_relay):
    relay = make_relay(retain_bytes=1_000_000)
    relay.mqtt.deliver(DETECTIONS, detection(attributes={"x": "y" * 1000}))
    before = relay.app.state.store.stored_bytes()
    assert before > 1000
    relay.restart()
    assert relay.app.state.store.stored_bytes() == before


def test_stream_batches_are_capped_by_bytes(relay):
    store = relay.app.state.store
    for _ in range(5):
        relay.mqtt.deliver(DETECTIONS, detection(attributes={"x": "y" * 1000}))
    assert len(store.events_after(0, 100, max_bytes=3000)) == 3
    # One row always comes back, or a large row could never be sent.
    assert len(store.events_after(0, 100, max_bytes=1)) == 1


def test_the_number_of_streams_is_limited(make_relay, monkeypatch):
    relay = make_relay(max_streams=2)
    calls = []

    async def fake(store, after, **kwargs):
        calls.append(after)
        yield "id: 1\n\n"

    monkeypatch.setattr("relayapi.app.event_stream", fake)
    streams = relay.app.state.streams
    assert streams.acquire() and streams.acquire()
    response = relay.http.get("/events/stream")
    assert response.status_code == 503
    assert calls == []
    streams.release()
    assert relay.http.get("/events/stream").status_code == 200
    # The stream that ran gave its place back.
    assert streams.acquire()
    streams.release()
    streams.release()


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


def test_a_second_api_on_the_same_database_refuses_to_start(relay):
    # Two would keep pushing each other off the one broker session.
    with pytest.raises(StoreLocked):
        Store(relay.settings().db_path, 1, 1, 1, 1)


def test_the_database_is_free_again_after_a_clean_stop(relay):
    relay.restart()
    assert relay.http.get("/nodes").status_code == 200
