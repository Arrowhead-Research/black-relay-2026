"""The ingest parser must accept exactly what the observation schema accepts."""

from __future__ import annotations

import json

import jsonschema
import pytest

from conftest import RF_ROOT, record
from rfanalytics.observation import ObservationError, parse_observation

SCHEMA = json.loads((RF_ROOT / "schemas" / "observation.v0.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())

VALID = [
    record(),
    record(emitter_type="tpms", emitter_id="tpms:1a2b3c4d", frequency_mhz=315.0,
           attributes={"tpms_protocol": "Schrader", "pressure_kpa": 235.1}),
    record(emitter_type="wifi_ap", attributes={"ssid": "RELAY-OPS", "randomized_address": False}),
    record(rssi_dbm=0),
    record(timestamp="2026-11-02T12:00:00+00:00"),
]

INVALID = {
    "wrong schema": record(schema="rf.observation.v1"),
    "missing node": {k: v for k, v in record().items() if k != "node_id"},
    "uppercase mac": record(emitter_id="AA:BB:CC:DD:EE:FF"),
    "short tpms id": record(emitter_id="tpms:1a2b"),
    "unknown type": record(emitter_type="zigbee"),
    "float rssi": record(rssi_dbm=-70.5),
    "positive rssi": record(rssi_dbm=5),
    "zero frequency": record(frequency_mhz=0),
    "bad node id": record(node_id="RF Node 1"),
    "extra field": record(extra=1),
    "attributes not object": record(attributes=[]),
}


@pytest.mark.parametrize("rec", VALID)
def test_valid_records_pass_schema_and_parser(rec):
    VALIDATOR.validate(rec)
    parse_observation(rec)


@pytest.mark.parametrize("name", INVALID)
def test_invalid_records_fail_schema_and_parser(name):
    rec = INVALID[name]
    assert not VALIDATOR.is_valid(rec), f"schema accepted {name}"
    with pytest.raises(ObservationError):
        parse_observation(rec)


def test_parser_accepts_simulator_output():
    from rfsim import load_scenario, simulate

    from conftest import SIM_ROOT

    result = simulate(load_scenario(SIM_ROOT / "scenarios" / "baseline-friendly.yaml"))
    for o in result.observations[:2000]:
        parse_observation(o.to_dict())
