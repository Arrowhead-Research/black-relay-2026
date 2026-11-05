"""Contract tests for the camera subsystem v0 JSON Schemas.

Each schema file is loaded by path and checked two ways:

* the canonical example object from ``DESIGN.md`` sections 5.2-5.4 validates, and
* three deliberately-broken variants are rejected -- a message missing
  ``event_id``, a ``timestamp`` without the required ``Z`` suffix, and a message
  carrying an extra top-level key (``additionalProperties: false``).

These schemas are the contract of record consumed by every later task and by
other teams, so the valid samples are pinned to the design's example shapes.

One adaptation: ``DESIGN.md`` 5.2 prints ``event`` with pipe notation
(``"object_start | object_update | object_end"``) to show the three allowed
values. A concrete message carries exactly one enum member, so the detection
sample uses ``"object_start"``; the schema enforces the enum per the design and
the global constraints.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def load_schema(name: str) -> dict:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


DETECTION_SAMPLE = {
    "schema": "camera.detection.v0",
    "event_id": "b1e2...-uuid4",
    "timestamp": "2026-11-04T19:40:00Z",
    "node_id": "cam-01",
    "camera_id": "cam-01",
    "event": "object_start",
    "object_id": "1730749200.12-abc",
    "label": "person",
    "confidence": 0.87,
    "zones": ["perimeter-north"],
    "attributes": {
        "bbox": [120, 80, 64, 180],
        "area_px": 11520,
        "stationary": False,
        "sub_label": None,
        "snapshot_url": None,
        "clip_url": None,
        "rf_cue_id": None,
    },
}

NODE_STATUS_SAMPLE = {
    "schema": "camera.node-status.v0",
    "event_id": "c3d4...-uuid4",
    "timestamp": "2026-11-04T19:40:00Z",
    "node_id": "cam-01",
    "camera_online": True,
    "link_online": True,
    "recording_ok": True,
    "detector_ok": True,
    "attributes": {
        "frigate_version": None,
        "detection_fps": None,
        "storage_free_pct": None,
        "uplink_rssi_dbm": None,
    },
}

CUE_ACK_SAMPLE = {
    "schema": "camera.cue-ack.v0",
    "event_id": "d5e6...-uuid4",
    "timestamp": "2026-11-04T19:40:01Z",
    "node_id": "cam-01",
    "rf_cue_id": "rf-cue-...",
    "zone": "perimeter-north",
    "matched": True,
    "object_id": "1730749200.12-abc",
    "attributes": {"snapshot_url": None},
}

CASES = [
    ("detection.v0.schema.json", DETECTION_SAMPLE),
    ("node-status.v0.schema.json", NODE_STATUS_SAMPLE),
    ("cue-ack.v0.schema.json", CUE_ACK_SAMPLE),
]
CASE_IDS = [name for name, _ in CASES]


@pytest.mark.parametrize("schema_file, sample", CASES, ids=CASE_IDS)
def test_schema_document_is_valid_draft_2020_12(schema_file, sample):
    # Raises SchemaError if the schema file is not itself valid draft 2020-12.
    Draft202012Validator.check_schema(load_schema(schema_file))


@pytest.mark.parametrize("schema_file, sample", CASES, ids=CASE_IDS)
def test_canonical_sample_validates(schema_file, sample):
    validator = Draft202012Validator(load_schema(schema_file))
    errors = sorted(validator.iter_errors(sample), key=str)
    assert errors == [], [e.message for e in errors]


@pytest.mark.parametrize("schema_file, sample", CASES, ids=CASE_IDS)
def test_missing_event_id_is_rejected(schema_file, sample):
    bad = copy.deepcopy(sample)
    del bad["event_id"]
    assert not Draft202012Validator(load_schema(schema_file)).is_valid(bad)


@pytest.mark.parametrize("schema_file, sample", CASES, ids=CASE_IDS)
def test_timestamp_without_z_is_rejected(schema_file, sample):
    bad = copy.deepcopy(sample)
    bad["timestamp"] = "2026-11-04T19:40:00"  # dropped the trailing Z
    assert not Draft202012Validator(load_schema(schema_file)).is_valid(bad)


@pytest.mark.parametrize("schema_file, sample", CASES, ids=CASE_IDS)
def test_extra_top_level_key_is_rejected(schema_file, sample):
    bad = copy.deepcopy(sample)
    bad["unexpected_field"] = "nope"
    assert not Draft202012Validator(load_schema(schema_file)).is_valid(bad)
