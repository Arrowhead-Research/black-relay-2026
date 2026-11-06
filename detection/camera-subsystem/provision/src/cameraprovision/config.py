"""The node config model, its validation, and the bridge-config writer.

``NodeConfig`` nests the same sections as ``bridge/config/bridge.example.yaml``, and
its defaults are that file's values (a test holds them equal). It also carries the
fields only provisioning uses: WiFi credentials, hostname, the picture flip, and the
anonymous-broker choice. Those never reach the bridge YAML; ``_bridge_mapping`` is the
one place that decides what does. ``mediamtx_address`` is in that file for go2rtc,
and the bridge ignores it.

Secrets stay out of every rendering: their fields are excluded from ``repr``, and
``validate`` names the offending field without quoting any value.

Two levels of validation. :func:`validate` is what a save must pass. :func:`validate_saved`
is what a config on disk must pass to be used, and is looser: a node provisioned before a
rule existed keeps working and shows its values on the form, and only a new save has to
meet the new rule.
"""

from __future__ import annotations

import ipaddress
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
    "effective_ntp_server",
    "frigate_problems",
    "render_central_hosts",
    "validate",
    "validate_saved",
    "validate_wifi",
    "write_bridge_config",
]

# What a saved config must match. A save must also pass the relay's rule, below.
_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_RFC1123_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_HTTP_URL_RE = re.compile(r"https?://\S+")
# WPA2: an 8-63 character printable-ASCII passphrase, or a raw 64-digit hex key.
_WPA_PSK_RE = re.compile(r"[\x20-\x7e]{8,63}|[0-9a-fA-F]{64}")
# C0, DEL, C1 (which includes NEL), and the lone surrogates UTF-8 cannot encode.
_SSID_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_SSID_MAX_BYTES = 32
# ASCII and bounded: int() accepts other scripts' digits and raises on very long strings.
_PORT_DIGITS_RE = re.compile(r"[0-9]{1,5}")
# Where Tailscale numbers its nodes. Fusion traffic must stay on the site LAN (DESIGN 7.3).
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_BROKER_HOST_RULE = "broker.host must be a plain hostname or IP address"
# Addresses that save fine and can never be a central node, and each one's name. Loopback is the
# worst: the bridge would reach this node's own broker, and report the central link as up.
_NOT_A_CENTRAL_NODE = tuple(
    (ipaddress.ip_network(network), name)
    for network, name in (
        ("255.255.255.255/32", "the broadcast address"),
        ("0.0.0.0/8", "unspecified"),
        ("127.0.0.0/8", "loopback, which is this node itself"),
        ("169.254.0.0/16", "link-local"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
        ("10.42.0.0/24", "in the setup AP's subnet"),
        ("10.77.0.0/24", "in the camera link's subnet"),
    )
)

# Copies of the rules in cameralink.frigate_config, which this package cannot import at run
# time. A value they refuse makes camera-link's render raise, and that stops Frigate, so the
# portal refuses it first. cameralink/tests/test_portal_rules.py holds the two sets equal.
_ZONE_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{2,}")
_RELAY_USER_RE = re.compile(r"[a-zA-Z0-9._~@+-]+")
_RELAY_PASSWORD_RE = re.compile(r"[!#%&(-\[\]-_a-~]*")

# Copies of the rule in relayapi.nodes, which this package cannot import at run time. The relay
# drops a message whose node or camera name breaks it, so a kit saved with one never appears on
# central. relay/api/tests/test_node_id_rule.py holds the two equal.
_RELAY_NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_RELAY_RESERVED_NODE_IDS = frozenset({"all", "all_others"})


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

    # Keys of the bridge config file.
    node_id: str
    broker: BrokerConfig
    covered_zones: list[str] | tuple[str, ...]
    frigate: FrigateConfig
    outbox: OutboxConfig = field(default_factory=OutboxConfig)
    cue_window_s: float = 5.0
    status_interval_s: float = 30
    mediamtx_address: str

    # Provisioning only: never written to the bridge config.
    hostname: str
    wifi_ssid: str
    wifi_psk: str = field(repr=False)
    # Applied to the camera by camera-link, which reads it from node.json.
    camera_upside_down: bool = False
    # Credentials are required unless this says the broker takes none.
    broker_anonymous: bool = False
    # Empty means the broker host. See effective_ntp_server.
    ntp_server: str = ""


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_clean_text(value: Any) -> bool:
    """Non-empty, printable, and with no surrounding whitespace."""
    return isinstance(value, str) and bool(value) and value == value.strip() and value.isprintable()


def _is_host(value: Any) -> bool:
    """A plain hostname or IP address, with no scheme, port or spaces."""
    # ipaddress accepts anything after "%", including newlines, which would then
    # inject lines into the files these values are written to.
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        return (
            len(value) <= 253
            and all(_RFC1123_LABEL_RE.fullmatch(label) for label in labels)
            # An all-numeric ending is a mistyped IP address, not a name.
            and not labels[-1].isdigit()
        )
    return True


def _is_ipv4_literal(value: Any) -> bool:
    """An IPv4 address, the only kind the node's ``central_hosts`` firewall set can hold."""
    if not isinstance(value, str):
        return False
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _central_address_problems(host: Any) -> list[str]:
    """Why an IPv4 ``host`` cannot be a central node, naming the kind of address it is."""
    address = ipaddress.IPv4Address(host)
    for network, name in _NOT_A_CENTRAL_NODE:
        if address in network:
            return [f"broker.host must be the central node's site LAN address, and this is {name}"]
    return []


def _is_tailscale_address(value: Any) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address) in _TAILSCALE_NETWORK


def _is_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _is_host_port(value: Any) -> bool:
    """``host:port``, with the host as :func:`_is_host` takes it, and IPv6 in brackets."""
    if not isinstance(value, str):
        return False
    host, separator, port = value.rpartition(":")
    if not separator or not _PORT_DIGITS_RE.fullmatch(port) or not _is_port(int(port)):
        return False
    if host.startswith("[") and host.endswith("]"):
        return ":" in host and _is_host(host[1:-1])
    return ":" not in host and _is_host(host)


def _is_positive_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:  # an int too large for a float
        return False


def _is_ssid(value: Any) -> bool:
    return (
        _has_text(value)
        and not _SSID_FORBIDDEN_RE.search(value)
        and len(value.encode("utf-8")) <= _SSID_MAX_BYTES
    )


def _zones_valid(zones: Any) -> bool:
    return (
        isinstance(zones, (list, tuple))
        and len(zones) > 0
        and all(_is_clean_text(zone) for zone in zones)
    )


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _credential_checks(cfg: NodeConfig) -> list[tuple[bool, str]]:
    broker = cfg.broker
    if cfg.broker_anonymous:
        return [
            (
                broker.username == "" and broker.password == "",
                "broker.username and broker.password must be empty for an anonymous broker",
            )
        ]
    return [
        (_is_clean_text(broker.username), "broker.username is required, with no stray spaces"),
        (_has_text(broker.password), "broker.password is required"),
    ]


def _wifi_checks(ssid: Any, psk: Any) -> list[tuple[bool, str]]:
    return [
        (_is_ssid(ssid), "wifi_ssid is required: at most 32 bytes, no control characters"),
        (
            _matches(_WPA_PSK_RE, psk),
            "wifi_psk must be 8-63 printable ASCII characters or 64 hex digits",
        ),
    ]


def validate_wifi(ssid: str, psk: str) -> list[str]:
    """Return the problems with a WiFi SSID and PSK alone, under ``validate``'s rules."""
    return [message for ok, message in _wifi_checks(ssid, psk) if not ok]


def frigate_problems(
    node_id: str, covered_zones: Any, relay_user: Any, relay_password: Any
) -> list[str]:
    """Return why camera-link could not render Frigate's config and env file from these.

    ``relay_user`` and ``relay_password`` are the broker username and password, which
    go2rtc also uses. Messages never quote a value.
    """
    zones = covered_zones if isinstance(covered_zones, (list, tuple)) else []
    checks = [
        (
            all(_matches(_ZONE_NAME_RE, zone) for zone in zones),
            "covered_zones: each zone needs at least two characters, "
            "using only letters, digits, hyphens and underscores",
        ),
        (node_id not in zones, "covered_zones: a zone cannot have the same name as node_id"),
        (
            relay_user == "" or _matches(_RELAY_USER_RE, relay_user),
            "broker.username must use only letters, digits and . _ ~ @ + -",
        ),
        (
            _matches(_RELAY_PASSWORD_RE, relay_password),
            "broker.password must be printable ASCII, with no space, quote, backslash or $",
        ),
    ]
    return [message for ok, message in checks if not ok]


def _relay_name_problems(cfg: NodeConfig) -> list[str]:
    """Why the central relay would refuse a name that :func:`validate_saved` has let through."""
    return [
        f"{field_name} must be at most 63 characters and not the reserved name all: "
        "the central relay refuses anything else, and this kit would not appear there"
        for field_name, name in (("node_id", cfg.node_id), ("frigate.camera", cfg.frigate.camera))
        if _matches(_NODE_ID_RE, name)
        and (not _matches(_RELAY_NODE_ID_RE, name) or name in _RELAY_RESERVED_NODE_IDS)
    ]


def validate(cfg: NodeConfig) -> list[str]:
    """Return every problem that stops ``cfg`` being saved, as readable text; empty means valid.

    Messages name fields and rules, and never quote a value, so they are safe to log
    or show on the portal whichever field is wrong. The WiFi SSID and PSK are not held to
    the clean-text rule, because spaces are legal in them.
    """
    broker = cfg.broker
    problems = validate_saved(cfg) + _relay_name_problems(cfg)
    if not _is_ipv4_literal(broker.host):
        # This says more about the same field.
        problems = [problem for problem in problems if problem != _BROKER_HOST_RULE]
        problems.append(
            "broker.host must be an IPv4 address and not a name: "
            "the node's firewall admits only this address for SSH and Frigate"
        )
    else:
        problems += _central_address_problems(broker.host)
    problems += frigate_problems(cfg.node_id, cfg.covered_zones, broker.username, broker.password)
    return problems


def validate_saved(cfg: NodeConfig) -> list[str]:
    """Return every problem that stops a saved ``cfg`` being used, under ``validate``'s terms."""
    broker, outbox, frigate = cfg.broker, cfg.outbox, cfg.frigate
    checks = [
        (
            _matches(_NODE_ID_RE, cfg.node_id),
            "node_id must be lowercase letters, digits and hyphens, with no leading hyphen",
        ),
        (_is_host(broker.host), _BROKER_HOST_RULE),
        (
            not _is_tailscale_address(broker.host),
            "broker.host must not be in Tailscale's 100.64.0.0/10: "
            "use the broker's site LAN address, because fusion must not depend on Tailscale",
        ),
        (_is_port(broker.port), "broker.port must be a whole number from 1 to 65535"),
        (
            # Following the broker: a bad address is reported once, against the broker.
            cfg.ntp_server in ("", broker.host) or _is_host(cfg.ntp_server),
            "ntp_server must be a plain hostname or IP address, or blank to use the broker",
        ),
        *_credential_checks(cfg),
        (isinstance(broker.tls, bool), "broker.tls must be true or false"),
        (_zones_valid(cfg.covered_zones), "covered_zones needs at least one non-empty zone"),
        (_is_clean_text(outbox.path), "outbox.path is required, with no stray spaces"),
        (_is_positive_number(outbox.max_bytes), "outbox.max_bytes must be a positive number"),
        (_is_positive_number(outbox.max_age_s), "outbox.max_age_s must be a positive number"),
        (_is_positive_number(cfg.cue_window_s), "cue_window_s must be a positive number"),
        (_is_positive_number(cfg.status_interval_s), "status_interval_s must be a positive number"),
        (_is_clean_text(frigate.topic), "frigate.topic is required, with no stray spaces"),
        (
            _is_clean_text(frigate.base_url) and _matches(_HTTP_URL_RE, frigate.base_url),
            "frigate.base_url must be an http(s) URL",
        ),
        (
            frigate.camera is None or _matches(_NODE_ID_RE, frigate.camera),
            "frigate.camera must be lowercase letters, digits and hyphens, "
            "with no leading hyphen",
        ),
        (_is_host(frigate.mqtt_host), "frigate.mqtt_host must be a plain hostname or IP address"),
        (_is_port(frigate.mqtt_port), "frigate.mqtt_port must be a whole number from 1 to 65535"),
        (
            _matches(_RFC1123_LABEL_RE, cfg.hostname),
            "hostname must be 1-63 letters, digits or hyphens, "
            "and must not start or end with a hyphen",
        ),
        *_wifi_checks(cfg.wifi_ssid, cfg.wifi_psk),
        (_is_host_port(cfg.mediamtx_address), "mediamtx_address must be host:port"),
        (isinstance(cfg.camera_upside_down, bool), "camera_upside_down must be true or false"),
    ]
    return [message for ok, message in checks if not ok]


def effective_ntp_server(cfg: NodeConfig) -> str:
    """The time server this node uses: the one given, or else the broker host."""
    return cfg.ntp_server or cfg.broker.host


def render_central_hosts(broker_host: str) -> str:
    """The nftables file adding the central node to ``central_hosts``, which the rules declare.

    Only ever given a validated address.
    """
    return (
        "# Written by camera-provision on each save. Included by /etc/nftables.conf.\n"
        f"add element inet black_relay central_hosts {{ {broker_host} }}\n"
    )


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
        "mediamtx_address": cfg.mediamtx_address,
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
    # ASCII output: PyYAML's emitter writes NEL and friends raw when allowed to, and its
    # own reader then treats them as line breaks.
    try:
        text = yaml.safe_dump(_bridge_mapping(cfg), sort_keys=False, allow_unicode=False)
    except yaml.YAMLError:
        text = None
    # Raised out here so the YAML error, whose message quotes the value it could not
    # represent, is not even attached as __context__.
    if text is None:
        raise ValueError("node config holds a value that cannot be written as YAML")
    _write_private_atomic(Path(path), text)
