"""Tests for ``camerabridge.health``: Frigate MQTT topics to the four health booleans.

The payload shapes follow Frigate's documented topics (``frigate/available``,
``frigate/stats``, ``frigate/<camera>/recordings/state``). The rule under test is
fail-safe: no data, stale stats, or Frigate offline must report False, never True.
"""

from __future__ import annotations

import json

import pytest

from camerabridge.health import FrigateHealth

CAMERA = "cam-01"
STALE_S = 90.0
AVAILABLE = "frigate/available"
STATS = "frigate/stats"
RECORDINGS = f"frigate/{CAMERA}/recordings/state"


class Mono:
    """An injected monotonic clock."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def stats(camera_fps=5.0, *, detectors=None, camera=CAMERA) -> bytes:
    if detectors is None:
        detectors = {"hailo": {"inference_speed": 9.5, "detection_start": 0.0, "pid": 42}}
    return json.dumps(
        {
            "cameras": {camera: {"camera_fps": camera_fps, "process_fps": 5.0}},
            "detectors": detectors,
            "service": {"version": "0.16.0", "uptime": 10},
        }
    ).encode()


def make_health(mono=None, camera=CAMERA) -> FrigateHealth:
    return FrigateHealth(camera=camera, stale_after_s=STALE_S, monotonic_fn=mono or Mono())


def feed_healthy(health: FrigateHealth) -> None:
    health.update(AVAILABLE, b"online")
    health.update(RECORDINGS, b"ON")
    health.update(STATS, stats())


def flags(state) -> tuple[bool, bool, bool]:
    return state.camera_online, state.detector_ok, state.recording_ok


def test_topics_follow_the_configured_prefix_and_camera():
    assert set(make_health().topics) == {AVAILABLE, STATS, RECORDINGS}
    health = FrigateHealth(camera="front", topic_prefix="site1", stale_after_s=STALE_S)
    assert set(health.topics) == {"site1/available", "site1/stats", "site1/front/recordings/state"}


def test_healthy():
    health = make_health()
    feed_healthy(health)
    state = health.evaluate(link_online=True)
    assert flags(state) == (True, True, True)
    assert state.link_online is True


@pytest.mark.parametrize("link", [True, False])
def test_link_online_is_passed_through(link):
    assert make_health().evaluate(link_online=link).link_online is link


def test_no_data_reports_false():
    assert flags(make_health().evaluate(link_online=True)) == (False, False, False)


def test_dead_camera_zero_fps():
    health = make_health()
    feed_healthy(health)
    health.update(STATS, stats(camera_fps=0.0))
    state = health.evaluate(link_online=True)
    assert state.camera_online is False
    assert state.detector_ok is True
    # Recording requires a live camera: Frigate's setting can stay ON with no frames.
    assert state.recording_ok is False


def test_camera_missing_from_stats():
    health = make_health()
    feed_healthy(health)
    health.update(STATS, stats(camera="some-other-camera"))
    state = health.evaluate(link_online=True)
    assert state.camera_online is False
    assert state.recording_ok is False


def test_stale_stats_report_false():
    mono = Mono()
    health = make_health(mono)
    feed_healthy(health)

    mono.now += STALE_S  # exactly at the limit: not yet "older than"
    assert flags(health.evaluate(link_online=True)) == (True, True, True)

    mono.now += 0.001
    assert flags(health.evaluate(link_online=True)) == (False, False, False)

    health.update(STATS, stats())  # fresh stats recover
    assert flags(health.evaluate(link_online=True)) == (True, True, True)


@pytest.mark.parametrize("payload", [b"offline", b"stopped", b"", b"garbage"])
def test_frigate_not_online_reports_false_even_with_fresh_stats(payload):
    health = make_health()
    feed_healthy(health)
    health.update(AVAILABLE, payload)
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


def test_stats_alone_without_available_report_false():
    health = make_health()
    health.update(STATS, stats())
    health.update(RECORDINGS, b"ON")
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


@pytest.mark.parametrize("payload", [b"OFF", b"", b"on", b"garbage"])
def test_recording_requires_state_exactly_on(payload):
    health = make_health()
    feed_healthy(health)
    health.update(RECORDINGS, payload)
    state = health.evaluate(link_online=True)
    assert state.recording_ok is False
    assert state.camera_online is True


def test_no_recordings_state_received_reports_false():
    health = make_health()
    health.update(AVAILABLE, b"online")
    health.update(STATS, stats())
    assert health.evaluate(link_online=True).recording_ok is False


@pytest.mark.parametrize(
    "detectors",
    [
        {},  # no detector present
        {"hailo": {"inference_speed": 0.0, "pid": 42}},  # never reported an inference
        {"hailo": {"inference_speed": 9.5, "pid": None}},  # no process
        {"hailo": {"pid": 42}},  # no speed
        {"hailo": {"inference_speed": "fast", "pid": 42}},  # wrong type
        {"hailo": {"inference_speed": True, "pid": 42}},  # bool is not a measurement
        {"a": {"inference_speed": 9.5, "pid": 1}, "b": {"inference_speed": 0.0, "pid": 2}},
        ["hailo"],  # wrong shape
        {"hailo": "ok"},  # wrong shape
    ],
)
def test_detector_not_reporting_is_false(detectors):
    health = make_health()
    feed_healthy(health)
    health.update(STATS, stats(detectors=detectors))
    state = health.evaluate(link_online=True)
    assert state.detector_ok is False
    assert state.camera_online is True


@pytest.mark.parametrize(
    "payload", [b"not-json{{", b"[]", b"null", b'{"cameras":[],"detectors":{}}', b"\xff\xfe"]
)
def test_malformed_stats_report_false_without_raising(payload):
    health = make_health()
    feed_healthy(health)
    health.update(STATS, payload)
    state = health.evaluate(link_online=True)
    assert state.camera_online is False
    assert state.detector_ok is False


@pytest.mark.parametrize("fps", ["5", None, True, float("nan"), float("inf"), -1.0])
def test_bad_camera_fps_is_false(fps):
    health = make_health()
    feed_healthy(health)
    health.update(STATS, stats(camera_fps=fps))
    assert health.evaluate(link_online=True).camera_online is False


def test_reset_forgets_everything():
    # The local broker connection dropped: every cached value is now unverifiable,
    # including the retained available/recordings state that would otherwise freeze.
    health = make_health()
    feed_healthy(health)
    health.reset()
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


def test_unconfigured_camera_reports_false_and_has_no_recordings_topic():
    health = make_health(camera=None)
    assert set(health.topics) == {AVAILABLE, STATS}
    health.update(AVAILABLE, b"online")
    health.update(STATS, stats())
    state = health.evaluate(link_online=True)
    assert (state.camera_online, state.recording_ok) == (False, False)
