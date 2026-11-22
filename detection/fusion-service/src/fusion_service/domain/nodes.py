"""Latest-status snapshot per sensor node, with the staleness rule applied."""

from typing import Any

from pydantic import BaseModel

from fusion_service.domain.events import Event, Subsystem


class Node(BaseModel):
    site_id: str
    subsystem: Subsystem
    node_id: str
    event_id: str
    timestamp: str
    received_at: str
    source_age_s: float
    receipt_age_s: float
    clock_ahead: bool
    stale: bool
    data: dict[str, Any]

    @classmethod
    def from_status(
        cls,
        event: Event,
        *,
        occurred: float,
        received: float,
        now: float,
        stale_after_s: float,
        future_tolerance_s: float,
    ) -> "Node":
        source_age = now - occurred
        receipt_age = now - received
        return cls(
            site_id=event.site_id,
            subsystem=event.subsystem,
            node_id=event.node_id or "",
            event_id=event.id,
            timestamp=event.timestamp,
            received_at=event.received_at,
            source_age_s=max(0, source_age),
            receipt_age_s=max(0, receipt_age),
            clock_ahead=source_age < 0,
            stale=max(source_age, receipt_age) > stale_after_s or source_age < -future_tolerance_s,
            data=event.data,
        )
