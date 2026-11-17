"""Parse a raw Frigate MQTT ``frigate/events`` payload into an internal event.

Frigate publishes a JSON message on ``frigate/events`` for every tracked object,
with a top-level ``type`` in ``new | update | end`` and ``before``/``after``
objects describing the tracked object. The **current** state lives in ``after``;
``before`` is the prior snapshot and is ignored here.

:func:`parse_frigate_event` is the only place in the subsystem that knows
Frigate's event shape. It lowers that shape into a small, stable
:class:`FrigateEvent` dataclass. Keeping this translation isolated means the rest
of the bridge never touches a Frigate field name: the service wiring (Task 7)
takes a :class:`FrigateEvent` and feeds it to ``formats.detection_event`` to mint
the ``camera.detection.v0`` envelope. This module deliberately does **not** import
``formats`` — the two layers meet only at the wiring, so each stays independently
testable.

Robustness contract: Frigate (or a flaky broker) can hand us a malformed or
partial payload. Every field access is guarded; a payload that is not a dict, is
missing ``after``, or is missing the identifying ``id``/``label`` yields ``None``
rather than raising. Optional fields fall back to safe, schema-compatible
defaults (empty lists, ``0``, ``0.0``, ``False``, ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FrigateEvent", "parse_frigate_event"]

# Frigate's lifecycle `type` -> our internal event verb (parallel to the RF
# subsystem's visit_start / visit_end). An unknown type falls out of this map
# and the parse returns None.
_TYPE_TO_EVENT = {
    "new": "object_start",
    "update": "object_update",
    "end": "object_end",
}


@dataclass(frozen=True)
class FrigateEvent:
    """A single Frigate object event, normalized for the bridge.

    Fields mirror the ``camera.detection.v0`` payload the service wiring will
    build from it: ``event``/``object_id``/``label``/``confidence``/``zones`` are
    top-level detection fields, while ``bbox``/``area_px``/``stationary`` become the
    detection's ``attributes``. Frigate's event carries no snapshot URL: the bridge mints one.
    """

    event: str
    object_id: str
    label: str
    confidence: float
    zones: list[str]
    bbox: list[int]
    area_px: int
    stationary: bool


def parse_frigate_event(payload: dict) -> FrigateEvent | None:
    """Translate a raw ``frigate/events`` payload into a :class:`FrigateEvent`.

    Returns ``None`` for any payload that is not a well-formed Frigate event: a
    non-dict payload, an unknown/missing ``type``, a missing/non-dict ``after``,
    or an ``after`` lacking a non-null ``id`` or ``label``. Never raises on
    malformed input.
    """
    try:
        event = _TYPE_TO_EVENT[payload["type"]]
        after = payload["after"]

        # `id` and `label` identify the track; without them the event is
        # unusable. Guard both presence (KeyError) and explicit null.
        object_id = after["id"]
        label = after["label"]
        if object_id is None or label is None:
            return None

        # Optional fields: fall back to defaults that match the contract's
        # attribute shapes. `or` collapses both "missing" and explicit null
        # (Frigate sends current_zones: null when an object is in no zone).
        # Numeric fields are coerced so a flaky upstream type cannot leak a
        # non-numeric through (a non-coercible value raises ValueError and is
        # treated as malformed below → None). zones/bbox are copied out so the
        # FrigateEvent never shares a list object with the input payload.
        confidence = float(after.get("score") or 0.0)
        zones = list(after.get("current_zones") or [])
        bbox = list(after.get("box") or [])
        area_px = int(after.get("area") or 0)
        stationary = bool(after.get("stationary"))
    except (KeyError, TypeError, AttributeError, ValueError):
        return None

    return FrigateEvent(
        event=event,
        object_id=object_id,
        label=label,
        confidence=confidence,
        zones=zones,
        bbox=bbox,
        area_px=area_px,
        stationary=stationary,
    )
