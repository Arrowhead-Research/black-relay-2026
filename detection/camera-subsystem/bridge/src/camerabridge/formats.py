"""The one module that knows the camera subsystem's wire format.

Every ``camera.*`` message published to the fusion broker is minted here. Later
tasks (cue handling, status heartbeats, service wiring) import the topic
constants, the ``Clock`` protocol, and these three envelope builders rather than
re-deriving the shapes. The builders are the single place the contract — the
Task-1 JSON Schemas under ``bridge/schemas/`` — turns into dicts, and the place
where the invariants JSON Schema cannot express are enforced.

Design rules baked in here (see DESIGN.md 5.2-5.4 and the task's carried
decisions):

* each builder pins ``schema`` to its type's const;
* ``event_id`` is minted with :func:`new_event_id` (UUIDv4) only when the caller
  passes ``event_id=None``; a supplied id is preserved verbatim so replayed
  messages keep stable ids for idempotent fusion dedupe;
* ``timestamp`` is stamped from the injected ``clock`` (RFC 3339 UTC, trailing
  ``Z``), never from a builder-local wall clock;
* ``detection_event`` sets ``camera_id == node_id`` — in v0 the RLC-520A is the
  only camera per node, and a Task-1 review flagged that JSON Schema cannot
  enforce cross-field equality, so the invariant lives in the builder.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

__all__ = [
    "TOPIC_DETECTIONS",
    "TOPIC_CUE_ACKS",
    "TOPIC_STATUS",
    "TOPIC_RF_CUES_IN",
    "new_event_id",
    "Clock",
    "ClockNotReady",
    "SystemClock",
    "detection_event",
    "cue_ack",
    "node_status",
]

# --- MQTT topics ------------------------------------------------------------
# Exact strings per the task's global constraints. Outbound camera.* topics plus
# the one inbound RF-cue topic this subsystem subscribes to.
TOPIC_DETECTIONS = "detection/camera/detections"
TOPIC_CUE_ACKS = "detection/camera/cue-acks"
TOPIC_STATUS = "detection/camera/status/nodes"
TOPIC_RF_CUES_IN = "detection/rf/cues/camera"

# --- schema consts ----------------------------------------------------------
_SCHEMA_DETECTION = "camera.detection.v0"
_SCHEMA_CUE_ACK = "camera.cue-ack.v0"
_SCHEMA_NODE_STATUS = "camera.node-status.v0"


def new_event_id() -> str:
    """Return a fresh UUIDv4 string for a message's ``event_id``."""
    return str(uuid.uuid4())


@runtime_checkable
class Clock(Protocol):
    """Source of publish timestamps.

    Implementations return an RFC 3339 UTC timestamp with a trailing ``Z``.
    Injected into every builder so tests can stamp a fixed, known time and the
    production clock can gate on NTP readiness.
    """

    def now_rfc3339(self) -> str:  # pragma: no cover - protocol signature
        ...


class ClockNotReady(RuntimeError):
    """Raised by :class:`SystemClock` before it has been marked NTP-synced.

    Timestamps are the fusion layer's dedupe and ordering key, so publishing one
    from an unsynchronised wall clock is worse than not publishing: callers must
    confirm NTP sync (via :meth:`SystemClock.mark_synced`) first.
    """


class SystemClock:
    """Wall-clock ``Clock`` that refuses to stamp until NTP-synced.

    Starts un-synced and raises :class:`ClockNotReady` from
    :meth:`now_rfc3339`. Once the node confirms NTP synchronisation it calls
    :meth:`mark_synced` and the clock begins returning UTC timestamps formatted
    with a trailing ``Z``.
    """

    def __init__(self) -> None:
        self._synced = False

    def mark_synced(self) -> None:
        """Mark the clock NTP-synced; subsequent stamps are allowed."""
        self._synced = True

    @property
    def is_synced(self) -> bool:
        return self._synced

    def now_rfc3339(self) -> str:
        if not self._synced:
            raise ClockNotReady(
                "SystemClock has not been marked NTP-synced; refusing to stamp a "
                "timestamp from an unsynchronised clock."
            )
        return _format_rfc3339_z(datetime.now(timezone.utc))


def _format_rfc3339_z(moment: datetime) -> str:
    """Format a UTC datetime as RFC 3339 with a trailing ``Z`` (second precision)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(event_id: Optional[str]) -> str:
    """Preserve a supplied ``event_id``; mint a fresh UUIDv4 when it is ``None``."""
    return new_event_id() if event_id is None else event_id


def detection_event(
    *,
    node_id: str,
    event: str,
    object_id: str,
    label: str,
    confidence: float,
    zones: list[str],
    attributes: dict[str, Any],
    clock: Clock,
    event_id: Optional[str] = None,
) -> dict:
    """Build a ``camera.detection.v0`` envelope.

    Enforces the v0 invariant ``camera_id == node_id`` (one camera per node).
    """
    return {
        "schema": _SCHEMA_DETECTION,
        "event_id": _event_id(event_id),
        "timestamp": clock.now_rfc3339(),
        "node_id": node_id,
        "camera_id": node_id,
        "event": event,
        "object_id": object_id,
        "label": label,
        "confidence": confidence,
        # Copy the caller's mutable inputs so a later mutation of the source list /
        # dict cannot alias into (and silently change) an already-emitted message.
        "zones": list(zones),
        "attributes": dict(attributes),
    }


def cue_ack(
    *,
    node_id: str,
    rf_cue_id: str,
    zone: str,
    matched: bool,
    object_id: Optional[str],
    snapshot_url: Optional[str] = None,
    clock: Clock,
    event_id: Optional[str] = None,
) -> dict:
    """Build a ``camera.cue-ack.v0`` envelope.

    ``object_id`` is the matched track's id, or ``None`` when the cue had no
    visual match. ``snapshot_url`` is folded into the open ``attributes``
    sub-object only when provided.
    """
    attributes: dict[str, Any] = {}
    if snapshot_url is not None:
        attributes["snapshot_url"] = snapshot_url
    return {
        "schema": _SCHEMA_CUE_ACK,
        "event_id": _event_id(event_id),
        "timestamp": clock.now_rfc3339(),
        "node_id": node_id,
        "rf_cue_id": rf_cue_id,
        "zone": zone,
        "matched": matched,
        "object_id": object_id,
        "attributes": attributes,
    }


def node_status(
    *,
    node_id: str,
    camera_online: bool,
    link_online: bool,
    recording_ok: bool,
    detector_ok: bool,
    attributes: dict[str, Any],
    clock: Clock,
    event_id: Optional[str] = None,
) -> dict:
    """Build a ``camera.node-status.v0`` heartbeat envelope."""
    return {
        "schema": _SCHEMA_NODE_STATUS,
        "event_id": _event_id(event_id),
        "timestamp": clock.now_rfc3339(),
        "node_id": node_id,
        "camera_online": camera_online,
        "link_online": link_online,
        "recording_ok": recording_ok,
        "detector_ok": detector_ok,
        # Copy so a later mutation of the caller's dict can't alias the message.
        "attributes": dict(attributes),
    }
