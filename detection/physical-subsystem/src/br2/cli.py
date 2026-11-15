"""Entry points.  `python -m br2 run --config X`  = supervisor (all services as separate OS processes).
`python -m br2 service <iq-remote|control4|normalizer|outbound> --config X` = a single service (containers)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config as C


def _logging(level: str, svc: str) -> None:
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO),
                        format=f"%(asctime)s %(levelname)s [{svc}] %(name)s: %(message)s", stream=sys.stderr)


def run_service(name: str, cfg: C.Config) -> None:
    _logging(cfg["log_level"], name)
    from .keyperm import check_startup, KeyPermissionError
    try:
        check_startup(name, cfg)
    except KeyPermissionError as e:
        raise SystemExit(f"refusing to start {name}: {e}")
    if name == "iq-remote":
        from .adapters.iq_remote import IQRemoteAdapter
        coro = IQRemoteAdapter(cfg).run()
    elif name == "control4":
        from .adapters.control4 import Control4Adapter
        coro = Control4Adapter(cfg).run()
    elif name == "normalizer":
        import os
        os.umask(0o007)      # db/-wal/-shm group-writable for outbound's read-only WAL access (shared gid 10000)
        from .normalizer.service import amain
        coro = amain(cfg)
    elif name == "outbound":
        from .outbound.service import amain as oamain
        coro = oamain(cfg)
    elif name == "fusion":
        from .outbound.service import amain_fusion
        coro = amain_fusion(cfg)
    else:
        raise SystemExit(f"unknown service {name}")
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        pass


def hash_key() -> None:
    import secrets
    from argon2 import PasswordHasher
    from .outbound.api import key_id
    key = "br_" + secrets.token_urlsafe(32)
    print("API key (shown once):", key)
    print("key_id:", key_id(key))
    print("argon2_hash:", PasswordHasher().hash(key))


def healthcheck(what: str, cfg: C.Config) -> int:
    """Container healthcheck: exit 0 healthy, 1 not. `fusion` asks the standalone fusion service's /healthz."""
    if what != "fusion":
        print(f"unknown healthcheck {what!r}", file=sys.stderr)
        return 2
    import urllib.request
    port = int(cfg.fusion.get("metrics_port") or 0)
    if not port:
        print("fusion.metrics_port is 0: no health endpoint configured", file=sys.stderr)
        return 1
    host = cfg.fusion.get("metrics_host") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=3).read()
    except Exception as e:  # noqa: BLE001  (HTTPError 503 included)
        print(f"unhealthy: {e}", file=sys.stderr)
        return 1
    return 0


def _field_main(a, cfg: C.Config) -> int:
    """pair / snapshot / watch: operator commands that talk to the panel (read-only; see pairing.py, field.py)."""
    import logging as _l
    for n in ("aiomqtt", "zeroconf", "asyncio"):
        _l.getLogger(n).setLevel(_l.CRITICAL)
    _logging("WARNING", a.cmd)
    from . import field as F
    try:
        st = F.iq_settings(cfg, a.panel_ip, a.pki_dir, a.plugin_ip, getattr(a, "panel_port", None), a.ca_file)
        import os
        # Files written by a command run as root belong to the service uid (iq_remote.owner, default 10001:10000, or
        # `dir`); --owner overrides. Not root: the files belong to whoever runs the command.
        owner = getattr(a, "owner", None) or (st.get("owner") if hasattr(os, "geteuid") and os.geteuid() == 0 else None)
        st["owner"] = owner or st.get("owner")
        if a.cmd == "pair":
            st["pin_source"] = "pair"
            st["pin_mode"] = "tofu"          # the operator is physically at the panel, tapping Pair: this IS the first use
            from . import pairing as P
            return asyncio.run(P.run_pair(st, timeout_s=a.timeout, pairing_port=a.pairing_port, owner=owner,
                                          verify=not a.no_verify))
        if a.cmd == "repin":
            return asyncio.run(F.run_repin(st, yes=a.yes))
        if a.cmd == "snapshot":
            return asyncio.run(F.run_snapshot(st))
        return asyncio.run(F.run_watch(st, a.minutes))
    except F.FieldError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Nothing was changed on the panel.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        from .adapters.base import OneWayViolation
        kind = "write guard blocked a panel write" if isinstance(e, OneWayViolation) else "failed"
        print(f"ERROR: {kind}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="br2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--config", required=True)
    s = sub.add_parser("service"); s.add_argument("name"); s.add_argument("--config", required=True)
    sub.add_parser("hash-key")
    h = sub.add_parser("healthcheck", help="exit 0 if healthy (container healthcheck)")
    h.add_argument("what", choices=["fusion"]); h.add_argument("--config", required=True)
    for name, helptxt in (("pair", "one-time IQ Remote pairing (operator taps Pair on the panel)"),
                          ("repin", "accept the panel's CURRENT TLS certificate as the pin (after a legitimate change)"),
                          ("snapshot", "connect, print zones/partitions as JSON, exit"),
                          ("watch", "connect and print panel changes for --minutes")):
        c = sub.add_parser(name, help=helptxt)
        c.add_argument("--config", required=True)
        c.add_argument("--panel-ip"); c.add_argument("--pki-dir"); c.add_argument("--plugin-ip")
        c.add_argument("--ca-file", help="panel CA override (a simulated panel only)")
        if name != "pair":
            c.add_argument("--panel-port", type=int)
        if name == "repin":
            c.add_argument("--yes", action="store_true", help="really replace the pin (without it: print old/new only)")
        if name == "pair":
            c.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for Pair on the panel")
            c.add_argument("--pairing-port", type=int, default=0, help="fixed TCP port for the pairing server")
            c.add_argument("--owner", help="UID:GID (or `dir`) to hand the stored files to; default iq_remote.owner (10001:10000) when run as root")
            c.add_argument("--no-verify", action="store_true", help="skip the read-only session after pairing")
        if name == "watch":
            c.add_argument("--minutes", type=float, default=10.0)
    a = ap.parse_args(argv)
    if a.cmd == "hash-key":
        hash_key(); return
    cfg = C.load(a.config)
    if a.cmd == "healthcheck":
        raise SystemExit(healthcheck(a.what, cfg))
    if a.cmd in ("pair", "snapshot", "watch", "repin"):
        raise SystemExit(_field_main(a, cfg))
    if a.cmd == "service":
        run_service(a.name, cfg)
    else:
        from .supervisor import supervise
        supervise(a.config, cfg)
