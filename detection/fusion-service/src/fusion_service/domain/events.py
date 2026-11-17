"""The fusion.event.v1 envelope: persisted as-is and served as-is."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "camera.detection",
    "camera.cue_ack",
    "camera.node_status",
    "rf.position",
    "rf.alert",
    "rf.node_status",
    "physical.event",
    "physical.panel_status",
    "physical.zone_status",
]

Subsystem = Literal["camera", "rf", "physical"]

ENVELOPE_SCHEMA = "fusion.event.v1"


class Source(BaseModel):
    broker_id: str
    topic: str
    payload: dict[str, Any]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)
    schema_version: Literal["fusion.event.v1"] = Field(default=ENVELOPE_SCHEMA, alias="schema")
    cursor: int
    id: str
    source_id: str
    source_schema: str
    timestamp: str
    received_at: str
    site_id: str
    subsystem: Subsystem
    node_id: str | None
    type: EventType
    data: dict[str, Any]
    source: Source
