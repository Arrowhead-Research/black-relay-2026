"""Outbound-side in-memory view, fed only by the one-way normalizer stream."""
from __future__ import annotations

import asyncio
import copy
import time

from .. import model as M


class OutState:
    def __init__(self) -> None:
        self.zones: dict[str, dict] = {}
        self.parts: dict[str, dict] = {}
        self.status = "offline"
        self.health: dict = {}
        self.last_seq = 0
        self.first_dump_seq: int | None = None
        self.have_dump = False
        self.ipc_up = False
        self.changed = asyncio.Event()
        self.dirty_zones: set[str] = set()
        self.removed_zones: set[str] = set()
        self.dirty_parts: set[str] = set()
        self.removed_parts: set[str] = set()
        self.status_dirty = True
        self.counters = {"api_requests": 0, "api_auth_failures": 0, "api_rate_limited": 0, "mqtt_published": 0,
                         "mqtt_reconnects": 0, "sse_clients": 0, "sse_events_sent": 0, "sse_idle_dropped": 0, "ipc_reconnects": 0}
        self.mqtt_connected = False
        self.fusion = None            # FusionPublisher when the team output is enabled (read by /metrics)

    def notify(self) -> None:
        old = self.changed
        self.changed = asyncio.Event()
        old.set()

    def _set_status(self, st: str | None) -> None:
        if st and st != self.status:
            self.status = st
            self.status_dirty = True

    def apply(self, m: dict) -> None:
        t = m.get("t")
        if t == "dump":
            self.ipc_up = True
            self.have_dump = True
            new_z = {z["id"]: z for z in m["zones"]}
            for zid in set(self.zones) - set(new_z):
                self.removed_zones.add(zid)
            for zid, z in new_z.items():
                self.dirty_zones.add(zid)
            self.zones = new_z
            new_p = {p["id"]: p for p in m["partitions"]}
            for pid in set(self.parts) - set(new_p):
                self.removed_parts.add(pid)
            self.dirty_parts |= set(new_p)
            self.parts = new_p
            self.health = m.get("health", {})
            self.last_seq = max(self.last_seq, m.get("last_seq", 0))
            if self.first_dump_seq is None:
                self.first_dump_seq = m.get("last_seq", 0)
            self._set_status(m.get("status"))
        elif t == "batch":
            for z in m["zones"]:
                self.zones[z["id"]] = z
                self.dirty_zones.add(z["id"])
                self.removed_zones.discard(z["id"])
            for zid in m["removed_zones"]:
                self.zones.pop(zid, None)
                self.dirty_zones.discard(zid)
                self.removed_zones.add(zid)
            for p in m["partitions"]:
                self.parts[p["id"]] = p
                self.dirty_parts.add(p["id"])
            for pid in m["removed_partitions"]:
                self.parts.pop(pid, None)
                self.dirty_parts.discard(pid)
                self.removed_parts.add(pid)
            self.health = m.get("health", self.health)
            if m["events"]:
                self.last_seq = m["events"][-1]["seq"]
            self._set_status(m.get("status"))
        elif t == "health":
            self.health = m["health"]
            self._set_status(m.get("status"))
        elif t == "ipc_closed":
            self.ipc_up = False
            # normalizer gone: nothing vouches for the data any more -> flag everything stale + offline
            for z in self.zones.values():
                if not z.get("stale"):
                    z["stale"] = True
                    self.dirty_zones.add(z["id"])
            for p in self.parts.values():
                if not p.get("stale"):
                    p["stale"] = True
                    self.dirty_parts.add(p["id"])
            self._set_status("offline")
            self.health = dict(self.health, status="offline", active_source=None)
        self.notify()

    def health_view(self) -> dict:
        h = self.health or {}
        epoch = h.get("last_panel_message_epoch")
        return {
            "status": "offline" if not self.ipc_up else self.status,
            "active_source": h.get("active_source") if self.ipc_up else None,
            "links": h.get("links", {"iq_remote": "down", "control4": "down"}),
            "link_reasons": h.get("link_reasons") or {},
            "last_panel_message_at": M.iso(epoch) if epoch else None,
            "seconds_since_panel_message": round(time.time() - epoch, 3) if epoch else None,
            "zones": len(self.zones),
            "partitions": len(self.parts),
            "last_seq": self.last_seq,
            "stale_zones": sum(1 for z in self.zones.values() if z.get("stale")),
            "normalizer": h.get("normalizer") if self.ipc_up else {"healthy": False},
        }

    def take_dirty(self):
        d = (self.dirty_zones, self.removed_zones, self.dirty_parts, self.removed_parts, self.status_dirty)
        self.dirty_zones, self.removed_zones = set(), set()
        self.dirty_parts, self.removed_parts = set(), set()
        self.status_dirty = False
        return d

    def mark_all_dirty(self) -> None:
        self.dirty_zones |= set(self.zones)
        self.dirty_parts |= set(self.parts)
        self.status_dirty = True
        self.notify()
