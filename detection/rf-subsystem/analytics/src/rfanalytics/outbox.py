"""Durable outbound delivery (DESIGN.md 7.3.8, CEN-13).

Every outbound message is written to an on-disk outbox *before* it is
published, and removed only when the broker acknowledges it (QoS 1). If the
broker is unreachable, or the process stops, unacknowledged messages stay in
the outbox and are replayed with their original timestamps on the next start
or reconnect.

The outbox is a JSONL file rewritten on every change. Outbound volume is
alerts, cues and status changes (a few per minute at most), so simplicity
beats throughput here.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Protocol


class Publisher(Protocol):
    def publish(self, topic: str, payload: str) -> int | None:
        """Start publishing; return a message id to await, or None if not sent."""


class Outbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._pending: dict[str, tuple[str, str]] = {}  # message id -> (topic, payload)
        self._in_flight: dict[int, str] = {}  # broker mid -> message id
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._pending[rec["id"]] = (rec["topic"], rec["payload"])

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def add(self, msg_id: str, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._pending[msg_id] = (topic, json.dumps(payload, separators=(",", ":")))
            self._save()

    def send_pending(self, publisher: Publisher) -> None:
        """(Re)send every pending message that is not already in flight.

        The lock is held across publish and registration so a fast broker
        acknowledgement cannot arrive before the message id is recorded.
        """
        with self._lock:
            in_flight = set(self._in_flight.values())
            for msg_id, (topic, payload) in list(self._pending.items()):
                if msg_id in in_flight:
                    continue
                mid = publisher.publish(topic, payload)
                if mid is not None:
                    self._in_flight[mid] = msg_id

    def acknowledge(self, mid: int) -> None:
        with self._lock:
            msg_id = self._in_flight.pop(mid, None)
            if msg_id is not None and self._pending.pop(msg_id, None) is not None:
                self._save()

    def connection_lost(self) -> None:
        """Forget in-flight state so everything pending is resent on reconnect."""
        with self._lock:
            self._in_flight.clear()

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            for msg_id, (topic, payload) in self._pending.items():
                f.write(json.dumps({"id": msg_id, "topic": topic, "payload": payload}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class MqttPublisher:
    """paho-mqtt publisher bound to an outbox. QoS 1, auto-reconnect."""

    def __init__(self, outbox: Outbox, host: str, port: int = 1883, client_id: str = "rfanalytics-out") -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise SystemExit("MQTT output needs paho-mqtt: uv sync --extra mqtt") from None
        self._mqtt = mqtt
        self.outbox = outbox
        self.connected = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
        self.client.reconnect_delay_set(1, 30)
        self.client.connect_async(host, port)
        self.client.loop_start()

    def publish(self, topic: str, payload: str) -> int | None:
        if not self.connected.is_set():
            return None
        info = self.client.publish(topic, payload, qos=1)
        return info.mid if info.rc == self._mqtt.MQTT_ERR_SUCCESS else None

    def send(self, msg_id: str, topic: str, payload: dict[str, Any]) -> None:
        self.outbox.add(msg_id, topic, payload)
        self.outbox.send_pending(self)

    def close(self, timeout: float = 5.0) -> None:
        """Wait up to ``timeout`` for pending messages to be delivered, then stop.

        Waits even while not yet connected, so a short run (or a replay-only
        start) still gets a chance to connect and drain the outbox.
        """
        waiter = threading.Event()
        for _ in range(int(timeout * 10)):
            if not len(self.outbox):
                break
            waiter.wait(0.1)
        if len(self.outbox):
            print(f"rfanalytics: {len(self.outbox)} message(s) left in outbox {self.outbox.path}", file=sys.stderr)
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            print(f"rfanalytics: MQTT publish connection failed: {reason_code}", file=sys.stderr)
            return
        self.connected.set()
        self.outbox.send_pending(self)

    def _on_disconnect(self, *_args):
        self.connected.clear()
        self.outbox.connection_lost()

    def _on_publish(self, _client, _userdata, mid, _reason_code, _props):
        self.outbox.acknowledge(mid)
