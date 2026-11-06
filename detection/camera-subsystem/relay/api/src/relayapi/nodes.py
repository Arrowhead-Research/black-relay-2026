"""What a heartbeat means to the relay: its name, its time, its address, and how it is reported."""

import json
import re
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from typing import Any

# The portal's rule (provision config.py, _RELAY_NODE_ID_RE), with hostname's limit of 63. Schema
# patterns end in "$", which also matches before a final newline, so this is applied again with
# fullmatch to anything that later reaches a URL or a stored field.
_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# MediaMTX 1.21.3, internal/conf/path.go:455-456 makes a path of either name the catch-all
# "^.*$", so a reader of any unknown name would pull from that node. The pattern above already
# refuses "all_others" and a leading "~", which is path.go:458-470's regular-expression path.
# path.go:24 and 30-52 allow the rest of the pattern's characters, with no other special name.
_MEDIAMTX_SPECIAL = frozenset({"all", "all_others"})
_TIMESTAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
NO_CENTRAL_PROXY = "central_proxy_unavailable"
# What a stored row can raise. A row is only ever what ingest let through, but a database can be
# older than today's ingest.
READ_FAILURES = (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError)


def node_id_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _NODE_ID_RE.fullmatch(value) is not None
        and value not in _MEDIAMTX_SPECIAL
    )


def timestamp_us(text: str) -> int:
    """Whole microseconds since the epoch, so that order is exact. Raises ValueError on
    anything the schema's pattern let through that is not a real time."""
    match = _TIMESTAMP_RE.fullmatch(text)
    if match is None:
        raise ValueError("not an RFC 3339 UTC timestamp")
    fraction = (match[7] or "")[:6].ljust(6, "0")
    moment = datetime(
        *(int(match[index]) for index in range(1, 7)), int(fraction), tzinfo=timezone.utc
    )
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class AddressPolicy:
    """The one rule for where the relay may send a node's login and a user's request.

    IPv4 only, as nodes report wlan0's IPv4 address, and in canonical form only. It must be a
    host on the site's subnet, which settings have already kept clear of loopback, link-local,
    multicast and the bundle's networks. An address outside this rule is never stored, and one
    already stored is treated as absent, so that it can reach neither the proxy nor MediaMTX.
    """

    def __init__(
        self,
        site: IPv4Network,
        internal: tuple[IPv4Network, ...] = (),
        central: IPv4Address | None = None,
    ):
        self._site = site
        self._internal = internal
        self._central = central

    def check(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            address = IPv4Address(value)
        except ValueError:
            return None
        if address not in self._site:
            return None
        if address in (self._site.network_address, self._site.broadcast_address, self._central):
            return None
        if any(address in network for network in self._internal):
            return None
        # IPv4Address takes only dotted-decimal, with no leading zeros, whitespace or scope, so
        # this is the same text. Tests hold that to account.
        return str(address)


def too_deep(message: Any, limit: int) -> bool:
    """Walked with a list, not recursion, so that it cannot fail the way the problem does."""
    stack = [(message, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def attributes_of(message_text: str) -> dict:
    attributes = json.loads(message_text)["attributes"]
    if not isinstance(attributes, dict):
        raise ValueError("attributes is not an object")
    return attributes


def report(
    row: Any, policy: AddressPolicy, now: float, stale_after_s: float, max_depth: int
) -> dict:
    """A node's newest heartbeat, as sent, plus how long ago the relay received it. The node's
    stamp plays no part in that, so a clock ahead gains no time. A dead node's last ``true``
    values are still shown, and ``stale`` says they are not current. ``detector_ok: false``
    with no reason is passed on as it is: it means "not yet confirmed", and the relay does not
    turn it into a state of its own. A stored row deeper than ingest allows raises: encoding it
    might work here and still break the client that parses it."""
    message = json.loads(row["message"])
    if too_deep(message, max_depth):
        raise ValueError("nested too deep")
    attributes = attributes_of(row["message"])
    age = max(0.0, round(now - row["received_at"], 3))
    return {
        "node_id": row["node_id"],
        "timestamp": message["timestamp"],
        "heartbeat_age_s": age,
        "stale": age > stale_after_s,
        "node_address": policy.check(row["node_address"]),
        "camera_online": message["camera_online"],
        "link_online": message["link_online"],
        "recording_ok": message["recording_ok"],
        "detector_ok": message["detector_ok"],
        "reason": attributes.get("reason"),
        "attributes": attributes,
    }
