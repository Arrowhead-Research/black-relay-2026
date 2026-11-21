"""Build the CoT the plugin reads. One function per map item type, all through one builder.

Why build it here and never pass Detection's text through: the TAK Server relays whatever it is
given to every phone, so the gateway is the last place a bad value can be stopped.

"""

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from .models import ANY_NODE, Location, Node, NodeState, Status

STALE_AFTER = timedelta(minutes=5)  # the event's stale time; ATAK may keep a stale item on the map
NODE_TYPE = "a-f-G-E-S"  # friendly ground equipment: CoT's type for a device
SITE_TYPE = "a-f-G-I"  # friendly ground installation
THREAT_TYPE = "a-u-G"  # unknown on the ground
DELETE_TYPE = "t-x-d-d"  # asks ATAK to remove the item named in the link
THREAT_LINE_COLOR = "#ffff0000"  # solid red
THREAT_FILL_COLOR = "#40ff0000"  # see-through red, so the map stays readable under it
UNKNOWN_CE = 9999999  # CoT's "not known" for ce and le
MIN_THREAT_RADIUS_M = 1.0  # ce may be 0, and a circle needs a size to be seen

# The line of detail a tripped node carries: a Fusion trip brings its own, built by trip_words from the
# event's fields; anything else (MQTT detections) is picked here by the event's type. The words are ours
# either way: an event's own text is never sent (PRD, Must never), so a type not listed gets GENERIC_DETAIL.
DETAILS = {
    "rf.detect": "RF emitter",
}
GENERIC_DETAIL = "detection"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms(t: datetime) -> str:
    """For `updated`: readers drop anything not newer, so two changes in one second must differ."""
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event(uid: str, cot_type: str, lat: float, lon: float, ce: float, now: datetime) -> ET.Element:
    ev = ET.Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "how": "m-g",
            "time": _iso(now),
            "start": _iso(now),
            "stale": _iso(now + STALE_AFTER),
        },
    )
    ET.SubElement(
        ev, "point", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "hae": "0", "ce": f"{ce:.0f}", "le": "9999999"}
    )
    ET.SubElement(ev, "detail")
    return ev


def _safehouse(parent: ET.Element, /, **attrs: str) -> None:
    ET.SubElement(parent, "__safehouse", attrs)


def _mission(detail: ET.Element, name: str | None) -> None:
    """Addresses the event to a TAK Server mission: its site's mission package. Only site-bound events carry
    it, and each takes the name of its own site (a node's, from `Node.mission_package`); no name, no element.

    TAK adds the event to the mission (and sends it to the mission's subscribers) only when the connection it
    arrives on is itself subscribed: see identify_event. A delete never carries it (delete_event)."""
    if name:
        ET.SubElement(ET.SubElement(detail, "marti"), "dest", {"mission": name})


def severity_label(severity: int) -> str:
    """Detection's 0-10 as the contract's three words: 0-3 info, 4-6 warning, 7-10 alert."""
    return "info" if severity <= 3 else "warning" if severity <= 6 else "alert"


def _trip_attrs(state: NodeState) -> dict[str, str]:
    """What only a tripped node carries: the facts of the event that tripped it."""
    if state.status != Status.tripped:
        return {}
    attrs: dict[str, str] = {}
    if state.severity is not None:
        attrs["severity"] = severity_label(state.severity)
    attrs["detail"] = state.detail or DETAILS.get(state.event_type or "", GENERIC_DETAIL)
    # RF: which emitter caused it, and where Fusion last put that emitter, in feet east (x) and north (y) of the
    # site's middle point. The id is a MAC address and nothing else (models.EMITTER_ID); the feet are numbers.
    if state.emitter is not None:
        attrs["emitter"] = state.emitter
        if state.x_ft is not None and state.y_ft is not None:
            attrs["x_ft"] = f"{state.x_ft:.1f}"
            attrs["y_ft"] = f"{state.y_ft:.1f}"
    return attrs


def node_uid(node: Node) -> str:
    """A node's id on the map. A node is only unique within its subsystem and site, so all three are in it;
    the "*" node, which stands for the subsystem as a whole, is named by the subsystem."""
    if node.node_id == ANY_NODE:
        return f"{node.site_id}-{node.subsystem.value}"
    return f"{node.site_id}-{node.subsystem.value}-{node.node_id}"


def _callsign(node: Node) -> str:
    return (node.subsystem.value if node.node_id == ANY_NODE else node.node_id).upper()


def site_uid(site_id: str) -> str:
    return f"{site_id}-site"


def threat_uid(node: Node) -> str:
    return f"{node_uid(node)}-threat"


def node_event(state: NodeState, now: datetime) -> bytes:
    n = state.node
    ev = _event(node_uid(n), NODE_TYPE, n.lat, n.lon, 10, now)
    detail = ev.find("detail")
    ET.SubElement(detail, "contact", {"callsign": _callsign(n)})
    _mission(detail, n.mission_package)
    stream = {"stream": n.stream} if n.stream else {}  # from the registry, checked at registration
    # Where a camera or the panel looks, as the admin set it: a phone draws the trip there. Every phone that
    # follows the site needs it, not only the one that built the site.
    watch = {"watch_lat": f"{n.watch[0]:.6f}", "watch_lon": f"{n.watch[1]:.6f}"} if n.watch else {}
    _safehouse(
        detail,
        site=n.site_id,
        role="node",
        node=n.node_id,
        subsystem=n.subsystem.value,
        status=state.status.value,
        updated=_iso_ms(state.updated),
        **_trip_attrs(state),
        **stream,
        **({} if state.placed else {"placed": "false"}),
        **watch,
    )
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


def site_event(
    site_id: str, lat: float, lon: float, now: datetime, alert: bool = False, mission: str | None = None
) -> bytes:
    """The site element. Its status is `alert` when any of its nodes is tripped, else `secure`
    (the contract's two site values). The plugin derives its own site status and ignores this
    attribute (FR-4); it is here for stock ATAK viewers and any other reader of the feed."""
    ev = _event(site_uid(site_id), SITE_TYPE, lat, lon, 25, now)
    detail = ev.find("detail")
    ET.SubElement(detail, "contact", {"callsign": f"SITE {site_id.upper()}"})
    _mission(detail, mission)
    _safehouse(
        detail,
        site=site_id,
        role="site",
        node="",
        subsystem="",
        status="alert" if alert else "secure",
        updated=_iso_ms(now),
    )
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


BOUNDARY_TYPE = "u-d-f"  # a drawn shape: stock ATAK draws it, the plugin ignores it (SafehouseDetail)
BOUNDARY_STROKE = "-256"  # opaque yellow, as a signed ARGB int
BOUNDARY_FILL = "16711680"  # red with zero alpha: no fill, so the map under it stays readable


def boundary_uid(site_id: str) -> str:
    return f"{site_id}-boundary"


def boundary_event(site_id: str, points: list[tuple[float, float]], now: datetime, mission: str | None = None) -> bytes:
    """The boundary the admin traced round the site (Gateway Changes Spec, change 2), as a closed stock ATAK
    shape: one `link` per corner and the first corner again to close it, centred on the corners' mean. The
    plugin's fixture gateway-cot-asked/boundary.xml is the shape asked for. `updated` is when it was sent."""
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    ev = _event(boundary_uid(site_id), BOUNDARY_TYPE, lat, lon, 0, now)
    ev.set("how", "h-e")
    detail = ev.find("detail")
    ET.SubElement(detail, "contact", {"callsign": f"{site_id.upper()} boundary"})
    _mission(detail, mission)
    ET.SubElement(detail, "__safehouse", {"site": site_id, "role": "boundary", "updated": _iso_ms(now)})
    for p_lat, p_lon in [*points, points[0]]:
        ET.SubElement(detail, "link", {"relation": "c", "type": "b-m-p-w", "point": f"{p_lat:.6f},{p_lon:.6f}"})
    ET.SubElement(detail, "strokeColor", {"value": BOUNDARY_STROKE})
    ET.SubElement(detail, "strokeWeight", {"value": "3.0"})
    ET.SubElement(detail, "strokeStyle", {"value": "dashed"})
    ET.SubElement(detail, "fillColor", {"value": BOUNDARY_FILL})
    ET.SubElement(detail, "labels_on", {"value": "true"})
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


def trip_events(state: NodeState, now: datetime) -> list[bytes]:
    """What a trip sends: the node, and its threat area when the detection said where it is."""
    events = [node_event(state, now)]
    if state.threat is not None:
        events.append(threat_event(state.node, state.threat, state.updated, now))
    return events


def threat_event(node: Node, where: Location, updated: datetime, now: datetime) -> bytes:
    """A threat area: a marker with a circle of radius ce attached (none when ce is None), which the plugin
    ignores on purpose.

    ATAK draws nothing from ce alone. It also leaves the marker up after its stale time, so whoever
    sends a threat has to take it down with delete_event.
    """
    ev = _event(threat_uid(node), THREAT_TYPE, where.lat, where.lon, UNKNOWN_CE if where.ce is None else where.ce, now)
    detail = ev.find("detail")
    if where.ce is not None:  # no number, no circle (Gateway Changes Spec, change 3)
        diameter = 2 * max(where.ce, MIN_THREAT_RADIUS_M)  # ATAK reads major and minor as full widths
        ET.SubElement(
            ET.SubElement(detail, "shape"),
            "ellipse",
            {
                "major": f"{diameter:.0f}",
                "minor": f"{diameter:.0f}",
                "angle": "0",
                "color": THREAT_LINE_COLOR,
                "fillColor": THREAT_FILL_COLOR,
                "lineWidth": "3",
            },
        )
    ET.SubElement(detail, "contact", {"callsign": f"{_callsign(node)} detection"})
    _mission(detail, node.mission_package)
    ET.SubElement(detail, "remarks").text = f"Approximate area from {node.node_id}. Site {node.site_id}."
    _safehouse(
        detail,
        site=node.site_id,
        role="threat",
        node=node.node_id,
        subsystem=node.subsystem.value,
        status="tripped",
        updated=_iso_ms(updated),
    )
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


def delete_event(uid: str, now: datetime) -> bytes:
    """Removes an item from every phone that receives this, along with anything attached to it.

    It carries no mission: TAK 5.6 would add the delete itself to the mission as one more item (tested
    2026-11-20). It goes to everyone in the group, mission subscribers included, and the item comes out of
    its mission package through the Mission API instead (AtakMissionPackages.remove_items)."""
    ev = _event(f"{uid}.delete", DELETE_TYPE, 0, 0, 9999999, now)
    detail = ev.find("detail")
    ET.SubElement(detail, "link", {"uid": uid, "relation": "none", "type": "none"})
    ET.SubElement(detail, "__forcedelete")
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


def identify_event(uid: str, now: datetime) -> bytes:
    """Tells TAK Server which client this connection is, so it can find the connection's mission subscriptions.

    TAK sends an event with <marti><dest mission> on, and adds it to the mission, only when the connection it came
    in on is subscribed to that mission; a connection that never named itself has no client uid and is
    subscribed to nothing ("unable to find mission subscription for client ..." in the messaging log). The
    gateway names itself `uid`, the creatorUid of every mission package it makes, which TAK subscribes as the
    owner. It is a ping (t-x-c-t), which puts nothing on a map; the contact endpoint is what TAK reads the
    client uid from (a ping with takv alone is not enough, tested 2026-11-20)."""
    ev = _event(uid, "t-x-c-t", 0, 0, UNKNOWN_CE, now)
    ev.set("how", "h-g-i-g-o")
    detail = ev.find("detail")
    ET.SubElement(detail, "contact", {"callsign": uid, "endpoint": "*:-1:stcp"})
    ET.SubElement(detail, "takv", {"device": "gateway", "platform": "pytak-gateway", "os": "linux", "version": "1"})
    return ET.tostring(ev, encoding="utf-8", xml_declaration=True)


def health_marker(uid: str, remarks: str, source: str, now: datetime) -> bytes:
    """A status marker for a feed the gateway watches (the http monitor's `HealthCheck-fusion`): a point at 0,0
    with no position of its own, a one-minute stale time, and the status line in its remarks. `source` says which
    process sent it."""
    stamp = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # noqa: E731
    ev = ET.Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": "t-x-g",
            "how": "m-g",
            "time": stamp(now),
            "start": stamp(now),
            "stale": stamp(now + timedelta(seconds=60)),
        },
    )
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": "0.0", "ce": "9999999.0", "le": "9999999.0"})
    detail = ET.SubElement(ev, "detail")
    ET.SubElement(detail, "raw", {"source": source})
    ET.SubElement(detail, "remarks").text = remarks
    return ET.tostring(ev)
