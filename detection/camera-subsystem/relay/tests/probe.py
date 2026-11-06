"""Helpers for test_relay.sh: MQTT with a real client, an SNTP query, and the clock.

Passwords come from the environment, not argv, and nothing here prints one.
"""

import argparse
import json
import os
import socket
import struct
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

NTP_EPOCH_OFFSET = 2_208_988_800


def free_ports(args):
    held = []
    for _ in range(args.count):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        # Also free for UDP, as NTP and WebRTC media need that.
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", port))
        held += [sock, udp]
        print(port)


def _client(user, password):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"probe-{uuid.uuid4()}")
    if user:
        client.username_pw_set(user, password)
    return client


def _connect(args, user, password):
    """Return a connected client, or (None, reason)."""
    client = _client(user, password)
    outcome = {}
    client.on_connect = lambda c, u, f, code, p: outcome.update(failed=code.is_failure, code=str(code))
    client.connect(args.host, args.port, keepalive=10)
    client.loop_start()
    deadline = time.monotonic() + 10
    while "failed" not in outcome and time.monotonic() < deadline:
        time.sleep(0.05)
    if outcome.get("failed", True):
        client.loop_stop()
        return None, outcome.get("code", "no answer")
    return client, ""


def login(args):
    user = os.environ.get("PROBE_USER", "") if not args.anonymous else ""
    password = os.environ.get("PROBE_PASSWORD", "") if not args.anonymous else ""
    if args.wrong_password:
        password = "not-the-password"
    client, reason = _connect(args, user, password)
    if client is None:
        print(f"refused: {reason}")
        return 1
    client.disconnect()
    client.loop_stop()
    print("accepted")
    return 0


def _publish(args, topic, message):
    payload = message if isinstance(message, bytes) else json.dumps(message).encode()
    client, reason = _connect(args, os.environ["PROBE_USER"], os.environ["PROBE_PASSWORD"])
    if client is None:
        print(f"refused: {reason}", file=sys.stderr)
        return 1
    info = client.publish(topic, payload, qos=1)
    info.wait_for_publish(timeout=10)
    client.disconnect()
    client.loop_stop()
    return 0 if info.is_published() else 1


def _stamp(age_s):
    moment = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def heartbeat(args):
    return _publish(
        args,
        "detection/camera/status/nodes",
        {
            "schema": "camera.node-status.v0",
            "event_id": str(uuid.uuid4()),
            "timestamp": _stamp(args.age),
            "node_id": args.node,
            "camera_online": True,
            "link_online": True,
            "recording_ok": True,
            "detector_ok": False,
            "attributes": {"node_address": args.address},
        },
    )


def detection(args):
    return _publish(
        args,
        "detection/camera/detections",
        {
            "schema": "camera.detection.v0",
            "event_id": args.event_id,
            "timestamp": _stamp(0),
            "node_id": args.node,
            "camera_id": args.node,
            "event": "object_start",
            "object_id": "1730749200.12-abc",
            "label": "person",
            "confidence": 0.87,
            "zones": [],
            "attributes": {},
        },
    )


def raw(args):
    return _publish(args, args.topic, args.payload.encode())


def huge(args):
    """Messages that have crashed JSON handling before."""
    for payload in (
        b"[" * 100_000 + b"]" * 100_000,
        b'{"confidence":' + b"1" + b"0" * 5000 + b"}",
        b'{"a":1e999}',
    ):
        code = _publish(args, "detection/camera/detections", payload)
        if code:
            return code
    return 0


def acl(args):
    """Exit 0 if a publish outside detection/ is not delivered to this same login."""
    client, reason = _connect(args, os.environ["PROBE_USER"], os.environ["PROBE_PASSWORD"])
    if client is None:
        print(f"refused: {reason}", file=sys.stderr)
        return 2
    received = []
    client.on_message = lambda c, u, m: received.append(m.topic)
    topic = f"elsewhere/{uuid.uuid4()}"
    client.subscribe([(topic, 1), ("detection/camera/acl-check", 1)])
    time.sleep(0.5)
    client.publish(topic, b"x", qos=1)
    client.publish("detection/camera/acl-check", b"x", qos=1)
    time.sleep(1.5)
    client.disconnect()
    client.loop_stop()
    # The allowed topic arriving shows that the check itself works.
    return 0 if received == ["detection/camera/acl-check"] else 1


def sntp(args):
    """Print the server's stratum and its offset from this host, in seconds."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        for _ in range(10):
            sent = time.time()
            try:
                sock.sendto(b"\x23" + 47 * b"\0", (args.host, args.port))
                data, _ = sock.recvfrom(512)
            except OSError:
                time.sleep(1)
                continue
            received = time.time()
            stratum = data[1]
            seconds, fraction = struct.unpack("!II", data[40:48])
            served = seconds - NTP_EPOCH_OFFSET + fraction / 2**32
            print(json.dumps({"stratum": stratum, "offset": served - (sent + received) / 2}))
            return 0 if seconds else 1
    print("no answer", file=sys.stderr)
    return 1


def clock(args):
    """CLOCK_REALTIME minus CLOCK_MONOTONIC: moves only if something changes the wall clock."""
    print(f"{time.clock_gettime(time.CLOCK_REALTIME) - time.clock_gettime(time.CLOCK_MONOTONIC):.6f}")


def hold_port(args):
    kind = socket.SOCK_DGRAM if args.udp else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, kind)
    sock.bind(("127.0.0.1", args.port))
    if not args.udp:
        sock.listen()
    print("held", flush=True)
    time.sleep(args.seconds)


def node_field(args):
    """Read GET /nodes from stdin and print one field of one node."""
    for node in json.load(sys.stdin)["nodes"]:
        if node["node_id"] == args.node:
            print(json.dumps(node.get(args.field)))
            return 0
    return 1


def broker_field(args):
    print(json.dumps(json.load(sys.stdin)["broker"].get(args.field)))


def event_count(args):
    events = json.load(sys.stdin)["events"]
    print(sum(1 for e in events if e["message"]["event_id"] == args.event_id))


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)

    def command(name, function, *arguments, mqtt=False):
        sub = commands.add_parser(name)
        sub.set_defaults(function=function)
        if mqtt:
            sub.add_argument("--host", default="127.0.0.1")
            sub.add_argument("--port", type=int, required=True)
        for flags, options in arguments:
            sub.add_argument(*flags, **options)

    number = {"type": int}
    command("free-ports", free_ports, (["count"], number))
    command(
        "login",
        login,
        (["--anonymous"], {"action": "store_true"}),
        (["--wrong-password"], {"action": "store_true"}),
        mqtt=True,
    )
    command(
        "heartbeat",
        heartbeat,
        (["--node"], {"required": True}),
        (["--address"], {"required": True}),
        (["--age"], {"type": float, "default": 0.0}),
        mqtt=True,
    )
    command(
        "detection",
        detection,
        (["--node"], {"required": True}),
        (["--event-id"], {"required": True}),
        mqtt=True,
    )
    command("raw", raw, (["--topic"], {"required": True}), (["--payload"], {"required": True}), mqtt=True)
    command("huge", huge, mqtt=True)
    command("acl", acl, mqtt=True)
    command("sntp", sntp, (["--host"], {"default": "127.0.0.1"}), (["--port"], {**number, "required": True}))
    command("clock", clock)
    command(
        "hold-port",
        hold_port,
        (["port"], number),
        (["--udp"], {"action": "store_true"}),
        (["--seconds"], {"type": float, "default": 120}),
    )
    command("node-field", node_field, (["node"], {}), (["field"], {}))
    command("broker-field", broker_field, (["field"], {}))
    command("event-count", event_count, (["event_id"], {}))
    args = parser.parse_args()
    sys.exit(args.function(args) or 0)


if __name__ == "__main__":
    main()
