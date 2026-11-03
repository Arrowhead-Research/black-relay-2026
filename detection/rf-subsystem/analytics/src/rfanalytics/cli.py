"""Command-line entry point.

    rfanalytics run      process observations from a file or MQTT, write events
    rfanalytics score    compare events with rfsim ground truth
    rfanalytics friendly-from-truth   build a friendly list from rfsim ground truth
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
from pathlib import Path
from typing import IO, Iterator

from rfanalytics.alerts import AlertEngine, Message, load_rules
from rfanalytics.analyzer import Analyzer, AnalyzerConfig
from rfanalytics.events import Event
from rfanalytics.formats import TOPIC_PREFIX, to_wire
from rfanalytics.friendly import FriendlyEntry, FriendlyList, load_friendly
from rfanalytics.localization import METHODS
from rfanalytics.observation import ObservationError, parse_observation
from rfanalytics.score import score
from rfanalytics.site import load_site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfanalytics", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process observations into analytics events")
    run.add_argument("--site", type=Path, required=True, help="site YAML (zones and nodes)")
    run.add_argument("--friendly", type=Path, help="friendly list YAML")
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="observation JSONL file, or '-' for stdin")
    src.add_argument("--mqtt-host", help="subscribe to observations on this MQTT broker")
    run.add_argument("--mqtt-port", type=int, default=1883)
    run.add_argument("--mqtt-topic", default="detection/rf/sim/observations")
    run.add_argument("-o", "--out", default="-", help="events JSONL output (default: stdout; 'none' to skip)")
    run.add_argument("--method", choices=METHODS, default="strongest")
    run.add_argument("--stats", type=Path, help="write run statistics JSON here")
    run.add_argument("--rules", type=Path, help="alert rules YAML; enables alerting")
    run.add_argument("--alerts-out", help="write outbound messages as JSONL {topic, payload} ('-' for stdout)")
    run.add_argument("--publish-host", help="publish outbound messages to this MQTT broker")
    run.add_argument("--publish-port", type=int, default=1883)
    run.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    run.add_argument("--outbox", type=Path, default=Path("rf-outbox.jsonl"),
                     help="durable outbox for unacknowledged messages (default: ./rf-outbox.jsonl)")

    sc = sub.add_parser("score", help="score events against rfsim ground truth")
    sc.add_argument("--site", type=Path, required=True)
    sc.add_argument("--truth", type=Path, required=True)
    sc.add_argument("--events", type=Path, required=True)
    sc.add_argument("--json", action="store_true", help="print the full report as JSON")

    fr = sub.add_parser("friendly-from-truth", help="friendly list YAML from rfsim ground truth (testing aid)")
    fr.add_argument("truth", type=Path)
    fr.add_argument("--include-randomized", action="store_true",
                    help="also include rotating addresses (unrealistic: operators cannot know these)")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "score":
        return _score(args)
    return _friendly_from_truth(args)


def _run(args: argparse.Namespace) -> int:
    site = load_site(args.site)
    friendly = load_friendly(args.friendly) if args.friendly else FriendlyList()
    analyzer = Analyzer(site, friendly, AnalyzerConfig(method=args.method))
    engine = None
    if args.rules:
        rules, tz = load_rules(args.rules)
        engine = AlertEngine(rules, node_ids=sorted(site.nodes), tz=tz)
    sink = _MessageSink(args) if engine else None
    out = None if args.out in ("none", "") else (sys.stdout if args.out == "-" else open(args.out, "w"))
    invalid = 0
    next_tick = float("-inf")
    try:
        records = _mqtt_records(args) if args.mqtt_host else _file_records(args.input)
        for line_no, record in records:
            try:
                obs = parse_observation(record)
            except ObservationError as exc:
                invalid += 1
                print(f"rfanalytics: skipping record {line_no}: {exc}", file=sys.stderr)
                continue
            messages = engine.observe(obs) if engine else []
            events = analyzer.ingest(obs)
            _write(out, events)
            if engine:
                for ev in events:
                    messages += engine.handle(ev)
                if obs.t >= next_tick:
                    messages += engine.tick(obs.t)
                    next_tick = obs.t + 1.0
                sink.send(messages)
    except KeyboardInterrupt:
        pass
    finally:
        events = analyzer.flush()
        _write(out, events)
        if engine:
            sink.send([m for ev in events for m in engine.handle(ev)])
            sink.close()
        if out not in (None, sys.stdout):
            out.close()

    stats = {**analyzer.stats.to_dict(), "invalid_records": invalid}
    if sink:
        stats.update(sink.counts)
    if args.stats:
        args.stats.write_text(json.dumps(stats, indent=2) + "\n")
    print("rfanalytics: " + ", ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


class _MessageSink:
    """Routes outbound messages to a JSONL file and/or MQTT (via the durable outbox)."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.prefix = args.topic_prefix
        self.file = None
        if args.alerts_out:
            self.file = sys.stdout if args.alerts_out == "-" else open(args.alerts_out, "w")
        self.publisher = None
        if args.publish_host:
            from rfanalytics.outbox import MqttPublisher, Outbox

            outbox = Outbox(args.outbox)
            if len(outbox):
                print(f"rfanalytics: replaying {len(outbox)} message(s) from {args.outbox}", file=sys.stderr)
            self.publisher = MqttPublisher(outbox, args.publish_host, args.publish_port)
        self.counts: dict[str, int] = {"alerts": 0, "camera_cues": 0, "node_status": 0}

    def send(self, messages: list[Message]) -> None:
        for msg in messages:
            topic, payload = to_wire(msg, self.prefix)
            self.counts[{"alert": "alerts", "camera_cue": "camera_cues"}.get(msg.kind, msg.kind)] += 1
            if self.file:
                self.file.write(json.dumps({"topic": topic, "payload": payload}, separators=(",", ":")) + "\n")
                self.file.flush()
            if self.publisher:
                self.publisher.send(msg.id, topic, payload)

    def close(self) -> None:
        if self.publisher:
            self.publisher.close()
        if self.file not in (None, sys.stdout):
            self.file.close()


def _write(out: IO[str] | None, events: list[Event]) -> None:
    if out is None:
        return
    for ev in events:
        out.write(json.dumps(ev.to_dict(), separators=(",", ":")) + "\n")
    if events:
        out.flush()


def _file_records(path: str) -> Iterator[tuple[int, dict]]:
    f = sys.stdin if path == "-" else open(path)
    try:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield n, json.loads(line)
            except json.JSONDecodeError as exc:
                yield n, {"_invalid": str(exc)}
    finally:
        if f is not sys.stdin:
            f.close()


def _mqtt_records(args: argparse.Namespace) -> Iterator[tuple[int, dict]]:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit("MQTT input needs paho-mqtt: uv sync --extra mqtt") from None

    inbox: queue.Queue = queue.Queue()

    def on_message(_client, _userdata, msg):
        inbox.put(msg.payload)

    def on_connect(client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            print(f"rfanalytics: MQTT connect failed: {reason_code}", file=sys.stderr)
        else:
            client.subscribe(args.mqtt_topic, qos=1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rfanalytics")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port)
    client.loop_start()
    print(f"rfanalytics: listening on {args.mqtt_host}:{args.mqtt_port} {args.mqtt_topic} (Ctrl+C to stop)",
          file=sys.stderr)
    n = 0
    try:
        while True:
            payload = inbox.get()
            n += 1
            try:
                yield n, json.loads(payload)
            except json.JSONDecodeError as exc:
                yield n, {"_invalid": str(exc)}
    finally:
        client.loop_stop()
        client.disconnect()


def _score(args: argparse.Namespace) -> int:
    site = load_site(args.site)
    truth = json.loads(args.truth.read_text())
    events = [json.loads(line) for line in args.events.read_text().splitlines() if line.strip()]
    report = score(events, truth, site.node_zones)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"scenario: {report['scenario']}")
    print("localization accuracy (zones with a node):")
    for role, r in report["localization"].items():
        acc = "n/a" if r["accuracy"] is None else f"{r['accuracy']:.1%}"
        print(f"  {role:<15} {acc:>7}  (correct={r['correct']} wrong={r['wrong']} unreachable={r['unreachable']})")
    leak = report["friendly_leakage"]
    print(f"friendly leakage: stable={leak['stable']} randomized={leak['randomized']}")
    for v in report["subject_visits"]:
        print(f"subject visit {v['actor_id']} @ {v['start']}: {'detected' if v['detected'] else 'MISSED'}")
    return 0


def _friendly_from_truth(args: argparse.Namespace) -> int:
    truth = json.loads(args.truth.read_text())
    friendly = FriendlyList()
    for actor in truth["actors"]:
        if not actor["friendly"]:
            continue
        for d in actor["devices"]:
            if d["randomized"] and not args.include_randomized:
                continue
            ids = [d["static_id"]] if d["static_id"] else [e["emitter_id"] for e in d["emitter_ids"]]
            for emitter_id in ids:
                friendly.add(FriendlyEntry(label=f"{actor['actor_id']} {d['profile']}", source="test",
                                           emitter_id=emitter_id))
    sys.stdout.write(friendly.to_yaml())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
