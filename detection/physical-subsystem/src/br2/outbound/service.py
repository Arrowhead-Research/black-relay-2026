"""Outbound service: REST/SSE (HTTPS), Prometheus metrics, MQTT publisher. Receives only (one-way) from the
normalizer; it has no connection of any kind to a panel or to the adapters."""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from .. import config as C
from ..ipc import RecvOnlyClient
from ..normalizer.store import Reader
from .api import KeyStore, make_app, make_fusion_health_app, make_metrics_app, tls_ctx
from .mqtt_pub import MqttPublisher
from .state import OutState

log = logging.getLogger("br2.outbound")


async def pump(st: OutState, ipc_dir: str, deadline_s: float = 5.0) -> None:
    """The normalizer sends a health frame every second: silence for `deadline_s` means it is hung/dead even though
    the socket is open => treat like EOF (everything stale/offline) and reconnect for a fresh dump."""
    async for m in RecvOnlyClient(os.path.join(ipc_dir, "out.sock"), deadline_s=deadline_s).messages():
        if m.get("t") == "ipc_closed":
            st.counters["ipc_reconnects"] += 1
        st.apply(m)


async def amain(cfg: C.Config, ipc_dir: str | None = None) -> None:
    ipc_dir = ipc_dir or C.ipc_out_dir()
    st = OutState()
    reader = Reader(cfg.db_path)
    keys = KeyStore(cfg.api["keys"], int(cfg.api["rate_limit_per_minute"]))
    app = make_app(st, reader, keys, int(cfg.api.get("sse_max_total", 256)), int(cfg.api.get("sse_max_per_key", 64)),
                   float(cfg.api["sse_idle_drop_s"]) if cfg.api.get("sse_idle_drop_s") is not None else None,
                   sse_probe_bytes=cfg.api.get("sse_probe_bytes"), sse_probe_interval_s=cfg.api.get("sse_probe_interval_s"))
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, cfg.api["host"], int(cfg.api["port"]),
                      ssl_context=tls_ctx(cfg.api["tls_cert"], cfg.api["tls_key"])).start()
    mrunner = web.AppRunner(make_metrics_app(st), access_log=None)
    await mrunner.setup()
    await web.TCPSite(mrunner, cfg.api.get("metrics_host") or "127.0.0.1", int(cfg.api["metrics_port"])).start()
    pub = MqttPublisher(cfg, st, reader)
    log.info("outbound up: https :%s metrics :%s", cfg.api["port"], cfg.api["metrics_port"])
    tasks = [pump(st, ipc_dir, float(cfg.timers.get('ipc_liveness_s', 5))), pub.run()]
    if cfg.fusion.get("enabled") and not os.environ.get("BR2_FUSION_STANDALONE"):
        from .fusion import FusionPublisher
        tasks.append(FusionPublisher(cfg, st, reader).run())
    await asyncio.gather(*tasks)


async def amain_fusion(cfg: C.Config, ipc_dir: str | None = None) -> None:
    """Team output only (compose: its own container on the `fusion` network, because the outbound container uses
    host networking and cannot join a Docker network). Same code, same state files, no API, no local broker."""
    if not cfg.fusion.get("enabled"):
        raise SystemExit("fusion.enabled is false: nothing to do")
    from .fusion import FusionPublisher
    ipc_dir = ipc_dir or C.ipc_out_dir()
    st = OutState()
    pub = FusionPublisher(cfg, st, Reader(cfg.db_path))
    tasks = [pump(st, ipc_dir, float(cfg.timers.get('ipc_liveness_s', 5))), pub.run()]
    port = int(cfg.fusion.get("metrics_port") or 0)
    if port:                                  # 0/null disables; the host defaults to loopback (inside the container)
        host = cfg.fusion.get("metrics_host") or "127.0.0.1"
        runner = web.AppRunner(make_fusion_health_app(st), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("fusion service up (standalone); /metrics and /healthz on %s:%s", host, port)
    else:
        log.info("fusion service up (standalone); metrics endpoint disabled")
    await asyncio.gather(*tasks)
