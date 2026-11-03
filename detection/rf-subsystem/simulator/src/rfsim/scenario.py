"""Scenario loading: actors, their movement, and the devices they carry.

Scenario files are YAML. See ``scenarios/README.md`` for the format.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from rfsim.devices import Device, build_kit
from rfsim.site import Point, Site, load_site

Waypoint = tuple[float, Point]  # (seconds from scenario start, position)


@dataclass
class Actor:
    """A person, vehicle, or fixed installation that carries devices.

    An actor is present only during its visits. Within a visit its position
    is linearly interpolated between waypoints.
    """

    actor_id: str
    role: str
    friendly: bool
    visits: list[list[Waypoint]]
    devices: list[Device] = field(default_factory=list)

    def __post_init__(self) -> None:
        for visit in self.visits:
            if len(visit) < 2:
                raise ValueError(f"actor {self.actor_id!r}: each visit needs at least two waypoints")
            times = [t for t, _ in visit]
            if times != sorted(times):
                raise ValueError(f"actor {self.actor_id!r}: waypoint times must be non-decreasing")
        self.visits.sort(key=lambda v: v[0][0])

    @property
    def windows(self) -> list[tuple[float, float]]:
        return [(v[0][0], v[-1][0]) for v in self.visits]

    def _segment(self, t: float) -> tuple[Waypoint, Waypoint] | None:
        for visit in self.visits:
            if visit[0][0] <= t <= visit[-1][0]:
                times = [w[0] for w in visit]
                # A waypoint time belongs to the segment that starts there.
                i = min(max(1, bisect.bisect_right(times, t)), len(visit) - 1)
                return visit[i - 1], visit[i]
        return None

    def position(self, t: float) -> Point | None:
        seg = self._segment(t)
        if seg is None:
            return None
        (t0, p0), (t1, p1) = seg
        f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        return (p0[0] + f * (p1[0] - p0[0]), p0[1] + f * (p1[1] - p0[1]))

    def is_moving(self, t: float) -> bool:
        seg = self._segment(t)
        return seg is not None and seg[0][1] != seg[1][1] and seg[0][0] != seg[1][0]

    def motion_starts(self) -> list[float]:
        """Times at which the actor starts moving after being stationary or absent."""
        starts = []
        for visit in self.visits:
            was_moving = False
            for (t0, p0), (t1, p1) in zip(visit, visit[1:]):
                moving = p0 != p1 and t0 != t1
                if moving and not was_moving:
                    starts.append(t0)
                was_moving = moving
        return starts


@dataclass
class Scenario:
    name: str
    description: str
    site: Site
    start_time: datetime
    duration_s: float
    seed: int
    actors: list[Actor]


def parse_duration(value: Any, *, end: float | None = None) -> float:
    """Seconds from a number, 'H:MM:SS' / 'MM:SS' string, or 'end'."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if value == "end":
            if end is None:
                raise ValueError("'end' is only valid for waypoint times")
            return end
        parts = [float(p) for p in value.split(":")]
        if 1 <= len(parts) <= 3:
            total = 0.0
            for p in parts:
                total = total * 60 + p
            return total
    raise ValueError(f"cannot parse duration {value!r}")


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("start_time must include a timezone (use Z for UTC)")
    return ts.astimezone(timezone.utc)


def load_scenario(path: str | Path, *, seed: int | None = None) -> Scenario:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    site = load_site((path.parent / data["site"]).resolve())
    duration = parse_duration(data["duration"])
    seed = int(data.get("seed", 0)) if seed is None else seed
    rng = random.Random(f"scenario:{seed}")

    actors = []
    seen_ids: set[str] = set()
    for spec in data.get("actors", []):
        actor = _build_actor(spec, rng, duration)
        if actor.actor_id in seen_ids:
            raise ValueError(f"duplicate actor id {actor.actor_id!r}")
        seen_ids.add(actor.actor_id)
        actors.append(actor)

    if bg := data.get("background"):
        actors.extend(_background_traffic(bg, random.Random(f"background:{seed}"), duration))

    return Scenario(
        name=data["name"],
        description=data.get("description", "").strip(),
        site=site,
        start_time=_parse_time(data["start_time"]),
        duration_s=duration,
        seed=seed,
        actors=actors,
    )


def _build_actor(spec: dict[str, Any], rng: random.Random, duration: float) -> Actor:
    actor_id = spec["id"]
    role = spec.get("role", "subject")
    if "path" in spec and "visits" in spec:
        raise ValueError(f"actor {actor_id!r}: use either 'path' or 'visits', not both")
    raw_visits = spec["visits"] if "visits" in spec else [spec["path"]]
    visits = [
        [(parse_duration(w["t"], end=duration), (float(w["pos"][0]), float(w["pos"][1]))) for w in visit]
        for visit in raw_visits
    ]
    for visit in visits:
        if visit[0][0] < 0 or visit[-1][0] > duration:
            raise ValueError(f"actor {actor_id!r}: waypoints must fall within the scenario duration")
    actor = Actor(actor_id, role, bool(spec.get("friendly", role == "friendly")), visits)

    for index, dev in enumerate(spec.get("devices", [])):
        dev = {"kit": dev} if isinstance(dev, str) else dev
        actor.devices.extend(
            build_kit(rng, actor_id, dev["kit"], index, randomize=dev.get("randomize"), overrides=dev.get("attributes"))
        )
    return actor


def _poisson_times(rng: random.Random, rate_per_hour: float, duration: float) -> list[float]:
    times, t = [], 0.0
    if rate_per_hour <= 0:
        return times
    while True:
        t += rng.expovariate(rate_per_hour / 3600.0)
        if t >= duration:
            return times
        times.append(t)


def _background_traffic(bg: dict[str, Any], rng: random.Random, duration: float) -> list[Actor]:
    """Through-traffic along the road: the noise analytics must not alert on."""
    a = tuple(float(v) for v in bg["road"]["from"])
    b = tuple(float(v) for v in bg["road"]["to"])
    length = math.dist(a, b)
    actors = []

    def traverse(actor_id: str, t0: float, speed: float, kits: list[str]) -> None:
        start, end = (a, b) if rng.random() < 0.5 else (b, a)
        t1 = min(t0 + length / speed, duration)
        actor = Actor(actor_id, "background", False, [[(t0, start), (t1, end)]])
        for index, kit in enumerate(kits):
            actor.devices.extend(build_kit(rng, actor_id, kit, index))
        actors.append(actor)

    for i, t0 in enumerate(_poisson_times(rng, float(bg.get("pedestrians_per_hour", 0)), duration)):
        kits = ["phone"] + (["smartwatch"] if rng.random() < 0.4 else [])
        traverse(f"bg-ped-{i:04d}", t0, rng.uniform(1.1, 1.7), kits)

    for i, t0 in enumerate(_poisson_times(rng, float(bg.get("vehicles_per_hour", 0)), duration)):
        kits = ["vehicle" if rng.random() < 0.7 else "vehicle_no_bt"]
        kits += ["phone"] * rng.choice((0, 1, 1, 2))
        traverse(f"bg-veh-{i:04d}", t0, rng.uniform(8.0, 15.0), kits)

    return actors
