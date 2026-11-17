"""The MQTT topics fusion subscribes to and what each one becomes."""

from dataclasses import dataclass

from fusion_service.domain.events import EventType, Subsystem


@dataclass(frozen=True)
class Contract:
    subsystem: Subsystem
    filename: str
    event_type: EventType
    status: bool = False
    """True when the topic carries node status that should update the nodes snapshot."""


TOPICS: dict[str, Contract] = {
    "detection/camera/detections": Contract("camera", "detection", "camera.detection"),
    "detection/camera/cue-acks": Contract("camera", "cue-ack", "camera.cue_ack"),
    "detection/camera/status/nodes": Contract("camera", "node-status", "camera.node_status", True),
    "detection/rf/positions": Contract("rf", "position", "rf.position"),
    "detection/rf/alerts": Contract("rf", "alert", "rf.alert"),
    "detection/rf/status/nodes": Contract("rf", "node-status", "rf.node_status", True),
    "detection/physical/events": Contract("physical", "event", "physical.event"),
    "detection/physical/status/panel": Contract(
        "physical",
        "panel-status",
        "physical.panel_status",
        True,
    ),
    "detection/physical/status/zones": Contract("physical", "zone-status", "physical.zone_status"),
}


def topics_for(subsystems: list[Subsystem] | set[Subsystem]) -> list[str]:
    wanted = set(subsystems)
    return [topic for topic, contract in TOPICS.items() if contract.subsystem in wanted]
