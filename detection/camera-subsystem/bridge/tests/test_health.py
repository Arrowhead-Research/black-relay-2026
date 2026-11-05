"""Tests for ``camerabridge.health``: Frigate MQTT topics to the four health booleans.

The payload shapes follow Frigate's documented topics (``frigate/available``,
``frigate/stats``, ``frigate/<camera>/recordings/state``). The rule under test is
fail-safe: no data, stale stats, or Frigate offline must report False, never True.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from camerabridge.health import FrigateHealth, interface_ipv4

CAMERA = "cam-01"
STALE_S = 90.0
AVAILABLE = "frigate/available"
STATS = "frigate/stats"
RECORDINGS = f"frigate/{CAMERA}/recordings/state"


WALL = 1_800_000_000.0
STARTED = WALL - 3600
# What a healthy camera-link reports. detector_ok and recording_ok cannot be true without it.
HEALTHY_LINK = {
    "state": "adopted",
    "updated": int(WALL),
    "detector_error": False,
    "log_read_at": int(WALL),
    "record_writing": True,
}
_HEALTHY_LINK_FILE = Path(tempfile.mkdtemp(prefix="link-state-")) / "state.json"
_HEALTHY_LINK_FILE.write_text(json.dumps(HEALTHY_LINK))


class Mono:
    """An injected monotonic clock."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def wall() -> float:
    return WALL


def stats(
    camera_fps=5.0,
    *,
    detectors=None,
    camera=CAMERA,
    detection_enabled=True,
    detection_fps=20.2,
    last_updated=WALL,
    started=STARTED,
    processes=None,
) -> bytes:
    if detectors is None:
        detectors = {"hailo": {"inference_speed": 7.65, "detection_start": 0.0, "pid": 42}}
    if processes is None:
        processes = {"recording": {"pid": 77}}
    return json.dumps(
        {
            "cameras": {
                camera: {
                    "camera_fps": camera_fps,
                    "process_fps": 5.0,
                    "detection_fps": detection_fps,
                    "detection_enabled": detection_enabled,
                }
            },
            "detectors": detectors,
            "processes": processes,
            "service": {
                "version": "0.18.2",
                "uptime": int(last_updated - started),
                "last_updated": last_updated,
            },
        }
    ).encode()


def make_health(mono=None, camera=CAMERA, wall_fn=wall) -> FrigateHealth:
    return FrigateHealth(
        camera=camera,
        stale_after_s=STALE_S,
        monotonic_fn=mono or Mono(),
        wall_fn=wall_fn,
        link_state_path=_HEALTHY_LINK_FILE,
    )


def feed_healthy(health: FrigateHealth) -> None:
    health.update(AVAILABLE, b"online")
    health.update(RECORDINGS, b"ON")
    health.update(STATS, stats())


def flags(state) -> tuple[bool, bool, bool]:
    return state.camera_online, state.detector_ok, state.recording_ok


def test_topics_follow_the_configured_prefix_and_camera():
    assert set(make_health().topics) == {AVAILABLE, STATS, RECORDINGS}
    health = FrigateHealth(
        camera="front", topic_prefix="site1", stale_after_s=STALE_S, wall_fn=wall
    )
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
        {"a": {"inference_speed": 9.5, "pid": 1}, "b": {"inference_speed": 10.0, "pid": 2}},
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


# --- kit 1's real numbers (Frigate 0.18.2) -----------------------------------


def kit1(**kwargs) -> bytes:
    return stats(**kwargs)


HAILO_AT_START = {"hailo": {"inference_speed": 10.0, "detection_start": 0.0, "pid": 42}}
HAILO_WORKING = {"hailo": {"inference_speed": 7.65, "detection_start": 0.0, "pid": 42}}


def online(health, payload) -> None:
    health.update(AVAILABLE, b"online")
    health.update(RECORDINGS, b"ON")
    health.update(STATS, payload)


def test_kit1_detection_disabled_is_not_detector_ok():
    health = make_health()
    online(
        health,
        kit1(
            camera_fps=5.1, detectors=HAILO_AT_START, detection_enabled=False, detection_fps=0.0
        ),
    )
    state = health.evaluate(link_online=True)
    assert state.detector_ok is False
    assert state.camera_online is True


def test_kit1_detector_thread_crashed_is_not_detector_ok():
    # Detection on, a live pid and the starting value: the crash leaves stats looking normal.
    health = make_health()
    online(health, kit1(camera_fps=5.1, detectors=HAILO_AT_START, detection_fps=0.0))
    assert health.evaluate(link_online=True).detector_ok is False


def test_kit1_working_is_detector_ok():
    health = make_health()
    online(health, kit1(detectors=HAILO_WORKING, detection_fps=20.2))
    assert health.evaluate(link_online=True).detector_ok is True


def test_kit1_idle_after_working_stays_detector_ok():
    health = make_health()
    online(health, kit1(detectors=HAILO_WORKING, detection_fps=20.2))
    health.update(STATS, kit1(detectors=HAILO_WORKING, detection_fps=0.0))
    assert health.evaluate(link_online=True).detector_ok is True


@pytest.mark.parametrize("enabled", [False, None, "true", 1])
def test_detection_enabled_must_be_exactly_true(enabled):
    health = make_health()
    online(health, stats(detection_enabled=enabled))
    assert health.evaluate(link_online=True).detector_ok is False


def test_detection_enabled_is_read_from_this_nodes_camera():
    health = make_health()
    online(health, stats(camera="some-other-camera"))
    assert health.evaluate(link_online=True).detector_ok is False


def test_an_average_landing_on_ten_after_real_inference_stays_true():
    # The running average can round to 10.0 on a detector really taking 10 ms.
    health = make_health()
    online(health, stats(detectors=HAILO_WORKING))
    health.update(STATS, stats(detectors=HAILO_AT_START))
    assert health.evaluate(link_online=True).detector_ok is True


def test_a_later_frigate_start_must_prove_inference_again():
    health = make_health()
    online(health, stats(detectors=HAILO_WORKING))
    health.update(STATS, stats(detectors=HAILO_AT_START, started=STARTED + 600))
    assert health.evaluate(link_online=True).detector_ok is False


@pytest.mark.parametrize("payload", [b"offline", b"stopped"])
def test_frigate_going_away_forgets_the_inference_proof(payload):
    # Its start time in the next stats could look the same to the second.
    health = make_health()
    online(health, stats(detectors=HAILO_WORKING))
    health.update(AVAILABLE, payload)
    online(health, stats(detectors=HAILO_AT_START))
    assert health.evaluate(link_online=True).detector_ok is False


def test_stats_with_no_start_time_cannot_carry_the_proof_over():
    health = make_health()
    online(health, stats(detectors=HAILO_WORKING))
    no_uptime = json.loads(stats(detectors=HAILO_AT_START))
    del no_uptime["service"]["uptime"]
    health.update(STATS, json.dumps(no_uptime).encode())
    assert health.evaluate(link_online=True).detector_ok is False


def test_a_lost_local_broker_keeps_a_real_value_provable():
    health = make_health()
    online(health, stats(detectors=HAILO_WORKING))
    health.reset()
    # The starting value could not earn the proof again, so a true here is the proof kept.
    online(health, stats(detectors=HAILO_AT_START))
    assert health.evaluate(link_online=True).detector_ok is True


# --- frozen stats ------------------------------------------------------------


def test_a_frozen_stats_timestamp_reports_all_false():
    # Arriving now does not make them current: a stuck publisher can repeat old stats.
    health = make_health()
    online(health, stats(last_updated=WALL - STALE_S - 1))
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


def test_a_stats_timestamp_at_the_limit_still_counts():
    health = make_health()
    online(health, stats(last_updated=WALL - STALE_S))
    assert flags(health.evaluate(link_online=True)) == (True, True, True)


def test_a_stats_timestamp_from_the_future_reports_all_false():
    health = make_health()
    online(health, stats(last_updated=WALL + STALE_S + 1))
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


def test_frozen_stats_go_stale_while_the_clock_runs():
    now = [WALL]
    health = make_health(wall_fn=lambda: now[0])
    online(health, stats())
    assert flags(health.evaluate(link_online=True)) == (True, True, True)
    now[0] = WALL + STALE_S + 1
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


@pytest.mark.parametrize("value", [None, "1800000000", True, float("nan")])
def test_a_missing_or_bad_stats_timestamp_reports_all_false(value):
    health = make_health()
    payload = json.loads(stats())
    payload["service"]["last_updated"] = value
    online(health, json.dumps(payload).encode())
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


def test_stats_with_no_service_section_report_all_false():
    health = make_health()
    payload = json.loads(stats())
    del payload["service"]
    online(health, json.dumps(payload).encode())
    assert flags(health.evaluate(link_online=True)) == (False, False, False)


# --- recording_ok ------------------------------------------------------------


@pytest.mark.parametrize(
    "processes",
    [
        {},
        {"recording": {"pid": 0}},
        {"recording": {"pid": None}},
        {"recording": 77},
        {"review_segment": {"pid": 77}},
    ],
)
def test_recording_ok_needs_frigates_recording_process(processes):
    health = make_health()
    online(health, stats(processes=processes))
    state = health.evaluate(link_online=True)
    assert state.recording_ok is False
    assert state.camera_online is True


def test_recording_ok_with_no_process_section_is_false():
    health = make_health()
    payload = json.loads(stats())
    del payload["processes"]
    online(health, json.dumps(payload).encode())
    assert health.evaluate(link_online=True).recording_ok is False


def test_recording_ok_does_not_depend_on_detection():
    health = make_health()
    online(health, stats(detectors=HAILO_AT_START, detection_enabled=False))
    assert health.evaluate(link_online=True).recording_ok is True


# --- camera-link's state file and the node address ---------------------------


def link_health(tmp_path, content, *, address=None, age=0.0) -> FrigateHealth:
    path = tmp_path / "state.json"
    if isinstance(content, (bytes, str)):
        path.write_bytes(content.encode() if isinstance(content, str) else content)
    elif content is not None:
        path.write_text(
            json.dumps(
                {
                    "detector_error": False,
                    "log_read_at": int(WALL),
                    "record_writing": True,
                    "updated": int(WALL - age),
                    **content,
                }
            )
        )
    return FrigateHealth(
        camera=CAMERA,
        stale_after_s=STALE_S,
        monotonic_fn=Mono(),
        wall_fn=wall,
        link_state_path=path,
        address_fn=lambda: address,
    )


def test_all_link_attributes_are_carried(tmp_path):
    health = link_health(
        tmp_path,
        {"state": "needs_reset", "retry_after": 1800000100.5, "reason": "limit", "mac": "x"},
    )
    state = health.evaluate(link_online=True)
    assert state.camera_link_state == "needs_reset"
    assert state.retry_after == 1800000100.5
    assert state.reason == "limit"


def test_unset_link_fields_stay_unset(tmp_path):
    state = link_health(tmp_path, {"state": "adopted", "retry_after": None, "reason": None}).evaluate(
        link_online=True
    )
    assert state.camera_link_state == "adopted"
    assert state.retry_after is None
    assert state.reason is None


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b"not json{",
        b"\xff\xfe",
        b"[]",
        b"null",
        {"state": 7},
        {"state": ""},
        {"reason": "no state"},
    ],
)
def test_an_unusable_state_file_gives_no_attributes(tmp_path, content):
    state = link_health(tmp_path, content).evaluate(link_online=True)
    assert state.camera_link_state is None
    assert state.retry_after is None
    assert state.reason is None


def test_a_state_path_that_is_a_directory_is_not_an_error(tmp_path):
    health = FrigateHealth(
        camera=CAMERA, stale_after_s=STALE_S, wall_fn=wall, link_state_path=tmp_path
    )
    assert health.evaluate(link_online=True).camera_link_state is None


@pytest.mark.parametrize("retry", ["soon", True, float("inf")])
def test_a_bad_retry_after_is_dropped(tmp_path, retry):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"state": "failed", "updated": WALL, "retry_after": retry}))
    health = FrigateHealth(
        camera=CAMERA, stale_after_s=STALE_S, wall_fn=wall, link_state_path=path
    )
    state = health.evaluate(link_online=True)
    assert state.camera_link_state == "failed"
    assert state.retry_after is None


def test_a_detector_error_makes_detector_ok_false_and_touches_nothing_else(tmp_path):
    health = link_health(tmp_path, {"state": "adopted", "detector_error": True})
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.detector_ok is False
    assert state.camera_online is True
    assert state.recording_ok is True


@pytest.mark.parametrize("claim", ["yes", 1, 0, {}, None])
def test_a_detector_error_that_is_not_plainly_false_counts_as_a_claim(tmp_path, claim):
    health = link_health(tmp_path, {"state": "adopted", "detector_error": claim})
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is False


def test_a_clean_fresh_report_lets_the_stats_decide(tmp_path):
    health = link_health(tmp_path, {"state": "adopted"})
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is True


def test_detector_ok_is_false_with_no_report_at_all(tmp_path):
    # camera-link is the only thing that can see a crash, and it is not there to say.
    health = link_health(tmp_path, None)
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.detector_ok is False
    assert state.camera_online is True


@pytest.mark.parametrize("content", [b"", b"not json{", b"\xff\xfe", b"[]", b"null"])
def test_detector_ok_is_false_with_a_corrupt_report(tmp_path, content):
    health = link_health(tmp_path, content)
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is False


def test_detector_ok_is_false_when_the_report_does_not_mention_the_detector(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"state": "adopted", "updated": WALL, "log_read_at": WALL}))
    health = FrigateHealth(
        camera=CAMERA, stale_after_s=STALE_S, wall_fn=wall, link_state_path=path
    )
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is False


def test_a_state_file_at_the_age_limit_is_believed(tmp_path):
    health = link_health(tmp_path, {"state": "adopted"}, age=300)
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state == "adopted"
    assert state.detector_ok is True


@pytest.mark.parametrize("age", [301, -301])
@pytest.mark.parametrize("claim", [True, False])
def test_a_stale_state_file_gives_no_attributes_and_no_true(tmp_path, age, claim):
    health = link_health(
        tmp_path, {"state": "needs_reset", "reason": "old", "detector_error": claim}, age=age
    )
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state is None
    assert state.reason is None
    assert state.detector_ok is False
    assert state.recording_ok is False


@pytest.mark.parametrize("updated", [None, "now", True])
def test_a_state_file_with_no_usable_time_is_not_believed(tmp_path, updated):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"state": "adopted", "updated": updated, "detector_error": False})
    )
    health = FrigateHealth(
        camera=CAMERA, stale_after_s=STALE_S, wall_fn=wall, link_state_path=path
    )
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state is None
    assert state.detector_ok is False


# --- the log scan's own evidence -------------------------------------------------


def test_a_log_read_at_the_limit_counts(tmp_path):
    health = link_health(tmp_path, {"state": "adopted", "log_read_at": WALL - 90})
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is True


@pytest.mark.parametrize(
    "read_at", [WALL - 91, WALL + 91, None, "recently", True, 0], ids=str
)
def test_no_recent_log_read_is_unknown_and_so_false(tmp_path, read_at):
    # A clean detector_error from a scan that has not run is only its starting value.
    health = link_health(tmp_path, {"state": "adopted", "log_read_at": read_at})
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.detector_ok is False
    assert state.camera_link_state == "adopted"


# --- a detector that answers without inferring -----------------------------------


def hailo(speed=7.65, started=0.0):
    return {"hailo": {"inference_speed": speed, "detection_start": started, "pid": 42}}


def test_a_detector_timing_out_from_the_start_never_earns_the_proof():
    # hailo8l.py:343 gives up after 1 s and returns zeros, and base.py:182 averages that in:
    # (10 * 9 + 1000) / 10 is 109 ms, with a live pid and no crash.
    health = make_health()
    online(health, stats(detectors=hailo(109.0)))
    assert health.evaluate(link_online=True).detector_ok is False


@pytest.mark.parametrize("speed", [100.0, 109.0, 1000.0])
def test_a_working_detector_that_starts_timing_out_goes_false(speed):
    health = make_health()
    online(health, stats(detectors=hailo()))
    health.update(STATS, stats(detectors=hailo(speed)))
    assert health.evaluate(link_online=True).detector_ok is False


def test_just_below_the_ceiling_counts():
    # Whatever the average was, one 1000 ms sample leaves it at 100 ms or more.
    health = make_health()
    online(health, stats(detectors=hailo(99.99)))
    assert health.evaluate(link_online=True).detector_ok is True


def test_a_detector_recovered_below_the_ceiling_reads_true_again():
    health = make_health()
    online(health, stats(detectors=hailo()))
    health.update(STATS, stats(detectors=hailo(109.0)))
    health.update(STATS, stats(detectors=hailo(98.8)))
    assert health.evaluate(link_online=True).detector_ok is True


def test_a_detection_stuck_for_over_ten_seconds_is_false():
    # watchdog.py:125 calls the same thing stuck, and restarting keeps the speed.
    health = make_health()
    online(health, stats(detectors=hailo(started=WALL - 60)))
    assert health.evaluate(link_online=True).detector_ok is False


def test_a_detection_running_right_now_is_no_fault():
    health = make_health()
    online(health, stats(detectors=hailo(started=WALL - 10)))
    assert health.evaluate(link_online=True).detector_ok is True


def test_detection_start_is_timed_against_the_stats_own_clock():
    health = make_health()
    online(health, stats(detectors=hailo(started=WALL - 70), last_updated=WALL - 60))
    assert health.evaluate(link_online=True).detector_ok is True


@pytest.mark.parametrize("started", [None, "0", True, float("nan")])
def test_an_unreadable_detection_start_is_false(started):
    health = make_health()
    online(health, stats(detectors=hailo(started=started)))
    assert health.evaluate(link_online=True).detector_ok is False


# --- recording_ok needs segments really being written ------------------------------


def test_recording_ok_with_segments_being_written(tmp_path):
    health = link_health(tmp_path, {"state": "adopted", "record_writing": True})
    online(health, stats())
    assert health.evaluate(link_online=True).recording_ok is True


def test_a_dead_main_stream_is_not_recording(tmp_path):
    # The sub stream keeps camera_fps up, and the setting stays ON.
    health = link_health(tmp_path, {"state": "adopted", "record_writing": False})
    online(health, stats(camera_fps=5.1))
    state = health.evaluate(link_online=True)
    assert state.recording_ok is False
    assert state.camera_online is True
    assert state.detector_ok is True


@pytest.mark.parametrize("claim", [None, "true", 1])
def test_record_writing_must_be_exactly_true(tmp_path, claim):
    health = link_health(tmp_path, {"state": "adopted", "record_writing": claim})
    online(health, stats())
    assert health.evaluate(link_online=True).recording_ok is False


def test_recording_ok_is_false_with_no_report(tmp_path):
    health = link_health(tmp_path, None)
    online(health, stats())
    assert health.evaluate(link_online=True).recording_ok is False


def test_segments_alone_do_not_make_recording_ok(tmp_path):
    health = link_health(tmp_path, {"state": "adopted", "record_writing": True})
    online(health, stats(camera_fps=0.0))
    assert health.evaluate(link_online=True).recording_ok is False


# --- malformed payloads never raise ------------------------------------------------

HUGE = "1" + "0" * 400


@pytest.mark.parametrize(
    "payload",
    [
        ('{"service": {"last_updated": %s, "uptime": 1}}' % HUGE).encode(),
        ('{"service": {"last_updated": 1800000000, "uptime": %s}}' % HUGE).encode(),
        ('{"cameras": {"cam-01": {"camera_fps": %s}}}' % HUGE).encode(),
        ('{"detectors": {"hailo": {"inference_speed": %s, "pid": 1}}}' % HUGE).encode(),
        ('{"detectors": {"hailo": {"detection_start": %s, "pid": 1}}}' % HUGE).encode(),
        ('{"processes": {"recording": {"pid": %s}}}' % HUGE).encode(),
        b"[" * 200000,
        b'{"a":' * 200000,
    ],
    ids=["updated", "uptime", "fps", "speed", "start", "pid", "array-nest", "object-nest"],
)
def test_a_hostile_stats_payload_never_raises_and_is_no_evidence(payload):
    health = make_health()
    feed_healthy(health)
    health.update(STATS, payload)
    assert flags(health.evaluate(link_online=True))[1] is False


@pytest.mark.parametrize(
    "content",
    [
        ('{"state": "adopted", "updated": %s}' % HUGE).encode(),
        ('{"state": "adopted", "updated": 1800000000, "retry_after": %s}' % HUGE).encode(),
        ('{"state": "adopted", "updated": 1800000000, "log_read_at": %s}' % HUGE).encode(),
        b"[" * 200000,
        b'{"a":' * 200000,
    ],
    ids=["updated", "retry", "read-at", "array-nest", "object-nest"],
)
def test_a_hostile_state_file_never_raises_and_gives_no_true(tmp_path, content):
    health = link_health(tmp_path, content)
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is False


def test_attributes_survive_frigate_being_down(tmp_path):
    health = link_health(tmp_path, {"state": "failed", "reason": "why"}, address="10.0.0.9")
    state = health.evaluate(link_online=True)
    assert (state.camera_link_state, state.reason, state.node_address) == (
        "failed",
        "why",
        "10.0.0.9",
    )
    assert flags(state) == (False, False, False)


def test_interface_ipv4_reads_a_real_interface():
    assert interface_ipv4("lo") == "127.0.0.1"


def test_interface_ipv4_of_a_missing_interface_is_none():
    assert interface_ipv4("nosuchif0") is None


def test_interface_ipv4_never_reports_the_setup_ap_address(monkeypatch):
    # The central node could not reach the node there, and a queued heartbeat would say so.
    monkeypatch.setattr("camerabridge.health._read_ipv4", lambda interface: "10.42.0.1")
    assert interface_ipv4("wlan0") is None
