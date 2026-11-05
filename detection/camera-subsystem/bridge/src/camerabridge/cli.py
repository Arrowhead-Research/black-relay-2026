"""``camerabridge run --config <path>`` — construct and run the edge bridge.

The CLI is the only place broker/Frigate/outbox settings turn into live objects.
It reads a YAML config (``config/bridge.example.yaml`` is the annotated template),
builds the durable :class:`~camerabridge.outbox.Outbox`, the
:class:`~camerabridge.cues.CueMatcher` over this node's covered zones, the NTP-gated
:class:`~camerabridge.formats.SystemClock`, and the :class:`~camerabridge.bridge.Bridge`
over two real paho MQTT clients (central fusion broker and node-local Frigate
broker), then runs both network loops and a periodic status heartbeat.

Packaging: this module is the ``camerabridge`` console-script entry declared in
``pyproject.toml`` ``[project.scripts]`` (with a matching ``python -m camerabridge``
entry in ``__main__.py``). The project gained a ``[build-system]`` so the script
installs; ``uv run camerabridge run --config <path>`` works.

Clock gating (DESIGN.md 7.5): a Pi 5 has no battery RTC, so a node booting before
NTP sync must not stamp an untrusted time. :func:`mark_clock_if_synced` confirms
NTP (``timedatectl``) and only then marks the clock ready; until then the bridge
holds durable events in memory and drops ``object_update`` and status. The check is
re-run each heartbeat, so a late NTP sync releases the held events and flips the
node to publishing without a restart.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Sequence

import yaml

from . import formats
from .bridge import Bridge
from .cues import CueMatcher
from .health import FrigateHealth
from .outbox import Outbox

logger = logging.getLogger("camerabridge")

__all__ = ["load_config", "make_bridge", "mark_clock_if_synced", "start_client", "main"]

# Frigate publishes to a broker on the node itself (DESIGN.md 4.1).
_DEFAULT_LOCAL_HOST = "127.0.0.1"
_DEFAULT_MQTT_PORT = 1883

# Outbox overflow caps: defaults sized for a node riding out a multi-day uplink
# outage without unbounded disk use (overridable via config).
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_AGE_S = 7 * 24 * 3600
_DEFAULT_STATUS_INTERVAL_S = 30.0
_DEFAULT_CUE_WINDOW_S = 5.0

# Frigate stats older than this many status intervals count as stale (fail safe).
_STATS_STALE_INTERVALS = 3

# Every camera.* message carries node_id, and the contract pins it to this pattern
# (schemas/*.schema.json). A bad field-provisioned value (uppercase, underscore,
# leading hyphen) would make every message schema-invalid and be silently dropped
# by fusion — so it is validated once, loudly, at startup. The pattern carries no
# `$` and is applied with re.fullmatch so a trailing newline ("cam-01\n") is
# rejected, matching the JSON-schema ECMA `$` (Python's `$` would let it pass).
_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _validate_node_id(node_id: Any) -> str:
    """Return ``node_id`` if it matches the contract pattern, else raise ValueError."""
    if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError(
            f"node_id {node_id!r} is invalid: must match ^[a-z0-9][a-z0-9-]*$ "
            "(lowercase letters, digits and hyphens; no leading hyphen). Fix the "
            "node_id in the config before starting the service."
        )
    return node_id


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
    central_client: Any,
    local_client: Any,
    clock: Optional[formats.Clock] = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> Bridge:
    """Construct a :class:`Bridge` from parsed config and an injected MQTT client.

    Pure wiring — no network. Both clients and ``clock`` are injected so tests build
    a bridge without a broker or a real wall clock. Reads ``node_id``,
    ``covered_zones``, ``cue_window_s``, ``status_interval_s``, the ``outbox``
    path/caps, and the ``frigate`` topic/base URL/camera name.
    """
    node_id = _validate_node_id(cfg["node_id"])
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
    frigate_topic = frigate_cfg.get("topic", "frigate/events")
    camera = frigate_cfg.get("camera")
    if not camera:
        logger.warning(
            "frigate.camera is not set: camera_online and recording_ok will always "
            "report false. Set it to the Frigate camera name."
        )
    status_interval = float(cfg.get("status_interval_s", _DEFAULT_STATUS_INTERVAL_S))
    health = FrigateHealth(
        camera=camera,
        stale_after_s=_STATS_STALE_INTERVALS * status_interval,
        # Frigate's health topics share the events topic's prefix (mqtt.topic_prefix).
        topic_prefix=frigate_topic.rsplit("/", 1)[0],
        monotonic_fn=monotonic_fn,
    )

    return Bridge(
        central_client=central_client,
        local_client=local_client,
        outbox=outbox,
        matcher=matcher,
        clock=clock or formats.SystemClock(),
        node_id=node_id,
        frigate_topic=frigate_topic,
        frigate_base_url=frigate_cfg.get("base_url", ""),
        # Age pending cues on the same horizon as the matcher's track window so the
        # two bound together over the node's uptime.
        pending_ttl_s=window_s,
        health=health,
    )


def _build_central_client(cfg: dict) -> Any:
    """Build the paho client for the central broker (host/port/creds/TLS).

    Imported lazily so unit tests (which inject fakes) need no paho and no broker.
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


def _build_local_client(cfg: dict) -> Any:
    """Build the paho client for the node-local broker Frigate publishes to."""
    import paho.mqtt.client as mqtt  # lazy: only the live service needs paho

    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{cfg.get('node_id')}-local",
    )


def _central_endpoint(cfg: dict) -> tuple[str, int]:
    """Return the central broker's (host, port); ``broker.host`` is required.

    A default here would silently send fusion traffic to whatever listens on that
    address and report the uplink as healthy.
    """
    broker = cfg.get("broker") or {}
    host = broker.get("host")
    if not host:
        raise ValueError("broker.host is required: set the central broker address in the config")
    return host, int(broker.get("port", _DEFAULT_MQTT_PORT))


def mark_clock_if_synced(clock: formats.Clock) -> bool:
    """Mark ``clock`` NTP-synced iff the system reports NTP synchronization.

    Returns whether the clock is (now) synced. A :class:`SystemClock` exposes
    ``is_synced`` / ``mark_synced``; any other injected clock is treated as already
    trusted. The NTP state is read from ``timedatectl`` and any failure is treated
    as not-synced (fail safe: keep holding rather than stamp an untrusted time).
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
    logger.warning("NTP not yet synchronized; holding durable events until it is")
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


def start_client(client: Any, host: str, port: int, *, min_delay: int = 1, max_delay: int = 60) -> None:
    """Start the MQTT loop with an async connect that survives a broker-down boot.

    Uses ``connect_async`` (which does NOT raise when the broker is unreachable)
    plus ``reconnect_delay_set`` so the paho network thread keeps retrying with
    backoff. A broker outage is a documented degradation case (DESIGN.md 4.3): the
    node must stay up detecting/recording and queueing durable events, not exit at
    boot because a broker happens to be down. ``loop_start`` runs the retrying
    network thread. Each client gets its own call, so each retries independently.
    """
    client.reconnect_delay_set(min_delay=min_delay, max_delay=max_delay)
    client.connect_async(host, port)
    client.loop_start()


def _loop_alive(client: Any) -> bool:
    """Return whether the client's paho network thread is running.

    paho has no public accessor. A paho upgrade that renames ``_thread`` reads as dead,
    which restarts the service loudly (and fails the real-broker test) rather than
    leaving a dead thread unnoticed.
    """
    thread = getattr(client, "_thread", None)
    return thread is not None and thread.is_alive()


def _failure(bridge: Bridge, central: Any, local: Any) -> Optional[str]:
    """Return why the service can no longer run and must restart, or ``None``."""
    if bridge.fatal:
        return "outbox I/O failed in an MQTT callback"
    if not _loop_alive(central):
        return "the central MQTT network thread died"
    if not _loop_alive(local):
        return "the local MQTT network thread died"
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``camerabridge run --config <path>``.

    Exits nonzero if a callback hit an outbox I/O error, a network thread died, or the
    heartbeat hit an I/O error, so the service manager restarts the bridge. A dead
    thread would otherwise leave a node reporting a stale ``link_online``. The service
    unit therefore needs ``Restart=always``, ``RestartSec=5`` and ``StartLimitIntervalSec=0``.
    """
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
    central_host, central_port = _central_endpoint(cfg)
    frigate_cfg = cfg.get("frigate", {})
    central = _build_central_client(cfg)
    local = _build_local_client(cfg)
    bridge = make_bridge(cfg, central_client=central, local_client=local, clock=clock)
    bridge.wire()

    status_interval = float(cfg.get("status_interval_s", _DEFAULT_STATUS_INTERVAL_S))

    mark_clock_if_synced(clock)
    # Async connect + auto-reconnect: stay up and retry if either broker is down now.
    start_client(central, central_host, central_port)
    start_client(
        local,
        frigate_cfg.get("mqtt_host", _DEFAULT_LOCAL_HOST),
        int(frigate_cfg.get("mqtt_port", _DEFAULT_MQTT_PORT)),
    )
    logger.info("camerabridge running for node %s", cfg["node_id"])
    try:
        while True:
            # Publish the heartbeat (and retire stale tracks/pending cues) at the
            # TOP of the loop so a node is not status-silent for a whole interval at
            # boot; then sleep until the next tick.
            failure = _failure(bridge, central, local)
            if failure:
                logger.critical("exiting for a restart: %s", failure)
                return 1
            mark_clock_if_synced(clock)
            bridge.publish_status(bridge.node_state())
            bridge.expire_tracks()
            time.sleep(status_interval)
    except KeyboardInterrupt:
        logger.info("shutting down")
    except OSError:
        logger.exception("exiting for a restart: outbox I/O failed in the heartbeat")
        return 1
    finally:
        for client in (central, local):
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
