import asyncio
import sqlite3
import time

import pytest
from conftest import CUE_ACKS, DETECTIONS, cue_ack, detection

from relayapi.app import event_stream
from relayapi.store import _MAX_DEFERRED, Store, StoreLocked


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


def test_the_running_byte_total_stays_true_through_both_kinds_of_pruning(make_relay):
    relay = make_relay(
        retain_bytes=5000, retain_updates=4, retain_durable=6, retention_check_every=2
    )
    store = relay.app.state.store
    for index in range(40):
        event = "object_update" if index % 3 else "object_start"
        relay.mqtt.deliver(DETECTIONS, detection(event, attributes={"x": "y" * (index * 10)}))
        assert store._bytes == store.stored_bytes()
    assert store.stored_bytes() <= 5000


def test_the_retention_check_does_not_sum_the_table(make_relay):
    # It would read every page of up to 256 MiB, with the lock held, every 256 events.
    relay = make_relay(retain_updates=2, retain_durable=2, retention_check_every=1)
    store = relay.app.state.store
    statements = []
    store._db.set_trace_callback(statements.append)
    for _ in range(6):
        relay.mqtt.deliver(DETECTIONS, detection("object_update"))
    store._db.set_trace_callback(None)
    assert not [s for s in statements if "sum(length(message))" in s and "seq <=" not in s]


def test_a_store_reopened_at_the_limit_keeps_its_byte_total(make_relay):
    relay = make_relay(retain_bytes=3000, retention_check_every=1)
    for _ in range(20):
        relay.mqtt.deliver(DETECTIONS, detection())
    relay.restart()
    store = relay.app.state.store
    assert store._bytes == store.stored_bytes() <= 3000


def test_the_unused_time_index_is_gone_from_an_older_database(tmp_path):
    # ORDER BY seq LIMIT makes SQLite walk the table in seq order, so nothing ever read this index.
    store = Store(tmp_path / "relay.sqlite3", 10, 10, 10, 10_000)
    store._db.execute("CREATE INDEX events_by_time ON events (timestamp_us, seq)")
    store.close()
    store = Store(tmp_path / "relay.sqlite3", 10, 10, 10, 10_000)
    names = [row[0] for row in store._db.execute("SELECT name FROM sqlite_master")]
    store.close()
    assert "events_by_time" not in names
    assert "events_by_class" in names


def on_disk(path):
    """What another connection sees, which is what a commit has made durable."""
    other = sqlite3.connect(path)
    try:
        return [row[0] for row in other.execute("SELECT event_id FROM events ORDER BY seq")]
    finally:
        other.close()


@pytest.fixture
def bare_store(tmp_path):
    # With no consumer running, so that no timer settles anything during a test.
    store = Store(tmp_path / "relay.sqlite3", 1000, 1000, 1000, 10**9)
    yield store
    store.close()


def add(store, event_id, sync, message="{}"):
    return store.add_event(
        event_id, "detection", not sync, "cam-01", None, 1, 1.0, message, sync=sync
    )


def test_a_qos_0_row_waits_and_a_synced_row_commits_it_too(bare_store, tmp_path):
    db = tmp_path / "relay.sqlite3"
    add(bare_store, "update", False)
    assert on_disk(db) == []
    add(bare_store, "start", True)
    assert on_disk(db) == ["update", "start"]


def test_a_redelivery_of_a_row_still_held_back_commits_it(bare_store, tmp_path):
    # Its acknowledgement says it is on disk.
    add(bare_store, "same", False)
    assert add(bare_store, "same", True) is False
    assert on_disk(tmp_path / "relay.sqlite3") == ["same"]


def test_a_heartbeat_commits_rows_held_back(bare_store, tmp_path):
    add(bare_store, "update", False)
    bare_store.add_heartbeat("cam-01", 1, 1.0, None, "{}", 10)
    assert on_disk(tmp_path / "relay.sqlite3") == ["update"]


@pytest.mark.parametrize(
    "read",
    [
        lambda store: store.events_after(0, 10),
        lambda store: store.events_latest(10),
        lambda store: store.events_from_time(0, 10),
        lambda store: store.latest_seq(),
        lambda store: store.track_of_event("cam-01", "update"),
        lambda store: store.stored_bytes(),
    ],
)
def test_no_read_of_events_can_see_a_row_that_is_not_on_disk(bare_store, tmp_path, read):
    add(bare_store, "update", False)
    read(bare_store)
    assert on_disk(tmp_path / "relay.sqlite3") == ["update"]


def test_rows_held_back_are_limited(bare_store, tmp_path):
    for index in range(_MAX_DEFERRED - 1):
        add(bare_store, f"update-{index}", False)
    assert on_disk(tmp_path / "relay.sqlite3") == []
    add(bare_store, "last", False)
    assert len(on_disk(tmp_path / "relay.sqlite3")) == _MAX_DEFERRED


def test_closing_commits_rows_held_back(tmp_path):
    store = Store(tmp_path / "relay.sqlite3", 1000, 1000, 1000, 10**9)
    add(store, "update", False)
    store.close()
    assert on_disk(tmp_path / "relay.sqlite3") == ["update"]


def test_the_consumer_settles_a_qos_0_row_on_its_own(relay, tmp_path):
    message = detection("object_update")
    relay.mqtt.deliver(DETECTIONS, message, qos=0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not on_disk(tmp_path / "relay.sqlite3"):
        time.sleep(0.01)
    assert on_disk(tmp_path / "relay.sqlite3") == [message["event_id"]]


def test_a_qos_1_update_is_on_disk_when_acknowledged(relay, tmp_path):
    seen = []
    real = relay.mqtt.ack

    def ack(mid, qos):
        seen.append(on_disk(tmp_path / "relay.sqlite3"))
        real(mid, qos)

    relay.mqtt.ack = ack
    message = detection("object_update")
    relay.mqtt.deliver(DETECTIONS, message, qos=1)
    assert seen == [[message["event_id"]]]


class FailingCommits:
    """A connection whose commits fail as they do on a full disk."""

    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("database or disk is full")
        return self.db.execute(sql, *args)


def test_a_failed_commit_undoes_the_batch_counts_it_and_leaves_reads_working(bare_store, tmp_path):
    add(bare_store, "kept", True, "{}")
    before = bare_store.stored_bytes()
    add(bare_store, "update-1", False, "xxxx")
    add(bare_store, "update-2", False, "xxxx")
    real = bare_store._db
    bare_store._db = FailingCommits(real)
    try:
        # A read carries on with what is on disk.
        assert [row["event_id"] for row in bare_store.events_after(0, 10)] == ["kept"]
        assert bare_store.lost_rows() == {"full": 2}
        with pytest.raises(sqlite3.OperationalError):
            add(bare_store, "start", True)
        # That one the broker sends again, so it is not counted as lost.
        assert bare_store.lost_rows() == {"full": 2}
    finally:
        bare_store._db = real
    assert bare_store._bytes == before == bare_store.stored_bytes()
    assert add(bare_store, "start", True) is True
    assert on_disk(tmp_path / "relay.sqlite3") == ["kept", "start"]


def test_rows_lost_to_a_failed_commit_show_in_the_store_counts(relay, monkeypatch):
    store = relay.app.state.store
    monkeypatch.setattr(store, "lost_rows", lambda: {"full": 2, "other": 1})
    broker = relay.http.get("/nodes").json()["broker"]
    assert (broker["store_full"], broker["store_errors"]) == (2, 1)


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
