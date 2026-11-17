"""Publish device positions and node status to MQTT (for the fusion service).

Wire format: ``rfanalytics.formats`` (the only place that knows topics and
payload envelopes); payload schemas: ``schemas/position.v0.schema.json`` and
``schemas/node-status.v0.schema.json``.

- **Positions** (``detection/rf/positions``, rf.position.v0) are latest-wins
  telemetry: sent on their own connection at QoS 0 and dropped while the
  broker is unreachable (a replayed old position is worse than none). A device
  is sent when its estimate has new readings, at most every POS_MIN_S, and
  while it barely moves only every POS_MAX_S. The nodes' own radios are not
  published (they are the team's infrastructure, at their nodes).
- **Alerts** (``detection/rf/alerts``, rf.alert.v0) from ``alerting.py``, through
  the durable outbox.
- **Node status** (``detection/rf/status/nodes``, rf.node-status.v0) goes
  through the durable outbox (QoS 1, kept on disk until the broker
  acknowledges it): on every state change and every NODE_HEARTBEAT_S.
"""

from __future__ import annotations

import json
import math
import sys
import threading
from pathlib import Path
from typing import Any

from rfanalytics.alerts import Message
from rfanalytics.formats import TOPIC_PREFIX, to_wire

POS_MIN_S = 2.0           # a device at most this often
POS_MAX_S = 30.0          # while it moves less than POS_MOVE_FT, only this often
POS_MOVE_FT = 1.0
NODE_HEARTBEAT_S = 60.0     # a subscriber that joins late learns every node within a minute


class LiveClient:
    """paho-mqtt client for latest-wins messages: QoS 0, nothing kept while disconnected."""

    def __init__(self, host: str, port: int = 1883, client_id: str = "rf-console-live") -> None:
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self.connected = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = lambda c, u, f, rc, p: self.connected.set() if not rc.is_failure else None
        self.client.on_disconnect = lambda *a: self.connected.clear()
        self.client.reconnect_delay_set(1, 30)
        self.client.connect_async(host, port)
        self.client.loop_start()

    def publish(self, topic: str, payload: str) -> bool:
        if not self.connected.is_set():
            return False
        return self.client.publish(topic, payload, qos=0).rc == self._mqtt.MQTT_ERR_SUCCESS


def node_states(report: list[dict[str, Any]], health: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Per node: state and per-source status from the capture-source report, with the
    node agent's health (nodehealth.py) when it reports: problems make an online node degraded."""
    nodes: dict[str, dict[str, str]] = {}
    for s in report:
        if ":" not in s["name"]:
            continue
        node, kind = s["name"].split(":", 1)
        nodes.setdefault(node.lower(), {})[kind] = s["status"]
    out = {}
    for node, sources in nodes.items():
        ok = [v == "ok" for v in sources.values()]
        running = [v in ("ok", "silent") for v in sources.values()]
        state = "online" if all(ok) else "offline" if not any(running) else "degraded"
        out[node] = {"state": state, "sources": dict(sorted(sources.items()))}
    for node, h in (health or {}).items():
        st = out.setdefault(node, {"state": "offline" if "agent silent" in h["problems"] else "online", "sources": {}})
        if h["problems"] and st["state"] == "online":
            st["state"] = "degraded"
        st["health"] = {"temp_c": h.get("temp_c"), "throttled": h.get("throttled"), "uptime_s": h.get("uptime_s"),
                        "agent_age_s": h.get("age_s"), "problems": h["problems"]}
    return out


class Publisher:
    def __init__(self, live: Any, durable: Any, topic_prefix: str = TOPIC_PREFIX) -> None:
        """``live.publish(topic, str) -> bool``; ``durable.send(msg_id, topic, dict)`` (outbox-backed)."""
        self.live, self.durable, self.prefix = live, durable, topic_prefix
        self.last_pos: dict[str, tuple[float, float, float]] = {}    # id -> (sent at, x, y)
        self.last_node: dict[str, tuple[float, str, dict[str, str], Any]] = {}
        self.stats = {"positions_sent": 0, "positions_dropped": 0, "node_status_queued": 0}

    @classmethod
    def mqtt(cls, host: str, port: int, outbox_path: Path, topic_prefix: str = TOPIC_PREFIX) -> "Publisher":
        from rfanalytics.outbox import MqttPublisher, Outbox

        return cls(LiveClient(host, port), MqttPublisher(Outbox(outbox_path), host, port, client_id="rf-console-out"),
                   topic_prefix)

    def positions(self, positions: dict[str, dict[str, Any]], devices: dict[str, dict[str, Any]],
                  site: dict[str, str], skip: set[str], now: float, marks: dict[str, dict[str, Any]] | None = None) -> None:
        for eid, p in positions.items():
            if eid in skip or not p.get("updated"):
                continue
            last = self.last_pos.get(eid)
            if last:
                dt, moved = now - last[0], math.hypot(p["x"] - last[1], p["y"] - last[2])
                if dt < POS_MIN_S or (dt < POS_MAX_S and moved < POS_MOVE_FT):
                    continue
            body = position_body(eid, p, devices[eid], site, ((marks or {}).get(eid) or {}).get("kind"))
            topic, payload = to_wire(Message("position", p["t"], body), self.prefix)
            if self.live.publish(topic, json.dumps(payload, separators=(",", ":"))):
                self.stats["positions_sent"] += 1
                self.last_pos[eid] = (now, p["x"], p["y"])
            else:
                self.stats["positions_dropped"] += 1
        for eid in [k for k, v in self.last_pos.items() if now - v[0] > 3600]:
            del self.last_pos[eid]

    def nodes(self, report: list[dict[str, Any]], site: dict[str, str], now: float,
              health: dict[str, dict[str, Any]] | None = None) -> None:
        for node, st in node_states(report, health).items():
            last = self.last_node.get(node)
            problems = (st.get("health") or {}).get("problems")
            changed = last is None or last[1] != st["state"] or last[2] != st["sources"] or last[3] != problems
            if not changed and now - last[0] < NODE_HEARTBEAT_S:
                continue
            body = {"node_id": node, "state": st["state"], "site": site, "sources": st["sources"]}
            if st.get("health"):
                body["health"] = st["health"]
            msg = Message("node_status", now, body)
            topic, payload = to_wire(msg, self.prefix)
            try:
                self.durable.send(msg.id, topic, payload)
                self.stats["node_status_queued"] += 1
            except Exception as exc:  # noqa: BLE001 - never let publishing stop the poller
                print(f"console: node status not queued: {exc}", file=sys.stderr)
                continue
            self.last_node[node] = (now, st["state"], st["sources"], problems)

    def alert(self, rec: dict[str, Any]) -> None:
        """One alert (AlertLog record) through the durable outbox, as rf.alert.v0."""
        body = {k: rec[k] for k in ("rule_id", "rule_type", "severity", "site", "context") if k in rec}
        for k in ("emitter", "location"):
            if rec.get(k):
                body[k] = rec[k]
        msg = Message("alert", rec["t"], body, id=rec["id"])
        topic, payload = to_wire(msg, self.prefix)
        try:
            self.durable.send(msg.id, topic, payload)
            self.stats["alerts_queued"] = self.stats.get("alerts_queued", 0) + 1
        except Exception as exc:  # noqa: BLE001
            print(f"console: alert not queued: {exc}", file=sys.stderr)

    def status(self) -> dict[str, Any]:
        live = getattr(self.live, "connected", None)
        outbox = getattr(getattr(self.durable, "outbox", None), "__len__", None)
        return {"enabled": True, "connected": bool(live.is_set()) if live is not None else None,
                "outbox": outbox() if outbox else None, **self.stats}


def position_body(eid: str, p: dict[str, Any], dev: dict[str, Any], site: dict[str, str], mark: str | None = None) -> dict[str, Any]:
    pos: dict[str, Any] = {"x_ft": p["x"], "y_ft": p["y"], "calibrated": p["calibrated"],
                           "error_ft": None if p.get("err_ft") is None else round(p["err_ft"], 1)}
    if "lat" in p:
        pos.update(lat=p["lat"], lon=p["lon"], error_m=p.get("err_m"))
    return {
        "site": site,
        "emitter": {"id": eid, "type": dev["type"], "band": dev["band"], "name": dev.get("name"),
                    "manufacturer": dev.get("manufacturer"), "randomized": bool(dev.get("randomized")), "mark": mark},
        "position": pos,
        "heard_by": list(p["ids"]),
    }
