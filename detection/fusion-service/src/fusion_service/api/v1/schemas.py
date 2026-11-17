"""Response models for v1. The Event envelope itself is domain and is served unchanged."""

from typing import Any, Literal

from pydantic import BaseModel

from fusion_service.domain.events import Event
from fusion_service.domain.nodes import Node


class EventPage(BaseModel):
    events: list[Event]
    next_cursor: int
    has_more: bool


class NodePage(BaseModel):
    nodes: list[Node]


class StorageHealth(BaseModel):
    ready: bool
    last_error: str | None
    event_count: int | None = None
    event_bytes: int | None = None
    max_event_bytes: int | None = None
    database_bytes: int | None = None
    max_database_bytes: int | None = None
    dedupe_entries: int | None = None
    floor_cursor: int | None = None


class BrokerHealth(BaseModel):
    id: str
    connected: bool
    subscribed: bool
    last_message_at: str | None
    last_error: str | None
    connections: int
    counts: dict[str, int]


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    storage: StorageHealth
    brokers: list[BrokerHealth]

    @classmethod
    def assess(cls, storage: dict[str, Any], brokers: list[dict[str, Any]]) -> "Health":
        """ok only when storage is ready and every broker is connected and subscribed."""
        storage_health = StorageHealth.model_validate(storage)
        broker_health = [BrokerHealth.model_validate(broker) for broker in brokers]
        good = storage_health.ready and all(b.connected and b.subscribed for b in broker_health)
        return cls(
            status="ok" if good else "degraded", storage=storage_health, brokers=broker_health
        )
