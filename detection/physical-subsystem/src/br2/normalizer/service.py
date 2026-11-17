"""Normalizer service: receives adapter feeds (one-way), runs the Engine in a single task, persists to
SQLite, fans results out (one-way) to the outbound service.

IPC intake rules
  * ONE unix socket per adapter (`iq_remote.sock`, `control4.sock`, each in its own directory/volume). The socket a
    peer connected to decides which source it may speak for; `src` inside frames is overwritten, never trusted.
  * the first frame MUST be `hello` naming that source (5 s to send it), otherwise the connection is closed;
  * a source that already has a LIVE connection (any frame within the liveness deadline) refuses a second claim; a
    connection that has gone silent is superseded by a new one;
  * frame types are allow-listed; non-dict / malformed frames end the connection.
Persistence rule: engine state moves only together with its durable Batch (see Engine.commit/rollback)."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import socket
import time

from .. import config as C
from ..ipc import Listener, RecvOnlyConn, SendOnlyConn
from .core import Engine, Batch, PRIMARY, FALLBACK
from .store import Store

log = logging.getLogger("br2.normalizer")

HELLO_TIMEOUT_S = 5.0
ALLOWED_T = frozenset({"hello", "ping", "snapshot", "zone", "zone_removed", "partition", "link", "seen", "stat"})


class NormalizerService:
    def __init__(self, cfg: C.Config, ipc_dir: str | None = None, engine: Engine | None = None):
        self.cfg = cfg
        self.in_dir = ipc_dir          # None => per-adapter dirs from the environment (compose: one volume each)
        self.out_dir = ipc_dir or C.ipc_out_dir()
        self.engine = engine or Engine(cfg)
        self.deadline = self.engine.ipc_deadline
        self.store = Store(cfg.db_path, cfg.storage["retention_days"])
        z, p = self.store.load_cache()
        self._restore_batch = self.engine.restore(z, p, self.store.load_engine_meta())
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self.clients: dict[int, tuple[SendOnlyConn, asyncio.Queue]] = {}
        self._cid = 0
        self._conn: dict[str, dict] = {}              # source -> {"cid", "conn", "last"(mono)}
        self.adapter_listeners: dict[str, Listener] = {}
        for name, enabled in ((PRIMARY, cfg.iq_enabled), (FALLBACK, cfg.c4_enabled)):
            if enabled:
                d = ipc_dir or C.ipc_adapter_dir(name)
                self.adapter_listeners[name] = Listener(os.path.join(d, f"{name}.sock"),
                                                        functools.partial(self._on_adapter, name=name))
        self.out_listener = Listener(os.path.join(self.out_dir, "out.sock"), self._on_outbound)
        self._tasks: list[asyncio.Task] = []
        self.last_seq = self.store.last_seq()
        self.store.prune()
        # supervision / health
        self.ticker_errors = 0
        self.ticker_last_ok = time.monotonic()
        self.engine_errors = 0
        self.persist_failures = 0
        self.task_restarts = 0
        self.refused_claims = 0
        self.rejected_frames = 0
        self._last_err_log = 0.0

    # ---------------------------------------------------------------- IPC in
    async def _on_adapter(self, sock: socket.socket, name: str) -> None:
        conn = RecvOnlyConn(sock)
        self._cid += 1
        cid = self._cid
        registered = False
        try:
            it = conn.frames(timeout=self.deadline * 2).__aiter__()
            try:
                first = await asyncio.wait_for(it.__anext__(), HELLO_TIMEOUT_S)
            except (StopAsyncIteration, asyncio.TimeoutError):
                log.warning("%s socket: peer sent no hello; closing", name)
                return
            if not isinstance(first, dict) or first.get("t") != "hello" or first.get("src") != name:
                self.rejected_frames += 1
                log.warning("%s socket: first frame is not hello(src=%s); closing", name, name)
                return
            cur = self._conn.get(name)
            if cur is not None and time.monotonic() - cur["last"] <= self.deadline:
                self.refused_claims += 1
                log.warning("%s: duplicate claim refused (a live connection already holds it)", name)
                return
            if cur is not None:
                cur["conn"].close()                      # silent/dead holder: superseded
            self._conn[name] = {"cid": cid, "conn": conn, "last": time.monotonic()}
            registered = True
            self.engine.ipc_up(name)
            await self.inbox.put(("msg", {"t": "hello", "src": name}))
            async for m in it:
                if not isinstance(m, dict) or m.get("t") not in ALLOWED_T:
                    self.rejected_frames += 1
                    log.warning("%s: bad frame, closing connection", name)
                    return
                self._conn[name]["last"] = time.monotonic()
                self.engine.touch(name)
                if m["t"] == "ping":
                    continue
                m["src"] = name
                await self.inbox.put(("msg", m))
        except (ValueError, OSError, asyncio.TimeoutError) as e:
            log.warning("%s ipc ended: %r", name, e)
        finally:
            conn.close()
            if registered and self._conn.get(name, {}).get("cid") == cid:
                del self._conn[name]
                await self.inbox.put(("down", (name, cid)))

    # ---------------------------------------------------------------- IPC out
    async def _on_outbound(self, sock: socket.socket) -> None:
        conn = SendOnlyConn(sock)
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._cid += 1
        cid = self._cid
        # dump is queued BEFORE registration is visible to the processor => no batch can precede it
        q.put_nowait(self._dump())
        self.clients[cid] = (conn, q)
        try:
            while True:
                m = await q.get()
                if m is None:
                    break
                await conn.send(m)
        except OSError:
            pass
        finally:
            self.clients.pop(cid, None)
            conn.close()

    def _dump(self) -> dict:
        e = self.engine
        return {"t": "dump", "zones": list(e.zones.values()), "partitions": list(e.parts.values()),
                "status": e.status, "health": self._health(), "last_seq": self.last_seq}

    def _health(self) -> dict:
        h = self.engine.health()
        h["last_seq"] = self.last_seq
        now = time.monotonic()
        h["normalizer"] = {
            "healthy": (now - self.ticker_last_ok < 5.0 and not self.engine.resync_needed),
            "ticker_errors": self.ticker_errors, "engine_errors": self.engine_errors,
            "persist_failures": self.persist_failures, "task_restarts": self.task_restarts,
            "refused_claims": self.refused_claims, "rejected_frames": self.rejected_frames,
            "ticker_age_s": round(now - self.ticker_last_ok, 2),
        }
        return h

    def _broadcast(self, m: dict) -> None:
        for cid, (conn, q) in list(self.clients.items()):
            try:
                q.put_nowait(m)
            except asyncio.QueueFull:
                log.warning("outbound client too slow; dropping (it will resync via dump)")
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)  # sentinel: sender task exits and closes; client reconnects + gets a dump
                self.clients.pop(cid, None)

    # ---------------------------------------------------------------- processing
    def _log_rate_limited(self, what: str) -> None:
        now = time.monotonic()
        if now - self._last_err_log > 5.0:
            self._last_err_log = now
            log.exception("%s", what)

    def process_batch(self, b: Batch) -> bool:
        """Persist atomically, THEN let the engine's new state stand, THEN publish. On a persistence failure the
        engine is rolled back to its previous state (nothing leaks to memory, nothing is broadcast) and a full resync
        is scheduled for the next tick. Returns True if the batch is durable."""
        if b.empty():
            self.engine.commit()
            return True
        try:
            evs = self.store.commit(b.events, b.zones, b.removed_zones, b.partitions, b.removed_partitions,
                                    meta=b.meta or None)
        except Exception:  # noqa: BLE001
            self.persist_failures += 1
            self._log_rate_limited("persisting batch failed; engine state rolled back, resync scheduled")
            self.engine.rollback()
            return False
        self.engine.commit()
        if evs:
            self.last_seq = evs[-1]["seq"]
        self._broadcast({"t": "batch", "events": evs, "zones": list(b.zones.values()),
                         "removed_zones": b.removed_zones, "partitions": list(b.partitions.values()),
                         "removed_partitions": b.removed_partitions, "status": self.engine.status,
                         "health": self._health()})
        return True

    async def _run_engine(self) -> None:
        while True:
            kind, payload = await self.inbox.get()
            try:
                if kind == "msg":
                    self.engine.touch(payload["src"])
                    b = self.engine.handle(payload)
                elif kind == "down":
                    name, cid = payload
                    b = self.engine.ipc_down(name)
                else:
                    continue
            except Exception:  # noqa: BLE001  (malformed frame content: drop it, keep the previous state)
                self.engine_errors += 1
                self._log_rate_limited(f"engine failure processing {kind}")
                self.engine.rollback(resync=False)
                continue
            self.process_batch(b)

    async def _ticker(self) -> None:
        """Supervised: one failing iteration is logged and counted, never ends the loop."""
        last_health = 0.0
        last_prune = -1e9
        prev = time.monotonic()
        while True:
            await asyncio.sleep(0.25)
            try:
                now = time.monotonic()
                if now - prev > 1.5:
                    self.engine.grant_grace()        # we were stalled: unread frames are queued, not missing
                prev = now
                self.process_batch(self.engine.tick())
                if now - last_health >= 1.0:
                    last_health = now
                    self._broadcast({"t": "health", "health": self._health(), "status": self.engine.status})
                if now - last_prune >= 3600:
                    last_prune = now
                    n = self.store.prune()
                    if n:
                        log.info("pruned %d events", n)
                self.ticker_last_ok = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.ticker_errors += 1
                self._log_rate_limited("ticker iteration failed")
                try:
                    self.engine.rollback()
                except Exception:  # noqa: BLE001
                    pass

    async def _supervised(self, factory, label: str) -> None:
        while True:
            try:
                await factory()
                log.error("task %s returned; restarting", label)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001
                log.exception("task %s died; restarting", label)
            self.task_restarts += 1
            await asyncio.sleep(0.5)

    async def start(self) -> None:
        # the restored cache is stale until a source vouches for it: persist that (zone.stale events) first
        self.process_batch(self._restore_batch)
        for lst in self.adapter_listeners.values():
            await lst.start()
        await self.out_listener.start()
        self._tasks = [asyncio.create_task(self._supervised(self._run_engine, "engine")),
                       asyncio.create_task(self._supervised(self._ticker, "ticker"))]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for lst in self.adapter_listeners.values():
            await lst.stop()
        await self.out_listener.stop()
        self.store.close()


async def amain(cfg: C.Config) -> None:
    for n in (PRIMARY, FALLBACK):
        os.makedirs(C.ipc_adapter_dir(n), exist_ok=True)
    os.makedirs(C.ipc_out_dir(), exist_ok=True)
    svc = NormalizerService(cfg)
    await svc.start()
    log.info("normalizer up; last_seq=%d", svc.last_seq)
    await asyncio.Event().wait()
