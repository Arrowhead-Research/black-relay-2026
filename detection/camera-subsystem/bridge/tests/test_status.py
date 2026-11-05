"""Tests for ``camerabridge.status`` — node heartbeat builder.

Each edge node periodically publishes a ``camera.node-status.v0`` heartbeat on
``detection/camera/status/nodes`` (``formats.TOPIC_STATUS``) so fusion knows the
node is alive and healthy. ``status.py`` is a thin adapter: it gathers a node's
health into a :class:`~camerabridge.status.NodeState` and lowers it to a
schema-valid envelope via :func:`formats.node_status`.

This module checks that :func:`~camerabridge.status.build_status`:

* produces a message that validates against the Task-1 node-status JSON Schema
  (same loader approach as ``test_cues.py`` / ``test_contract.py``);
* carries the four health booleans from the :class:`NodeState` verbatim;
* folds the optional attribute fields into the open ``attributes`` sub-object
  only when they are set (mirroring ``formats.cue_ack``'s ``snapshot_url``), so a
  bare state yields an empty ``attributes`` and omits the keys entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema.validators import Draft202012Validator

from camerabridge.health import FrigateHealth
from camerabridge.status import NodeState, build_status

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

FIXED_TS = "2026-11-04T19:40:01Z"


def load_schema(name: str) -> dict:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_status(message: dict) -> None:
    validator = Draft202012Validator(load_schema("node-status.v0.schema.json"))
    errors = sorted(validator.iter_errors(message), key=str)
    assert errors == [], [e.message for e in errors]


class FakeClock:
    """A ready clock that always returns a fixed RFC-3339 ``Z`` timestamp."""

    def __init__(self, ts: str = FIXED_TS) -> None:
        self._ts = ts

    def now_rfc3339(self) -> str:
        return self._ts


# --- the happy path: healthy node -------------------------------------------


def test_build_status_all_healthy_validates():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["schema"] == "camera.node-status.v0"
    assert msg["node_id"] == "cam-01"
    assert msg["timestamp"] == FIXED_TS


def test_build_status_carries_the_four_booleans():
    state = NodeState(
        camera_online=True,
        link_online=False,
        recording_ok=True,
        detector_ok=False,
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["camera_online"] is True
    assert msg["link_online"] is False
    assert msg["recording_ok"] is True
    assert msg["detector_ok"] is False


def test_build_status_all_unhealthy_still_validates():
    state = NodeState(
        camera_online=False,
        link_online=False,
        recording_ok=False,
        detector_ok=False,
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["camera_online"] is False
    assert msg["link_online"] is False
    assert msg["recording_ok"] is False
    assert msg["detector_ok"] is False


# --- attributes: optional fields folded in only when set --------------------


def test_build_status_bare_state_has_empty_attributes():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["attributes"] == {}


def test_build_status_folds_optional_attributes():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
        frigate_version="0.14.1",
        detection_fps=5.0,
        storage_free_pct=42.5,
        uplink_rssi_dbm=-67,
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["attributes"] == {
        "frigate_version": "0.14.1",
        "detection_fps": 5.0,
        "storage_free_pct": 42.5,
        "uplink_rssi_dbm": -67,
    }


def test_build_status_omits_unset_optional_attributes():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
        frigate_version="0.14.1",
    )
    msg = build_status(state, "cam-01", FakeClock())
    validate_status(msg)
    assert msg["attributes"] == {"frigate_version": "0.14.1"}
    assert "detection_fps" not in msg["attributes"]
    assert "storage_free_pct" not in msg["attributes"]
    assert "uplink_rssi_dbm" not in msg["attributes"]


def test_build_status_defaults_are_none():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
    )
    assert state.frigate_version is None
    assert state.detection_fps is None
    assert state.storage_free_pct is None
    assert state.uplink_rssi_dbm is None


def test_build_status_mints_event_id():
    state = NodeState(
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
    )
    msg = build_status(state, "cam-01", FakeClock())
    assert isinstance(msg["event_id"], str)
    assert msg["event_id"]


# --- the heartbeat as Frigate's topics and camera-link's state file make it -----

STALE_S = 90.0
WALL = 1_800_000_000.0
RECORDINGS = "frigate/cam-01/recordings/state"
HEALTHY_REPORT = {"detector_error": False, "log_read_at": int(WALL), "record_writing": True}


def frigate_stats(
    *, fps=5.1, inference_speed=7.65, detection_enabled=True, detection_fps=20.2, age=0.0
) -> bytes:
    """/api/stats as Frigate 0.18.2 sent it on kit 1, cut down to what the bridge reads."""
    last_updated = WALL - age
    return json.dumps(
        {
            "cameras": {
                "cam-01": {
                    "camera_fps": fps,
                    "detection_fps": detection_fps,
                    "detection_enabled": detection_enabled,
                }
            },
            "detectors": {
                "hailo": {"inference_speed": inference_speed, "detection_start": 0.0, "pid": 42}
            },
            "processes": {"recording": {"pid": 77}},
            "service": {"version": "0.18.2", "uptime": 3600, "last_updated": last_updated},
        }
    ).encode()


def heartbeat(tmp_path, stats_payload, *, link=None, address=None) -> dict:
    state_file = tmp_path / "state.json"
    if link is not None:
        state_file.write_text(json.dumps({"updated": int(WALL), **HEALTHY_REPORT, **link}))
    health = FrigateHealth(
        camera="cam-01",
        stale_after_s=STALE_S,
        wall_fn=lambda: WALL,
        link_state_path=state_file,
        address_fn=lambda: address,
    )
    health.update("frigate/available", b"online")
    health.update(RECORDINGS, b"ON")
    if stats_payload is not None:
        health.update("frigate/stats", stats_payload)
    message = build_status(health.evaluate(link_online=True), "cam-01", FakeClock())
    validate_status(message)
    return message


def test_detector_ok_false_when_detection_disabled(tmp_path):
    stats = frigate_stats(inference_speed=10.0, detection_enabled=False, detection_fps=0.0)
    message = heartbeat(tmp_path, stats)
    assert message["detector_ok"] is False
    assert message["camera_online"] is True


def test_detector_ok_false_when_inference_never_ran_since_start(tmp_path):
    message = heartbeat(tmp_path, frigate_stats(inference_speed=10.0, detection_fps=0.0))
    assert message["detector_ok"] is False


def test_detector_ok_false_when_a_hailo_failure_is_reported(tmp_path):
    link = {"state": "adopted", "detector_error": True}
    assert heartbeat(tmp_path, frigate_stats(), link=link)["detector_ok"] is False


def test_detector_ok_true_when_idle_after_real_inference(tmp_path):
    message = heartbeat(tmp_path, frigate_stats(detection_fps=0.0), link={"state": "adopted"})
    assert message["detector_ok"] is True


def test_camera_online_false_at_zero_fps_and_on_stale_stats(tmp_path):
    assert heartbeat(tmp_path, frigate_stats(fps=0.0))["camera_online"] is False
    assert heartbeat(tmp_path, frigate_stats(age=STALE_S + 1))["camera_online"] is False
    assert heartbeat(tmp_path, None)["camera_online"] is False


def test_recording_ok_false_when_camera_is_offline(tmp_path):
    message = heartbeat(tmp_path, frigate_stats(fps=0.0), link={"state": "adopted"})
    assert message["recording_ok"] is False


def test_heartbeat_attributes_carry_the_camera_link_state(tmp_path):
    link = {"state": "needs_reset", "retry_after": 1800000100, "reason": "the camera needs a reset"}
    assert heartbeat(tmp_path, frigate_stats(), link=link)["attributes"] == {
        "camera_link_state": "needs_reset",
        "retry_after": 1800000100,
        "reason": "the camera needs a reset",
    }


def test_unset_retry_after_and_reason_are_omitted(tmp_path):
    link = {"state": "adopted", "retry_after": None, "reason": None}
    assert heartbeat(tmp_path, frigate_stats(), link=link)["attributes"] == {
        "camera_link_state": "adopted"
    }


def test_missing_state_file_omits_the_attribute(tmp_path):
    message = heartbeat(tmp_path, frigate_stats())
    assert message["attributes"] == {}
    # The only thing that can see a detector crash is not there, so nothing proves it healthy.
    assert message["detector_ok"] is False
    assert message["recording_ok"] is False


def test_heartbeat_attributes_carry_the_node_address(tmp_path):
    message = heartbeat(tmp_path, frigate_stats(), address="192.168.8.21")
    assert message["attributes"] == {"node_address": "192.168.8.21"}


def test_no_wlan0_address_omits_the_attribute(tmp_path):
    assert "node_address" not in heartbeat(tmp_path, frigate_stats(), address=None)["attributes"]
