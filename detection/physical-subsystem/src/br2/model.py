"""Source-neutral mapping: panel observations -> contract records."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

KINDS = ("glass_break", "motion", "contact", "other")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def zone_id(n: int) -> str:
    return f"zone-{int(n)}"


def partition_key(n: int | str) -> str:
    return f"partition-{n}" if not str(n).startswith("partition-") else str(n)


def _norm(s: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(s or "").lower())


def is_keypad(type_: str | None, group: str | None, name: str | None = None) -> bool:
    """A keypad / IQ Remote zone: the panel lists every paired keypad-type device (including this bridge, which pairs as
    an IQ Remote and shows up as e.g. "IQ Remote 1") in its zone table. These are not sensors. Matches on the panel's
    sensor type / group (contains "keypad" or "iqremote") or, as a fallback, a zone named exactly "IQ Remote <n>"."""
    t, g, n = _norm(type_), _norm(group), _norm(name)
    return ("keypad" in t or "iqremote" in t or "keypad" in g or "iqremote" in g
            or re.fullmatch(r"iqremote\d*", n) is not None)


def classify(type_: str | None, group: str | None, override: str | None = None) -> str:
    if override in KINDS:
        return override  # type: ignore[return-value]
    if is_keypad(type_, group):
        return "other"                   # never contact/motion, whatever group the panel files the keypad under
    t, g = _norm(type_), _norm(group)
    if t in ("glassbreak", "panelglassbreak") or g in ("glassbreak", "glassbreakawayonly"):
        return "glass_break"
    if t in ("motion", "panelmotion", "occupancysensor") or "motion" in g:
        return "motion"
    if t in ("doorwindow", "doorwindowm", "tilt", "contact") or g in ("entryexitdelay", "entryexitlongdelay",
                                                                        "instantperimeter", "instantinterior",
                                                                        "garagetilt1", "garagetilt2"):
        return "contact"
    return "other"


_ACTIVE = {"open", "active", "activated", "alarmed", "occupied", "tampered"}
_IDLE = {"closed", "idle", "inactive", "normal", "vacant", "disarm", "connected"}
_OFFLINE = {"unreachable", "notnetworked", "disconnected", "failure", "synchronizing"}


def map_state(kind: str, status: Any) -> tuple[str | None, bool | None]:
    """Return (state, online). state None = unchanged (tamper-only report)."""
    s = _norm(status)
    if s == "tampered":
        return None, None
    if s in _OFFLINE:
        return "unknown", False
    if s in _ACTIVE:
        on = True
    elif s in _IDLE:
        on = False
    else:
        return "unknown", None
    if kind in ("motion", "glass_break"):
        return ("active" if on else "idle"), True
    if kind == "contact":
        return ("open" if on else "closed"), True
    # other: preserve the panel vocabulary where distinct
    if s in ("open", "closed"):
        return s, True
    return ("active" if on else "idle"), True


def map_battery(b: Any) -> bool | None:
    """Panel convention: battery_low = value not in {"", "Normal"}. Empty/absent = unknown (None)."""
    if b is None or str(b).strip() == "":
        return None
    return _norm(b) != "normal"


def map_arming(status: Any, alarm_state: Any = None) -> str:
    a = _norm(alarm_state)
    s = _norm(status)
    if a == "alarm" or s == "alarm":
        return "alarm"
    if a == "delay" or s == "entrydelay":
        return "entry_delay"
    if "exitdelay" in s:
        return "exit_delay"
    return {"disarm": "disarmed", "disarmed": "disarmed", "armstay": "arm_stay", "armaway": "arm_away",
            "armnight": "arm_night"}.get(s, "unknown")


def blank_zone(n: int) -> dict:
    return {"id": zone_id(n), "panel_zone_id": n, "name": f"Zone {n}", "kind": "other", "partition_id": 0,
            "state": "unknown", "tamper": False, "battery_low": False, "online": True,
            "last_changed": None, "last_seen": None, "source": None, "stale": True, "raw": {}}
