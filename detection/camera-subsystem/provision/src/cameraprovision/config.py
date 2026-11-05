"""The node config model, its validation, and the bridge-config writer.

``NodeConfig`` nests the same sections as ``bridge/config/bridge.example.yaml``, and
its defaults are that file's values (a test holds them equal). It also carries the
fields only provisioning uses: WiFi credentials, hostname, Tailscale key, and the
MediaMTX address. Those never reach the bridge YAML; ``_bridge_mapping`` is the one
place that decides what does.

Secrets stay out of every rendering: their fields are excluded from ``repr``, and
``validate`` names the offending field without quoting any value.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "BrokerConfig",
    "FrigateConfig",
    "NodeConfig",
    "OutboxConfig",
    "validate",
    "write_bridge_config",
]

_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_RFC1123_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_HTTP_URL_RE = re.compile(r"https?://\S+")
# WPA2: an 8-63 character printable-ASCII passphrase, or a raw 64-digit hex key.
_WPA_PSK_RE = re.compile(r"[\x20-\x7e]{8,63}|[0-9a-fA-F]{64}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SSID_MAX_BYTES = 32


@dataclass(frozen=True, kw_only=True)
class BrokerConfig:
    """The ``broker`` section: the central fusion MQTT broker."""

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 1883
    tls: bool = False


@dataclass(frozen=True, kw_only=True)
class OutboxConfig:
    """The ``outbox`` section: the bridge's durable on-disk queue."""

    path: str = "/var/lib/camerabridge/outbox.jsonl"
    max_bytes: int = 67108864
    max_age_s: int = 604800


@dataclass(frozen=True, kw_only=True)
class FrigateConfig:
    """The ``frigate`` section. ``mqtt_host`` and ``mqtt_port`` are not portal fields.

    ``camera`` defaults to the node's ``node_id``, because in v0 camera_id == node_id.
    """

    base_url: str
    topic: str = "frigate/events"
    camera: str | None = None
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883


@dataclass(frozen=True, kw_only=True)
class NodeConfig:
    """Everything one node is provisioned with."""

    # Bridge keys.
    node_id: str
    broker: BrokerConfig
    covered_zones: list[str] | tuple[str, ...]
    frigate: FrigateConfig
    outbox: OutboxConfig = field(default_factory=OutboxConfig)
    cue_window_s: float = 5.0
    status_interval_s: float = 30

    # Provisioning only: never written to the bridge config.
    hostname: str
    wifi_ssid: str
    wifi_psk: str = field(repr=False)
    mediamtx_address: str
    tailscale_auth_key: str = field(repr=False)
    vpn_requested: bool = True


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _is_positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _is_ssid(value: Any) -> bool:
    return (
        _has_text(value)
        and not _CONTROL_CHAR_RE.search(value)
        and len(value.encode("utf-8", "replace")) <= _SSID_MAX_BYTES
    )


def _zones_valid(zones: Any) -> bool:
    return (
        isinstance(zones, (list, tuple))
        and len(zones) > 0
        and all(_has_text(zone) for zone in zones)
    )


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate(cfg: NodeConfig) -> list[str]:
    """Return every problem with ``cfg`` as readable text; empty means valid.

    Messages name fields and rules, and never quote a value, so they are safe to log
    or show on the portal whichever field is wrong.
    """
    broker, outbox, frigate = cfg.broker, cfg.outbox, cfg.frigate
    checks = [
        (
            _matches(_NODE_ID_RE, cfg.node_id),
            "node_id must be lowercase letters, digits and hyphens, with no leading hyphen",
        ),
        (_has_text(broker.host), "broker.host is required"),
        (_is_port(broker.port), "broker.port must be a whole number from 1 to 65535"),
        (_has_text(broker.username), "broker.username is required"),
        (_has_text(broker.password), "broker.password is required"),
        (isinstance(broker.tls, bool), "broker.tls must be true or false"),
        (_zones_valid(cfg.covered_zones), "covered_zones needs at least one non-empty zone"),
        (_has_text(outbox.path), "outbox.path is required"),
        (_is_positive_number(outbox.max_bytes), "outbox.max_bytes must be a positive number"),
        (_is_positive_number(outbox.max_age_s), "outbox.max_age_s must be a positive number"),
        (_is_positive_number(cfg.cue_window_s), "cue_window_s must be a positive number"),
        (_is_positive_number(cfg.status_interval_s), "status_interval_s must be a positive number"),
        (_has_text(frigate.topic), "frigate.topic is required"),
        (_matches(_HTTP_URL_RE, frigate.base_url), "frigate.base_url must be an http(s) URL"),
        (_has_text(frigate.mqtt_host), "frigate.mqtt_host is required"),
        (_is_port(frigate.mqtt_port), "frigate.mqtt_port must be a whole number from 1 to 65535"),
        (
            _matches(_RFC1123_LABEL_RE, cfg.hostname),
            "hostname must be 1-63 letters, digits or hyphens, "
            "and must not start or end with a hyphen",
        ),
        (_is_ssid(cfg.wifi_ssid), "wifi_ssid is required: at most 32 bytes, no control characters"),
        (
            _matches(_WPA_PSK_RE, cfg.wifi_psk),
            "wifi_psk must be 8-63 printable ASCII characters or 64 hex digits",
        ),
        (_has_text(cfg.mediamtx_address), "mediamtx_address is required"),
        (
            not cfg.vpn_requested or _has_text(cfg.tailscale_auth_key),
            "tailscale_auth_key is required when the VPN is requested",
        ),
    ]
    return [message for ok, message in checks if not ok]


def _bridge_mapping(cfg: NodeConfig) -> dict[str, Any]:
    """The bridge config, with the keys and order of ``bridge.example.yaml``."""
    return {
        "node_id": cfg.node_id,
        "broker": {
            "host": cfg.broker.host,
            "port": cfg.broker.port,
            "username": cfg.broker.username,
            "password": cfg.broker.password,
            "tls": cfg.broker.tls,
        },
        "covered_zones": list(cfg.covered_zones),
        "cue_window_s": cfg.cue_window_s,
        "outbox": {
            "path": cfg.outbox.path,
            "max_bytes": cfg.outbox.max_bytes,
            "max_age_s": cfg.outbox.max_age_s,
        },
        "status_interval_s": cfg.status_interval_s,
        "frigate": {
            "topic": cfg.frigate.topic,
            "base_url": cfg.frigate.base_url,
            "camera": cfg.frigate.camera or cfg.node_id,
            "mqtt_host": cfg.frigate.mqtt_host,
            "mqtt_port": cfg.frigate.mqtt_port,
        },
    }


def _write_private_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, mode 0600, surviving a power cut at any point.

    The file is 0600 from creation, so the secret is never briefly world-readable. A
    crash leaves the old file or the whole new one. Syncing the directory makes the
    rename itself durable, so a node cannot reboot into a lost config.
    """
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_bridge_config(cfg: NodeConfig, path: str | os.PathLike[str]) -> None:
    """Validate ``cfg`` and atomically write the bridge's YAML config, mode 0600.

    Raises ``ValueError`` listing the problems, before touching the disk, if ``cfg``
    is invalid. The message is built only from :func:`validate`, so it carries no
    secret.
    """
    errors = validate(cfg)
    if errors:
        raise ValueError("invalid node config: " + "; ".join(errors))
    try:
        text = yaml.safe_dump(_bridge_mapping(cfg), sort_keys=False, allow_unicode=True)
    except yaml.YAMLError:
        # PyYAML's message quotes the value it could not represent.
        raise ValueError("node config holds a value that cannot be written as YAML") from None
    _write_private_atomic(Path(path), text)
