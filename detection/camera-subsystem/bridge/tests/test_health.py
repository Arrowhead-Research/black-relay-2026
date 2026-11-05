"""Tests for ``camerabridge.health``: Frigate MQTT topics to the four health booleans.

The payload shapes follow Frigate's documented topics (``frigate/available``,
``frigate/stats``, ``frigate/<camera>/recordings/state``). The rule under test is
fail-safe: no data, stale stats, or Frigate offline must report False, never True.
"""

from __future__ import annotations

import json

import pytest

from camerabridge.health import FrigateHealth, interface_ipv4

CAMERA = "cam-01"
STALE_S = 90.0
AVAILABLE = "frigate/available"
STATS = "frigate/stats"
RECORDINGS = f"frigate/{CAMERA}/recordings/state"


WALL = 1_800_000_000.0
STARTED = WALL - 3600


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
        camera=camera, stale_after_s=STALE_S, monotonic_fn=mono or Mono(), wall_fn=wall_fn
    )


def feed_healthy(health: FrigateHealth) -> None:
    health.update(AVAILABLE, b"online")
    health.update(RECORDINGS, b"ON")
    health.update(STATS, stats())


def flags(state) -> tuple[bool, bool, bool]:
    return state.camera_online, state.detector_ok, state.recording_ok


def test_topics_follow_the_configured_prefix_and_camera():
    assert set(make_health().topics) == {AVAILABLE, STATS, RECORDINGS}
    health = FrigateHealth(camera="front", topic_prefix="site1", stale_after_s=STALE_S, wall_fn=wall)
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
    online(health, stats(detectors=HAILO_WORKING))
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
        path.write_text(json.dumps({"updated": int(WALL - age), **content}))
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


@pytest.mark.parametrize("claim", ["yes", 1, 0, {}])
def test_a_detector_error_that_is_not_plainly_false_counts_as_a_claim(tmp_path, claim):
    health = link_health(tmp_path, {"state": "adopted", "detector_error": claim})
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is False


@pytest.mark.parametrize(
    "report", [{"state": "adopted", "detector_error": False}, {"state": "adopted"}, None, b"nope"]
)
def test_no_error_claim_leaves_detector_ok_to_the_stats(tmp_path, report):
    health = link_health(tmp_path, report)
    online(health, stats())
    assert health.evaluate(link_online=True).detector_ok is True


def test_a_state_file_at_the_age_limit_is_believed(tmp_path):
    health = link_health(tmp_path, {"state": "adopted", "detector_error": True}, age=300)
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state == "adopted"
    assert state.detector_ok is False


@pytest.mark.parametrize("age", [301, -301])
def test_a_stale_state_file_is_not_believed_either_way(tmp_path, age):
    health = link_health(
        tmp_path, {"state": "needs_reset", "reason": "old", "detector_error": True}, age=age
    )
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state is None
    assert state.reason is None
    # Its claim of an error is as old as the rest of it.
    assert state.detector_ok is True


@pytest.mark.parametrize("updated", [None, "now", True])
def test_a_state_file_with_no_usable_time_is_not_believed(tmp_path, updated):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"state": "adopted", "updated": updated, "detector_error": True}))
    health = FrigateHealth(
        camera=CAMERA, stale_after_s=STALE_S, wall_fn=wall, link_state_path=path
    )
    online(health, stats())
    state = health.evaluate(link_online=True)
    assert state.camera_link_state is None
    assert state.detector_ok is True


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
