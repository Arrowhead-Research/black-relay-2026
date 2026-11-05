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
