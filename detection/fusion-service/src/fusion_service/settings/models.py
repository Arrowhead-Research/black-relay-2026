"""Configuration schema. Relative paths resolve against `base_dir` from the validation context."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, model_validator

from fusion_service.domain.events import Subsystem


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _base_dir(info: ValidationInfo) -> Path | None:
    return (info.context or {}).get("base_dir")


def _resolve(model: BaseModel, names: tuple[str, ...], base: Path) -> None:
    for name in names:
        value = getattr(model, name)
        if value is not None and not value.is_absolute():
            setattr(model, name, base / value)


class Broker(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=63)
    namespace: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1)
    port: int = Field(default=1883, ge=1, le=65535)
    client_id: str = Field(min_length=1, max_length=128)
    subsystems: list[Subsystem] = Field(min_length=1)
    physical_node_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        max_length=63,
    )
    site_id: str | None = Field(default=None, min_length=1, max_length=128)
    node_sites: dict[str, str] = Field(default_factory=dict)
    username_env: str | None = None
    password_env: str | None = None
    tls_ca: Path | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @model_validator(mode="after")
    def consistency(self):
        if "physical" in self.subsystems and not self.physical_node_id:
            raise ValueError("physical subscriptions require physical_node_id")
        if bool(self.username_env) != bool(self.password_env):
            raise ValueError("username_env and password_env must be configured together")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("tls_cert and tls_key must be configured together")
        if self.tls_cert and not self.tls_ca:
            raise ValueError("tls_cert requires tls_ca")
        if any(not key or not value.strip() for key, value in self.node_sites.items()):
            raise ValueError("node_sites must contain nonempty node and site identifiers")
        return self

    @model_validator(mode="after")
    def resolve_paths(self, info: ValidationInfo):
        if base := _base_dir(info):
            _resolve(self, ("tls_ca", "tls_cert", "tls_key"), base)
        return self


class Settings(StrictModel):
    database: Path = Path("data/fusion.sqlite3")
    # Defaults work from a source checkout. Docker explicitly sets these to /contracts/... .
    camera_schemas: Path = Path("../camera-subsystem/bridge/schemas")
    rf_schemas: Path = Path("../rf-subsystem/schemas")
    physical_schemas: Path = Path("../physical-subsystem/schemas")
    brokers: list[Broker] = Field(min_length=1)
    api_token_env: str = "FUSION_API_TOKEN"
    retention_s: int = Field(default=7 * 86400, ge=60)
    dedupe_s: int = Field(default=14 * 86400, ge=60)
    max_event_bytes: int = Field(default=256 * 1024 * 1024, ge=65536)
    max_database_bytes: int = Field(default=512 * 1024 * 1024, ge=1048576)
    max_dedupe_entries: int = Field(default=1_000_000, ge=10)
    max_nodes: int = Field(default=1000, ge=1)
    max_message_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_message_depth: int = Field(default=32, ge=4, le=100)
    future_tolerance_s: int = Field(default=60, ge=0)
    camera_stale_s: int = Field(default=90, ge=1)
    rf_stale_s: int = Field(default=180, ge=1)
    physical_stale_s: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def consistency(self):
        if self.dedupe_s < self.retention_s:
            raise ValueError("dedupe_s must be at least retention_s")
        if self.max_database_bytes < self.max_event_bytes * 1.5:
            raise ValueError("max_database_bytes needs at least 50% headroom over max_event_bytes")
        if len({b.id for b in self.brokers}) != len(self.brokers):
            raise ValueError("broker ids must be unique")
        sessions = [(b.host, b.port, b.client_id) for b in self.brokers]
        if len(set(sessions)) != len(sessions):
            raise ValueError("MQTT sessions must have distinct client ids on each broker")
        return self

    @model_validator(mode="after")
    def resolve_paths(self, info: ValidationInfo):
        if base := _base_dir(info):
            _resolve(self, ("database", "camera_schemas", "rf_schemas", "physical_schemas"), base)
        return self

    def schema_dir(self, subsystem: Subsystem) -> Path:
        return {
            "camera": self.camera_schemas,
            "rf": self.rf_schemas,
            "physical": self.physical_schemas,
        }[subsystem]

    def stale_after_s(self, subsystem: Subsystem) -> int:
        return {
            "camera": self.camera_stale_s,
            "rf": self.rf_stale_s,
            "physical": self.physical_stale_s,
        }[subsystem]

    def token(self) -> SecretStr:
        """The API bearer token, read from the environment variable named by api_token_env."""
        value = os.environ.get(self.api_token_env, "")
        if not value.strip():
            raise ValueError(f"required environment variable is empty: {self.api_token_env}")
        return SecretStr(value)
