"""The one line a tripped sensor shows (`detail`), in the gateway's own words.

Built from the structured fields of the Fusion event that tripped it: rule types, emitter types, zone kinds,
labels and numbers. Every word comes from the tables here; no text of the event is ever passed through
(PRD, Must never: "The gateway SHALL NOT pass a detection source's text through as CoT"), so a zone Fusion
calls "Back Door" still reads "Door or window opened". A value the tables do not know falls back to a
plain phrase. The plugin shows at most 80 characters on one line (docs/contract.md), and every line here
is well under that.
"""

from typing import Any

MAX_LEN = 80

# rf.alert's emitter.type
EMITTERS = {
    "wifi_ap": "Wi-Fi access point",
    "wifi_client": "Wi-Fi device",
    "ble": "Bluetooth device",
    "bt_classic": "Bluetooth device",
}
UNKNOWN_EMITTER = "Radio device"

# camera.detection's label (Frigate's object names)
LABELS = {
    "person": "Person",
    "car": "Car",
    "truck": "Truck",
    "motorcycle": "Motorcycle",
    "bicycle": "Bicycle",
    "bus": "Bus",
    "boat": "Boat",
    "dog": "Dog",
    "cat": "Cat",
    "bird": "Bird",
    "horse": "Horse",
    "bear": "Bear",
    "deer": "Deer",
    "package": "Package",
    "face": "Face",
    "license_plate": "Licence plate",
}

# physical.event's zone.kind, when the zone left its resting state
ZONES = {
    "contact": "Door or window opened",
    "motion": "Motion detected",
    "glass_break": "Glass break detected",
}

# Fusion's three severity words, as the gateway's 0-10 (cot.severity_label turns it back into info/warning/alert)
SEVERITIES = {"low": 3, "medium": 5, "high": 8, "critical": 9}


def describe(event_type: str, data: dict[str, Any]) -> str:
    """The line for a trip by an event of this type with this data."""
    match event_type:
        case "rf.alert":
            line = _rf_alert(data)
        case "camera.detection":
            line = _camera(data)
        case "physical.event":
            line = _physical(data)
        case _:
            line = "Detection"
    return line[:MAX_LEN]


def severity(data: dict[str, Any], default: int) -> int:
    """Fusion's own severity word on the event (rf.alert has one), else `default`."""
    word = data.get("severity")
    return SEVERITIES.get(word.lower(), default) if isinstance(word, str) else default


def _rf_alert(data: dict[str, Any]) -> str:
    emitter = data.get("emitter")
    kind = emitter.get("type") if isinstance(emitter, dict) else None
    who = EMITTERS.get(kind, UNKNOWN_EMITTER) if isinstance(kind, str) else UNKNOWN_EMITTER
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    match data.get("rule_type"):
        case "loiter":
            dwell = _number(context.get("dwell_s"))
            return f"{who} lingering for {_duration(dwell)}" if dwell else f"{who} lingering"
        case "repeat_visitor":
            visits = _number(context.get("visits"))
            window = _number(context.get("window_s"))
            if visits and window:
                return f"{who} back {int(visits)} times in {_duration(window)}"
            return f"{who} keeps coming back"
    return f"{who} flagged" if kind else "RF alert"


def _camera(data: dict[str, Any]) -> str:
    label = data.get("label")
    what = LABELS.get(label.lower(), "Object") if isinstance(label, str) else "Object"
    confidence = _number(data.get("confidence"))
    if confidence is not None and 0 < confidence <= 1:
        return f"{what} seen on camera · {round(confidence * 100)}% sure"
    return f"{what} seen on camera"


def _physical(data: dict[str, Any]) -> str:
    if data.get("tamper") is True or data.get("event_type") == "tamper":
        return "Sensor tampered with"
    zone = data.get("zone")
    kind = zone.get("kind") if isinstance(zone, dict) else None
    return ZONES.get(kind, "Alarm zone triggered") if isinstance(kind, str) else "Alarm zone triggered"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _duration(seconds: float) -> str:
    """601 -> "10 min", 86400 -> "24 h", 45 -> "45 s"."""
    if seconds < 60:
        return f"{round(seconds)} s"
    if seconds < 2 * 3600:
        return f"{round(seconds / 60)} min"
    if seconds < 3 * 86400:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"
