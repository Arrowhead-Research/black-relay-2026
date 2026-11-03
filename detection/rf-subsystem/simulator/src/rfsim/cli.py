"""Command-line entry point: ``rfsim run <scenario.yaml>``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rfsim.engine import simulate
from rfsim.model import Observation
from rfsim.scenario import load_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfsim", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="simulate a scenario")
    run.add_argument("scenario", type=Path, help="scenario YAML file")
    run.add_argument("-o", "--out", default="-", help="observations JSONL output path (default: stdout)")
    run.add_argument("-t", "--truth", type=Path, help="write ground truth JSON to this path")
    run.add_argument("--seed", type=int, help="override the scenario seed")
    run.add_argument("--start-now", action="store_true", help="shift timestamps so the scenario starts now")
    run.add_argument("--realtime", action="store_true", help="emit observations paced to their timestamps")
    run.add_argument("--speed", type=float, default=1.0, help="time multiplier for --realtime (default 1.0)")
    run.add_argument("--mqtt-host", help="also publish each observation to this MQTT broker")
    run.add_argument("--mqtt-port", type=int, default=1883)
    run.add_argument("--mqtt-topic", default="detection/rf/sim/observations")

    summary = sub.add_parser("summary", help="print scenario statistics without writing observations")
    summary.add_argument("scenario", type=Path)
    summary.add_argument("--seed", type=int)

    args = parser.parse_args(argv)
    scenario = load_scenario(args.scenario, seed=args.seed)

    if args.command == "summary":
        result = simulate(scenario)
        _print_summary(scenario.name, result.observations, result.truth)
        return 0

    if args.start_now:
        scenario.start_time = datetime.now(timezone.utc)
    result = simulate(scenario)

    if args.truth:
        args.truth.parent.mkdir(parents=True, exist_ok=True)
        args.truth.write_text(json.dumps(result.truth, indent=2) + "\n")

    publisher = _mqtt_publisher(args.mqtt_host, args.mqtt_port) if args.mqtt_host else None
    out = sys.stdout if args.out == "-" else open(args.out, "w")
    try:
        _emit(result.observations, out, publisher, args.mqtt_topic, args.realtime, args.speed)
    finally:
        if out is not sys.stdout:
            out.close()
        if publisher:
            publisher.loop_stop()
            publisher.disconnect()
    print(f"rfsim: {scenario.name}: {len(result.observations)} observations", file=sys.stderr)
    return 0


def _emit(observations: list[Observation], out, publisher, topic: str, realtime: bool, speed: float) -> None:
    if speed <= 0:
        raise SystemExit("--speed must be positive")
    wall_start = time.monotonic()
    sim_start = observations[0].timestamp if observations else None
    for obs in observations:
        if realtime and sim_start is not None:
            due = (obs.timestamp - sim_start) / timedelta(seconds=1) / speed
            delay = due - (time.monotonic() - wall_start)
            if delay > 0:
                out.flush()
                time.sleep(delay)
        line = json.dumps(obs.to_dict(), separators=(",", ":"))
        out.write(line + "\n")
        if publisher:
            publisher.publish(topic, line, qos=1)


def _mqtt_publisher(host: str, port: int):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit("MQTT output needs paho-mqtt: uv sync --extra mqtt") from None
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rfsim")
    client.connect(host, port)
    client.loop_start()
    return client


def _print_summary(name: str, observations: list[Observation], truth: dict) -> None:
    by_type = Counter(o.emitter_type for o in observations)
    by_node = Counter(o.node_id for o in observations)
    roles = Counter(a["role"] for a in truth["actors"])
    print(f"scenario: {name}  ({truth['start_time']} -> {truth['end_time']})")
    print(f"observations: {len(observations)}")
    print("  by type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print("  by node: " + ", ".join(f"{k}={v}" for k, v in sorted(by_node.items())))
    print(f"distinct emitter ids: {len({o.emitter_id for o in observations})}")
    print("actors: " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    for actor in truth["actors"]:
        if actor["role"] == "background":
            continue
        flag = ", friendly" if actor["friendly"] and actor["role"] != "friendly" else ""
        print(f"  {actor['actor_id']} ({actor['role']}{flag})")
        for visit in actor["visits"]:
            zones = " > ".join(f"{z['zone']}[{z['dwell_s']:.0f}s]" for z in visit["zone_track"])
            print(f"    {visit['start']} .. {visit['end']}: {zones}")


if __name__ == "__main__":
    raise SystemExit(main())
