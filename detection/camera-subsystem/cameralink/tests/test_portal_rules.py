"""The portal's copy of render's rules, held equal to the rules themselves.

``cameraprovision`` cannot import this package at run time, so it keeps its own copy. A value it
lets through that render refuses would stop Frigate, and one it refuses that render takes would
turn an operator away for nothing. Both packages are real here.
"""

from pathlib import Path

import pytest
from cameraprovision import config as portal
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig, frigate_problems
from cameraprovision.store import NodeStore
from cameraprovision.store import Paths as StorePaths

from cameralink import frigate_config
from cameralink.daemon import _read_flipped
from cameralink.frigate_config import FrigateConfigError, render, write_env

NODE_ID = "cam-01"
SECRET = "a" * 64
# Every ASCII character and some past it, in the first, middle and last place.
CHARACTERS = [chr(code) for code in range(0x180)] + [" ", "﻿", "\U0001f600"]
PLACED = [
    text for character in CHARACTERS for text in (f"{character}ab", f"a{character}b", f"ab{character}")
]


def render_accepts(zones=("driveway",), relay_user="kit1") -> bool:
    try:
        render(NODE_ID, list(zones), relay_user, None, SECRET)
    except FrigateConfigError:
        return False
    return True


def write_env_accepts(path: Path, relay_password: str) -> bool:
    try:
        write_env(path, "cam-password", relay_password, SECRET)
    except (FrigateConfigError, UnicodeError):
        return False
    return True


def portal_accepts(zones=("driveway",), relay_user="kit1", relay_password="pw") -> bool:
    return frigate_problems(NODE_ID, list(zones), relay_user, relay_password) == []


@pytest.mark.parametrize("name", ["_ZONE_NAME_RE", "_RELAY_USER_RE", "_RELAY_PASSWORD_RE"])
def test_the_patterns_are_the_same(name):
    assert getattr(portal, name).pattern == getattr(frigate_config, name).pattern


@pytest.mark.parametrize("zone", [*PLACED, "", "x", "xy", NODE_ID, "1e3", "0o17", "a" * 300])
def test_the_portal_and_render_agree_on_a_zone(zone):
    assert portal_accepts(zones=[zone]) is render_accepts(zones=[zone])


@pytest.mark.parametrize("user", [*PLACED, "", "x", "camera-cam-01", "a" * 300])
def test_the_portal_and_render_agree_on_a_relay_user(user):
    assert portal_accepts(relay_user=user) is render_accepts(relay_user=user)


@pytest.mark.parametrize("password", [*PLACED, "", "x", "a" * 300])
def test_the_portal_and_write_env_agree_on_a_relay_password(tmp_path, password):
    env = tmp_path / "frigate.env"
    assert portal_accepts(relay_password=password) is write_env_accepts(env, password)


def test_a_config_the_portal_saves_renders(tmp_path):
    cfg = NodeConfig(
        node_id=NODE_ID,
        hostname="edge-7",
        wifi_ssid="Site-WiFi",
        wifi_psk="wifi-password",
        camera_upside_down=True,
        mediamtx_address="10.0.0.2:8554",
        covered_zones=["perimeter-north", "driveway"],
        broker=BrokerConfig(host="10.0.0.2", username="camera-cam-01", password="p@ss:#{}"),
        frigate=FrigateConfig(base_url="https://cam-01.local:8971"),
    )
    assert portal.validate(cfg) == []

    assert render(cfg.node_id, cfg.covered_zones, cfg.broker.username, None, SECRET)
    write_env(tmp_path / "frigate.env", "cam-password", cfg.broker.password, SECRET)


def test_the_daemon_reads_the_flip_where_the_portal_saves_it(tmp_path):
    paths = StorePaths(
        bridge_config=tmp_path / "bridge.yaml",
        mediamtx_env=tmp_path / "mediamtx.env",
        nm_connections_dir=tmp_path / "nm",
        state_dir=tmp_path / "state",
        ntp_dropin=tmp_path / "ntp.conf",
        central_hosts=tmp_path / "central-hosts.nft",
    )
    paths.state_dir.mkdir()
    store = NodeStore(paths, chown=lambda path: None)
    cfg = NodeConfig(
        node_id=NODE_ID,
        hostname="edge-7",
        wifi_ssid="Site-WiFi",
        wifi_psk="wifi-password",
        camera_upside_down=True,
        mediamtx_address="10.0.0.2:8554",
        covered_zones=["driveway"],
        broker=BrokerConfig(host="10.0.0.2", username="kit1", password="pw"),
        frigate=FrigateConfig(base_url="https://cam-01.local:8971"),
    )

    store.save(cfg)

    assert _read_flipped(paths.node_file) is True
