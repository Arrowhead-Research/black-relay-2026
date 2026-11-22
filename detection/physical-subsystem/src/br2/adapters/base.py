"""Shared adapter plumbing. An adapter owns exactly one panel connection and one SendOnlyClient to the
normalizer; it has no code path that reads from the normalizer."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

from .. import config as C
from ..ipc import SendOnlyClient

log = logging.getLogger("br2.adapter")


class OneWayViolation(Exception):
    """Raised by a panel write guard when code attempts to send anything outside the read-only allow-list."""


class AdapterBase:
    name = "?"

    def __init__(self, cfg: C.Config, ipc_dir: str | None = None):
        self.cfg = cfg
        t = cfg.timers
        self.silence_probe_s = float(t["silence_probe_s"])
        self.probe_timeout_s = float(t["probe_timeout_s"])
        self.backoff_initial = float(t["backoff_initial_s"])
        self.backoff_max = float(t["backoff_max_s"])
        self.zones: dict[int, dict] = {}
        self.parts: dict[int, dict] = {}
        self.up = False
        self.down_reason: str | None = None            # why the last session ended (e.g. "pin_mismatch")
        self.stats = {"reconnects": 0, "snapshots": 0, "violations": 0, "panel_messages": 0, "probes": 0,
                      "probe_failures": 0, "panel_sent": 0}
        self._last_seen_sent = 0.0
        self.last_panel_msg = time.monotonic()
        d = ipc_dir or C.ipc_adapter_dir(self.name)
        # one socket per adapter: the file name IS the identity the normalizer expects on that socket
        self.ipc = SendOnlyClient(os.path.join(d, f"{self.name}.sock"), {"t": "hello", "src": self.name},
                                  on_connect=self._on_ipc_connect, on_gap=self._on_gap,
                                  ping_extra=lambda: {"src": self.name, "up": bool(self.up)})

    # ---- IPC emit (adapter -> normalizer only)
    async def _on_ipc_connect(self) -> None:
        # normalizer (re)started: it has no memory of us. Re-send a full picture if our panel link is up.
        if self.up:
            await self.emit_snapshot()

    async def _on_gap(self) -> None:
        """Our own loop stalled (SIGSTOP / starvation): the normalizer's liveness deadline may have fired and
        quarantined our source; only a full snapshot revives it."""
        log.warning("%s: local stall detected; re-sending full snapshot", self.name)
        if self.up:
            await self.emit_snapshot()

    async def _resnap_loop(self) -> None:
        """Safety net: a full snapshot every 60 s (cheap; the normalizer diffs it)."""
        while True:
            await asyncio.sleep(60.0)
            if self.up:
                await self.emit_snapshot()

    async def emit_snapshot(self) -> None:
        self.stats["snapshots"] += 1
        m = {"t": "snapshot", "src": self.name, "zones": list(self.zones.values()),
             "partitions": list(self.parts.values())}
        if not getattr(self, "zones_complete", True):
            m["zones_complete"] = False                # engine: omitted zones are unknown, not deleted
        await self.ipc.send(m)

    async def emit(self, **m) -> None:
        m.setdefault("src", self.name)
        await self.ipc.send(m)

    async def link_down(self, reason: str = "") -> None:
        was = self.up
        self.up = False
        if was:
            log.warning("%s link down: %s", self.name, reason)
        await self.emit(t="link", up=False, reason=reason)

    def panel_message(self) -> None:
        self.last_panel_msg = time.monotonic()
        self.stats["panel_messages"] += 1

    async def note_seen(self) -> None:
        now = time.monotonic()
        if now - self._last_seen_sent >= 0.5:
            self._last_seen_sent = now
            await self.emit(t="seen")

    # ---- supervision
    async def session(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.emit(t="stat", **self.stats)
            except Exception:  # noqa: BLE001
                log.exception("stats emit failed")

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.ipc.maintain()), asyncio.create_task(self._stats_loop()),
                 asyncio.create_task(self._resnap_loop())]
        delay = self.backoff_initial
        try:
            while True:
                reached = False
                try:
                    reached = await self.session() or False
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("%s session ended: %r", self.name, e)
                await self.link_down(self.down_reason or "session ended")
                self.down_reason = None
                if reached:
                    delay = self.backoff_initial
                self.stats["reconnects"] += 1
                await asyncio.sleep(delay * (1 + random.uniform(0, 0.1)))
                delay = min(delay * 2, self.backoff_max)
        finally:
            for t in tasks:
                t.cancel()
