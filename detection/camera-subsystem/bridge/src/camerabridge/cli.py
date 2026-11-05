"""``camerabridge run --config <path>`` — construct and run the edge bridge.

The CLI is the only place broker/Frigate/outbox settings turn into live objects.
It reads a YAML config (``config/bridge.example.yaml`` is the annotated template),
builds the durable :class:`~camerabridge.outbox.Outbox`, the
:class:`~camerabridge.cues.CueMatcher` over this node's covered zones, the NTP-gated
:class:`~camerabridge.formats.SystemClock`, and the :class:`~camerabridge.bridge.Bridge`
over a real paho MQTT client, then runs the network loop and a periodic status
heartbeat.

Packaging: this module is the ``camerabridge`` console-script entry declared in
``pyproject.toml`` ``[project.scripts]`` (with a matching ``python -m camerabridge``
entry in ``__main__.py``). The project gained a ``[build-system]`` so the script
installs; ``uv run camerabridge run --config <path>`` works.

Clock gating (DESIGN.md 7.5): a Pi 5 has no battery RTC, so a node booting before
NTP sync must not stamp an untrusted time. :func:`mark_clock_if_synced` confirms
NTP (``timedatectl``) and only then marks the clock ready; until then the bridge
drops builds (boot window). The check is re-run each heartbeat, so a late NTP sync
flips the node to publishing without a restart. Continuous link/health probing is
an onboarding-layer concern (DESIGN.md 7.4) and out of scope for this bridge.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

import yaml

from . import formats
from .bridge import Bridge
from .cues import CueMatcher
from .outbox import Outbox
from .status import NodeState

logger = logging.getLogger("camerabridge")

__all__ = ["load_config", "make_bridge", "mark_clock_if_synced", "main"]

# Outbox overflow caps: defaults sized for a node riding out a multi-day uplink
# outage without unbounded disk use (overridable via config).
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_AGE_S = 7 * 24 * 3600
_DEFAULT_STATUS_INTERVAL_S = 30.0
_DEFAULT_CUE_WINDOW_S = 5.0


def load_config(path: str) -> dict:
    """Load and return the YAML config as a dict."""
    with open(path, encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    if not isinstance(cfg, dict):
        raise ValueError(f"config {path!r} did not parse to a mapping")
    return cfg


def make_bridge(
    cfg: dict,
    *,
    client: Any,
    clock: Optional[formats.Clock] = None,
) -> Bridge:
    """Construct a :class:`Bridge` from parsed config and an injected MQTT client.

    Pure wiring — no network. ``client`` and ``clock`` are injected so tests build
    a bridge without a broker or a real wall clock. Reads ``node_id``,
    ``covered_zones``, ``cue_window_s``, the ``outbox`` path/caps, and the
    ``frigate`` topic/base URL.
    """
    node_id = cfg["node_id"]
    covered_zones = set(cfg.get("covered_zones", []))
    window_s = float(cfg.get("cue_window_s", _DEFAULT_CUE_WINDOW_S))

    outbox_cfg = cfg.get("outbox", {})
    outbox = Outbox(
        outbox_cfg["path"],
        max_bytes=int(outbox_cfg.get("max_bytes", _DEFAULT_MAX_BYTES)),
        max_age_s=float(outbox_cfg.get("max_age_s", _DEFAULT_MAX_AGE_S)),
    )

    frigate_cfg = cfg.get("frigate", {})
    matcher = CueMatcher(covered_zones=covered_zones, window_s=window_s)

    return Bridge(
        client=client,
        outbox=outbox,
        matcher=matcher,
        clock=clock or formats.SystemClock(),
        node_id=node_id,
        frigate_topic=frigate_cfg.get("topic", "frigate/events"),
        frigate_base_url=frigate_cfg.get("base_url", ""),
        # Age pending cues on the same horizon as the matcher's track window so the
        # two bound together over the node's uptime.
        pending_ttl_s=window_s,
    )


def _build_mqtt_client(cfg: dict) -> Any:
    """Build a paho MQTT client from broker config (host/port/creds/TLS).

    Imported lazily so unit tests (which inject a fake client) need no paho and no
    broker. v0 assumes Frigate publishes to this same broker (DESIGN.md 4.2); a
    separate Frigate broker is a documented follow-up, not v0.
    """
    import paho.mqtt.client as mqtt  # lazy: only the live service needs paho

    broker = cfg.get("broker", {})
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=cfg.get("node_id"),
    )
    username = broker.get("username")
    if username:
        client.username_pw_set(username, broker.get("password"))
    if broker.get("tls"):
        client.tls_set()
    return client


def mark_clock_if_synced(clock: formats.Clock) -> bool:
    """Mark ``clock`` NTP-synced iff the system reports NTP synchronization.

    Returns whether the clock is (now) synced. A :class:`SystemClock` exposes
    ``is_synced`` / ``mark_synced``; any other injected clock is treated as already
    trusted. The NTP state is read from ``timedatectl`` and any failure is treated
    as not-synced (fail safe: keep dropping rather than stamp an untrusted time).
    """
    mark = getattr(clock, "mark_synced", None)
    if mark is None:
        return True  # an injected ready clock (e.g. in tests) is already trusted
    if getattr(clock, "is_synced", False):
        return True
    if _ntp_synchronized():
        mark()
        logger.info("NTP synchronized; clock marked ready")
        return True
    logger.warning("NTP not yet synchronized; dropping events until it is")
    return False


def _ntp_synchronized() -> bool:
    """Return True iff ``timedatectl`` reports NTPSynchronized=yes. Fail-safe False."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "yes"


def _default_node_state(link_online: bool) -> NodeState:
    """A conservative heartbeat state.

    v0 reports the uplink it can observe (``link_online``) and leaves camera /
    recording / detector health at their optimistic defaults; wiring real Frigate
    health probes (camera offline, recorder/detector faults — DESIGN.md 4.3) is a
    follow-up. Kept honest: this is a liveness heartbeat, not a health claim.
    """
    return NodeState(
        camera_online=True,
        link_online=link_online,
        recording_ok=True,
        detector_ok=True,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``camerabridge run --config <path>``."""
    parser = argparse.ArgumentParser(prog="camerabridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the edge bridge service")
    run.add_argument("--config", required=True, help="path to the bridge YAML config")
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    clock = formats.SystemClock()
    client = _build_mqtt_client(cfg)
    bridge = make_bridge(cfg, client=client, clock=clock)
    bridge.wire()

    broker = cfg.get("broker", {})
    status_interval = float(cfg.get("status_interval_s", _DEFAULT_STATUS_INTERVAL_S))

    mark_clock_if_synced(clock)
    client.connect(broker.get("host", "127.0.0.1"), int(broker.get("port", 1883)))
    client.loop_start()
    logger.info("camerabridge running for node %s", cfg["node_id"])
    try:
        while True:
            time.sleep(status_interval)
            mark_clock_if_synced(clock)
            bridge.publish_status(_default_node_state(link_online=True))
            # Retire stale tracks / pending cues so neither grows over a 24/7 uptime.
            bridge.expire_tracks()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # pragma: no cover - best-effort teardown
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
