"""Command-line entry point.

    rfanalytics run      process observations from a file or MQTT, write events
    rfanalytics kismet   poll Kismet and emit rf.observation.v0 JSONL
    rfanalytics live     poll Kismet, run analytics and alerting, publish to MQTT
    rfanalytics score    compare events with rfsim ground truth
    rfanalytics friendly-from-truth   build a friendly list from rfsim ground truth
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from rfanalytics.alerts import Message
    from rfanalytics.events import Event

METHODS = ("strongest", "centroid")
TOPIC_PREFIX = "detection/rf"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rfanalytics",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    run.add_argument(
        "--outbox",
        type=Path,
        default=Path("rf-outbox.jsonl"),
        help="durable outbox for unacknowledged messages (default: ./rf-outbox.jsonl)",
    )

    ki = sub.add_parser("kismet", help="poll Kismet REST API and emit rf.observation.v0 JSONL")
    _add_kismet_args(ki)
    ki.add_argument("--once", action="store_true", help="fetch once and exit")
    ki.add_argument("-o", "--out", default="-", help="observation JSONL output (default: stdout)")
    ki.add_argument("--publish-host", help="also publish observations to this MQTT broker")
    ki.add_argument("--publish-port", type=int, default=1883)
    ki.add_argument("--publish-topic", default="detection/rf/observations")

    lv = sub.add_parser(
        "live",
        help="poll Kismet, run analytics and alert rules, publish alerts/cues/status to MQTT",
    )
    _add_kismet_args(lv)
    lv.add_argument("--site", type=Path, required=True, help="site YAML (zones and nodes)")
    lv.add_argument("--friendly", type=Path, help="friendly list YAML")
    lv.add_argument("--rules", type=Path, required=True, help="alert rules YAML")
    lv.add_argument("--method", choices=METHODS, default="strongest")
    lv.add_argument(
        "--backfill-s",
        type=float,
        default=120.0,
        help="on start, only process devices Kismet heard in the last N seconds (default: 120)",
    )
    lv.add_argument(
        "--max-age-s",
        type=float,
        default=30.0,
        help="drop observations older than this when polled (default: 30). Kismet keeps "
        "each node's last-heard time per device, which can be far older than the device's",
    )
    lv.add_argument("--events-out", default="none", help="analytics events JSONL ('-' for stdout; default: none)")
    lv.add_argument("--observations-out", default="none", help="observation JSONL archive (default: none)")
    lv.add_argument("--alerts-out", help="write outbound messages as JSONL {topic, payload} ('-' for stdout)")
    lv.add_argument("--publish-host", help="publish outbound messages to this MQTT broker")
    lv.add_argument("--publish-port", type=int, default=1883)
    lv.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    lv.add_argument("--outbox", type=Path, default=Path("rf-outbox.jsonl"), help="durable outbox path")
    lv.add_argument("--stats-every", type=int, default=12, help="print stats every N polls (default: 12)")
    lv.add_argument("--duration", type=float, help="stop after N seconds (default: run until Ctrl+C)")

    sc = sub.add_parser("score", help="score events against rfsim ground truth")
    sc.add_argument("--site", type=Path, required=True)
    sc.add_argument("--truth", type=Path, required=True)
    sc.add_argument("--events", type=Path, required=True)
    sc.add_argument("--json", action="store_true", help="print the full report as JSON")

    fr = sub.add_parser("friendly-from-truth", help="friendly list YAML from rfsim ground truth (testing aid)")
    fr.add_argument("truth", type=Path)
    fr.add_argument(
        "--include-randomized",
        action="store_true",
        help="also include rotating addresses (unrealistic: operators cannot know these)",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "kismet":
        return _kismet(args)
    if args.command == "live":
        return _live(args)
    if args.command == "score":
        return _score(args)
    return _friendly_from_truth(args)


def _run(args: argparse.Namespace) -> int:
    from rfanalytics.observation import ObservationError, parse_observation

    pipeline = _build_pipeline(args)
    sink = _MessageSink(args) if pipeline.engine else None
    out = _open_out(args.out)
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
            events, messages = pipeline.process(obs)
            _write(out, events)
            if sink:
                sink.send(messages)
    except KeyboardInterrupt:
        pass
    finally:
        events, messages = pipeline.flush()
        _write(out, events)
        if sink:
            sink.send(messages)
            sink.close()
        if out not in (None, sys.stdout):
            out.close()

    stats = {**pipeline.analyzer.stats.to_dict(), "invalid_records": invalid}
    if sink:
        stats.update(sink.counts)
    if args.stats:
        args.stats.write_text(json.dumps(stats, indent=2) + "\n")
    print("rfanalytics: " + ", ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)
    return 0


def _build_pipeline(args: argparse.Namespace):
    from rfanalytics.alerts import AlertEngine, load_rules
    from rfanalytics.analyzer import Analyzer, AnalyzerConfig
    from rfanalytics.friendly import FriendlyList, load_friendly
    from rfanalytics.pipeline import Pipeline
    from rfanalytics.site import load_site

    site = load_site(args.site)
    friendly = load_friendly(args.friendly) if args.friendly else FriendlyList()
    analyzer = Analyzer(site, friendly, AnalyzerConfig(method=args.method))
    engine = None
    if args.rules:
        rules, tz = load_rules(args.rules)
        engine = AlertEngine(rules, node_ids=sorted(site.nodes), tz=tz)
    return Pipeline(analyzer, engine)


def _open_out(path: str | None, mode: str = "w") -> IO[str] | None:
    if path in (None, "", "none"):
        return None
    return sys.stdout if path == "-" else open(path, mode)


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
        from rfanalytics.formats import to_wire

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
    print(
        f"rfanalytics: listening on {args.mqtt_host}:{args.mqtt_port} {args.mqtt_topic} (Ctrl+C to stop)",
        file=sys.stderr,
    )
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
    from rfanalytics.score import score
    from rfanalytics.site import load_site

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


def _add_kismet_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kismet-url", required=True, help="Kismet base URL, e.g. http://127.0.0.1:2501")
    p.add_argument("--user", default=None, help="Kismet username (or set KISMET_USER)")
    p.add_argument("--password", default=None, help="Kismet password (prefer --password-env)")
    p.add_argument("--password-env", default=None, help="read Kismet password from this environment variable")
    p.add_argument("--poll", type=float, default=5.0, help="poll interval seconds (default: 5)")


def _kismet_poller(args: argparse.Namespace):
    import os

    from rfanalytics.kismet import KismetPoller

    password = args.password
    if args.password_env:
        password = os.environ.get(args.password_env)
        if password is None:
            raise SystemExit(f"rfanalytics: environment variable {args.password_env} is not set")
    user = args.user or os.environ.get("KISMET_USER")
    if password is None and os.environ.get("KISMET_PASSWORD"):
        password = os.environ.get("KISMET_PASSWORD")
    return KismetPoller(args.kismet_url, username=user, password=password)


def _live(args: argparse.Namespace) -> int:
    """Kismet -> analytics -> alert engine -> MQTT, in one process.

    Time-based rules are ticked on the wall clock after every poll, so node
    offline and loiter alerts fire even when no observations arrive. Kismet
    timestamps are server time; run this close to the Kismet server (or keep
    clocks in sync with NTP).
    """
    import signal
    import time

    from rfanalytics.kismet import KismetIngestError
    from rfanalytics.observation import ObservationError, parse_observation

    poller = _kismet_poller(args)
    pipeline = _build_pipeline(args)
    sink = _MessageSink(args)
    # Append: a restarted live service keeps its earlier archive.
    events_out = _open_out(args.events_out, "a")
    obs_out = _open_out(args.observations_out, "a")

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)  # docker stop -> clean flush and outbox drain

    # Negative "last-time" values are relative to now on the Kismet server.
    since: float = -abs(args.backfill_s) if args.backfill_s else 0.0
    deadline = time.time() + args.duration if args.duration else None
    counts = {"polls": 0, "poll_errors": 0, "observations": 0, "stale": 0, "invalid": 0, "events": 0}
    print(
        f"rfanalytics live: polling {args.kismet_url} every {args.poll:g}s; "
        f"publishing to {args.publish_host or '(no broker)'} under {args.topic_prefix}/ (Ctrl+C to stop)",
        file=sys.stderr,
    )
    try:
        while deadline is None or time.time() < deadline:
            started = time.time()
            try:
                records, max_t = poller.poll(since=since or None)
            except KismetIngestError as exc:
                counts["poll_errors"] += 1
                print(f"rfanalytics live: {exc}", file=sys.stderr)
                records, max_t = [], 0.0
            else:
                counts["polls"] += 1
                if max_t > 0:
                    since = max_t - 1  # overlap so boundary updates are not missed
            # A device heard now can carry a node's seenby entry from long ago.
            # Processing those would replay history (e.g. "node offline" in the
            # past) and feed the analyzer out of order, so only recent ones pass.
            max_age = args.backfill_s if counts["polls"] <= 1 else args.max_age_s
            cutoff = started - max(max_age, args.poll)
            for rec in records:
                try:
                    obs = parse_observation(rec)
                except ObservationError as exc:
                    counts["invalid"] += 1
                    print(f"rfanalytics live: dropping invalid observation: {exc}", file=sys.stderr)
                    continue
                if obs.t < cutoff:
                    counts["stale"] += 1
                    continue
                counts["observations"] += 1
                if obs_out:
                    obs_out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                events, messages = pipeline.process(obs)
                counts["events"] += len(events)
                _write(events_out, events)
                sink.send(messages)
            if obs_out and records:
                obs_out.flush()
            sink.send(pipeline.tick(time.time()))
            if args.stats_every and (counts["polls"] == 1 or counts["polls"] % args.stats_every == 0):
                _live_stats(counts, pipeline, sink)
            time.sleep(max(0.2, args.poll - (time.time() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        events, messages = pipeline.flush()
        _write(events_out, events)
        sink.send(messages)
        _live_stats(counts, pipeline, sink)
        sink.close()
        for f in (events_out, obs_out):
            if f not in (None, sys.stdout):
                f.close()
    return 0


def _live_stats(counts: dict[str, int], pipeline, sink: "_MessageSink") -> None:
    stats = {**counts, "open_visits": len(pipeline.analyzer.open_visits()), **sink.counts}
    print("rfanalytics live: " + " ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)


def _kismet(args: argparse.Namespace) -> int:
    import time

    from rfanalytics.kismet import KismetIngestError
    from rfanalytics.observation import ObservationError, parse_observation

    poller = _kismet_poller(args)
    out = sys.stdout if args.out == "-" else open(args.out, "w")
    publisher = None
    if args.publish_host:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise SystemExit("MQTT publish needs paho-mqtt: uv sync --extra mqtt") from None
        publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rfanalytics-kismet")
        publisher.connect(args.publish_host, args.publish_port)
        publisher.loop_start()

    since: float | None = None
    emitted = 0
    polls = 0
    try:
        while True:
            try:
                records, max_t = poller.poll(since=since)
            except KismetIngestError as exc:
                print(f"rfanalytics: {exc}", file=sys.stderr)
                if args.once:
                    return 2
                time.sleep(args.poll)
                continue
            polls += 1
            if max_t > 0:
                # Overlap by 1s so boundary updates are not missed.
                since = max_t - 1
            for rec in records:
                try:
                    parse_observation(rec)  # enforce contract before emitting
                except ObservationError as exc:
                    print(f"rfanalytics: dropping invalid observation: {exc}", file=sys.stderr)
                    continue
                line = json.dumps(rec, separators=(",", ":"))
                out.write(line + "\n")
                if publisher:
                    publisher.publish(args.publish_topic, line, qos=1)
                emitted += 1
            if records or polls == 1:
                out.flush()
                print(
                    f"rfanalytics kismet: poll={polls} emitted={emitted} tracked={len(poller.state)}",
                    file=sys.stderr,
                )
            if args.once:
                break
            time.sleep(max(0.2, args.poll))
    except KeyboardInterrupt:
        pass
    finally:
        if publisher:
            publisher.loop_stop()
            publisher.disconnect()
        if out is not sys.stdout:
            out.close()
    return 0


def _friendly_from_truth(args: argparse.Namespace) -> int:
    from rfanalytics.friendly import FriendlyEntry, FriendlyList

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
                friendly.add(
                    FriendlyEntry(
                        label=f"{actor['actor_id']} {d['profile']}",
                        source="test",
                        emitter_id=emitter_id,
                    )
                )
    sys.stdout.write(friendly.to_yaml())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
