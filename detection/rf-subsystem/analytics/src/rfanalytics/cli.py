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

from rfanalytics.analyzer import Analyzer, AnalyzerConfig
from rfanalytics.events import Event
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
    run.add_argument("-o", "--out", default="-", help="events JSONL output (default: stdout)")
    run.add_argument("--method", choices=METHODS, default="strongest")
    run.add_argument("--stats", type=Path, help="write run statistics JSON here")

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
    out = sys.stdout if args.out == "-" else open(args.out, "w")
    invalid = 0
    try:
        records = _mqtt_records(args) if args.mqtt_host else _file_records(args.input)
        for line_no, record in records:
            try:
                obs = parse_observation(record)
            except ObservationError as exc:
                invalid += 1
                print(f"rfanalytics: skipping record {line_no}: {exc}", file=sys.stderr)
                continue
            _write(out, analyzer.ingest(obs))
    except KeyboardInterrupt:
        pass
    finally:
        _write(out, analyzer.flush())
        if out is not sys.stdout:
            out.close()

    stats = {**analyzer.stats.to_dict(), "invalid_records": invalid}
    if args.stats:
        args.stats.write_text(json.dumps(stats, indent=2) + "\n")
    print("rfanalytics: " + ", ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


def _write(out: IO[str], events: list[Event]) -> None:
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
