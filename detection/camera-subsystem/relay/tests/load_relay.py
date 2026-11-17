#!/usr/bin/env python3
"""Measures what the relay API costs. Not part of the test suite, and it asserts nothing.

    cd relay/api
    uv run python ../tests/load_relay.py ingest /var/tmp/relay-load
    uv run python ../tests/load_relay.py reads /var/tmp/relay-load
    uv run python ../tests/load_relay.py pathsync
    uv run python ../tests/load_relay.py e2e /var/tmp/relay-load --rate 60 --seconds 60

Put the work directory on the disk under test, never a tmpfs, or fsync costs nothing. ``SRC``
in the environment measures another checkout's ``relay/api/src``. ``--fsync-ms`` adds a delay to
every fsync and counts them, which stands in for a slower disk. ``e2e`` and ``pathsync`` run the
real broker and MediaMTX under their own compose project on free loopback ports, and always tear
that down. Touches no other container, and not a broker on the host.
"""

import argparse
import asyncio
import http.client
import inspect
import ipaddress
import json
import multiprocessing
import os
import random
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAY = HERE.parent
if os.environ.get("SRC"):
    sys.path.insert(0, os.environ["SRC"])

from relayapi.mediamtx import PathSync  # noqa: E402
from relayapi.mqtt import TOPIC_DETECTIONS, TOPIC_STATUS, Ingest  # noqa: E402
from relayapi.nodes import AddressPolicy  # noqa: E402
from relayapi.settings import Settings  # noqa: E402
from relayapi.store import Store  # noqa: E402

SITE = "192.168.8.0/24"
TOKEN = "load-test-token"
NODE_LOGIN = ("relay-load", "node-pass")
API_LOGIN = ("relay-load-api", "api-pass")
CONTROL_PASSWORD = "control-pass"
READ_PASSWORD = "read-pass"
TICK_S = 0.02


def stamp(moment=None):
    moment = time.time() if moment is None else moment
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def detection(node_id, event="object_update", moment=None, padding=0):
    """The bridge's message, field for field (camerabridge/bridge.py _handle_event)."""
    object_id = f"{(moment or time.time()):.6f}-{uuid.uuid4().hex[:6]}"
    attributes = {
        "bbox": [118, 82, 178, 252],
        "area_px": 10200,
        "stationary": False,
        "snapshot_url": f"https://192.168.8.21:8971/api/events/{object_id}/snapshot.jpg",
    }
    if padding:
        attributes["padding"] = "x" * padding
    return {
        "schema": "camera.detection.v0",
        "event_id": str(uuid.uuid4()),
        "timestamp": stamp(moment),
        "node_id": node_id,
        "camera_id": node_id,
        "event": event,
        "object_id": object_id,
        "label": "person",
        "confidence": 0.87,
        "zones": ["perimeter-north"],
        "attributes": attributes,
    }


def heartbeat(node_id, address, moment=None):
    return {
        "schema": "camera.node-status.v0",
        "event_id": str(uuid.uuid4()),
        "timestamp": stamp(moment),
        "node_id": node_id,
        "camera_online": True,
        "link_online": True,
        "recording_ok": True,
        "detector_ok": True,
        "attributes": {
            "camera_link_state": "adopted",
            "retry_after": None,
            "reason": None,
            "node_address": address,
        },
    }


def settings_for(work: Path, **overrides) -> Settings:
    values = dict(
        api_token=TOKEN,
        broker_user=NODE_LOGIN[0],
        broker_password=NODE_LOGIN[1],
        api_broker_user=API_LOGIN[0],
        api_broker_password=API_LOGIN[1],
        mqtt_client_id="load-test-client",
        mediamtx_api_url="http://127.0.0.1:1",
        mediamtx_api_user="relay-api",
        mediamtx_api_password=CONTROL_PASSWORD,
        site_subnet=ipaddress.ip_network(SITE),
        db_path=work / "relay.sqlite3",
        schema_dir=RELAY.parent / "bridge" / "schemas",
    )
    values.update(overrides)
    return Settings(**values)


# --- A slower disk ---------------------------------------------------------------------------

_SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static atomic_long count;
static void wait_a_while(void) {
    const char *ms = getenv("SHIM_FSYNC_MS");
    long delay = ms ? atol(ms) : 0;
    atomic_fetch_add(&count, 1);
    if (delay > 0) {
        struct timespec t = {delay / 1000, (delay % 1000) * 1000000L};
        nanosleep(&t, NULL);
    }
}
int fsync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fsync");
    wait_a_while();
    return real(fd);
}
int fdatasync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fdatasync");
    wait_a_while();
    return real(fd);
}
__attribute__((destructor)) static void report(void) {
    const char *path = getenv("SHIM_LOG");
    if (!path) return;
    FILE *f = fopen(path, "w");
    if (f) { fprintf(f, "%ld\n", atomic_load(&count)); fclose(f); }
}
"""


def shim_env(work: Path, fsync_ms: int | None) -> dict:
    if fsync_ms is None:
        return {}
    work.mkdir(parents=True, exist_ok=True)
    source = work / "fsync_shim.c"
    library = work / "fsync_shim.so"
    source.write_text(_SHIM)
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-O1", "-o", str(library), str(source), "-ldl"], check=True
    )
    return {
        "LD_PRELOAD": str(library),
        "SHIM_FSYNC_MS": str(fsync_ms),
        "SHIM_LOG": str(work / "fsyncs.txt"),
    }


# --- Full retention --------------------------------------------------------------------------


def seed(work: Path, updates: int, durable: int, padding: int, nodes: int = 4) -> Path:
    """A database at its retention limits, written in one transaction. Rows look like the bridge's,
    spread over the last three days, oldest first, as they would have arrived."""
    work.mkdir(parents=True, exist_ok=True)
    db = work / "relay.sqlite3"
    for leftover in work.glob("relay.sqlite3*"):
        leftover.unlink()
    store = Store(db, updates, durable, 10**9, 2**62)
    store.close()
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("BEGIN")
    total = updates + durable
    start = time.time() - 3 * 86_400
    step = 3 * 86_400 / max(total, 1)
    rng = random.Random(1)
    for index in range(total):
        moment = start + index * step
        is_durable = rng.random() < durable / total
        node = f"cam-{index % nodes + 1:02d}"
        event = rng.choice(["object_start", "object_end"]) if is_durable else "object_update"
        message = detection(node, event, moment, padding)
        connection.execute(
            "INSERT INTO events (event_id, kind, durable, node_id, object_id, timestamp_us, "
            "received_at, message) VALUES (?, 'detection', ?, ?, ?, ?, ?, ?)",
            (
                message["event_id"],
                int(is_durable),
                node,
                message["object_id"],
                int(moment * 1_000_000),
                moment,
                json.dumps(message, separators=(",", ":")),
            ),
        )
    connection.execute("COMMIT")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    return db


# --- Ingest, with no broker --------------------------------------------------------------------


def cmd_ingest(args) -> None:
    work = Path(args.work)
    env = shim_env(work, args.fsync_ms) if args.fsync_ms is not None else {}
    if env and "LD_PRELOAD" not in os.environ:
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, **env})
    if args.full:
        db = seed(work, args.updates, args.durable, args.padding)
        print(f"seeded {args.updates}+{args.durable} rows: {db.stat().st_size / 2**20:.1f} MiB")
    else:
        work.mkdir(parents=True, exist_ok=True)
        for leftover in work.glob("relay.sqlite3*"):
            leftover.unlink()
    opened = time.perf_counter()
    settings = settings_for(work, retain_bytes=args.retain_bytes or Settings.retain_bytes)
    store = Store(
        settings.db_path,
        settings.retain_updates,
        settings.retain_durable,
        settings.retention_check_every,
        settings.retain_bytes,
    )
    print(f"opening the store: {(time.perf_counter() - opened) * 1000:.1f} ms")
    policy = AddressPolicy(settings.site_subnet)
    ingest = Ingest(settings, store, policy, time.time)
    node_ids = [f"cam-{index:02d}" for index in range(1, 5)]
    payloads = [
        (
            TOPIC_DETECTIONS,
            json.dumps(
                detection(
                    node_ids[index % 4],
                    "object_update" if index % args.durable_every else "object_start",
                    padding=args.padding,
                )
            ).encode(),
        )
        for index in range(args.count)
    ]
    size = statistics.mean(len(p) for _, p in payloads)
    latencies = []
    started = time.perf_counter()
    cpu = time.process_time()
    profile = None
    if args.profile:
        import cProfile

        profile = cProfile.Profile()
        profile.enable()
    # An older checkout's handle() does not take a QoS.
    takes_qos = "qos" in inspect.signature(ingest.handle).parameters
    for index, (topic, payload) in enumerate(payloads):
        extra = (0 if index % args.durable_every else 1,) if takes_qos else ()
        before = time.perf_counter()
        ingest.handle(topic, payload, *extra)
        latencies.append(time.perf_counter() - before)
    if takes_qos:
        ingest.settle()
    if profile:
        profile.disable()
        import pstats

        pstats.Stats(profile).sort_stats("tottime").print_stats(18)
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu
    latencies.sort()
    print(
        json.dumps(
            {
                "messages": args.count,
                "avg_bytes": round(size),
                "rate_per_s": round(args.count / elapsed),
                "cpu_us_per_msg": round(cpu / args.count * 1e6),
                "p50_ms": round(latencies[len(latencies) // 2] * 1000, 3),
                "p99_ms": round(latencies[int(len(latencies) * 0.99)] * 1000, 3),
                "max_ms": round(latencies[-1] * 1000, 3),
                "dropped": ingest.dropped(),
                "failures": ingest.store_failures(),
                "wal_mib": round((work / "relay.sqlite3-wal").stat().st_size / 2**20, 2),
                "db_mib": round((work / "relay.sqlite3").stat().st_size / 2**20, 1),
            }
        )
    )
    store.close()


# --- Reads at full retention ---------------------------------------------------------------------


def timed(call, repeats=7):
    samples = []
    for _ in range(repeats):
        before = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - before) * 1000)
    return statistics.median(samples), max(samples), result


def cmd_reads(args) -> None:
    work = Path(args.work)
    db = work / "relay.sqlite3"
    if args.seed or not db.exists():
        seed(work, args.updates, args.durable, args.padding)
    print(f"database: {db.stat().st_size / 2**20:.1f} MiB, sqlite {sqlite3.sqlite_version}")
    store = Store(db, args.updates, args.durable, 256, Settings.retain_bytes)
    raw = store._db
    newest = store.latest_seq()
    times = [
        row[0]
        for row in raw.execute(
            "SELECT timestamp_us FROM events WHERE seq IN (?, ?)",
            (max(newest - 200, 1), max(newest // 10, 1)),
        )
    ]
    recent, old = max(times), min(times)
    print("newest seq", newest, "rows", raw.execute("SELECT count(*) FROM events").fetchone()[0])
    cases = {
        "events_latest(100)": lambda: store.events_latest(100),
        "events_latest(1000)": lambda: store.events_latest(1000),
        "events_after(old, 100)": lambda: store.events_after(newest // 10, 100),
        "events_after(tail, 1000)": lambda: store.events_after(newest - 1000, 1000),
        "events_after(end, 100) [idle SSE poll]": lambda: store.events_after(newest, 100),
        "events_from_time(oldest, 100)": lambda: store.events_from_time(old, 100),
        "events_from_time(recent, 100)": lambda: store.events_from_time(recent, 100),
        "events_from_time(oldest, 1000)": lambda: store.events_from_time(old, 1000),
        "track_of_event": lambda: store.track_of_event("cam-01", "absent"),
        "latest_seq": store.latest_seq,
        "nodes": store.nodes,
        "stored_bytes (sum over the table)": store.stored_bytes,
    }
    for name, call in cases.items():
        median, worst, _ = timed(call)
        print(f"{median:10.3f} ms median {worst:10.3f} ms worst  {name}")
    print("--- plans")
    for name, sql, parameters in [
        ("events_after", "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (1, 100)),
        (
            "events_from_time",
            "SELECT * FROM events WHERE timestamp_us >= ? ORDER BY seq LIMIT ?",
            (old, 100),
        ),
        ("events_latest", "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (100,)),
        (
            "track_of_event",
            "SELECT object_id FROM events WHERE node_id = ? AND event_id = ?",
            ("cam-01", "x"),
        ),
        (
            "prune count",
            "SELECT seq FROM events WHERE durable = ? ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (0, 20000),
        ),
        ("nodes prune", "DELETE FROM nodes WHERE received_at < ?", (0,)),
    ]:
        plan = raw.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
        print(f"{name}: " + " | ".join(row["detail"] for row in plan))
    # The retention check by count: _prune here, before that was split and stopped summing.
    prune = getattr(store, "_prune_by_count", None) or store._prune
    median, worst, _ = timed(prune, 5)
    print(f"{median:10.3f} ms median {worst:10.3f} ms worst  retention check, nothing to free")
    store.close()


# --- Containers ----------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_COMPOSE = """
services:
  mosquitto:
    image: eclipse-mosquitto:2.1.2-alpine
    entrypoint: ["/bin/sh", "/relay/entrypoint.sh"]
    environment:
      RELAY_BROKER_USER: {node_user}
      RELAY_BROKER_PASSWORD: {node_password}
      RELAY_API_BROKER_USER: {api_user}
      RELAY_API_BROKER_PASSWORD: {api_password}
    tmpfs: [/run/mosquitto-auth:mode=0700, /mosquitto/log]
    volumes:
      - {relay}/mosquitto/mosquitto.conf:/relay/mosquitto.conf:ro
      - {relay}/mosquitto/aclfile:/relay/aclfile:ro
      - {relay}/mosquitto/entrypoint.sh:/relay/entrypoint.sh:ro
      - data:/mosquitto/data
    ports: ["127.0.0.1:{broker_port}:1883"]
  mediamtx:
    image: bluenviron/mediamtx:1.21.3
    environment:
      MTX_AUTHINTERNALUSERS_0_USER: viewer
      MTX_AUTHINTERNALUSERS_0_PASS: {read_password}
      MTX_AUTHINTERNALUSERS_1_PASS: {control_password}
      MTX_AUTHINTERNALUSERS_1_IPS: 0.0.0.0/0
      MTX_APIADDRESS: :9997
      MTX_WEBRTC: "no"
    volumes:
      - {relay}/mediamtx.yml:/mediamtx.yml:ro
    ports: ["127.0.0.1:{control_port}:9997"]
volumes:
  data: {{}}
"""


class Bundle:
    """The real broker and MediaMTX, under a compose project of this run's own."""

    def __init__(self):
        self.project = f"relayload-{os.getpid()}"
        self.dir = Path(tempfile.mkdtemp(prefix="relayload-"))
        self.broker_port = free_port()
        self.control_port = free_port()
        self.file = self.dir / "compose.yaml"
        self.file.write_text(
            _COMPOSE.format(
                node_user=NODE_LOGIN[0],
                node_password=NODE_LOGIN[1],
                api_user=API_LOGIN[0],
                api_password=API_LOGIN[1],
                relay=RELAY,
                broker_port=self.broker_port,
                control_port=self.control_port,
                read_password=READ_PASSWORD,
                control_password=CONTROL_PASSWORD,
            )
        )

    def compose(self, *arguments, check=True):
        return subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", str(self.file), *arguments],
            check=check,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def __enter__(self):
        self.compose("up", "-d", "--wait")
        return self

    def __exit__(self, *_):
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "3", check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


class CountingTransport:
    """Counts what the path sync really sends, by wrapping httpx's own transport."""

    def __new__(cls):
        import httpx

        class Counting(httpx.AsyncHTTPTransport):
            calls: list[str] = []

            async def handle_async_request(self, request):
                self.calls.append(request.method)
                return await super().handle_async_request(request)

        return Counting()


def cmd_pathsync(args) -> None:
    import time as clock_module

    with Bundle() as bundle, tempfile.TemporaryDirectory() as temporary:
        for count in args.nodes:
            work = Path(temporary) / str(count)
            work.mkdir()
            settings = settings_for(
                work, mediamtx_api_url=f"http://127.0.0.1:{bundle.control_port}"
            )
            store = Store(settings.db_path, 20_000, 200_000, 256, Settings.retain_bytes)
            for index in range(count):
                node_id = f"cam-{index:04d}"
                address = f"192.168.8.{index % 250 + 2}"
                store.add_heartbeat(node_id, 1, clock_module.time(), address, "{}", 10_000)
            transport = CountingTransport()
            policy = AddressPolicy(settings.site_subnet)
            sync = PathSync(settings, store, policy, clock_module.time, transport)

            async def run():
                await sync.sync_once(True)
                created = list(transport.calls)
                results = {}
                for full in (True, True, False):
                    transport.calls.clear()
                    before = clock_module.perf_counter()
                    cpu = clock_module.process_time()
                    await sync.sync_once(full)
                    results[f"full={full}"] = {
                        "calls": len(transport.calls),
                        "ms": round((clock_module.perf_counter() - before) * 1000, 1),
                        "cpu_ms": round((clock_module.process_time() - cpu) * 1000, 1),
                    }
                # A restart forgets the paths, and the next full pass puts them back.
                await sync._client.delete("/v3/config/paths/delete/cam-0000")
                transport.calls.clear()
                await sync.sync_once(True)
                results["after one path vanished"] = sorted(transport.calls)
                results["failing"] = sync.status()["failing"]
                for index in range(count):
                    await sync._client.delete(f"/v3/config/paths/delete/cam-{index:04d}")
                await sync.aclose()
                return created, results

            created, results = asyncio.run(run())
            print(count, "nodes; first pass calls", len(created), json.dumps(results))
            store.close()


# --- End to end ----------------------------------------------------------------------------------


def _publisher(port, node_id, address, rate, durable_rate, seconds, sent, padding):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"{node_id}-{uuid.uuid4().hex}"
    )
    client.username_pw_set(*NODE_LOGIN)
    client.max_queued_messages_set(0)
    client.max_inflight_messages_set(1000)
    client.connect("127.0.0.1", port)
    client.loop_start()
    start = time.monotonic()
    updates = durable = beats = 0
    client.publish(TOPIC_STATUS, json.dumps(heartbeat(node_id, address)), qos=1)
    beats += 1
    while (elapsed := time.monotonic() - start) < seconds:
        while updates < elapsed * rate:
            client.publish(TOPIC_DETECTIONS, json.dumps(detection(node_id, padding=padding)), qos=0)
            updates += 1
        while durable < elapsed * durable_rate:
            event = "object_start" if durable % 2 == 0 else "object_end"
            client.publish(TOPIC_DETECTIONS, json.dumps(detection(node_id, event)), qos=1)
            durable += 1
        if beats * 30 <= elapsed:
            client.publish(TOPIC_STATUS, json.dumps(heartbeat(node_id, address)), qos=1)
            beats += 1
        time.sleep(TICK_S)
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()
    sent.put((updates, durable, beats))


def proc_stat(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])
    status = Path(f"/proc/{pid}/status").read_text()
    rss = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS"))
    return ticks / os.sysconf("SC_CLK_TCK"), rss / 1024


def http_get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    before = time.perf_counter()
    connection.request("GET", path, headers={"Authorization": f"Bearer {TOKEN}"})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return (time.perf_counter() - before) * 1000, response.status, body


class SseClient(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.bytes = 0
        self.status = None
        self.stop = threading.Event()
        self.sock = None

    def run(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        connection.request("GET", "/events/stream", headers={"Authorization": f"Bearer {TOKEN}"})
        response = connection.getresponse()
        self.status = response.status
        while not self.stop.is_set():
            chunk = response.read1(65536)
            if not chunk:
                break
            self.bytes += len(chunk)
        connection.close()


def percentile(values, share):
    return round(sorted(values)[min(int(len(values) * share), len(values) - 1)], 2)


def cmd_e2e(args) -> None:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    env_extra = shim_env(work, args.fsync_ms)
    if args.full:
        seed(work, args.updates, args.durable, args.padding)
    else:
        for leftover in work.glob("relay.sqlite3*"):
            leftover.unlink()
    with Bundle() as bundle:
        api_port = free_port()
        env = {
            **os.environ,
            **env_extra,
            "RELAY_API_TOKEN": TOKEN,
            "RELAY_BROKER_HOST": "127.0.0.1",
            "RELAY_BROKER_PORT": str(bundle.broker_port),
            "RELAY_BROKER_USER": NODE_LOGIN[0],
            "RELAY_BROKER_PASSWORD": NODE_LOGIN[1],
            "RELAY_API_BROKER_USER": API_LOGIN[0],
            "RELAY_API_BROKER_PASSWORD": API_LOGIN[1],
            "RELAY_MQTT_CLIENT_ID": f"load-{os.getpid()}",
            "RELAY_MEDIAMTX_API_URL": f"http://127.0.0.1:{bundle.control_port}",
            "RELAY_MEDIAMTX_API_USER": "relay-api",
            "RELAY_MEDIAMTX_API_PASSWORD": CONTROL_PASSWORD,
            "RELAY_SITE_SUBNET": SITE,
            "RELAY_DB_PATH": str(work / "relay.sqlite3"),
            "RELAY_SCHEMA_DIR": str(RELAY.parent / "bridge" / "schemas"),
            "RELAY_LOG_LEVEL": "WARNING",
            "PYTHONPATH": os.environ.get("SRC", ""),
        }
        command = [
            sys.executable, "-m", "uvicorn", "--factory", "relayapi.app:create_app",
            "--host", "127.0.0.1", "--port", str(api_port), "--no-server-header",
            "--no-access-log", "--timeout-graceful-shutdown", "5",
        ]  # fmt: skip
        if args.pin_cpu is not None:
            command = ["taskset", "-c", str(args.pin_cpu), *command]
        launched = time.perf_counter()
        api = subprocess.Popen(command, env=env)
        try:
            while True:
                try:
                    _, status, body = http_get(api_port, "/nodes")
                    if status == 200 and json.loads(body)["broker"]["connected"]:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            ready_s = time.perf_counter() - launched
            streams = [SseClient(api_port) for _ in range(args.sse)]
            for stream in streams:
                stream.start()
            time.sleep(0.5)

            def newest_seq():
                _, _, body = http_get(api_port, "/events?limit=1")
                events = json.loads(body)["events"]
                return events[-1]["seq"] if events else 0

            first_seq = newest_seq()
            sent = multiprocessing.Queue()
            publishers = [
                multiprocessing.Process(
                    target=_publisher,
                    args=(
                        bundle.broker_port,
                        f"cam-{index:02d}",
                        f"192.168.8.{20 + index}",
                        args.rate / 4,
                        args.durable_rate / 4,
                        args.seconds,
                        sent,
                        args.padding,
                    ),
                )
                for index in range(1, 5)
            ]
            cpu_before, rss_before = proc_stat(api.pid)
            started = time.monotonic()
            for publisher in publishers:
                publisher.start()
            nodes_ms, events_ms, rss = [], [], [rss_before]
            while time.monotonic() - started < args.seconds:
                nodes_ms.append(http_get(api_port, "/nodes")[0])
                events_ms.append(http_get(api_port, "/events?limit=100")[0])
                rss.append(proc_stat(api.pid)[1])
                time.sleep(args.probe_every)
            cpu_at_end = proc_stat(api.pid)[0]
            for publisher in publishers:
                publisher.join()
            totals = [sent.get() for _ in publishers]
            published = sum(sum(total[:2]) for total in totals)
            drain = time.monotonic()
            last = -1
            while time.monotonic() - drain < args.drain_s:
                seq = newest_seq()
                if seq - first_seq >= published or (seq == last and time.monotonic() - drain > 3):
                    break
                last = seq
                time.sleep(0.25)
            stored = newest_seq() - first_seq
            drain_s = time.monotonic() - drain
            cpu_after, rss_after = proc_stat(api.pid)
            _, _, body = http_get(api_port, "/nodes")
            broker = json.loads(body)
            for stream in streams:
                stream.stop.set()
            print(
                json.dumps(
                    {
                        "rate_updates": args.rate,
                        "rate_durable": args.durable_rate,
                        "sse": args.sse,
                        "seconds": args.seconds,
                        "ready_s": round(ready_s, 2),
                        "published": published,
                        "stored": stored,
                        "not_stored_in_time": published - stored,
                        "stored_per_s": round(stored / (args.seconds + drain_s)),
                        "drain_s": round(drain_s, 2),
                        "cpu_core_share": round((cpu_at_end - cpu_before) / args.seconds, 3),
                        "cpu_us_per_msg": round((cpu_after - cpu_before) / max(stored, 1) * 1e6),
                        "rss_mib": {
                            "start": round(rss_before, 1),
                            "end": round(rss_after, 1),
                            "max": round(max(rss), 1),
                        },
                        "nodes_ms": {
                            "p50": percentile(nodes_ms, 0.5),
                            "p99": percentile(nodes_ms, 0.99),
                        },
                        "events_ms": {
                            "p50": percentile(events_ms, 0.5),
                            "p99": percentile(events_ms, 0.99),
                        },
                        "sse_bytes": [stream.bytes for stream in streams][:2],
                        "dropped": broker["broker"]["dropped"],
                        "unacked": broker["broker"]["unacked"],
                        "paths": broker["paths"],
                        "wal_mib": round((work / "relay.sqlite3-wal").stat().st_size / 2**20, 2),
                        "db_mib": round((work / "relay.sqlite3").stat().st_size / 2**20, 1),
                    }
                )
            )
        finally:
            api.send_signal(signal.SIGTERM)
            try:
                api.wait(timeout=15)
            except subprocess.TimeoutExpired:
                api.kill()
    log = work / "fsyncs.txt"
    if args.fsync_ms is not None and log.exists():
        print("fsyncs:", log.read_text().strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub):
        sub.add_argument("work")
        sub.add_argument("--updates", type=int, default=20_000)
        sub.add_argument("--durable", type=int, default=200_000)
        sub.add_argument("--padding", type=int, default=0)

    ingest = commands.add_parser("ingest")
    common(ingest)
    ingest.add_argument("--count", type=int, default=2000)
    ingest.add_argument("--full", action="store_true")
    ingest.add_argument("--durable-every", type=int, default=30)
    ingest.add_argument("--fsync-ms", type=int)
    ingest.add_argument("--retain-bytes", type=int)
    ingest.add_argument("--profile", action="store_true")
    ingest.set_defaults(run=cmd_ingest)

    reads = commands.add_parser("reads")
    common(reads)
    reads.add_argument("--seed", action="store_true")
    reads.set_defaults(run=cmd_reads)

    pathsync = commands.add_parser("pathsync")
    pathsync.add_argument("--nodes", type=int, nargs="+", default=[4, 250])
    pathsync.set_defaults(run=cmd_pathsync)

    e2e = commands.add_parser("e2e")
    common(e2e)
    e2e.add_argument("--rate", type=float, default=60.0, help="object_update per second, all nodes")
    e2e.add_argument("--durable-rate", type=float, default=2.0, help="start and end per second")
    e2e.add_argument("--seconds", type=float, default=60.0)
    e2e.add_argument("--sse", type=int, default=0)
    e2e.add_argument("--full", action="store_true")
    e2e.add_argument("--fsync-ms", type=int)
    e2e.add_argument("--pin-cpu", type=int)
    e2e.add_argument("--probe-every", type=float, default=1.0)
    e2e.add_argument("--drain-s", type=float, default=30.0)
    e2e.set_defaults(run=cmd_e2e)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
