"""Node health from rf-node-agent heartbeats (node/agent/rf-node-agent).

Each node POSTs its health every 30 s to /api/nodes/heartbeat (with
X-Node-Token when CONSOLE_NODE_TOKEN is set). ``report`` derives problems:
agent silent (no heartbeat for SILENT_S), hot, throttling or undervoltage
(vcgencmd bits), a capture unit not running, a capture adapter missing, disk
nearly full, and a capture unit the agent's watchdog restarted in the last
WATCHDOG_SHOW_S (it delivered no data). An admin can queue a restart of a capture unit; the agent
receives it in the reply to its next heartbeat.
"""

from __future__ import annotations

import re
import threading
from typing import Any

SILENT_S = 90.0
HOT_C = 80.0
LOW_DISK_PCT = 10.0
WATCHDOG_SHOW_S = 1800.0   # a node agent's watchdog restart is listed as a problem this long
RESTARTABLE = ("kismet-cap-wifi", "kismet-cap-bt", "kismet-cap-tpms")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MAX_NODES = 32
THROTTLE_NOW = {0x1: "undervoltage", 0x2: "frequency capped", 0x4: "throttled", 0x8: "soft temperature limit"}
ADAPTERS = {"wifi": "Wi-Fi capture adapter", "bt": "Bluetooth adapter", "sdr": "RTL-SDR"}


class NodeHealth:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, set[str]] = {}

    def heartbeat(self, body: Any, now: float) -> dict[str, Any]:
        if not isinstance(body, dict) or not NODE_RE.match(str(body.get("node", ""))):
            raise ValueError("heartbeat needs a node name")
        node = str(body["node"]).lower()
        keep = ("agent", "host", "uptime_s", "temp_c", "throttled", "load", "mem_free_pct", "disk_free_pct",
                "units", "adapters", "tailscale_ip", "actions", "watchdog")
        with self.lock:
            if node not in self.data and len(self.data) >= MAX_NODES:
                raise ValueError("too many nodes")
            prev = self.data.get(node, {})
            # keep the agent's watchdog restarts for WATCHDOG_SHOW_S (they are reported once)
            auto = [a for a in (prev.get("auto_restarts") or []) if now - a.get("t", 0) <= WATCHDOG_SHOW_S]
            auto += [a for a in (body.get("actions") or []) if isinstance(a, dict) and a.get("auto")]
            self.data[node] = {**{k: body.get(k) for k in keep}, "received": now,
                               "actions": (body.get("actions") or []) or prev.get("actions", []), "auto_restarts": auto}
            return {"restart": sorted(self.pending.pop(node, set()))}

    def queue_restart(self, node: str, unit: str) -> None:
        if unit not in RESTARTABLE:
            raise ValueError(f"unit must be one of {RESTARTABLE}")
        with self.lock:
            if node not in self.data:
                raise KeyError(node)
            self.pending.setdefault(node, set()).add(unit)

    def report(self, now: float) -> dict[str, dict[str, Any]]:
        out = {}
        with self.lock:
            for node, h in sorted(self.data.items()):
                age = now - h["received"]
                problems = []
                if age > SILENT_S:
                    problems.append("agent silent")
                if h.get("temp_c") is not None and h["temp_c"] >= HOT_C:
                    problems.append(f"hot ({h['temp_c']:.0f} °C)")
                thr = h.get("throttled") or 0
                problems += [name for bit, name in THROTTLE_NOW.items() if thr & bit]
                for unit, st in (h.get("units") or {}).items():
                    if isinstance(st, dict) and st.get("active") != "active":
                        problems.append(f"{unit} {st.get('active')}")
                for k, label in ADAPTERS.items():
                    if (h.get("adapters") or {}).get(k) is False:
                        problems.append(f"no {label}")
                for unit in sorted({a.get("restart") for a in h.get("auto_restarts") or []
                                    if now - a.get("t", 0) <= WATCHDOG_SHOW_S and a.get("restart")}):
                    problems.append(f"{unit} restarted by watchdog")
                if h.get("disk_free_pct") is not None and h["disk_free_pct"] < LOW_DISK_PCT:
                    problems.append(f"disk {h['disk_free_pct']:.0f}% free")
                out[node] = {**{k: v for k, v in h.items() if k != "received"}, "age_s": round(age),
                             "problems": problems, "pending": sorted(self.pending.get(node, ()))}
        return out
