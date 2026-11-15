"""Configuration loading (schema = config.example.yaml). Every key is honoured; unknown keys are ignored."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "iq_remote": {"enabled": True, "panel_ip": None, "panel_port": 8883, "plugin_ip": None,
                  "pki_dir": "/data/pki", "ca_file": None,
                  # leaf pin of the panel's TLS certificate (SPKI sha256, 64 hex). null = trust on first use, stored in
                  # <pki_dir>/panel_pin.json. A set value is strict: no TOFU, the file is ignored. See br2/pin.py.
                  "panel_spki_sha256": None,
                  # required (default): a pin must already be recorded (`br2 pair` / `br2 repin`), else the adapter refuses
                  # to connect. tofu: opt-in, the first verified connection records the pin. preset: implied when
                  # panel_spki_sha256 is set. See br2/pin.py.
                  "pin_mode": "required",
                  # uid:gid the pairing material and the pin are handed to when `br2 pair/repin` run as root (the uid the
                  # compose iq-remote service runs as), or `dir` = whoever owns pki_dir.
                  "owner": "10001:10000",
                  # follow a panel certificate rotation instead of refusing until `br2 repin --yes`. Harness/dev only.
                  "pin_accept_rotation": False},
    "control4": {"enabled": False, "host": None, "port": 12345, "token_file": None, "cert_sha256": None,
                 "uncovered_kinds": []},
    "timers": {"silence_probe_s": 60, "probe_timeout_s": 10, "backoff_initial_s": 1,
               "backoff_max_s": 60, "fallback_after_s": 120, "ipc_liveness_s": 5},
    "zones": {},
    "mqtt": {"host": "127.0.0.1", "port": 8883, "tls": True, "ca_file": "/certs/ca.crt",
             "username": "blackrelay", "password_file": "/run/secrets/mqtt_password",
             "topic_prefix": "blackrelay/v1", "client_id": "blackrelay-bridge",
             "ha_discovery": True, "ha_discovery_prefix": "homeassistant",
             "insecure_dev": False},
    "api": {"host": "0.0.0.0", "port": 8443, "tls_cert": "/certs/api/api.crt", "tls_key": "/certs/api/api.key",
            "keys": [], "rate_limit_per_minute": 120, "metrics_port": 9100,
            "metrics_host": "127.0.0.1"},
    "site": {"id": "default", "name": "default"},
    # Team ("fusion") MQTT output, draft v0. Off by default. Plaintext is allowed only because the team broker is
    # plaintext today; set `tls: {ca_file: ...}` (and username/password_file) when it is not.
    "fusion": {"enabled": False, "host": "rf-mqtt", "port": 1883, "tls": None, "username": None,
               "password_file": None, "topic_prefix": "detection/physical", "client_id": "physical-subsystem",
               "status_interval_s": 60, "keepalive_s": 30, "max_inflight": 50, "startup_grace_s": 10,
               "metrics_port": 9101, "metrics_host": "127.0.0.1",
               "health_max_tick_age_s": 30, "health_max_backlog_age_s": 300,
               "outbox": {"dir": None, "max_bytes": 10485760, "max_messages": 20000, "max_age_s": 86400}},
    "storage": {"data_dir": "/data", "retention_days": 30},
    "log_level": "INFO",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "zones":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    zone_over: dict[int, dict[str, Any]] = field(default_factory=dict)

    def __getitem__(self, k: str) -> Any:
        return self.raw[k]

    @property
    def iq(self) -> dict: return self.raw["iq_remote"]
    @property
    def c4(self) -> dict: return self.raw["control4"]
    @property
    def timers(self) -> dict: return self.raw["timers"]
    @property
    def mqtt(self) -> dict: return self.raw["mqtt"]
    @property
    def api(self) -> dict: return self.raw["api"]
    @property
    def fusion(self) -> dict: return self.raw["fusion"]
    @property
    def site(self) -> dict: return self.raw["site"]
    @property
    def storage(self) -> dict: return self.raw["storage"]

    @property
    def include_keypads(self) -> bool:
        """zones.include_keypads (default false): publish keypad / IQ Remote zones (including the bridge's own virtual
        remote) like sensors. Off by default: they are not sensors."""
        return (self.raw.get("zones") or {}).get("include_keypads") is True

    @property
    def iq_enabled(self) -> bool:
        return bool(self.iq.get("enabled")) and bool(self.iq.get("panel_ip"))

    @property
    def c4_enabled(self) -> bool:
        return bool(self.c4.get("enabled")) and bool(self.c4.get("host"))

    @property
    def db_path(self) -> str:
        return str(Path(self.storage["data_dir"]) / "blackrelay.db")


def parse(raw: dict | None) -> Config:
    merged = _merge(DEFAULTS, raw or {})
    if os.environ.get("BR2_IQ_PIN_ACCEPT_ROTATION") == "1":       # set by harness_start.sh only (see br2/pin.py)
        merged["iq_remote"]["pin_accept_rotation"] = True
    if os.environ.get("BR2_IQ_PIN_MODE"):                         # ditto: harness_start.sh opts into `tofu`
        merged["iq_remote"]["pin_mode"] = os.environ["BR2_IQ_PIN_MODE"]
    zo: dict[int, dict] = {}
    for k, v in (merged.get("zones") or {}).items():
        try:
            zo[int(k)] = dict(v or {})
        except (TypeError, ValueError):
            continue
    return Config(merged, zo)


def load(path: str | os.PathLike) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        return parse(yaml.safe_load(fh) or {})


def ipc_dir() -> str:
    return os.environ.get("BR2_IPC_DIR", "/run/br2")


def ipc_in_dir() -> str:
    """Legacy/shared adapters -> normalizer socket dir (used when no per-adapter dir is configured)."""
    return os.environ.get("BR2_IPC_IN_DIR") or ipc_dir()


def ipc_adapter_dir(name: str) -> str:
    """Socket dir of ONE adapter. Compose: one volume per adapter, mounted only in that adapter + the normalizer."""
    env = {"iq_remote": "BR2_IPC_IQ_DIR", "control4": "BR2_IPC_C4_DIR"}.get(name)
    return (os.environ.get(env) if env else None) or ipc_in_dir()


def ipc_out_dir() -> str:
    """normalizer -> outbound socket dir (compose: volume shared ONLY by normalizer + outbound)."""
    return os.environ.get("BR2_IPC_OUT_DIR") or ipc_dir()


def beacon_dir() -> str:
    """Primary-link beacon dir (compose: dedicated volume, rw in iq-remote only, ro in control4)."""
    return os.environ.get("BR2_BEACON_DIR") or ipc_in_dir()


def out_state_dir(cfg: "Config") -> str:
    """Outbound's own durable state (replay pointer, published-topic set)."""
    return os.environ.get("BR2_OUT_STATE_DIR") or str(cfg.storage["data_dir"])
