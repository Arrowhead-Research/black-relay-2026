"""Process supervisor for non-compose runs (and the harness): one OS process per microservice.
In Docker each service is its own container instead (see deploy/docker-compose.yml)."""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import config as C

log = logging.getLogger("br2.supervisor")


def _die_with_parent() -> None:
    """Linux: if the supervisor is SIGKILLed, children get SIGTERM instead of lingering as orphans."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


def services(cfg: C.Config) -> list[str]:
    s = ["normalizer", "outbound"]
    if cfg.iq_enabled:
        s.append("iq-remote")
    if cfg.c4_enabled:
        s.append("control4")
    return s


def supervise(config_path: str, cfg: C.Config) -> None:
    logging.basicConfig(level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s [supervisor] %(message)s", stream=sys.stderr)
    ipc = os.environ.get("BR2_IPC_DIR") or tempfile.mkdtemp(prefix="br2-ipc-")
    os.makedirs(ipc, exist_ok=True)
    os.chmod(ipc, 0o700)
    env = dict(os.environ, BR2_IPC_DIR=ipc, PYTHONUNBUFFERED="1")
    procs: dict[str, subprocess.Popen] = {}
    backoff: dict[str, float] = {}
    started: dict[str, float] = {}
    stop = False

    def spawn(n: str) -> None:
        procs[n] = subprocess.Popen([sys.executable, "-m", "br2", "service", n, "--config", config_path], env=env,
                                    preexec_fn=_die_with_parent)
        started[n] = time.monotonic()
        log.info("started %s pid=%d", n, procs[n].pid)

    def on_sig(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    names = services(cfg)
    for n in names:
        spawn(n)
        if n == "normalizer":
            time.sleep(0.3)
    try:
        while not stop:
            time.sleep(0.25)
            for n in names:
                p = procs[n]
                if p.poll() is not None and not stop:
                    up = time.monotonic() - started[n]
                    backoff[n] = 0.5 if up > 30 else min(backoff.get(n, 0.5) * 2, 15)
                    log.error("%s exited rc=%s; restarting in %.1fs", n, p.returncode, backoff[n])
                    time.sleep(backoff[n])
                    spawn(n)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        end = time.monotonic() + 8
        for p in procs.values():
            try:
                p.wait(max(0.1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()
        if not os.environ.get("BR2_IPC_DIR"):
            shutil.rmtree(ipc, ignore_errors=True)
