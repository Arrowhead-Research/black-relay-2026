"""Tests for ``camerabridge.cues`` — RF cue consumer, matching, cue-ack.

The camera subsystem subscribes to the single ``detection/rf/cues/camera`` topic
and receives an ``rf.camera-cue.v0`` payload for every TPMS ``first_heard``. This
module exercises the three responsibilities of ``cues.py``:

* :func:`~camerabridge.cues.parse_cue` reads the **provisional** RF wire shape
  (``tests/fixtures/rf_cue.json``; timestamp carried as epoch seconds) into an
  :class:`~camerabridge.cues.RfCue`, returning ``None`` for malformed payloads
  rather than raising;
* :class:`~camerabridge.cues.CueMatcher` keeps a table of open camera tracks and
  matches a cue to one sharing its zone within ``±window_s`` — but only for zones
  the node actually covers;
* :func:`~camerabridge.cues.handle_cue` composes a schema-valid
  ``camera.cue-ack.v0`` via ``formats.cue_ack``, with ``matched`` reflecting the
  match outcome. A cue in an uncovered zone, with no open track, or outside the
  window yields ``matched:false`` and never raises.

The produced cue-ack is validated against its Task-1 JSON Schema (same loader
approach as ``test_contract.py``) so this layer can never drift from the contract.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator

from camerabridge import cues
from camerabridge.cues import CueMatcher, RfCue, handle_cue, parse_cue

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rf_cue.json"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

FIXED_TS = "2026-11-04T19:40:01Z"


def load_fixture() -> dict:
    with FIXTURE.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_schema(name: str) -> dict:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_ack(message: dict) -> None:
    validator = Draft202012Validator(load_schema("cue-ack.v0.schema.json"))
    errors = sorted(validator.iter_errors(message), key=str)
    assert errors == [], [e.message for e in errors]


class FakeClock:
    """A ready clock that always returns a fixed RFC-3339 ``Z`` timestamp."""

    def __init__(self, ts: str = FIXED_TS) -> None:
        self._ts = ts

    def now_rfc3339(self) -> str:
        return self._ts


def make_cue(zone: str = "perimeter-north", ts: float = 100.0) -> RfCue:
    """An :class:`RfCue` at a given zone/time for matcher tests."""
    return RfCue(
        rf_cue_id="rf-cue-7f3a9c",
        zone=zone,
        ts_epoch=ts,
        source_node="rf-node-07",
        tpms_id="tpms-4f2a1b",
    )


# --- parse_cue: the provisional fixture -------------------------------------


def test_parse_cue_fixture_returns_fields():
    cue = parse_cue(load_fixture())
    assert isinstance(cue, RfCue)
    assert cue.rf_cue_id == "rf-cue-7f3a9c"
    assert cue.zone == "perimeter-north"
    assert cue.ts_epoch == 1730749203.5
    assert cue.source_node == "rf-node-07"
    assert cue.tpms_id == "tpms-4f2a1b"


def test_parse_cue_accepts_numeric_string_timestamp():
    payload = load_fixture()
    payload["ts_epoch"] = "1730749203.5"
    cue = parse_cue(payload)
    assert cue is not None
    assert cue.ts_epoch == 1730749203.5


def test_parse_cue_optional_metadata_defaults_to_none():
    payload = {"rf_cue_id": "rf-1", "zone": "perimeter-north", "ts_epoch": 100.0}
    cue = parse_cue(payload)
    assert cue is not None
    assert cue.source_node is None
    assert cue.tpms_id is None


def test_parse_cue_does_not_mutate_input():
    payload = load_fixture()
    snapshot = copy.deepcopy(payload)
    parse_cue(payload)
    assert payload == snapshot


# --- parse_cue: malformed payloads return None (never raise) ----------------


@pytest.mark.parametrize("bad", [None, [], "string", 42, {}])
def test_parse_cue_non_cue_payload_returns_none(bad):
    assert parse_cue(bad) is None


def test_parse_cue_missing_rf_cue_id_returns_none():
    payload = load_fixture()
    del payload["rf_cue_id"]
    assert parse_cue(payload) is None


def test_parse_cue_missing_zone_returns_none():
    payload = load_fixture()
    del payload["zone"]
    assert parse_cue(payload) is None


def test_parse_cue_missing_timestamp_returns_none():
    payload = load_fixture()
    del payload["ts_epoch"]
    assert parse_cue(payload) is None


def test_parse_cue_null_zone_returns_none():
    payload = load_fixture()
    payload["zone"] = None
    assert parse_cue(payload) is None


def test_parse_cue_non_numeric_timestamp_returns_none():
    payload = load_fixture()
    payload["ts_epoch"] = "not-a-number"
    assert parse_cue(payload) is None


def test_parse_cue_boolean_timestamp_returns_none():
    payload = load_fixture()
    payload["ts_epoch"] = True
    assert parse_cue(payload) is None


# --- CueMatcher.match -------------------------------------------------------


def test_match_within_window_returns_object_id():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    assert matcher.match(make_cue("perimeter-north", 103.0)) == "trk-1"


def test_match_on_window_boundary_is_inclusive():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    assert matcher.match(make_cue("perimeter-north", 105.0)) == "trk-1"


def test_match_outside_window_returns_none():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    assert matcher.match(make_cue("perimeter-north", 110.0)) is None


def test_match_uncovered_zone_returns_none():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    # A track exists in a different (covered) zone, but the cue's zone is not covered.
    matcher.observe_track("trk-1", "driveway", 100.0)
    assert matcher.match(make_cue("driveway", 100.0)) is None


def test_match_no_tracks_returns_none():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    assert matcher.match(make_cue("perimeter-north", 100.0)) is None


def test_match_wrong_zone_track_returns_none():
    # Track is in a covered zone, but not the cue's zone.
    matcher = CueMatcher(covered_zones={"perimeter-north", "driveway"}, window_s=5.0)
    matcher.observe_track("trk-1", "driveway", 100.0)
    assert matcher.match(make_cue("perimeter-north", 100.0)) is None


def test_match_picks_closest_in_time_when_multiple():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-far", "perimeter-north", 100.0)
    matcher.observe_track("trk-near", "perimeter-north", 104.0)
    assert matcher.match(make_cue("perimeter-north", 103.0)) == "trk-near"


def test_observe_track_refreshes_timestamp():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    # Same track seen again later; the refreshed ts should drive matching.
    matcher.observe_track("trk-1", "perimeter-north", 120.0)
    assert matcher.match(make_cue("perimeter-north", 100.0)) is None
    assert matcher.match(make_cue("perimeter-north", 122.0)) == "trk-1"


# --- CueMatcher.expire ------------------------------------------------------


def test_expire_drops_stale_tracks():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    matcher.expire(now_epoch=200.0)
    assert matcher.match(make_cue("perimeter-north", 100.0)) is None


def test_expire_keeps_fresh_tracks():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    matcher.expire(now_epoch=102.0)
    assert matcher.match(make_cue("perimeter-north", 101.0)) == "trk-1"


# --- handle_cue -------------------------------------------------------------


def test_handle_cue_matched_builds_valid_ack():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    ack = handle_cue(make_cue("perimeter-north", 103.0), matcher, "cam-01", FakeClock())
    validate_ack(ack)
    assert ack["schema"] == "camera.cue-ack.v0"
    assert ack["matched"] is True
    assert ack["object_id"] == "trk-1"
    assert ack["rf_cue_id"] == "rf-cue-7f3a9c"
    assert ack["zone"] == "perimeter-north"
    assert ack["node_id"] == "cam-01"
    assert ack["timestamp"] == FIXED_TS


def test_handle_cue_no_track_builds_unmatched_ack():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    ack = handle_cue(make_cue("perimeter-north", 103.0), matcher, "cam-01", FakeClock())
    validate_ack(ack)
    assert ack["matched"] is False
    assert ack["object_id"] is None


def test_handle_cue_uncovered_zone_builds_unmatched_ack():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "driveway", 103.0)
    ack = handle_cue(make_cue("driveway", 103.0), matcher, "cam-01", FakeClock())
    validate_ack(ack)
    assert ack["matched"] is False
    assert ack["object_id"] is None
    # The cue's zone is still echoed, even when uncovered.
    assert ack["zone"] == "driveway"


def test_handle_cue_outside_window_builds_unmatched_ack():
    matcher = CueMatcher(covered_zones={"perimeter-north"}, window_s=5.0)
    matcher.observe_track("trk-1", "perimeter-north", 100.0)
    ack = handle_cue(make_cue("perimeter-north", 110.0), matcher, "cam-01", FakeClock())
    validate_ack(ack)
    assert ack["matched"] is False
    assert ack["object_id"] is None
