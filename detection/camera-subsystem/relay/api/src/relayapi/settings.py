"""The API's settings, from the environment. A missing secret stops startup."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024


class SettingsError(Exception):
    """A setting is missing or impossible. The message names it and never holds its value."""


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    broker_user: str
    broker_password: str = field(repr=False)
    mediamtx_api_url: str
    mediamtx_api_user: str
    mediamtx_api_password: str = field(repr=False)
    broker_host: str = "mosquitto"
    broker_port: int = 1883
    mqtt_client_id: str = "relay-api"
    db_path: Path = Path("/data/relay.sqlite3")
    schema_dir: Path = Path("/app/schemas")
    # Bridges report every 30 s. Three missed reports read as a node that has stopped.
    stale_after_s: float = 90.0
    # Further ahead than this is a broken clock, and must not become the node's "newest".
    future_tolerance_s: float = 300.0
    # Full check of MediaMTX's paths, which it forgets on a restart.
    path_sync_interval_s: float = 15.0
    max_nodes: int = 1000
    max_payload_bytes: int = 256 * 1024
    retain_updates: int = 20_000
    retain_durable: int = 200_000
    retention_check_every: int = 256
    proxy_connect_timeout_s: float = 5.0
    proxy_read_timeout_s: float = 30.0
    proxy_total_timeout_s: float = 300.0
    proxy_max_request_bytes: int = 20 * MIB  # Frigate's nginx client_max_body_size
    proxy_max_response_bytes: int = 512 * MIB
    proxy_max_snapshot_bytes: int = 32 * MIB

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        values = {
            attribute: _required(env, name)
            for attribute, name in {
                "api_token": "RELAY_API_TOKEN",
                "broker_user": "RELAY_BROKER_USER",
                "broker_password": "RELAY_BROKER_PASSWORD",
                "mediamtx_api_url": "RELAY_MEDIAMTX_API_URL",
                "mediamtx_api_user": "RELAY_MEDIAMTX_API_USER",
                "mediamtx_api_password": "RELAY_MEDIAMTX_API_PASSWORD",
            }.items()
        }
        for attribute, name in {
            "broker_host": "RELAY_BROKER_HOST",
            "mqtt_client_id": "RELAY_MQTT_CLIENT_ID",
        }.items():
            if env.get(name):
                values[attribute] = env[name]
        for attribute, name in {
            "db_path": "RELAY_DB_PATH",
            "schema_dir": "RELAY_SCHEMA_DIR",
        }.items():
            if env.get(name):
                values[attribute] = Path(env[name])
        for attribute, name in {
            "broker_port": "RELAY_BROKER_PORT",
            "max_nodes": "RELAY_MAX_NODES",
            "retain_updates": "RELAY_RETAIN_UPDATES",
            "retain_durable": "RELAY_RETAIN_DURABLE",
            "proxy_max_response_bytes": "RELAY_PROXY_MAX_RESPONSE_BYTES",
        }.items():
            if env.get(name):
                values[attribute] = _whole_number(env, name)
        for attribute, name in {
            "stale_after_s": "RELAY_STALE_AFTER_S",
            "proxy_total_timeout_s": "RELAY_PROXY_TOTAL_TIMEOUT_S",
        }.items():
            if env.get(name):
                values[attribute] = _seconds(env, name)
        return cls(**values)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value.strip():
        raise SettingsError(f"{name} is required: run relay/setup-credentials.sh")
    return value


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
