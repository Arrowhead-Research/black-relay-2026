"""Alert rule engine (DESIGN.md 7.3.7, 7.3.8; CEN-07, CEN-09).

The engine consumes analyzer events, sees every observation (for node
liveness), and is ticked with the current observation time so time-based
rules (loiter, node offline) fire without waiting for another event.

Rules are declarative (YAML, see ``config/rules.example.yaml``). Every rule
has an ``id``, ``type``, ``severity``, ``enabled``, and a per-emitter
``cooldown_s`` so a condition that persists alerts once, not continuously.

It produces ``Message`` objects of three kinds: ``alert``, ``camera_cue``
and ``node_status``. ``formats.py`` turns them into wire payloads.

Rule types:

* ``loiter``: an emitter stays in one zone (optionally limited to ``zones``)
  for at least ``min_dwell_s``. Stays end on a zone change or staleness.
* ``new_device``: a confirmed visit by an emitter never seen before, starting
  within optional local ``hours``, that stays at least ``min_duration_s``
  (so traffic passing on the road does not count). Nothing fires during the
  initial ``learn_s`` period while the engine builds its baseline. Randomized
  addresses are ignored unless ``include_randomized`` (every rotation looks
  "new").
* ``repeat_visitor``: an emitter starts at least ``min_visits`` visits within
  ``window_s``. Randomized addresses are ignored (they cannot repeat).
* ``watchlist``: a confirmed visit by a listed emitter ID or SSID.
* ``node_offline``: no observation from a site node for ``silence_s``. A
  ``node_status`` message is published on every offline/online change. This
  is a stand-in: silence also happens on a quiet site, and node heartbeats
  (DESIGN.md 6.7, NODE-06) will replace it as the health signal.
* ``camera_cue``: a ``first_heard`` event (TPMS by default). One cue per
  ``cooldown_s`` for the whole site, which groups the sensors of one vehicle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from rfanalytics.events import Event, iso_utc
from rfanalytics.observation import Observation

SEVERITIES = ("info", "low", "medium", "high", "critical")


@dataclass(frozen=True)
class Message:
    kind: str  # alert | camera_cue | node_status
    t: float
    body: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


# -- rules --------------------------------------------------------------------


class Rule:
    type: str = ""

    def __init__(self, spec: dict[str, Any], ctx: "EngineContext") -> None:
        self.id: str = spec["id"]
        self.severity: str = spec.get("severity", "medium")
        if self.severity not in SEVERITIES:
            raise ValueError(f"rule {self.id!r}: severity must be one of {SEVERITIES}")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.cooldown_s: float = float(spec.get("cooldown_s", 1800))
        self.zones: set[str] | None = set(spec["zones"]) if spec.get("zones") else None
        self.emitter_types: set[str] | None = set(spec["emitter_types"]) if spec.get("emitter_types") else None
        self.ctx = ctx
        self._last_fired: dict[str, float] = {}
        known = {"id", "type", "severity", "enabled", "cooldown_s", "zones", "emitter_types", "description"}
        unknown = set(spec) - known - self.extra_keys
        if unknown:
            raise ValueError(f"rule {self.id!r}: unknown keys {sorted(unknown)}")

    extra_keys: frozenset[str] = frozenset()

    def on_event(self, ev: Event) -> list[Message]:
        return []

    def on_observation(self, obs: Observation) -> list[Message]:
        return []

    def on_tick(self, now: float) -> list[Message]:
        return []

    # helpers
    def _type_ok(self, emitter_type: str) -> bool:
        return self.emitter_types is None or emitter_type in self.emitter_types

    def _zone_ok(self, zone: str | None) -> bool:
        return self.zones is None or zone in self.zones

    def _cooled(self, key: str, t: float) -> bool:
        last = self._last_fired.get(key)
        if last is not None and t - last < self.cooldown_s:
            return False
        self._last_fired[key] = t
        return True

    def _alert(self, t: float, emitter_id: str | None, emitter_type: str | None, context: dict[str, Any],
               zone: str | None = None, randomized: bool | None = None) -> Message:
        body: dict[str, Any] = {"rule_id": self.id, "rule_type": self.type, "severity": self.severity}
        if emitter_id is not None:
            body["emitter"] = {"id": emitter_id, "type": emitter_type, "randomized_address": randomized}
            tracked = self.ctx.emitters.get(emitter_id)
            if zone is None and tracked:
                zone = tracked.zone
            if tracked and tracked.confidence is not None:
                body["location"] = {"zone": zone, "confidence": tracked.confidence}
            else:
                body["location"] = {"zone": zone, "confidence": None}
        body["context"] = context
        return Message("alert", t, body)


class LoiterRule(Rule):
    type = "loiter"
    extra_keys = frozenset({"min_dwell_s"})

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self.min_dwell_s = float(spec.get("min_dwell_s", 600))
        self._fired_stays: set[tuple[str, float]] = set()

    def on_tick(self, now):
        out = []
        for emitter_id, e in self.ctx.emitters.items():
            if e.zone is None or not self._zone_ok(e.zone) or not self._type_ok(e.emitter_type):
                continue
            # Only count time the emitter is actually being heard.
            dwell = min(now, e.last_heard) - e.zone_since
            stay = (emitter_id, e.zone_since)
            if dwell >= self.min_dwell_s and stay not in self._fired_stays:
                self._fired_stays.add(stay)
                if self._cooled(f"{emitter_id}:{e.zone}", now):
                    out.append(self._alert(now, emitter_id, e.emitter_type,
                                           {"dwell_s": round(dwell), "since": iso_utc(e.zone_since)},
                                           zone=e.zone, randomized=e.randomized))
        return out


class NewDeviceRule(Rule):
    type = "new_device"
    extra_keys = frozenset({"hours", "learn_s", "include_randomized", "min_duration_s"})

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        hours = spec.get("hours")
        self.hours = (time.fromisoformat(hours["start"]), time.fromisoformat(hours["end"])) if hours else None
        self.learn_s = float(spec.get("learn_s", 3600))
        self.include_randomized = bool(spec.get("include_randomized", False))
        self.min_duration_s = float(spec.get("min_duration_s", 0))
        self._seen: set[str] = set()
        self._candidates: dict[str, tuple[float, str, bool]] = {}  # id -> (first_seen, type, randomized)

    def _in_hours(self, t: float) -> bool:
        if self.hours is None:
            return True
        local = datetime.fromtimestamp(t, timezone.utc).astimezone(self.ctx.tz).time()
        start, end = self.hours
        return start <= local < end if start <= end else (local >= start or local < end)

    def on_event(self, ev):
        if ev.type != "visit_start":
            return []
        new = ev.emitter_id not in self._seen
        self._seen.add(ev.emitter_id)
        randomized = ev.data.get("randomized_address", False)
        if (not new or (randomized and not self.include_randomized) or not self._type_ok(ev.emitter_type)
                or ev.t - self.ctx.started_at < self.learn_s or not self._in_hours(ev.t)):
            return []
        self._candidates[ev.emitter_id] = (ev.t, ev.emitter_type, randomized)
        return self.on_tick(ev.t) if self.min_duration_s <= 0 else []

    def on_tick(self, now):
        out = []
        for emitter_id, (first, etype, randomized) in list(self._candidates.items()):
            tracked = self.ctx.emitters.get(emitter_id)
            if tracked is None:  # visit ended before it stayed long enough
                del self._candidates[emitter_id]
                continue
            stayed = tracked.last_heard - first
            if stayed >= self.min_duration_s:
                del self._candidates[emitter_id]
                out.append(self._alert(now, emitter_id, etype,
                                       {"first_seen": iso_utc(first), "present_s": round(stayed)},
                                       randomized=randomized))
        return out


class RepeatVisitorRule(Rule):
    type = "repeat_visitor"
    extra_keys = frozenset({"min_visits", "window_s"})

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self.min_visits = int(spec.get("min_visits", 3))
        self.window_s = float(spec.get("window_s", 86400))
        if "cooldown_s" not in spec:
            self.cooldown_s = self.window_s
        self._visits: dict[str, list[float]] = {}

    def on_event(self, ev):
        if ev.type != "visit_start" or ev.data.get("randomized_address") or not self._type_ok(ev.emitter_type):
            return []
        times = [t for t in self._visits.get(ev.emitter_id, []) if ev.t - t <= self.window_s] + [ev.t]
        self._visits[ev.emitter_id] = times
        if len(times) >= self.min_visits and self._cooled(ev.emitter_id, ev.t):
            return [self._alert(ev.t, ev.emitter_id, ev.emitter_type,
                                {"visits": len(times), "window_s": self.window_s,
                                 "visit_starts": [iso_utc(t) for t in times]}, randomized=False)]
        return []


class WatchlistRule(Rule):
    type = "watchlist"
    extra_keys = frozenset({"emitter_ids", "ssids"})

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self.ids = {i.lower() for i in spec.get("emitter_ids", [])}
        self.ssids = set(spec.get("ssids", []))
        if not self.ids and not self.ssids:
            raise ValueError(f"rule {self.id!r}: watchlist needs emitter_ids or ssids")

    def on_event(self, ev):
        if ev.type != "visit_start":
            return []
        e = self.ctx.emitters.get(ev.emitter_id)
        ssid = e.attributes.get("ssid") if e else None
        if ev.emitter_id in self.ids or (ssid is not None and ssid in self.ssids):
            if self._cooled(ev.emitter_id, ev.t):
                return [self._alert(ev.t, ev.emitter_id, ev.emitter_type, {"matched": ssid or ev.emitter_id},
                                    randomized=ev.data.get("randomized_address"))]
        return []


class NodeOfflineRule(Rule):
    type = "node_offline"
    extra_keys = frozenset({"silence_s"})

    def __init__(self, spec, ctx):
        super().__init__(spec, ctx)
        self.silence_s = float(spec.get("silence_s", 300))
        self._last: dict[str, float] = {}
        self._offline: set[str] = set()

    def on_observation(self, obs):
        self._last[obs.node_id] = obs.t
        if obs.node_id in self._offline:
            self._offline.discard(obs.node_id)
            return [Message("node_status", obs.t, {"node_id": obs.node_id, "state": "online"})]
        return []

    def on_tick(self, now):
        out = []
        for node_id in self.ctx.node_ids:
            last = self._last.get(node_id, self.ctx.started_at)
            if node_id not in self._offline and now - last > self.silence_s:
                self._offline.add(node_id)
                ctx = {"node_id": node_id, "last_heard": iso_utc(last) if node_id in self._last else None}
                out.append(Message("node_status", now, {"node_id": node_id, "state": "offline",
                                                        "last_heard": ctx["last_heard"]}))
                out.append(self._alert(now, None, None, ctx))
        return out


class CameraCueRule(Rule):
    type = "camera_cue"

    def __init__(self, spec, ctx):
        spec = {"cooldown_s": 60, **spec}
        super().__init__(spec, ctx)

    def on_event(self, ev):
        if ev.type != "first_heard" or not self._type_ok(ev.emitter_type) or not self._zone_ok(ev.data.get("zone")):
            return []
        if not self._cooled("site", ev.t):
            return []
        return [Message("camera_cue", ev.t, {
            "rule_id": self.id,
            "reason": f"{ev.emitter_type}_detected",
            "emitter": {"id": ev.emitter_id, "type": ev.emitter_type},
            "zone": ev.data.get("zone"),
            "node_id": ev.data.get("node_id"),
            "rssi_dbm": ev.data.get("rssi_dbm"),
        })]


RULE_TYPES: dict[str, type[Rule]] = {
    r.type: r for r in (LoiterRule, NewDeviceRule, RepeatVisitorRule, WatchlistRule, NodeOfflineRule, CameraCueRule)
}


# -- engine -------------------------------------------------------------------


@dataclass
class _Tracked:
    emitter_type: str
    randomized: bool = False
    zone: str | None = None
    zone_since: float = 0.0
    confidence: float | None = None
    last_heard: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineContext:
    tz: ZoneInfo
    node_ids: tuple[str, ...]
    started_at: float = float("nan")
    emitters: dict[str, _Tracked] = field(default_factory=dict)


class AlertEngine:
    def __init__(self, rules: list[dict[str, Any]], *, node_ids: list[str] | tuple[str, ...] = (),
                 tz: str = "UTC") -> None:
        self.ctx = EngineContext(ZoneInfo(tz), tuple(node_ids))
        self.rules: list[Rule] = []
        seen = set()
        for spec in rules:
            kind = spec.get("type")
            if kind not in RULE_TYPES:
                raise ValueError(f"rule {spec.get('id')!r}: unknown type {kind!r}; known: {sorted(RULE_TYPES)}")
            if spec["id"] in seen:
                raise ValueError(f"duplicate rule id {spec['id']!r}")
            seen.add(spec["id"])
            rule = RULE_TYPES[kind](spec, self.ctx)
            if rule.enabled:
                self.rules.append(rule)

    def _start(self, t: float) -> None:
        if self.ctx.started_at != self.ctx.started_at:  # NaN: first input
            self.ctx.started_at = t

    def observe(self, obs: Observation) -> list[Message]:
        """Call for every observation, friendly or not (node liveness)."""
        self._start(obs.t)
        e = self.ctx.emitters.get(obs.emitter_id)
        if e is not None:
            e.last_heard = max(e.last_heard, obs.t)
        return [m for r in self.rules for m in r.on_observation(obs)]

    def handle(self, ev: Event) -> list[Message]:
        self._start(ev.t)
        self._track(ev)
        return [m for r in self.rules for m in r.on_event(ev)]

    def tick(self, now: float) -> list[Message]:
        self._start(now)
        return [m for r in self.rules for m in r.on_tick(now)]

    def _track(self, ev: Event) -> None:
        if ev.type == "visit_start":
            self.ctx.emitters[ev.emitter_id] = _Tracked(
                ev.emitter_type, ev.data.get("randomized_address", False), last_heard=ev.t,
                attributes=ev.data.get("attributes", {}))
        e = self.ctx.emitters.get(ev.emitter_id)
        if e is None:
            if ev.type == "first_heard":
                self.ctx.emitters[ev.emitter_id] = _Tracked(ev.emitter_type, last_heard=ev.t,
                                                            attributes=ev.data.get("attributes", {}))
            return
        if ev.type == "first_heard":
            e.attributes = ev.data.get("attributes", {})
        elif ev.type == "zone_estimate":
            e.zone, e.zone_since, e.confidence = ev.data.get("zone"), ev.t, ev.data.get("confidence")
        elif ev.type == "visit_end":
            del self.ctx.emitters[ev.emitter_id]


def load_rules(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Returns (rule specs, timezone name)."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    return data.get("rules", []), data.get("timezone", "UTC")
