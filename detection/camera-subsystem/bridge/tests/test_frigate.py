"""Tests for ``camerabridge.frigate`` — raw Frigate event → internal form.

``parse_frigate_event`` is the one place the subsystem reads Frigate's MQTT
``frigate/events`` shape. It consumes the raw payload and emits a small internal
:class:`~camerabridge.frigate.FrigateEvent` dataclass; later service wiring
(Task 7) feeds that into ``formats.detection_event``. This module intentionally
does **not** import ``formats`` — it is self-contained.

Covered here:

* the captured ``type:"new"`` fixture maps to ``event == "object_start"`` and
  passes ``after`` fields through (``id``→``object_id``, ``score``→``confidence``,
  ``box``→``bbox``, ``current_zones``→``zones``, ``area``→``area_px``,
  ``stationary``);
* ``update``→``object_update`` and ``end``→``object_end``;
* Frigate reports current state in ``after`` (not ``before``);
* ``snapshot_url`` passes through when present and defaults to ``None``;
* malformed payloads (``{}``, non-dict, missing ``after``, ``after`` not a dict,
  missing ``label``/``id``, unknown ``type``) return ``None`` — never raise.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from camerabridge.frigate import FrigateEvent, parse_frigate_event

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frigate_event.json"


def load_fixture() -> dict:
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


# --- the captured "new" fixture ---------------------------------------------


def test_new_fixture_maps_to_object_start_and_passes_fields_through():
    result = parse_frigate_event(load_fixture())
    assert isinstance(result, FrigateEvent)
    assert result.event == "object_start"
    assert result.object_id == "1730749200.123456-abc123"
    assert result.label == "person"
    assert result.confidence == 0.81
    assert result.zones == ["perimeter-north"]
    assert result.bbox == [120, 80, 64, 180]
    assert result.area_px == 11520
    assert result.stationary is False
    # A real Frigate "new" event has no snapshot yet.
    assert result.snapshot_url is None


def test_reads_current_state_from_after_not_before():
    # The fixture's `before` carries a different score/zones; we must read
    # `after`, the current state.
    result = parse_frigate_event(load_fixture())
    assert result.confidence == 0.81  # after.score, not before.score (0.42)
    assert result.zones == ["perimeter-north"]  # after, not before ([])


# --- type -> event mapping --------------------------------------------------


def test_update_maps_to_object_update():
    payload = load_fixture()
    payload["type"] = "update"
    result = parse_frigate_event(payload)
    assert result is not None
    assert result.event == "object_update"


def test_end_maps_to_object_end():
    payload = load_fixture()
    payload["type"] = "end"
    result = parse_frigate_event(payload)
    assert result is not None
    assert result.event == "object_end"


def test_unknown_type_returns_none():
    payload = load_fixture()
    payload["type"] = "bogus"
    assert parse_frigate_event(payload) is None


def test_missing_type_returns_none():
    payload = load_fixture()
    del payload["type"]
    assert parse_frigate_event(payload) is None


# --- optional-field handling ------------------------------------------------


def test_snapshot_url_passes_through_when_present():
    payload = load_fixture()
    payload["after"]["snapshot_url"] = "https://node.local/api/events/x/snapshot.jpg"
    result = parse_frigate_event(payload)
    assert result.snapshot_url == "https://node.local/api/events/x/snapshot.jpg"


def test_missing_optional_fields_use_safe_defaults():
    # Minimal valid payload: only the required type/id/label present.
    payload = {"type": "new", "after": {"id": "trk-1", "label": "car"}}
    result = parse_frigate_event(payload)
    assert result is not None
    assert result.confidence == 0.0
    assert result.zones == []
    assert result.bbox == []
    assert result.area_px == 0
    assert result.stationary is False
    assert result.snapshot_url is None


def test_null_current_zones_becomes_empty_list():
    payload = load_fixture()
    payload["after"]["current_zones"] = None
    result = parse_frigate_event(payload)
    assert result.zones == []


# --- malformed payloads return None (never raise) ---------------------------


def test_empty_dict_returns_none():
    assert parse_frigate_event({}) is None


def test_missing_after_returns_none():
    payload = load_fixture()
    del payload["after"]
    assert parse_frigate_event(payload) is None


def test_after_not_a_dict_returns_none():
    payload = load_fixture()
    payload["after"] = "not-a-dict"
    assert parse_frigate_event(payload) is None


def test_missing_label_returns_none():
    payload = load_fixture()
    del payload["after"]["label"]
    assert parse_frigate_event(payload) is None


def test_missing_id_returns_none():
    payload = load_fixture()
    del payload["after"]["id"]
    assert parse_frigate_event(payload) is None


def test_null_label_returns_none():
    payload = load_fixture()
    payload["after"]["label"] = None
    assert parse_frigate_event(payload) is None


def test_null_id_returns_none():
    payload = load_fixture()
    payload["after"]["id"] = None
    assert parse_frigate_event(payload) is None


@pytest.mark.parametrize("bad", [None, [], "string", 42])
def test_non_dict_payload_returns_none(bad):
    assert parse_frigate_event(bad) is None


def test_does_not_mutate_input_payload():
    payload = load_fixture()
    snapshot = copy.deepcopy(payload)
    parse_frigate_event(payload)
    assert payload == snapshot


# --- defensive copies: the event must not alias the payload's lists ----------


def test_zones_and_bbox_are_copied_out_of_payload():
    payload = load_fixture()
    result = parse_frigate_event(payload)
    # Mutating the SOURCE lists after parse must not change the built event.
    payload["after"]["current_zones"].append("driveway")
    payload["after"]["box"].append(999)
    assert result.zones == ["perimeter-north"]
    assert result.bbox == [120, 80, 64, 180]


# --- numeric coercion: a flaky upstream type can't leak a non-numeric through -


def test_numeric_fields_are_coerced_to_float_and_int():
    payload = load_fixture()
    payload["after"]["score"] = "0.81"  # stringified float from a flaky upstream
    payload["after"]["area"] = "11520"
    result = parse_frigate_event(payload)
    assert result is not None
    assert result.confidence == 0.81 and isinstance(result.confidence, float)
    assert result.area_px == 11520 and isinstance(result.area_px, int)


def test_non_numeric_score_is_treated_as_malformed():
    payload = load_fixture()
    payload["after"]["score"] = "not-a-number"
    # Non-coercible numeric → treated as malformed (None), never raises.
    assert parse_frigate_event(payload) is None
