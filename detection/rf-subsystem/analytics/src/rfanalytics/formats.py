"""Wire format adapter for outbound messages.

**This is the only place that knows the outbound message format.** The
fusion team has not published its standard yet (DESIGN.md open question 1),
so these are the draft topics and payloads from DESIGN.md 9.3. When the
standard arrives, change ``to_wire`` (and its tests); nothing else should
need to change.
"""

from __future__ import annotations

from typing import Any

from rfanalytics.alerts import Message
from rfanalytics.events import iso_utc

TOPIC_PREFIX = "detection/rf"
TOPICS = {
    "alert": f"{TOPIC_PREFIX}/alerts",
    "camera_cue": f"{TOPIC_PREFIX}/cues/camera",
    "node_status": f"{TOPIC_PREFIX}/status/nodes",
}
SCHEMAS = {
    "alert": "rf.alert.v0",
    "camera_cue": "rf.camera-cue.v0",
    "node_status": "rf.node-status.v0",
}


def to_wire(msg: Message, topic_prefix: str = TOPIC_PREFIX) -> tuple[str, dict[str, Any]]:
    """Return (topic, payload) for a message."""
    if msg.kind not in TOPICS:
        raise ValueError(f"unknown message kind {msg.kind!r}")
    topic = TOPICS[msg.kind].replace(TOPIC_PREFIX, topic_prefix, 1)
    payload = {"schema": SCHEMAS[msg.kind], "id": msg.id, "timestamp": iso_utc(msg.t), **msg.body}
    return topic, payload
