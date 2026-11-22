"""Team wire format ("fusion" output), DRAFT v0.

EVERYTHING that defines what goes on the team broker lives in this one module: topic names, schema identifiers,
the common envelope, payload layouts, the mapping from the bridge's own event log to team event types, timestamp
and id formats, and JSON serialization. When the fusion team publishes its standard, this is the only file that
should need to change (plus schemas/ and MQTT-CONTRACT.md).

`seq` exists only on event messages and is the SQLite event log's own sequence number; status messages carry none.

Pure functions only: no I/O, no clocks (callers pass time in), no MQTT. Versioning rule for v0: additive only
(new optional fields, new enum values documented in MQTT-CONTRACT.md); never rename, remove or retype a field.

Never put panel user codes, tokens, key material or arming/partition-control data in any payload built here.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

VERSION = "v0"
SCHEMA_EVENT = "physical.event.v0"
SCHEMA_PANEL = "physical.panel-status.v0"
SCHEMA_ZONE = "physical.zone-status.v0"

DEFAULT_PREFIX = "detection/physical"
DEFAULT_SITE = {"id": "default", "name": "default"}
QOS = 1                    # events and status: at least once
RETAIN = False             # the team broker takes no retained messages

KINDS = ("contact", "motion", "glass_break", "other")
SOURCES = ("iq_remote", "control4")
EVENT_TYPES = ("zone_opened", "zone_closed", "motion_detected", "motion_cleared", "glass_break_detected",
               "glass_break_cleared", "zone_active", "zone_idle", "tamper", "tamper_cleared", "zone_added",
               "zone_removed", "zone_stale", "zone_fresh", "battery_low", "battery_ok", "zone_renamed")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------- topics
def clean_prefix(prefix: str | None) -> str:
    p = (prefix or DEFAULT_PREFIX).strip().strip("/")
    if not p or any(c in p for c in "#+\x00") or "//" in p:
        raise ValueError(f"invalid fusion topic_prefix: {prefix!r}")
    return p


def topic_events(prefix: str) -> str:
    return f"{clean_prefix(prefix)}/events"


def topic_panel(prefix: str) -> str:
    return f"{clean_prefix(prefix)}/status/panel"


def topic_zones(prefix: str) -> str:
    return f"{clean_prefix(prefix)}/status/zones"


# ---------------------------------------------------------------- ids and time
def new_id() -> str:
    """Random UUID v4 (used for messages that have no natural identity, e.g. periodic status)."""
    return str(uuid.uuid4())


def derived_id(*parts: Any) -> str:
    """A valid UUID v4 (version/variant bits set) derived from a stable key. Used for log-driven events so that
    rebuilding the message for the same log row after a crash yields the SAME id (subscribers dedupe on it)."""
    h = bytearray(hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()[:16])
    h[6] = (h[6] & 0x0F) | 0x40
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def rfc3339(epoch: float) -> str:
    """UTC, millisecond precision, literal Z: 2026-01-01T12:00:00.123Z"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def norm_ts(ts: Any, fallback_epoch: float) -> str:
    """Accept the bridge's own log timestamp (already this format) or any ISO-8601 string; else fall back."""
    if isinstance(ts, str):
        if _TS_RE.match(ts):
            return ts
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return rfc3339(d.timestamp())
        except ValueError:
            pass
    return rfc3339(fallback_epoch)


def encode(msg: dict) -> bytes:
    """One UTF-8 JSON object."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- building blocks
def site_block(site: dict | None) -> dict:
    s = site or {}
    sid = str(s.get("id") or DEFAULT_SITE["id"])
    return {"id": sid, "name": str(s.get("name") or sid)}


def zone_block(z: dict | None, fallback_id: str = "unknown") -> dict:
    z = z or {}
    kind = z.get("kind") if z.get("kind") in KINDS else "other"
    try:
        part = int(z.get("partition_id", 0))
    except (TypeError, ValueError):
        part = 0
    zid = str(z.get("id") or fallback_id)
    return {"id": zid, "name": str(z.get("name") or zid), "kind": kind, "partition": part}


def _source(v: Any) -> str | None:
    return v if v in SOURCES else None


def envelope(schema: str, msg_id: str, timestamp: str, site: dict | None) -> dict:
    return {"schema": schema, "id": msg_id, "timestamp": timestamp, "site": site_block(site)}


# ---------------------------------------------------------------- event-log mapping
def map_log_event(e: dict, kind: str | None) -> str | None:
    """Bridge event-log row -> team event_type, or None when the team stream has no such event in v0
    (sensor online/offline, partition arming/alarm, bridge.link: the last two are arming data / covered by
    status/panel). `kind` is the zone kind if known."""
    t, to = e.get("type"), e.get("to")
    if t == "zone.added":
        return "zone_added"
    if t == "zone.removed":
        return "zone_removed"
    if t == "zone.renamed":
        return "zone_renamed"
    if t == "zone.tamper":
        return "tamper" if to else "tamper_cleared"
    if t == "zone.battery":
        return "battery_low" if to else "battery_ok"
    if t == "zone.stale":
        return "zone_stale" if to else "zone_fresh"
    if t == "zone.state":
        if to in ("open", "closed") and kind in (None, "contact", "other"):
            return "zone_opened" if to == "open" else "zone_closed"
        if to in ("active", "idle"):
            if kind == "motion":
                return "motion_detected" if to == "active" else "motion_cleared"
            if kind == "glass_break":
                return "glass_break_detected" if to == "active" else "glass_break_cleared"
            return "zone_active" if to == "active" else "zone_idle"
        if to in ("open", "closed"):      # open/closed reported by a motion/glass zone: keep the panel's word
            return "zone_opened" if to == "open" else "zone_closed"
    return None


# event types whose log row describes an attribute of the zone, not a state transition: `state` is the zone's state as
# of the event (recorded in the log row) and `previous_state` is null
_ATTRIBUTE_EVENTS = ("tamper", "tamper_cleared", "battery_low", "battery_ok", "zone_stale", "zone_fresh",
                     "zone_renamed")


def zone_at_event(zone: dict | None, e: dict) -> dict | None:
    """The zone as it was when the event was committed: name/kind/partition/stale/state from the log row (when the row
    has them), everything else from the live record. Rows written before `state` was recorded fall back to the live
    state."""
    zr = e.get("zone")
    if not isinstance(zr, dict):
        return zone
    out = dict(zone or {})
    for src, dst in (("name", "name"), ("kind", "kind"), ("partition", "partition_id"), ("stale", "stale"),
                     ("state", "state")):
        if zr.get(src) is not None:
            out[dst] = zr[src]
    return out


def event_builder(site: dict | None, e: dict, zone: dict | None, event_type: str, now: float,
                  db_id: str | None = None, prev_seq: int | None = None) -> tuple[str, Callable[[], dict]]:
    """Return (message id, build() -> payload dict) for a log-driven event. `seq` IS the log row's own seq (gap-free
    in the SQLite log, the same number the REST API shows); `prev_seq` is the seq of the event message published just
    before this one. The id is derived from the log row so a rebuilt message keeps its id."""
    mid = derived_id("physical.event", db_id or "", e.get("seq"), e.get("id"))
    zone = zone_at_event(zone, e)
    ts = norm_ts(e.get("ts"), now)

    if event_type in _ATTRIBUTE_EVENTS:
        state, prev = (zone or {}).get("state"), None
    elif event_type == "zone_removed":
        state, prev = e.get("from"), None            # last known state; the zone no longer exists
    elif event_type == "zone_added":
        state, prev = e.get("to"), None
    else:
        state, prev = e.get("to"), e.get("from")
    state = state if isinstance(state, str) else "unknown"
    prev = prev if isinstance(prev, str) else None
    stale = bool(e.get("to")) if event_type in ("zone_stale", "zone_fresh") else bool((zone or {}).get("stale", False))
    seq, pseq = int(e["seq"]), (int(prev_seq) if prev_seq is not None else None)

    def build() -> dict:
        m = envelope(SCHEMA_EVENT, mid, ts, site)
        m.update({"seq": seq, "prev_seq": pseq, "event_type": event_type,
                  "zone": zone_block(zone, str(e.get("subject"))), "state": state, "previous_state": prev,
                  "stale": stale, "source": _source(e.get("source")), "synthetic": bool(e.get("synthetic"))})
        if event_type in ("tamper", "tamper_cleared"):
            m["tamper"] = event_type == "tamper"
        elif event_type in ("battery_low", "battery_ok"):
            m["battery_low"] = event_type == "battery_low"
        elif event_type == "zone_renamed":
            m["previous_name"] = str(e.get("from")) if e.get("from") is not None else None
        return m
    return mid, build


# ---------------------------------------------------------------- status
def panel_view(health_view: dict) -> dict:
    """The comparable (change-detecting) part of the panel status, from OutState.health_view()."""
    links = health_view.get("links") or {}
    return {"state": health_view.get("status") if health_view.get("status") in ("online", "degraded", "offline")
            else "offline",
            "active_source": _source(health_view.get("active_source")),
            "links": {s: (links.get(s) if links.get(s) in ("up", "down", "disabled") else "down") for s in SOURCES},
            "stale_zones": int(health_view.get("stale_zones", 0)),
            "zones": int(health_view.get("zones", 0))}


def panel_builder(site: dict | None, view: dict, last_panel_message_at: str | None,
                  now: float) -> tuple[str, Callable[[], dict]]:
    mid, ts = new_id(), rfc3339(now)
    lp = norm_ts(last_panel_message_at, now) if last_panel_message_at else None

    def build() -> dict:
        m = envelope(SCHEMA_PANEL, mid, ts, site)
        m.update({**view, "last_panel_message_at": lp})
        return m
    return mid, build


def zone_view(z: dict) -> dict:
    """The comparable part of a zone's status record."""
    return {"zone": zone_block(z), "state": z.get("state") if isinstance(z.get("state"), str) else "unknown",
            "stale": bool(z.get("stale")), "source": _source(z.get("source")), "tamper": bool(z.get("tamper")),
            "battery_low": bool(z.get("battery_low")), "online": bool(z.get("online", True))}


def zone_status_builder(site: dict | None, z: dict, now: float) -> tuple[str, Callable[[], dict]]:
    mid, ts = new_id(), rfc3339(now)
    view = zone_view(z)
    lc = z.get("last_changed")
    last_changed = norm_ts(lc, now) if lc else None

    def build() -> dict:
        m = envelope(SCHEMA_ZONE, mid, ts, site)
        m.update({**view, "last_changed": last_changed})
        return m
    return mid, build
