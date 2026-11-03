"""Observation record shared with rf-analytics ingest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_ID = "rf.observation.v0"
EMITTER_TYPES = ("wifi_ap", "wifi_client", "bt_classic", "ble", "tpms")


def iso_utc(ts: datetime) -> str:
    """Format an aware UTC datetime as RFC 3339 with millisecond precision and Z suffix."""
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Observation:
    """One detection of one emitter by one node."""

    timestamp: datetime
    node_id: str
    emitter_type: str
    emitter_id: str
    rssi_dbm: int
    frequency_mhz: float
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_ID,
            "timestamp": iso_utc(self.timestamp),
            "node_id": self.node_id,
            "emitter_type": self.emitter_type,
            "emitter_id": self.emitter_id,
            "rssi_dbm": self.rssi_dbm,
            "frequency_mhz": self.frequency_mhz,
            "attributes": self.attributes,
        }
