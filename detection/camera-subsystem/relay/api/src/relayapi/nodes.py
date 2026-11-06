"""What a heartbeat means to the relay: its time, its address, and how it is reported."""

import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any

# Schema patterns end in "$", which also matches before a final newline, so these are applied
# again with fullmatch to anything that later reaches a URL or a stored field.
NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_TIMESTAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NO_CENTRAL_PROXY = "central_proxy_unavailable"


def timestamp_us(text: str) -> int:
    """Whole microseconds since the epoch, so that order is exact. Raises ValueError on
    anything the schema's pattern let through that is not a real time."""
    match = _TIMESTAMP_RE.fullmatch(text)
    if match is None:
        raise ValueError("not an RFC 3339 UTC timestamp")
    fraction = (match[7] or "")[:6].ljust(6, "0")
    moment = datetime(*(int(match[index]) for index in range(1, 7)), int(fraction), tzinfo=UTC)
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def node_address(value: Any) -> str | None:
    """The address as an IP literal, or None. A node's address only ever comes from here, and
    no host name, port or credential can pass. Addresses that cannot be another machine are
    refused, so that a heartbeat cannot point the proxy or MediaMTX back at the relay itself."""
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    target = mapped if mapped is not None else address
    if (
        target.is_loopback
        or target.is_link_local
        or target.is_multicast
        or target.is_unspecified
        or target.is_reserved
    ):
        return None
    return str(address)


def attributes_of(message_text: str) -> dict:
    return json.loads(message_text)["attributes"]


def report(row: Any, now: float, stale_after_s: float) -> dict:
    """A node's newest heartbeat, as sent, plus how old it is. A dead node's last ``true``
    values are still shown, and ``stale`` says they are not current. ``detector_ok: false``
    with no reason is passed on as it is: it means "not yet confirmed", and the relay does not
    turn it into a state of its own."""
    message = json.loads(row["message"])
    age = max(0.0, round(now - row["timestamp_us"] / 1_000_000, 3))
    attributes = message["attributes"]
    return {
        "node_id": row["node_id"],
        "timestamp": message["timestamp"],
        "heartbeat_age_s": age,
        "stale": age > stale_after_s,
        "node_address": row["node_address"],
        "camera_online": message["camera_online"],
        "link_online": message["link_online"],
        "recording_ok": message["recording_ok"],
        "detector_ok": message["detector_ok"],
        "reason": attributes.get("reason"),
        "attributes": attributes,
    }
