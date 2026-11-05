"""Consume inbound RF cues, match them to open camera tracks, and ack.

RF publishes a cue on ``detection/rf/cues/camera`` (``formats.TOPIC_RF_CUES_IN``)
for every TPMS ``first_heard`` event. The single topic is fanned to every node;
**each node filters by the zones it covers** — the cue is not addressed to a
specific camera (DESIGN.md 5.4). This module is the camera subsystem's one place
that reads the RF cue shape and decides whether a cue lines up with something the
camera actually saw.

Three pieces, mirroring the Frigate path (``frigate.py`` → ``formats``):

* :func:`parse_cue` lowers a raw ``rf.camera-cue.v0`` payload into an
  :class:`RfCue`, returning ``None`` for anything malformed rather than raising;
* :class:`CueMatcher` keeps a small table of open tracks and answers "did the
  camera see something in this cue's zone within ±``window_s``?";
* :func:`handle_cue` turns the match outcome into a schema-valid
  ``camera.cue-ack.v0`` via :func:`formats.cue_ack`.

Provisional contract (reconciles at RF merge)
---------------------------------------------
The exact ``rf.camera-cue.v0`` wire shape is **RF-owned and lives on the unmerged
``detection/wifi-dev`` branch** (DESIGN.md 1.1 / 5.4). Until it freezes, this
module parses a *provisional* shape captured in ``tests/fixtures/rf_cue.json``,
which carries at least the five fields DESIGN.md 5.4 commits to: a cue id
(``rf_cue_id``), a ``zone``, a timestamp, a source node (``source_node``), and the
TPMS sensor id (``tpms_id``). **Timestamp decision:** carried as epoch seconds in
the field ``ts_epoch`` (``float``); chosen over RFC-3339 because matching is a
numeric ``±window_s`` comparison against camera-track epoch times, so epoch avoids
a parse/timezone round-trip on the hot path. When the RF contract merges, only
:func:`parse_cue` and the fixture should need to change — ``CueMatcher`` and the
cue-ack output are contract-stable.

Robustness contract: a cue whose zone the node does not cover, a cue with no open
track, and a cue outside the time window all yield ``matched:false`` and never
raise; a malformed payload makes :func:`parse_cue` return ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import formats

__all__ = ["RfCue", "parse_cue", "CueMatcher", "handle_cue"]


@dataclass(frozen=True)
class RfCue:
    """A single inbound RF cue, normalized for the bridge.

    ``rf_cue_id``/``zone``/``ts_epoch`` drive acknowledgement and matching;
    ``source_node``/``tpms_id`` are carried-through provenance (not used in
    matching) and default to ``None`` when the provisional payload omits them.
    """

    rf_cue_id: str
    zone: str
    ts_epoch: float
    source_node: str | None = None
    tpms_id: str | None = None


def _coerce_epoch(value: object) -> float | None:
    """Coerce a cue timestamp to epoch seconds, or ``None`` if it is not numeric.

    Accepts an ``int``/``float`` (but not ``bool`` — ``True`` is an ``int``
    subclass and would silently become ``1.0``) or a numeric string. Anything
    else — ``None``, a non-numeric string, a list — yields ``None`` so the caller
    rejects the cue.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_cue(payload: dict) -> RfCue | None:
    """Translate a raw ``rf.camera-cue.v0`` payload into an :class:`RfCue`.

    Returns ``None`` for any payload that is not a well-formed cue: a non-dict
    payload, a missing/non-string ``rf_cue_id`` or ``zone``, or a missing/
    non-numeric ``ts_epoch``. ``source_node`` and ``tpms_id`` are optional and
    default to ``None``. Never raises on malformed input.
    """
    try:
        rf_cue_id = payload["rf_cue_id"]
        zone = payload["zone"]
        ts_epoch = _coerce_epoch(payload["ts_epoch"])
    except (KeyError, TypeError):
        return None

    # The three load-bearing fields must be present and well-typed; without them
    # the cue cannot be acknowledged or matched.
    if not isinstance(rf_cue_id, str) or not isinstance(zone, str):
        return None
    if ts_epoch is None:
        return None

    source_node = payload.get("source_node")
    tpms_id = payload.get("tpms_id")

    return RfCue(
        rf_cue_id=rf_cue_id,
        zone=zone,
        ts_epoch=ts_epoch,
        source_node=source_node,
        tpms_id=tpms_id,
    )


class CueMatcher:
    """Table of open camera tracks, matched to RF cues by zone + time window.

    A node observes its own camera tracks via :meth:`observe_track` (one entry
    per ``object_id``, refreshed on each sighting) and periodically drops stale
    ones via :meth:`expire`. :meth:`match` answers whether a cue lines up with an
    open track: the track must be in the cue's zone, that zone must be one the
    node covers, and the track's last-seen time must be within ``±window_s`` of
    the cue's timestamp.
    """

    def __init__(self, covered_zones: set[str], window_s: float = 5.0) -> None:
        self._covered_zones = set(covered_zones)
        self._window_s = window_s
        # object_id -> (zone, last-seen epoch seconds)
        self._tracks: dict[str, tuple[str, float]] = {}

    def observe_track(self, object_id: str, zone: str, ts_epoch: float) -> None:
        """Record or refresh an open track, keyed by ``object_id``."""
        self._tracks[object_id] = (zone, ts_epoch)

    def expire(self, now_epoch: float) -> None:
        """Drop tracks last seen more than one window behind ``now_epoch``.

        A track older than ``now_epoch - window_s`` can no longer match a cue
        arriving at (or near) ``now_epoch``, so it is pruned to keep the table
        bounded. The horizon is tied to ``window_s`` so a wider match window
        keeps tracks correspondingly longer.
        """
        horizon = now_epoch - self._window_s
        self._tracks = {
            object_id: entry
            for object_id, entry in self._tracks.items()
            if entry[1] >= horizon
        }

    def match(self, cue: RfCue) -> str | None:
        """Return an open track's ``object_id`` matching ``cue``, else ``None``.

        A cue in a zone the node does not cover never matches. Among the open
        tracks in the cue's zone within ``±window_s``, the one closest in time is
        returned (deterministic when several are in range); if none qualify,
        ``None``. Never raises.
        """
        if cue.zone not in self._covered_zones:
            return None

        best_object_id: str | None = None
        best_delta: float | None = None
        for object_id, (zone, ts_epoch) in self._tracks.items():
            if zone != cue.zone:
                continue
            delta = abs(ts_epoch - cue.ts_epoch)
            if delta <= self._window_s and (best_delta is None or delta < best_delta):
                best_object_id = object_id
                best_delta = delta
        return best_object_id


def handle_cue(cue: RfCue, matcher: CueMatcher, node_id: str, clock) -> dict:
    """Compose a ``camera.cue-ack.v0`` for ``cue``, matched or not.

    Delegates the match decision to ``matcher`` (which never raises for an
    uncovered zone, a missing track, or a stale/out-of-window track) and mints
    the envelope via :func:`formats.cue_ack`. ``matched`` reflects whether a
    track was found; ``object_id`` is that track's id or ``None``. Every cue the
    node acts on produces an ack so fusion always sees the cue was received.
    """
    object_id = matcher.match(cue)
    return formats.cue_ack(
        node_id=node_id,
        rf_cue_id=cue.rf_cue_id,
        zone=cue.zone,
        matched=object_id is not None,
        object_id=object_id,
        clock=clock,
    )
