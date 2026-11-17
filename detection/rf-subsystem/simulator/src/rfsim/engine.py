"""Simulation engine: turns a scenario into observations plus ground truth."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from rfsim.model import Observation, iso_utc
from rfsim.propagation import detection_probability, received_power_dbm
from rfsim.scenario import Actor, Scenario

ZONE_TRACK_STEP_S = 5.0


@dataclass
class SimulationResult:
    observations: list[Observation]
    truth: dict[str, Any]


def simulate(scenario: Scenario) -> SimulationResult:
    observations: list[Observation] = []
    for actor in scenario.actors:
        for device in actor.devices:
            observations.extend(_simulate_device(scenario, actor, device))
    observations.sort(key=lambda o: (o.timestamp, o.node_id, o.emitter_id))
    return SimulationResult(observations, _ground_truth(scenario))


def _simulate_device(scenario: Scenario, actor: Actor, device) -> list[Observation]:
    profile = device.profile
    rng = random.Random(f"{scenario.seed}:tx:{device.device_id}")
    out: list[Observation] = []
    for t in _transmit_times(rng, actor, profile):
        pos = actor.position(t)
        if pos is None or (profile.requires_motion and not actor.is_moving(t)):
            continue
        emitter_id = device.emitter_id_at(t)
        freq = rng.choice(profile.frequencies_mhz)
        ts = scenario.start_time + timedelta(seconds=t)
        attrs = _per_packet_attributes(rng, device)
        heard = False
        for node in scenario.site.nodes:
            rssi = received_power_dbm(
                rng, profile.tx_power_dbm, math.dist(pos, node.position), freq,
                profile.path_loss_exponent, profile.shadow_sigma_db,
            ) + node.rssi_offset_db
            if rng.random() < detection_probability(rssi, profile.sensitivity_dbm, profile.base_loss):
                heard = True
                out.append(Observation(
                    timestamp=ts,
                    node_id=node.node_id,
                    emitter_type=profile.emitter_type,
                    emitter_id=emitter_id,
                    rssi_dbm=max(-130, min(0, round(rssi))),
                    frequency_mhz=freq,
                    attributes=attrs,
                ))
        if heard:
            span = device.emitted_ids.setdefault(emitter_id, [t, t])
            span[1] = t
    return out


def _transmit_times(rng: random.Random, actor: Actor, profile) -> list[float]:
    """Periodic transmissions while present, plus a wake burst whenever a
    motion-triggered emitter (TPMS) starts moving."""
    times: list[float] = []
    for start, end in actor.windows:
        t = start + rng.uniform(0, profile.period_s[1])
        while t <= end:
            times.append(t)
            t += rng.uniform(*profile.period_s)
    if profile.requires_motion:
        times.extend(actor.motion_starts())
    return sorted(times)


def _per_packet_attributes(rng: random.Random, device) -> dict[str, Any]:
    attrs = dict(device.attributes)
    if device.profile.emitter_type == "tpms":
        attrs["pressure_kpa"] = round(rng.gauss(235.0, 3.0), 1)
        attrs["temperature_c"] = round(rng.gauss(22.0, 2.0), 1)
    return attrs


def _ground_truth(scenario: Scenario) -> dict[str, Any]:
    def ts(t: float) -> str:
        return iso_utc(scenario.start_time + timedelta(seconds=t))

    actors = []
    friendly_ids: set[str] = set()
    for actor in scenario.actors:
        devices = []
        for d in actor.devices:
            ids = [
                {"emitter_id": eid, "first_heard": ts(span[0]), "last_heard": ts(span[1])}
                for eid, span in sorted(d.emitted_ids.items(), key=lambda kv: kv[1][0])
            ]
            if actor.friendly:
                friendly_ids.update(d.emitted_ids)
                if d.static_id:
                    friendly_ids.add(d.static_id)
            devices.append({
                "device_id": d.device_id,
                "kit": d.kit,
                "profile": d.profile.name,
                "emitter_type": d.profile.emitter_type,
                "randomized": d.randomized,
                "static_id": d.static_id,
                "emitter_ids": ids,
            })
        actors.append({
            "actor_id": actor.actor_id,
            "role": actor.role,
            "friendly": actor.friendly,
            "visits": [
                {"start": ts(s), "end": ts(e), "zone_track": _zone_track(scenario, actor, s, e)}
                for s, e in actor.windows
            ],
            "devices": devices,
        })

    return {
        "schema": "rf.sim-truth.v0",
        "scenario": scenario.name,
        "site": scenario.site.name,
        "seed": scenario.seed,
        "start_time": ts(0),
        "end_time": ts(scenario.duration_s),
        "friendly_emitter_ids": sorted(friendly_ids),
        "actors": actors,
    }


def _zone_track(scenario: Scenario, actor: Actor, start: float, end: float) -> list[dict[str, Any]]:
    """Compress the actor's true zone over time into contiguous segments."""
    track: list[dict[str, Any]] = []
    t = start
    while True:
        pos = actor.position(t)
        zone = scenario.site.zone_at(pos) if pos is not None else None
        if zone is not None:
            if track and track[-1]["zone"] == zone:
                track[-1]["end_s"] = t
            else:
                track.append({"zone": zone, "start_s": t, "end_s": t})
        if t >= end:
            break
        t = min(t + ZONE_TRACK_STEP_S, end)
    return [
        {
            "zone": seg["zone"],
            "start": iso_utc(scenario.start_time + timedelta(seconds=seg["start_s"])),
            "end": iso_utc(scenario.start_time + timedelta(seconds=seg["end_s"])),
            "dwell_s": seg["end_s"] - seg["start_s"],
        }
        for seg in track
    ]
