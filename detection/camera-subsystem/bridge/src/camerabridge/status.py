"""Build a node's ``camera.node-status.v0`` heartbeat from its health.

Each edge node periodically publishes a heartbeat on
``detection/camera/status/nodes`` (``formats.TOPIC_STATUS``) so the fusion layer
knows the node is alive and can tell a healthy node apart from one whose camera,
uplink, recorder, or detector has fallen over. This module is a thin adapter: it
collects a node's health into a :class:`NodeState` and lowers it to a
schema-valid envelope via :func:`formats.node_status` — it does not re-derive the
wire shape (that invariant lives in ``formats.py``, the one module that knows the
contract).

The four health signals are first-class, required booleans on every heartbeat.
Everything else a node might report (Frigate build, detection frame rate, free
storage, uplink signal strength, ...) is optional telemetry that rides in the
open ``attributes`` sub-object. Mirroring ``formats.cue_ack``'s handling of
``snapshot_url``, an optional field is folded into ``attributes`` only when it is
set, so a bare :class:`NodeState` yields an empty ``attributes`` rather than a
clutter of ``null`` keys.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar

from . import formats

__all__ = ["NodeState", "build_status"]


@dataclass(frozen=True)
class NodeState:
    """A node's health plus optional telemetry, ready to lower to a heartbeat.

    The four booleans are the load-bearing health signals and map to the
    required top-level fields of ``camera.node-status.v0``. The remaining fields
    are optional telemetry that ride in the open ``attributes`` sub-object; each
    defaults to ``None`` and is emitted only when set.
    """

    camera_online: bool
    link_online: bool
    recording_ok: bool
    detector_ok: bool
    frigate_version: str | None = None
    detection_fps: float | None = None
    storage_free_pct: float | None = None
    uplink_rssi_dbm: int | None = None

    # The health booleans that become required top-level fields; everything else
    # on the dataclass is optional telemetry destined for ``attributes``.
    _HEALTH_FIELDS: ClassVar[tuple[str, ...]] = (
        "camera_online",
        "link_online",
        "recording_ok",
        "detector_ok",
    )

    def attributes(self) -> dict[str, Any]:
        """Return the set optional fields as the heartbeat's ``attributes`` dict.

        Only fields with a non-``None`` value are included, so an unset field is
        omitted entirely rather than carried as ``null``.
        """
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in self._HEALTH_FIELDS and getattr(self, f.name) is not None
        }


def build_status(state: NodeState, node_id: str, clock: formats.Clock) -> dict:
    """Compose a ``camera.node-status.v0`` heartbeat for ``state``.

    Passes the four health booleans and the set optional telemetry (as the
    ``attributes`` dict) through to :func:`formats.node_status`, which stamps the
    timestamp from ``clock`` and mints the ``event_id``. The returned dict is a
    valid ``camera.node-status.v0`` message.
    """
    return formats.node_status(
        node_id=node_id,
        camera_online=state.camera_online,
        link_online=state.link_online,
        recording_ok=state.recording_ok,
        detector_ok=state.detector_ok,
        attributes=state.attributes(),
        clock=clock,
    )
