from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rfanalytics.observation import Observation
from rfanalytics.site import Node, Site, Zone

RF_ROOT = Path(__file__).resolve().parents[2]
SIM_ROOT = RF_ROOT / "simulator"
DEV_SITE = SIM_ROOT / "sites" / "dev-site.yaml"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def line_site() -> Site:
    """Three nodes in a row, one zone each."""
    zones = (Zone("west", 0, 0, 10, 10), Zone("middle", 10, 0, 20, 10), Zone("east", 20, 0, 30, 10))
    nodes = {
        "n-west": Node("n-west", "west", (5, 5)),
        "n-middle": Node("n-middle", "middle", (15, 5)),
        "n-east": Node("n-east", "east", (25, 5)),
    }
    return Site("line", zones, nodes)


def obs(t_s: float, node: str, rssi: int, emitter_id: str = "02:00:00:00:00:01",
        emitter_type: str = "ble", **attrs) -> Observation:
    return Observation((T0 + timedelta(seconds=t_s)).timestamp(), node, emitter_type, emitter_id, rssi, 2426.0, attrs)


def record(**overrides) -> dict:
    base = {
        "schema": "rf.observation.v0",
        "timestamp": "2026-11-02T12:00:00.000Z",
        "node_id": "rf-node-01",
        "emitter_type": "ble",
        "emitter_id": "aa:bb:cc:dd:ee:ff",
        "rssi_dbm": -70,
        "frequency_mhz": 2426.0,
        "attributes": {},
    }
    base.update(overrides)
    return base
