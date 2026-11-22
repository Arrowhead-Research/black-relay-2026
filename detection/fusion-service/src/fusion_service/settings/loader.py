"""Read config.yaml, apply environment overrides, and check every referenced secret exists."""

import os
from pathlib import Path

import yaml

from fusion_service.settings.models import Settings

# Environment variables that override top-level settings. Docker Compose sets the path ones.
ENV_OVERRIDES = {
    "database": "FUSION_DATABASE",
    "camera_schemas": "FUSION_CAMERA_SCHEMAS",
    "rf_schemas": "FUSION_RF_SCHEMAS",
    "physical_schemas": "FUSION_PHYSICAL_SCHEMAS",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """A repeated broker/config key must not silently replace earlier configuration."""

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in keys:
                    raise ValueError("duplicate YAML mapping key")
                keys.add(key)
            except TypeError:
                raise ValueError("invalid YAML mapping key") from None
        return super().construct_mapping(node, deep=deep)


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ValueError(f"required environment variable is empty: {name}")
    return value


def default_config_path() -> Path:
    return Path(os.environ.get("FUSION_CONFIG", "config.yaml"))


def load_settings(path: Path) -> Settings:
    """Validate the file at `path`; relative paths inside it resolve against its directory."""
    path = path.resolve()
    raw = yaml.load(path.read_text(), Loader=UniqueKeyLoader)
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a YAML mapping")
    for field, variable in ENV_OVERRIDES.items():
        value = os.environ.get(variable, "")
        if value.strip():
            raw[field] = value
    settings = Settings.model_validate(raw, context={"base_dir": path.parent})
    for broker in settings.brokers:
        for name in (broker.username_env, broker.password_env):
            if name:
                required_env(name)
    settings.token()
    settings.camera_snapshot_password()
    return settings
