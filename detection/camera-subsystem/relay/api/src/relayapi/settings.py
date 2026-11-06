"""The API's settings, from the environment. A missing secret or site subnet stops startup."""

import ipaddress
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

MIB = 1024 * 1024
# No site's LAN can be any of these, and a site subnet overlapping them would admit them.
_NEVER_A_SITE = tuple(
    ipaddress.ip_network(net)
    for net in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4")
)


class SettingsError(Exception):
    """A setting is missing or impossible. The message names it and never holds its value."""


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    # The login nodes hold. MediaMTX pulls video with it, and it derives the proxy secret.
    broker_user: str
    broker_password: str = field(repr=False)
    # The API's own login, which nodes do not hold.
    api_broker_user: str
    api_broker_password: str = field(repr=False)
    # Kept secret, as whoever holds a broker login and knows a client id can take that session.
    mqtt_client_id: str = field(repr=False)
    mediamtx_api_url: str
    mediamtx_api_user: str
    mediamtx_api_password: str = field(repr=False)
    site_subnet: IPv4Network
    internal_subnets: tuple[IPv4Network, ...] = ()
    central_ip: IPv4Address | None = None
    broker_host: str = "mosquitto"
    broker_port: int = 1883
    db_path: Path = Path("/data/relay.sqlite3")
    schema_dir: Path = Path("/app/schemas")
    # Bridges report every 30 s. Three missed reports read as a node that has stopped.
    stale_after_s: float = 90.0
    # Further ahead than this is a broken clock, and must not become the node's "newest".
    future_tolerance_s: float = 60.0
    # Full check of MediaMTX's paths, which it forgets on a restart.
    path_sync_interval_s: float = 15.0
    path_backoff_max_s: float = 300.0
    max_nodes: int = 1000
    node_retention_s: float = 7 * 24 * 3600.0
    # Real messages are about 1 KB. Refused here, ahead of storage.
    max_payload_bytes: int = 16 * 1024
    max_depth: int = 32
    retain_updates: int = 20_000
    retain_durable: int = 200_000
    retain_bytes: int = 256 * MIB
    retention_check_every: int = 256
    store_retry_s: float = 5.0
    max_streams: int = 8
    proxy_connect_timeout_s: float = 5.0
    proxy_read_timeout_s: float = 30.0
    proxy_total_timeout_s: float = 300.0
    proxy_max_request_bytes: int = 20 * MIB  # Frigate's nginx client_max_body_size
    proxy_max_response_bytes: int = 512 * MIB
    proxy_max_snapshot_bytes: int = 32 * MIB

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        values: dict = {
            attribute: _required(env, name)
            for attribute, name in {
                "api_token": "RELAY_API_TOKEN",
                "broker_user": "RELAY_BROKER_USER",
                "broker_password": "RELAY_BROKER_PASSWORD",
                "api_broker_user": "RELAY_API_BROKER_USER",
                "api_broker_password": "RELAY_API_BROKER_PASSWORD",
                "mqtt_client_id": "RELAY_MQTT_CLIENT_ID",
                "mediamtx_api_url": "RELAY_MEDIAMTX_API_URL",
                "mediamtx_api_user": "RELAY_MEDIAMTX_API_USER",
                "mediamtx_api_password": "RELAY_MEDIAMTX_API_PASSWORD",
            }.items()
        }
        if values["api_broker_user"] == values["broker_user"]:
            raise SettingsError("RELAY_API_BROKER_USER must not be the login nodes hold")
        values["internal_subnets"] = _subnets(env, "RELAY_INTERNAL_SUBNETS")
        values["site_subnet"] = _site_subnet(env, values["internal_subnets"])
        if env.get("RELAY_CENTRAL_IP"):
            values["central_ip"] = _ipv4(env, "RELAY_CENTRAL_IP")
        if env.get("RELAY_BROKER_HOST"):
            values["broker_host"] = env["RELAY_BROKER_HOST"]
        for attribute, name in {
            "db_path": "RELAY_DB_PATH",
            "schema_dir": "RELAY_SCHEMA_DIR",
        }.items():
            if env.get(name):
                values[attribute] = Path(env[name])
        for attribute, name in {
            "broker_port": "RELAY_BROKER_PORT",
            "max_nodes": "RELAY_MAX_NODES",
            "max_payload_bytes": "RELAY_MAX_PAYLOAD_BYTES",
            "retain_updates": "RELAY_RETAIN_UPDATES",
            "retain_durable": "RELAY_RETAIN_DURABLE",
            "retain_bytes": "RELAY_RETAIN_BYTES",
            "max_streams": "RELAY_MAX_STREAMS",
            "proxy_max_response_bytes": "RELAY_PROXY_MAX_RESPONSE_BYTES",
        }.items():
            if env.get(name):
                values[attribute] = _whole_number(env, name)
        for attribute, name in {
            "stale_after_s": "RELAY_STALE_AFTER_S",
            "node_retention_s": "RELAY_NODE_RETENTION_S",
            "proxy_total_timeout_s": "RELAY_PROXY_TOTAL_TIMEOUT_S",
        }.items():
            if env.get(name):
                values[attribute] = _seconds(env, name)
        return cls(**values)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value.strip():
        raise SettingsError(f"{name} is required: see relay/README.md")
    return value


def _network(text: str, name: str) -> IPv4Network:
    try:
        network = ipaddress.ip_network(text)
    except ValueError:
        raise SettingsError(f"{name} must be an IPv4 network such as 192.168.1.0/24") from None
    if not isinstance(network, IPv4Network):
        raise SettingsError(f"{name} must be an IPv4 network such as 192.168.1.0/24")
    return network


def _subnets(env: Mapping[str, str], name: str) -> tuple[IPv4Network, ...]:
    parts = [part for part in env.get(name, "").split(",") if part]
    return tuple(_network(part, name) for part in parts)


def _site_subnet(env: Mapping[str, str], internal: tuple[IPv4Network, ...]) -> IPv4Network:
    name = "RELAY_SITE_SUBNET"
    network = _network(_required(env, name), name)
    if network.prefixlen < 8 or network.prefixlen > 30:
        raise SettingsError(f"{name} must be between a /8 and a /30")
    if any(network.overlaps(other) for other in (*_NEVER_A_SITE, *internal)):
        raise SettingsError(f"{name} overlaps loopback, link-local or the bundle's own networks")
    return network


def _ipv4(env: Mapping[str, str], name: str) -> IPv4Address:
    try:
        return IPv4Address(env[name])
    except ValueError:
        raise SettingsError(f"{name} must be an IPv4 address") from None


def _whole_number(env: Mapping[str, str], name: str) -> int:
    try:
        value = int(env[name])
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if value <= 0:
        raise SettingsError(f"{name} must be above zero")
    return value


def _seconds(env: Mapping[str, str], name: str) -> float:
    try:
        value = float(env[name])
    except ValueError:
        raise SettingsError(f"{name} must be a number of seconds") from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"{name} must be a number of seconds above zero")
    return value
