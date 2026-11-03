"""Parsing and validation of ``rf.observation.v0`` records.

Validation is hand-written (no jsonschema at runtime) to keep ingest fast;
``tests/test_contract.py`` keeps it in step with the JSON Schema file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_ID = "rf.observation.v0"
EMITTER_TYPES = frozenset({"wifi_ap", "wifi_client", "bt_classic", "ble", "tpms"})
_EMITTER_ID = re.compile(r"^(([0-9a-f]{2}:){5}[0-9a-f]{2}|tpms:[0-9a-f]{8})$")
_NODE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ObservationError(ValueError):
    """Raised for records that do not satisfy the observation contract."""


@dataclass(frozen=True, slots=True)
class Observation:
    t: float  # POSIX seconds, UTC
    node_id: str
    emitter_type: str
    emitter_id: str
    rssi_dbm: int
    frequency_mhz: float
    attributes: dict[str, Any]

    @property
    def randomized(self) -> bool:
        return bool(self.attributes.get("randomized_address", False))


def parse_observation(record: dict[str, Any]) -> Observation:
    if not isinstance(record, dict):
        raise ObservationError("observation must be a JSON object")
    if record.get("schema") != SCHEMA_ID:
        raise ObservationError(f"unsupported schema {record.get('schema')!r}")
    try:
        ts = datetime.fromisoformat(record["timestamp"])
        node_id = record["node_id"]
        emitter_type = record["emitter_type"]
        emitter_id = record["emitter_id"]
        rssi = record["rssi_dbm"]
        freq = record["frequency_mhz"]
        attrs = record["attributes"]
    except KeyError as exc:
        raise ObservationError(f"missing field {exc.args[0]!r}") from None
    except (TypeError, ValueError) as exc:
        raise ObservationError(f"bad timestamp: {exc}") from None

    if ts.tzinfo is None:
        raise ObservationError("timestamp must include a UTC offset")
    if not isinstance(node_id, str) or not _NODE_ID.match(node_id):
        raise ObservationError(f"bad node_id {node_id!r}")
    if emitter_type not in EMITTER_TYPES:
        raise ObservationError(f"bad emitter_type {emitter_type!r}")
    if not isinstance(emitter_id, str) or not _EMITTER_ID.match(emitter_id):
        raise ObservationError(f"bad emitter_id {emitter_id!r}")
    if isinstance(rssi, bool) or not isinstance(rssi, int) or not -130 <= rssi <= 0:
        raise ObservationError(f"bad rssi_dbm {rssi!r}")
    if isinstance(freq, bool) or not isinstance(freq, (int, float)) or freq <= 0:
        raise ObservationError(f"bad frequency_mhz {freq!r}")
    if not isinstance(attrs, dict):
        raise ObservationError("attributes must be an object")
    extra = set(record) - {"schema", "timestamp", "node_id", "emitter_type", "emitter_id",
                           "rssi_dbm", "frequency_mhz", "attributes"}
    if extra:
        raise ObservationError(f"unexpected fields {sorted(extra)}")

    return Observation(ts.timestamp(), node_id, emitter_type, emitter_id, rssi, float(freq), attrs)
