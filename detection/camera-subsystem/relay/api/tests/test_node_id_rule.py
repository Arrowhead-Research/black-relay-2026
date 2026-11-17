"""The portal's copy of the relay's node_id rule, checked against the rule itself on samples.

``cameraprovision`` cannot import this package at run time, so it keeps its own copy. An id it
saves that this rule refuses is a kit that looks set up and never appears on central. Both packages
are real here, and the portal's side is its whole save check, not just the copy. Sampling catches a
changed pattern or a new rule these values touch, not every rule someone could add.
"""

from typing import Any

import pytest
from cameraprovision import config as portal
from cameraprovision.config import BrokerConfig, FrigateConfig, NodeConfig, validate, validate_saved

from relayapi import nodes

REFUSED = ["all", "all_others", "~x", "a" * 64, "CAM", "-cam", "", "cam\n", "cam_01", None]
TAKEN = ["a", "0", "cam-01", "cam-", "all-1", "al", "a" * 63]
# Every ASCII character and some past it, in the first, middle and last place.
CHARACTERS = [chr(code) for code in range(0x180)] + ["﻿", "\U0001f600"]
PLACED = [
    text
    for character in CHARACTERS
    for text in (f"{character}ab", f"a{character}b", f"ab{character}")
]
WORDS = ["none", "null", "true", "ALL", "All", "all\n", " all", "~", "~all", "all-others"]
LENGTHS = ["a" * length for length in (62, 63, 64, 65, 300)] + ["a" * 62 + "-", "-" * 63]


def config(node_id: Any = "cam-01", camera: str | None = None) -> NodeConfig:
    return NodeConfig(
        node_id=node_id,
        hostname="edge-7",
        wifi_ssid="Site-WiFi",
        wifi_psk="wifi-password",
        covered_zones=["driveway"],
        broker=BrokerConfig(host="10.0.0.2", username="kit1", password="pw"),
        frigate=FrigateConfig(base_url="https://cam-01.local:8971", camera=camera),
    )


def portal_saves(cfg: NodeConfig, field: str) -> bool:
    return not [problem for problem in validate(cfg) if problem.startswith(field)]


def test_the_rules_are_the_same():
    assert portal._RELAY_NODE_ID_RE.pattern == nodes._NODE_ID_RE.pattern
    assert portal._RELAY_RESERVED_NODE_IDS == nodes._MEDIAMTX_SPECIAL


def test_a_valid_config_has_nothing_else_wrong():
    assert validate(config()) == []


@pytest.mark.parametrize("value", REFUSED)
def test_the_relay_and_the_portal_refuse(value):
    assert nodes.node_id_ok(value) is False
    assert portal_saves(config(node_id=value), "node_id") is False


@pytest.mark.parametrize("value", TAKEN)
def test_the_relay_and_the_portal_take(value):
    assert nodes.node_id_ok(value) is True
    assert portal_saves(config(node_id=value), "node_id") is True


@pytest.mark.parametrize("value", [*PLACED, *WORDS, *LENGTHS])
def test_the_relay_and_the_portal_agree_on_a_node_id(value):
    assert portal_saves(config(node_id=value), "node_id") is nodes.node_id_ok(value)


@pytest.mark.parametrize("value", [*REFUSED, *TAKEN, *WORDS, *LENGTHS])
def test_the_relay_and_the_portal_agree_on_a_camera(value):
    # The relay holds camera_id to the same rule. None means "use the node_id", so it is taken.
    relay_takes = value is None or nodes.node_id_ok(value)
    assert portal_saves(config(camera=value), "frigate.camera") is relay_takes


@pytest.mark.parametrize("value", ["all", "a" * 64, "a" * 300])
def test_an_old_config_the_relay_refuses_still_loads(value):
    assert validate_saved(config(node_id=value)) == []
    assert nodes.node_id_ok(value) is False
