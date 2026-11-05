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
    effective_ntp_server,
    validate,
    validate_wifi,
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
    "hostname",
    "tailscale_auth_key",
    "vpn_requested",
    "broker_anonymous",
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
        ("broker.host", {"broker": broker(host="mqtt://10.0.0.2")}),
        ("broker.host", {"broker": broker(host="10.0.0.2:1883")}),
        ("broker.host", {"broker": broker(host="10.0.0.2 ")}),
        ("broker.host", {"broker": broker(host="broker one")}),
        ("broker.host", {"broker": broker(host="broker\n")}),
        ("broker.host", {"broker": broker(host="-broker.local")}),
        ("broker.host", {"broker": broker(host="10.0.0.999")}),
        ("broker.username", {"broker": broker(username="")}),
        ("broker.username", {"broker": broker(username=" camera")}),
        ("broker.username", {"broker": broker(username="cam\x01era")}),
        ("broker.password", {"broker": broker(password="")}),
        ("broker.port", {"broker": broker(port=0)}),
        ("broker.port", {"broker": broker(port=65536)}),
        ("broker.port", {"broker": broker(port="1883")}),
        ("broker.port", {"broker": broker(port=True)}),
        ("broker.port", {"broker": broker(port=10**400)}),
        ("broker.tls", {"broker": broker(tls="false")}),
        ("frigate.mqtt_port", {"frigate": frigate(mqtt_port=70000)}),
        ("frigate.mqtt_host", {"frigate": frigate(mqtt_host="")}),
        ("frigate.mqtt_host", {"frigate": frigate(mqtt_host="tcp://localhost")}),
        ("frigate.topic", {"frigate": frigate(topic="")}),
        ("frigate.topic", {"frigate": frigate(topic="frigate/events\n")}),
        ("frigate.camera", {"frigate": frigate(camera="")}),
        ("frigate.camera", {"frigate": frigate(camera="Front Gate")}),
        ("frigate.camera", {"frigate": frigate(camera="cam\n")}),
        ("frigate.base_url", {"frigate": frigate(base_url="")}),
        ("frigate.base_url", {"frigate": frigate(base_url="cam-01.local:5000")}),
        ("frigate.base_url", {"frigate": frigate(base_url="http://cam\x01.local")}),
        ("outbox.path", {"outbox": outbox(path="")}),
        ("outbox.path", {"outbox": outbox(path="/var/lib/outbox.jsonl ")}),
        ("outbox.max_bytes", {"outbox": outbox(max_bytes=0)}),
        ("outbox.max_age_s", {"outbox": outbox(max_age_s=-1)}),
        ("outbox.max_bytes", {"outbox": outbox(max_bytes=10**400)}),
        ("status_interval_s", {"status_interval_s": 10**400}),
        ("status_interval_s", {"status_interval_s": 0}),
        ("status_interval_s", {"status_interval_s": float("nan")}),
        ("status_interval_s", {"status_interval_s": float("inf")}),
        ("cue_window_s", {"cue_window_s": "5"}),
        ("mediamtx_address", {"mediamtx_address": ""}),
        ("mediamtx_address", {"mediamtx_address": " 10.0.0.2:8554"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:8554\n"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2"}),
        ("mediamtx_address", {"mediamtx_address": "relay.site.local"}),
        ("mediamtx_address", {"mediamtx_address": ":8554"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:abc"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:0"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:65536"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:٨٥٥٤"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:" + "9" * 5000}),
        ("mediamtx_address", {"mediamtx_address": "rtsp://10.0.0.2:8554"}),
        ("mediamtx_address", {"mediamtx_address": "10.0.0.2:8554/stream"}),
        ("mediamtx_address", {"mediamtx_address": "bad host:8554"}),
        ("mediamtx_address", {"mediamtx_address": "::1:8554"}),  # IPv6 needs brackets
        ("mediamtx_address", {"mediamtx_address": "[relay.local]:8554"}),
        ("mediamtx_address", {"mediamtx_address": None}),
        ("covered_zones", {"covered_zones": []}),
        ("covered_zones", {"covered_zones": [""]}),
        ("covered_zones", {"covered_zones": ["driveway", "  "]}),
        ("covered_zones", {"covered_zones": [" driveway"]}),
        ("covered_zones", {"covered_zones": ["driveway\n"]}),
        ("covered_zones", {"covered_zones": ["drive\x85way"]}),
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
        ("wifi_ssid", {"wifi_ssid": "site\x85id"}),
        ("wifi_ssid", {"wifi_ssid": "site\udc80id"}),
        ("wifi_psk", {"wifi_psk": ""}),
        ("wifi_psk", {"wifi_psk": "short"}),
        ("wifi_psk", {"wifi_psk": "p" * 64}),  # 64 characters must be raw hex
        ("wifi_psk", {"wifi_psk": "line1234\nline2"}),
        ("tailscale_auth_key", {"tailscale_auth_key": ""}),
        ("tailscale_auth_key", {"tailscale_auth_key": "   "}),
        ("tailscale_auth_key", {"tailscale_auth_key": TAILSCALE_KEY + "\n"}),
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
        {"broker": broker(host="broker.site.local")},
        {"broker": broker(host="broker")},
        {"broker": broker(host="fd00::2")},
        {"broker": broker(password=" pw with spaces ")},
        {"wifi_ssid": " Cafe WiFi "},
        {"covered_zones": ["north gate"]},
        {"frigate": frigate(camera="front-gate")},
    ],
)
def test_boundary_values_are_accepted(overrides: dict) -> None:
    assert validate(make_config(**overrides)) == []


@pytest.mark.parametrize(
    "address",
    ["10.0.0.2:8554", "relay.site.local:8554", "relay:1", "relay:65535", "[fd7a::1]:8554"],
)
def test_mediamtx_host_port_forms_are_accepted(address: str) -> None:
    assert validate(make_config(mediamtx_address=address)) == []


@pytest.mark.parametrize(
    ("ssid", "psk", "named"),
    [
        ("", WIFI_PSK, "wifi_ssid"),
        ("s" * 33, WIFI_PSK, "wifi_ssid"),
        (WIFI_SSID, "short", "wifi_psk"),
        (WIFI_SSID, "", "wifi_psk"),
    ],
)
def test_validate_wifi_names_the_bad_field(ssid: str, psk: str, named: str) -> None:
    errors = validate_wifi(ssid, psk)
    assert len(errors) == 1
    assert errors[0].startswith(named)
    assert psk == "" or psk not in errors[0]


def test_validate_wifi_accepts_what_validate_accepts() -> None:
    assert validate_wifi(WIFI_SSID, WIFI_PSK) == []


def test_validate_applies_the_same_wifi_rules_as_validate_wifi() -> None:
    errors = validate(make_config(wifi_ssid="", wifi_psk="short"))
    assert validate_wifi("", "short") == errors


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
    for value in (HOSTNAME, WIFI_SSID, WIFI_PSK, TAILSCALE_KEY):
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


def test_every_key_writes_its_own_value(tmp_path: Path) -> None:
    """Every value differs from its default, so a hard-coded key cannot pass."""
    cfg = make_config(
        node_id="cam-77",
        broker=BrokerConfig(
            host="broker.site.local", port=8883, username="u-77", password="p-77", tls=True
        ),
        covered_zones=["zone-a", "zone-b"],
        cue_window_s=7.5,
        outbox=OutboxConfig(path="/data/outbox-77.jsonl", max_bytes=1024, max_age_s=3600),
        status_interval_s=45,
        frigate=FrigateConfig(
            topic="site77/events",
            base_url="https://cam-77.local:8971",
            camera="front-gate",
            mqtt_host="192.168.77.1",
            mqtt_port=11883,
        ),
        mediamtx_address="relay.site.local:8554",
    )

    assert yaml.safe_load(written(tmp_path, cfg).read_text(encoding="utf-8")) == {
        "node_id": "cam-77",
        "broker": {
            "host": "broker.site.local",
            "port": 8883,
            "username": "u-77",
            "password": "p-77",
            "tls": True,
        },
        "covered_zones": ["zone-a", "zone-b"],
        "cue_window_s": 7.5,
        "outbox": {"path": "/data/outbox-77.jsonl", "max_bytes": 1024, "max_age_s": 3600},
        "status_interval_s": 45,
        "frigate": {
            "topic": "site77/events",
            "base_url": "https://cam-77.local:8971",
            "camera": "front-gate",
            "mqtt_host": "192.168.77.1",
            "mqtt_port": 11883,
        },
        "mediamtx_address": "relay.site.local:8554",
    }


def test_mediamtx_address_is_in_the_file_and_the_bridge_still_loads(tmp_path: Path) -> None:
    """go2rtc reads it from this file; the bridge reads named keys and ignores it."""
    cfg = make_config(outbox=outbox(path=str(tmp_path / "outbox.jsonl")))
    loaded = bridge_cli.load_config(str(written(tmp_path, cfg)))

    assert loaded["mediamtx_address"] == MEDIAMTX_ADDRESS
    assert example()["mediamtx_address"]
    assert isinstance(
        bridge_cli.make_bridge(loaded, central_client=Mock(), local_client=Mock()), Bridge
    )


def test_real_central_client_gets_the_credentials_and_tls(tmp_path: Path) -> None:
    cfg = make_config(broker=broker(tls=True))
    client = bridge_cli._build_central_client(bridge_cli.load_config(str(written(tmp_path, cfg))))

    assert client.username == "camera-cam-01"
    assert client.password == BROKER_PASSWORD
    assert client._ssl_context is not None


def test_real_central_client_has_no_tls_when_it_is_off(tmp_path: Path) -> None:
    client = bridge_cli._build_central_client(bridge_cli.load_config(str(written(tmp_path))))
    assert client._ssl_context is None


def test_anonymous_broker_reaches_the_real_client_without_credentials(tmp_path: Path) -> None:
    cfg = make_config(broker_anonymous=True, broker=broker(username="", password=""))
    assert validate(cfg) == []

    path = written(tmp_path, cfg)

    assert "broker_anonymous" not in path.read_text(encoding="utf-8")
    assert bridge_cli._build_central_client(bridge_cli.load_config(str(path))).username is None


def test_credentials_are_required_by_default() -> None:
    errors = validate(make_config(broker=broker(username="", password="")))
    assert any(e.startswith("broker.username") for e in errors)
    assert any(e.startswith("broker.password") for e in errors)


@pytest.mark.parametrize("anonymous", [False, True])
@pytest.mark.parametrize(
    ("username", "password"), [("camera-cam-01", ""), ("", BROKER_PASSWORD)]
)
def test_partial_credentials_are_always_rejected(
    anonymous: bool, username: str, password: str
) -> None:
    cfg = make_config(
        broker_anonymous=anonymous, broker=broker(username=username, password=password)
    )
    assert any(e.startswith("broker.") for e in validate(cfg))


def test_anonymous_broker_with_full_credentials_is_contradictory() -> None:
    errors = validate(make_config(broker_anonymous=True))
    assert any(e.startswith("broker.username") for e in errors)


@pytest.mark.parametrize("password", ["a\x85b", "a\u2028b", "a\ufeffb", "tab\tinside", "新しい"])
def test_unicode_breaks_and_controls_in_a_password_round_trip(
    tmp_path: Path, password: str
) -> None:
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
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            assert info.st_ino == tmp_path.stat().st_ino
            calls.append("fsync-dir")
        else:
            # The secret must be private from creation, and the rename must stay on
            # one filesystem and in a directory this code syncs.
            assert stat.S_IMODE(info.st_mode) == 0o600
            assert info.st_ino in {p.stat().st_ino for p in tmp_path.iterdir()}
            calls.append("fsync-file")
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
    assert raised.value.__context__ is None
    assert not path.exists()


# --- M4: the broker must not depend on Tailscale ---------------------------------


@pytest.mark.parametrize(
    "host",
    ["100.64.0.0", "100.64.0.1", "100.100.100.100", "100.127.255.255", "::ffff:100.64.0.1"],
)
def test_m4_a_tailscale_range_broker_is_rejected_with_a_clear_error(host: str) -> None:
    errors = validate(make_config(broker=broker(host=host)))

    assert len(errors) == 1
    assert "broker.host" in errors[0]
    assert "100.64.0.0/10" in errors[0]
    assert "site LAN" in errors[0]


@pytest.mark.parametrize(
    "host", ["100.63.255.255", "100.128.0.0", "10.0.0.2", "fd00::2", "::ffff:10.0.0.2", "broker.lan"]
)
def test_m4_addresses_outside_the_tailscale_range_are_accepted(host: str) -> None:
    assert validate(make_config(broker=broker(host=host))) == []


def test_m4_a_tailscale_range_broker_is_never_written(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"

    with pytest.raises(ValueError, match="100.64.0.0/10"):
        write_bridge_config(make_config(broker=broker(host="100.64.0.1")), path)

    assert not path.exists()


# --- owner decision: the NTP source comes from the portal -------------------------


def test_ntp_defaults_to_the_broker_host() -> None:
    assert effective_ntp_server(make_config(broker=broker(host="10.0.0.2"))) == "10.0.0.2"


def test_an_explicit_ntp_server_is_used() -> None:
    cfg = make_config(ntp_server="time.site.local")

    assert validate(cfg) == []
    assert effective_ntp_server(cfg) == "time.site.local"


@pytest.mark.parametrize(
    "server", ["   ", "ntp://10.0.0.2", "10.0.0.2:123", "two words", "bad\nNTP=evil", "10.0.0.999", None]
)
def test_an_invalid_ntp_server_is_rejected_by_name(server: Any) -> None:
    errors = validate(make_config(ntp_server=server))

    assert any("ntp_server" in e for e in errors)


def test_ntp_server_never_reaches_the_bridge_config(tmp_path: Path) -> None:
    path = tmp_path / "bridge.yaml"

    write_bridge_config(make_config(ntp_server="time.site.local"), path)

    assert "ntp" not in path.read_text().lower().replace("mediamtx", "")
    assert "time.site.local" not in path.read_text()
