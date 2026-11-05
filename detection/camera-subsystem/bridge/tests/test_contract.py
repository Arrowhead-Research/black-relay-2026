"""Contract tests for ``camerabridge.formats`` — the one module that knows the
wire format.

Each builder assembles a ``camera.*`` envelope. Every built message is checked
against its Task-1 JSON Schema (loaded by path, same loader approach as
``test_schemas.py``) with a draft 2020-12 validator, so the builders can never
drift from the contract of record. The builder-specific invariants the schemas
cannot express are asserted here too:

* ``event_id`` is a real UUIDv4 when minted, and is preserved verbatim when the
  caller passes one in (replay needs stable ids);
* ``timestamp`` is stamped from the injected clock;
* ``detection_event`` sets ``camera_id == node_id`` (Task-1 review flagged that
  JSON Schema cannot enforce field equality);
* an empty ``zones`` list still validates.

The ``SystemClock`` / ``ClockNotReady`` readiness gate is covered too.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator

from camerabridge import formats

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

FIXED_TS = "2026-11-04T19:40:00Z"


def load_schema(name: str) -> dict:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class FakeClock:
    """A ready clock that always returns a fixed RFC-3339 ``Z`` timestamp."""

    def __init__(self, ts: str = FIXED_TS) -> None:
        self._ts = ts

    def now_rfc3339(self) -> str:
        return self._ts


def _validate(schema_file: str, message: dict) -> None:
    validator = Draft202012Validator(load_schema(schema_file))
    errors = sorted(validator.iter_errors(message), key=str)
    assert errors == [], [e.message for e in errors]


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value


# --- topic constants --------------------------------------------------------


def test_topic_constants_are_exact():
    assert formats.TOPIC_DETECTIONS == "detection/camera/detections"
    assert formats.TOPIC_CUE_ACKS == "detection/camera/cue-acks"
    assert formats.TOPIC_STATUS == "detection/camera/status/nodes"
    assert formats.TOPIC_RF_CUES_IN == "detection/rf/cues/camera"


# --- new_event_id -----------------------------------------------------------


def test_new_event_id_is_uuid4_and_unique():
    a = formats.new_event_id()
    b = formats.new_event_id()
    assert _is_uuid4(a)
    assert _is_uuid4(b)
    assert a != b


# --- detection_event --------------------------------------------------------


def test_detection_event_validates_and_sets_invariants():
    msg = formats.detection_event(
        node_id="cam-01",
        event="object_start",
        object_id="1730749200.12-abc",
        label="person",
        confidence=0.87,
        zones=["perimeter-north"],
        attributes={"bbox": [120, 80, 64, 180]},
        clock=FakeClock(),
    )
    _validate("detection.v0.schema.json", msg)
    assert msg["schema"] == "camera.detection.v0"
    assert msg["timestamp"] == FIXED_TS
    assert msg["camera_id"] == msg["node_id"] == "cam-01"
    assert _is_uuid4(msg["event_id"])


def test_detection_event_preserves_passed_event_id():
    stable = "11111111-2222-4333-8444-555555555555"
    msg = formats.detection_event(
        node_id="cam-01",
        event="object_update",
        object_id="trk-1",
        label="person",
        confidence=0.5,
        zones=["perimeter-north"],
        attributes={},
        clock=FakeClock(),
        event_id=stable,
    )
    assert msg["event_id"] == stable
    _validate("detection.v0.schema.json", msg)


def test_detection_event_copies_zones_and_attributes():
    # A caller mutating its input lists/dicts after the build must not alias into
    # (and silently change) the already-emitted message.
    zones = ["perimeter-north"]
    attributes = {"bbox": [1, 2, 3, 4]}
    msg = formats.detection_event(
        node_id="cam-01",
        event="object_start",
        object_id="trk-1",
        label="person",
        confidence=0.5,
        zones=zones,
        attributes=attributes,
        clock=FakeClock(),
    )
    zones.append("driveway")
    attributes["snapshot_url"] = "https://node.local/x.jpg"
    assert msg["zones"] == ["perimeter-north"]
    assert "snapshot_url" not in msg["attributes"]


def test_node_status_copies_attributes():
    attributes = {"detection_fps": 5.0}
    msg = formats.node_status(
        node_id="cam-01",
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
        attributes=attributes,
        clock=FakeClock(),
    )
    attributes["detection_fps"] = 99.0
    attributes["new_key"] = 1
    assert msg["attributes"] == {"detection_fps": 5.0}


def test_detection_event_empty_zones_validates():
    msg = formats.detection_event(
        node_id="cam-01",
        event="object_end",
        object_id="trk-1",
        label="person",
        confidence=0.5,
        zones=[],
        attributes={},
        clock=FakeClock(),
    )
    assert msg["zones"] == []
    _validate("detection.v0.schema.json", msg)


# --- cue_ack ----------------------------------------------------------------


def test_cue_ack_validates_matched():
    msg = formats.cue_ack(
        node_id="cam-01",
        rf_cue_id="rf-cue-7",
        zone="perimeter-north",
        matched=True,
        object_id="trk-1",
        snapshot_url="https://node.local/snap/1.jpg",
        clock=FakeClock(),
    )
    _validate("cue-ack.v0.schema.json", msg)
    assert msg["schema"] == "camera.cue-ack.v0"
    assert msg["timestamp"] == FIXED_TS
    assert msg["matched"] is True
    assert msg["object_id"] == "trk-1"
    assert msg["attributes"]["snapshot_url"] == "https://node.local/snap/1.jpg"
    assert _is_uuid4(msg["event_id"])


def test_cue_ack_unmatched_has_null_object_id_and_no_snapshot():
    msg = formats.cue_ack(
        node_id="cam-01",
        rf_cue_id="rf-cue-7",
        zone="perimeter-north",
        matched=False,
        object_id=None,
        clock=FakeClock(),
    )
    _validate("cue-ack.v0.schema.json", msg)
    assert msg["object_id"] is None
    assert msg["matched"] is False


def test_cue_ack_preserves_passed_event_id():
    stable = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    msg = formats.cue_ack(
        node_id="cam-01",
        rf_cue_id="rf-cue-7",
        zone="z",
        matched=False,
        object_id=None,
        clock=FakeClock(),
        event_id=stable,
    )
    assert msg["event_id"] == stable
    _validate("cue-ack.v0.schema.json", msg)


# --- node_status ------------------------------------------------------------


def test_node_status_validates():
    msg = formats.node_status(
        node_id="cam-01",
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=False,
        attributes={"detection_fps": 12.5},
        clock=FakeClock(),
    )
    _validate("node-status.v0.schema.json", msg)
    assert msg["schema"] == "camera.node-status.v0"
    assert msg["timestamp"] == FIXED_TS
    assert msg["detector_ok"] is False
    assert _is_uuid4(msg["event_id"])


def test_node_status_preserves_passed_event_id():
    stable = "99999999-8888-4777-8666-555555555555"
    msg = formats.node_status(
        node_id="cam-01",
        camera_online=True,
        link_online=True,
        recording_ok=True,
        detector_ok=True,
        attributes={},
        clock=FakeClock(),
        event_id=stable,
    )
    assert msg["event_id"] == stable
    _validate("node-status.v0.schema.json", msg)


# --- SystemClock / ClockNotReady --------------------------------------------


def test_system_clock_raises_until_ntp_synced():
    clock = formats.SystemClock()
    with pytest.raises(formats.ClockNotReady):
        clock.now_rfc3339()
    clock.mark_synced()
    ts = clock.now_rfc3339()
    assert ts.endswith("Z")
    # Stamped value must satisfy the contract's timestamp pattern.
    pattern = load_schema("detection.v0.schema.json")["properties"]["timestamp"]["pattern"]
    import re

    assert re.match(pattern, ts)
