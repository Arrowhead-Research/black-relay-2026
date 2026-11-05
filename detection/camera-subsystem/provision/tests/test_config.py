"""Tests for ``cameraprovision.config`` — node config, validation, bridge YAML.

The written file is checked against the bridge itself: it goes through the real
``camerabridge.cli`` loading path, and its key tree must equal the shipped example's.
The bridge is a dev-group dependency only; the provisioner never imports it.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock

import pytest
import yaml
from camerabridge import cli as bridge_cli
from camerabridge.bridge import Bridge

from cameraprovision.config import (
    BrokerConfig,
    FrigateConfig,
    NodeConfig,
    OutboxConfig,
    validate,
    write_bridge_config,
)

EXAMPLE_YAML = Path(__file__).parents[2] / "bridge" / "config" / "bridge.example.yaml"

BROKER_PASSWORD = "br0ker-s3cret-Pw"
WIFI_PSK = "w1fi-s3cret-Psk"
TAILSCALE_KEY = "tskey-auth-s3cret"
SECRETS = (BROKER_PASSWORD, WIFI_PSK, TAILSCALE_KEY)

# Distinct from every bridge value, so a raw-text search can only match a leak.
HOSTNAME = "edge-host-7"
WIFI_SSID = "Site-WiFi-XYZ"
MEDIAMTX_ADDRESS = "10.9.9.9:8554"
PROVISION_ONLY_KEYS = (
    "mediamtx_address",
    "hostname",
    "tailscale_auth_key",
    "vpn_requested",
    "wifi_ssid",
    "wifi_psk",
)


def make_config(**overrides: Any) -> NodeConfig:
    fields: dict[str, Any] = {
        "node_id": "cam-01",
        "hostname": HOSTNAME,
        "wifi_ssid": WIFI_SSID,
        "wifi_psk": WIFI_PSK,
        "tailscale_auth_key": TAILSCALE_KEY,
        "mediamtx_address": MEDIAMTX_ADDRESS,
        "covered_zones": ["perimeter-north", "driveway"],
        "broker": BrokerConfig(host="10.0.0.2", username="camera-cam-01", password=BROKER_PASSWORD),
        "frigate": FrigateConfig(base_url="http://cam-01.local:5000"),
    }
    fields.update(overrides)
    return NodeConfig(**fields)


def broker(**overrides: Any) -> BrokerConfig:
    return replace(make_config().broker, **overrides)


def outbox(**overrides: Any) -> OutboxConfig:
    return replace(make_config().outbox, **overrides)


def frigate(**overrides: Any) -> FrigateConfig:
    return replace(make_config().frigate, **overrides)


def key_tree(mapping: dict) -> dict:
    """The nested key structure of a mapping, with every leaf value dropped."""
    return {k: key_tree(v) if isinstance(v, dict) else None for k, v in mapping.items()}


def example() -> dict:
    return yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))


def written(tmp_path: Path, cfg: NodeConfig | None = None) -> Path:
    path = tmp_path / "bridge.yaml"
    write_bridge_config(cfg or make_config(), path)
    return path


# --- validate -------------------------------------------------------------------


def test_valid_config_has_no_errors() -> None:
    assert validate(make_config()) == []


@pytest.mark.parametrize("node_id", ["CAM", "cam\n", "", "-cam", "cam_01", None])
def test_bad_node_id_is_rejected(node_id: Any) -> None:
    errors = validate(make_config(node_id=node_id))
    assert any("node_id" in e for e in errors)


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("broker.host", {"broker": broker(host="")}),
        ("broker.host", {"broker": broker(host="   ")}),
        ("broker.host", {"broker": broker(host=None)}),
        ("broker.username", {"broker": broker(username="")}),
        ("broker.password", {"broker": broker(password="")}),
        ("broker.port", {"broker": broker(port=0)}),
        ("broker.port", {"broker": broker(port=65536)}),
        ("broker.port", {"broker": broker(port="1883")}),
        ("broker.port", {"broker": broker(port=True)}),
        ("broker.tls", {"broker": broker(tls="false")}),
        ("frigate.mqtt_port", {"frigate": frigate(mqtt_port=70000)}),
        ("frigate.mqtt_host", {"frigate": frigate(mqtt_host="")}),
        ("frigate.topic", {"frigate": frigate(topic="")}),
        ("frigate.base_url", {"frigate": frigate(base_url="")}),
        ("frigate.base_url", {"frigate": frigate(base_url="cam-01.local:5000")}),
        ("outbox.path", {"outbox": outbox(path="")}),
        ("outbox.max_bytes", {"outbox": outbox(max_bytes=0)}),
        ("outbox.max_age_s", {"outbox": outbox(max_age_s=-1)}),
        ("status_interval_s", {"status_interval_s": 0}),
        ("status_interval_s", {"status_interval_s": float("nan")}),
        ("status_interval_s", {"status_interval_s": float("inf")}),
        ("cue_window_s", {"cue_window_s": "5"}),
        ("mediamtx_address", {"mediamtx_address": ""}),
        ("covered_zones", {"covered_zones": []}),
        ("covered_zones", {"covered_zones": [""]}),
        ("covered_zones", {"covered_zones": ["driveway", "  "]}),
        # A bare string would become a set of single letters in the bridge.
        ("covered_zones", {"covered_zones": "driveway"}),
        ("hostname", {"hostname": ""}),
        ("hostname", {"hostname": "-edge"}),
        ("hostname", {"hostname": "edge-"}),
        ("hostname", {"hostname": "edge.local"}),
        ("hostname", {"hostname": "edge\n"}),
        ("hostname", {"hostname": "e" * 64}),
        ("hostname", {"hostname": None}),
        ("wifi_ssid", {"wifi_ssid": ""}),
        ("wifi_ssid", {"wifi_ssid": "s" * 33}),
        ("wifi_ssid", {"wifi_ssid": "é" * 17}),  # 34 bytes: the limit is in bytes
        ("wifi_ssid", {"wifi_ssid": "site\nid=evil"}),
        ("wifi_psk", {"wifi_psk": ""}),
        ("wifi_psk", {"wifi_psk": "short"}),
        ("wifi_psk", {"wifi_psk": "p" * 64}),  # 64 characters must be raw hex
        ("wifi_psk", {"wifi_psk": "line1234\nline2"}),
        ("tailscale_auth_key", {"tailscale_auth_key": ""}),
        ("tailscale_auth_key", {"tailscale_auth_key": "   "}),
    ],
)
def test_bad_field_is_rejected_by_name(field: str, overrides: dict) -> None:
    errors = validate(make_config(**overrides))
    assert any(e.startswith(field) for e in errors), errors


@pytest.mark.parametrize(
    "overrides",
    [
        {"hostname": "e" * 63},
        {"hostname": "7-Edge"},
        {"broker": broker(port=1)},
        {"broker": broker(port=65535)},
        {"wifi_ssid": "s" * 32},
        {"wifi_psk": "12345678"},
        {"wifi_psk": "p" * 63},
        {"wifi_psk": "a1" * 32},
        {"wifi_psk": " sp@ce #:%;\\'\""},
        {"vpn_requested": False, "tailscale_auth_key": ""},
    ],
)
def test_boundary_values_are_accepted(overrides: dict) -> None:
    assert validate(make_config(**overrides)) == []


def test_every_problem_is_reported_at_once() -> None:
    errors = validate(make_config(node_id="CAM", hostname="", broker=broker(host="")))
    assert len(errors) == 3


# --- bridge YAML ----------------------------------------------------------------


def test_written_key_tree_equals_the_bridge_example(tmp_path: Path) -> None:
    loaded = yaml.safe_load(written(tmp_path).read_text(encoding="utf-8"))
    assert key_tree(loaded) == key_tree(example())


def test_written_file_loads_through_the_real_bridge_config_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outbox_path = tmp_path / "outbox.jsonl"
    path = written(tmp_path, make_config(outbox=outbox(path=str(outbox_path))))

    cfg = bridge_cli.load_config(str(path))
    with caplog.at_level(logging.WARNING, logger="camerabridge"):
        bridge = bridge_cli.make_bridge(cfg, central_client=Mock(), local_client=Mock())

    assert isinstance(bridge, Bridge)
    assert cfg["outbox"]["path"] == str(outbox_path)
    # make_bridge warns when frigate.camera is unset, which breaks health reporting.
    assert "frigate.camera" not in caplog.text
    assert bridge_cli._central_endpoint(cfg) == ("10.0.0.2", 1883)


def test_written_values_are_the_config_values(tmp_path: Path) -> None:
    cfg = make_config(
        broker=broker(port=8883, tls=True),
        covered_zones=("driveway",),
        status_interval_s=45,
        frigate=frigate(camera="front-gate"),
    )
    loaded = yaml.safe_load(written(tmp_path, cfg).read_text(encoding="utf-8"))

    assert loaded["node_id"] == "cam-01"
    assert loaded["broker"] == {
        "host": "10.0.0.2",
        "port": 8883,
        "username": "camera-cam-01",
        "password": BROKER_PASSWORD,
        "tls": True,
    }
    assert loaded["covered_zones"] == ["driveway"]
    assert loaded["status_interval_s"] == 45
    assert loaded["frigate"]["base_url"] == "http://cam-01.local:5000"
    assert loaded["frigate"]["camera"] == "front-gate"


def test_unset_defaults_come_from_the_bridge_example(tmp_path: Path) -> None:
    loaded = yaml.safe_load(written(tmp_path).read_text(encoding="utf-8"))
    shipped = example()

    assert shipped["frigate"]["mqtt_host"] == "127.0.0.1"  # the value this task promises
    assert loaded["frigate"]["mqtt_host"] == shipped["frigate"]["mqtt_host"]
    assert loaded["frigate"]["mqtt_port"] == shipped["frigate"]["mqtt_port"]
    assert loaded["frigate"]["topic"] == shipped["frigate"]["topic"]
    assert loaded["broker"]["port"] == shipped["broker"]["port"]
    assert loaded["broker"]["tls"] == shipped["broker"]["tls"]
    assert loaded["outbox"] == shipped["outbox"]
    assert loaded["cue_window_s"] == shipped["cue_window_s"]
    assert loaded["status_interval_s"] == shipped["status_interval_s"]


def test_frigate_camera_defaults_to_node_id(tmp_path: Path) -> None:
    """In v0 camera_id == node_id; unset, the bridge would report the camera offline."""
    loaded = yaml.safe_load(
        written(tmp_path, make_config(node_id="cam-42")).read_text(encoding="utf-8")
    )
    assert loaded["frigate"]["camera"] == "cam-42"


def test_provision_only_fields_never_reach_the_bridge_yaml(tmp_path: Path) -> None:
    text = written(tmp_path).read_text(encoding="utf-8")

    for key in PROVISION_ONLY_KEYS:
        assert key not in text
    for value in (HOSTNAME, WIFI_SSID, WIFI_PSK, TAILSCALE_KEY, MEDIAMTX_ADDRESS):
        assert value not in text


@pytest.mark.parametrize("node_id", ["123", "0123", "1e3", "0x1f", "null", "true", "on", "no"])
def test_yaml_lookalike_node_id_stays_a_string_for_the_bridge(
    tmp_path: Path, node_id: str
) -> None:
    cfg = make_config(node_id=node_id, outbox=outbox(path=str(tmp_path / "outbox.jsonl")))

    loaded = bridge_cli.load_config(str(written(tmp_path, cfg)))

    assert loaded["node_id"] == node_id
    bridge_cli.make_bridge(loaded, central_client=Mock(), local_client=Mock())


@pytest.mark.parametrize("password", ["p@ss: #'\"{}[]", "%(x)s\\n", "pässwörd", "1234", "null"])
def test_awkward_broker_password_round_trips(tmp_path: Path, password: str) -> None:
    path = written(tmp_path, make_config(broker=broker(password=password)))
    assert bridge_cli.load_config(str(path))["broker"]["password"] == password


# --- atomic 0600 write ----------------------------------------------------------


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_written_file_is_0600_and_leaves_nothing_else(tmp_path: Path) -> None:
    path = written(tmp_path)
    assert mode_of(path) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["bridge.yaml"]


def test_rewrite_tightens_an_existing_loose_file(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"
    path.write_text("old: true\n", encoding="utf-8")
    path.chmod(0o644)

    write_bridge_config(make_config(), path)

    assert mode_of(path) == 0o600
    assert "old" not in path.read_text(encoding="utf-8")


def test_failed_write_keeps_the_previous_file_and_removes_the_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bridge.yaml"
    path.write_text("old: true\n", encoding="utf-8")

    def fail(*_args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_bridge_config(make_config(), path)

    assert path.read_text(encoding="utf-8") == "old: true\n"
    assert [p.name for p in tmp_path.iterdir()] == ["bridge.yaml"]


def test_contents_are_flushed_before_the_rename_and_the_rename_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A power cut must leave the old file or the whole new one (constraints.md)."""
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        calls.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    written(tmp_path)

    assert calls == ["fsync-file", "replace", "fsync-dir"]


def test_invalid_config_writes_nothing_and_error_names_the_field(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"
    with pytest.raises(ValueError, match="node_id"):
        write_bridge_config(make_config(node_id="cam\n"), path)
    assert list(tmp_path.iterdir()) == []


def test_invalid_config_leaves_an_existing_file_alone(tmp_path: Path) -> None:
    path = written(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        write_bridge_config(make_config(hostname=""), path)
    assert path.read_text(encoding="utf-8") == before


# --- secrets stay out of reprs and errors ---------------------------------------


@pytest.mark.parametrize(
    "render",
    [repr, str, lambda c: f"{c}", lambda c: repr(c.broker)],
    ids=["repr", "str", "format", "repr-broker"],
)
def test_renderings_hide_every_secret(render: Callable[[NodeConfig], str]) -> None:
    text = render(make_config())
    for secret in SECRETS:
        assert secret not in text


def test_repr_still_identifies_the_node() -> None:
    assert "cam-01" in repr(make_config())


def invalid_with_secret_like_values() -> list[NodeConfig]:
    """Configs whose secret-bearing fields are themselves what is invalid."""
    return [
        make_config(wifi_psk="short"),
        make_config(wifi_psk=WIFI_PSK + "\n"),
        make_config(wifi_psk="x" * 70),
        make_config(wifi_ssid="bad\nssid", node_id="CAM"),
        make_config(hostname="-bad", broker=broker(port=0)),
    ]


@pytest.mark.parametrize("cfg", invalid_with_secret_like_values())
def test_validation_errors_never_carry_secret_values(cfg: NodeConfig, tmp_path: Path) -> None:
    secrets = (cfg.wifi_psk, cfg.broker.password, cfg.tailscale_auth_key)

    errors = validate(cfg)
    assert errors
    with pytest.raises(ValueError) as raised:
        write_bridge_config(cfg, tmp_path / "bridge.yaml")

    for text in (*errors, str(raised.value), repr(raised.value)):
        for secret in secrets:
            assert secret not in text


def test_failed_write_error_never_carries_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError) as raised:
        write_bridge_config(make_config(), tmp_path / "bridge.yaml")
    for secret in SECRETS:
        assert secret not in str(raised.value)


def test_config_module_logs_no_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        validate(make_config(wifi_psk="short"))
        written(tmp_path)
    for secret in (*SECRETS, "short"):
        assert secret not in caplog.text


def test_unserializable_value_error_never_carries_secret_values(tmp_path: Path) -> None:
    """PyYAML quotes the object it cannot represent, and a str subclass passes validate."""

    class TaintedStr(str):
        pass

    cfg = make_config(broker=broker(password=TaintedStr(BROKER_PASSWORD)))
    path = tmp_path / "bridge.yaml"

    with pytest.raises(ValueError) as raised:
        write_bridge_config(cfg, path)

    assert BROKER_PASSWORD not in str(raised.value)
    assert raised.value.__suppress_context__
    assert not path.exists()
