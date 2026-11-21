"""What comes in from Detection and Fusion, and what the gateway knows about each node.

The words are Fusion's. A *site* is a place (a safehouse); a *subsystem* is the sort of thing that reports
(camera, rf, physical, ...); a *node* is one device of a subsystem at a site. A node is addressed by the
triple (site_id, subsystem, node_id). A message body never names its site: it comes from the URL (HTTP), the
topic (MQTT) or Fusion's own event (the http monitor).

Detection's events are normalized (event id, type, time, severity, location, node). They do not carry the
plugin's fields; the registry (registry.py) adds those.
"""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Keep in step with SafehouseDetail.PLAIN_RTSP in the plugin.
PLAIN_RTSP = re.compile(
    r"[Rr][Tt][Ss][Pp][Ss]?://(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::(?P<port>[0-9]{1,5}))?(?:/[A-Za-z0-9._~/-]*)?",
    re.ASCII,
)


def _plain_rtsp(v: Optional[str]) -> Optional[str]:
    """The stream rides on CoT to every phone and is opened by ATAK's video player, so it is
    rtsp://host[:port][/path] or rtsps:// and nothing else: no other scheme, no login, no
    query, only letters, digits and . _ ~ - / in the path. The plugin applies the same rule
    (SafehouseDetail.plainRtsp); the two tests share one list of URLs."""
    if v is None:
        return None
    m = PLAIN_RTSP.fullmatch(v)
    if m is None or (m["port"] is not None and not 0 < int(m["port"]) < 65536):
        raise ValueError("stream must be rtsp://host[:port][/path] or rtsps://, with no login or query")
    return v


class Subsystem(str, Enum):
    """What reports. camera, rf and physical are Fusion's three; wifi, bt and tpms are for platforms that
    push to the gateway directly (Fusion never sends them)."""

    camera = "camera"
    rf = "rf"
    physical = "physical"
    wifi = "wifi"
    bt = "bt"
    tpms = "tpms"


class Status(str, Enum):
    ok = "ok"
    tripped = "tripped"
    offline = "offline"


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ce: Optional[float] = Field(
        default=150.0, ge=0, le=100_000, description="circular error, metres; None: not known, so no circle is drawn"
    )


def _utc(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    if v.tzinfo is None:
        raise ValueError("time must carry a timezone")
    return v.astimezone(timezone.utc)


# Fusion allows 128 characters for a site id and a node id.
SITE_ID = r"^[A-Za-z0-9._-]{1,128}$"
ANY_NODE = "*"  # the subsystem's events that name no node (RF positions, device alerts)
NODE_ID = r"^(\*|[A-Za-z0-9._-]{1,128})$"


class NodeKey(BaseModel):
    """Which node of a site a message is about. The site is the caller's (URL, topic), never the body's."""

    subsystem: Subsystem
    node_id: str = Field(pattern=NODE_ID)


# An emitter's id as Fusion sends it for RF: a MAC address, here always lower case with colons. Nothing else
# is taken for one, so no text of a detection's own reaches a phone through it.
EMITTER_ID = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
# Fusion places an emitter in feet on the site's floor plan, from the site's middle point. A site is far
# smaller than this; a figure outside it is not a position.
FEET_MAX = 100_000


class DetectionEvent(NodeKey):
    """One normalized event from the Detection team, as seen on MQTT or mapped from Fusion."""

    event_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    time: datetime
    severity: int = Field(ge=0, le=10)
    location: Optional[Location] = None
    detail: Optional[str] = Field(default=None, max_length=80, description="the gateway's own words (trip_words)")
    emitter: Optional[str] = Field(default=None, pattern=EMITTER_ID, description="RF: the emitter that caused it")
    x_ft: Optional[float] = Field(default=None, ge=-FEET_MAX, le=FEET_MAX, description="RF: where Fusion last put it")
    y_ft: Optional[float] = Field(default=None, ge=-FEET_MAX, le=FEET_MAX)

    _time_utc = field_validator("time")(_utc)


class SiteCreate(BaseModel):
    site_id: str = Field(pattern=SITE_ID)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


MISSION_PACKAGE_NAME = r"^[A-Za-z0-9._-]{1,128}$"
MISSION_PASSWORD_MIN, MISSION_PASSWORD_MAX = 8, 128

LatLon = tuple[Annotated[float, Field(ge=-90, le=90)], Annotated[float, Field(ge=-180, le=180)]]
BOUNDARY_MIN, BOUNDARY_MAX = 3, 200  # Gateway Changes Spec, change 1; the plugin's Boundary.MAX_POINTS
PACKAGE_NODES_MAX = 500


class BuildNode(NodeKey):
    """One node of a build package, placed by the admin (Gateway Changes Spec, change 1).

    A camera or the panel carries `range_m`, how far it detects; RF carries `accuracy_m` instead, how far
    the emitter may be from where Fusion puts it. Either may be null: no number means no circle, and the
    gateway never fills one in. `watch` is where a camera or the panel looks (the target goes there when it
    trips, change 8); RF has none. Only a camera has a stream."""

    model_config = ConfigDict(extra="forbid")  # a misspelt field is refused, not silently dropped

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    range_m: Optional[float] = Field(default=None, gt=0, le=100_000, description="how far it can detect, metres")
    accuracy_m: Optional[float] = Field(default=None, gt=0, le=100_000, description="RF: how far off, metres")
    stream: Optional[str] = Field(default=None, max_length=256, description="a camera's RTSP URL, a VPN address")
    watch: Optional[LatLon] = None

    _plain_stream = field_validator("stream")(_plain_rtsp)

    @model_validator(mode="after")
    def _fits_its_subsystem(self) -> "BuildNode":
        if self.subsystem == Subsystem.rf:
            if self.range_m is not None:
                raise ValueError("RF carries accuracy_m, not range_m")
            if self.watch is not None:
                raise ValueError("RF has no watch point")
        elif self.accuracy_m is not None:
            raise ValueError("only RF carries accuracy_m; a camera or the panel carries range_m")
        if self.stream is not None and self.subsystem != Subsystem.camera:
            raise ValueError("only a camera node has a stream")
        return self


class BuildPackage(SiteCreate):
    """The build package: the whole site in one call, `POST /sites` to build it and `PUT /sites/<id>` to
    update it (Gateway Changes Spec, change 1). `boundary` and `nodes` are optional, so a caller that sends
    only the site keeps working. In an update the package is the whole site: a node left out is removed."""

    model_config = ConfigDict(extra="forbid")

    boundary: Optional[list[LatLon]] = Field(default=None, min_length=BOUNDARY_MIN, max_length=BOUNDARY_MAX)
    nodes: list[BuildNode] = Field(default_factory=list, max_length=PACKAGE_NODES_MAX)

    @field_validator("boundary")
    @classmethod
    def _a_simple_ring(cls, v: Optional[list[LatLon]]) -> Optional[list[LatLon]]:
        if v is not None:
            problem = ring_problem(v)
            if problem:
                raise ValueError(problem)
        return v

    @field_validator("nodes")
    @classmethod
    def _each_node_once(cls, v: list[BuildNode]) -> list[BuildNode]:
        seen = set()
        for n in v:
            key = (n.subsystem, n.node_id)
            if key in seen:
                raise ValueError(f"{n.subsystem.value}/{n.node_id} is in the package twice")
            seen.add(key)
        return v


class SiteBuild(BuildPackage):
    """`POST /sites`: the build package, and the mission package the site's CoT will belong to. The password is
    only ever hashed and is never returned or logged."""

    mission_package_name: str = Field(pattern=MISSION_PACKAGE_NAME)
    mission_package_password: str = Field(min_length=MISSION_PASSWORD_MIN, max_length=MISSION_PASSWORD_MAX)


def ring_problem(points: list[LatLon]) -> Optional[str]:
    """Why these points are not a boundary, or None. The ring closes from the last point back to the first;
    it may not repeat a point or cross itself. Worked on lat/lon as if flat, which is close enough at the
    size of a site."""
    n = len(points)
    if len(set(points)) != n:
        return "the boundary repeats a point"
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbours share a corner
            if _cross(*edges[i], *edges[j]):
                return "the boundary crosses itself"
    return None


def _cross(a, b, c, d) -> bool:
    """Whether segment ab and segment cd touch at all (crossing, or touching end on)."""

    def side(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (v > 0) - (v < 0)

    def within(p, q, r):  # r on the line pq: is it between them
        return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(p[1], q[1])

    s1, s2, s3, s4 = side(a, b, c), side(a, b, d), side(c, d, a), side(c, d, b)
    if s1 != s2 and s3 != s4:
        return True
    return (
        (s1 == 0 and within(a, b, c))
        or (s2 == 0 and within(a, b, d))
        or (s3 == 0 and within(c, d, a))
        or (s4 == 0 and within(c, d, b))
    )


class NodeRegistration(BaseModel):
    """What a node declares about itself, or an operator says of it by dropping it on the map. The site,
    subsystem and node id are in the URL, so none of them is here."""

    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    range_m: float = Field(default=150.0, gt=0, le=100_000, description="how far it can detect, metres")
    stream: Optional[str] = Field(default=None, max_length=256, description="a camera's RTSP URL, a VPN address")

    _plain_stream = field_validator("stream")(_plain_rtsp)

    @model_validator(mode="after")
    def _position_is_whole(self) -> "NodeRegistration":
        if (self.lat is None) != (self.lon is None):
            raise ValueError("give lat and lon together, or neither (the site's position is used)")
        return self


class HealthReport(NodeKey):
    """One line of Detection's service health feed: is this node up?"""

    up: bool
    time: datetime

    _time_utc = field_validator("time")(_utc)


EMITTER_NAME_MAX = 64
ALLOWED_EMITTERS_MAX = 500  # per site: a house has tens of devices of its own, not thousands


class AllowEmitter(BaseModel):
    """Allow one emitter at a site: `POST /sites/<id>/emitters`. `emitter` is its MAC address, as the trip's CoT
    gave it or as an admin typed it (colons or dashes, either case). `name` is the admin's own word for it."""

    model_config = ConfigDict(extra="forbid")

    emitter: str = Field(max_length=32)
    name: Optional[str] = Field(default=None, max_length=EMITTER_NAME_MAX)

    @field_validator("emitter")
    @classmethod
    def _a_mac_address(cls, v: str) -> str:
        mac = emitter_id(v)
        if mac is None:
            raise ValueError("is not a MAC address")
        return mac

    @field_validator("name")
    @classmethod
    def _a_plain_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if any(not c.isprintable() for c in v):
            raise ValueError("has a character that cannot be shown")
        return v or None


def emitter_id(raw: object) -> Optional[str]:
    """`raw` as an emitter id, lower case with colons, or None when it is not a MAC address. The one place a
    value becomes an emitter id, whether Fusion sent it or an admin typed it."""
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    mac = raw.strip().lower().replace("-", ":")
    return mac if re.fullmatch(EMITTER_ID, mac) else None


class ClearRequest(NodeKey):
    """End a trip (ADR-104). `tripped`, when given, is the `updated` of the trip being cleared:
    the clear then ends that trip and no later one."""

    time: datetime
    tripped: Optional[datetime] = None

    _time_utc = field_validator("time", "tripped")(_utc)


class Node(BaseModel):
    """What the registry says a node is."""

    site_id: str
    # The mission package of this node's own site (every site has one). Filled from the site's row by
    # registry.node_of, never from a caller, so the CoT built from a node can only name its own site's package.
    mission_package: Optional[str] = None
    subsystem: Subsystem
    node_id: str
    lat: float
    lon: float
    range_m: Optional[float] = Field(default=150.0, gt=0, description="how far the node can detect, metres")
    accuracy_m: Optional[float] = Field(default=None, gt=0, description="RF: how far off its positions are")
    stream: Optional[str] = Field(default=None, max_length=256, description="a camera's RTSP URL, a VPN address")
    watch: Optional[tuple[float, float]] = Field(default=None, description="where a camera or the panel looks")

    _plain_stream = field_validator("stream")(_plain_rtsp)

    @model_validator(mode="after")
    def _only_cameras_stream(self) -> "Node":
        if self.stream is not None and self.subsystem != Subsystem.camera:
            raise ValueError("only a camera node has a stream")
        return self


class NodeState(BaseModel):
    """The gateway's own memory of one node, re-sent on a timer."""

    node: Node
    status: Status
    updated: datetime
    threat: Optional[Location] = None
    severity: Optional[int] = Field(default=None, ge=0, le=10, description="of the event that tripped it")
    event_type: Optional[str] = Field(default=None, description="that event's type: picks the detail, never sent")
    detail: Optional[str] = Field(default=None, description="the trip's line in the gateway's own words, if it has one")
    # False for a node Fusion reports at a registered site that the admin has not placed: `node` then stands at its
    # site's position with no range, and a phone shows it in the pane only (cot.node_event says placed="false").
    placed: bool = True
    emitter: Optional[str] = Field(default=None, description="RF: the emitter that caused the trip (a MAC address)")
    x_ft: Optional[float] = Field(default=None, description="RF: the emitter's last position, feet from the site's")
    y_ft: Optional[float] = Field(default=None, description="middle point; both or neither")
