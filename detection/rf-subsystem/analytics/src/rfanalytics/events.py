"""Analytics events emitted by the analyzer.

``rf.analytics-event.v0`` is internal to the RF subsystem: it feeds the alert
engine and the scorer. It is not the fusion message format, which is still
to be defined by the fusion team (DESIGN.md open question 1).

Event types:

* ``visit_start``: an emitter's visit is confirmed (enough transmissions to
  rule out a spurious decode). ``timestamp`` is the first observation.
* ``zone_estimate``: the emitter's estimated zone changed.
* ``visit_end``: the emitter has been silent longer than the visit gap, or
  input ended. Includes duration and dwell time per zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_ID = "rf.analytics-event.v0"


def iso_utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    type: str
    t: float
    emitter_id: str
    emitter_type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_ID,
            "type": self.type,
            "timestamp": iso_utc(self.t),
            "emitter_id": self.emitter_id,
            "emitter_type": self.emitter_type,
            **self.data,
        }
