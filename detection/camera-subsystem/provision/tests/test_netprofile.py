"""Tests for ``cameraprovision.netprofile`` — pure builders, nothing is executed."""

from __future__ import annotations

import random
import string
from pathlib import Path

import pytest

from cameraprovision.netprofile import (
    generate_ap_passphrase,
    nm_wifi_profile,
    tailscale_up_args,
)

# --- nm_wifi_profile ------------------------------------------------------------


def test_profile_is_an_autoconnecting_wpa_psk_dhcp_client() -> None:
    profile = nm_wifi_profile("Site WiFi", "s3cret-psk", "edge-7")

    assert profile["connection"]["type"] == "wifi"
    assert profile["connection"]["autoconnect"] == "true"
    assert profile["wifi"]["ssid"] == "Site WiFi"
    assert profile["wifi"]["mode"] == "infrastructure"
    assert profile["wifi-security"]["key-mgmt"] == "wpa-psk"
    assert profile["wifi-security"]["psk"] == "s3cret-psk"
    assert profile["ipv4"]["method"] == "auto"


def test_profile_has_exactly_the_keyfile_sections() -> None:
    assert set(nm_wifi_profile("s", "p", "h")) == {"connection", "wifi", "wifi-security", "ipv4"}


def test_profile_sends_the_hostname_over_dhcp() -> None:
    assert nm_wifi_profile("s", "p", "edge-7")["ipv4"]["dhcp-hostname"] == "edge-7"


def test_profile_never_stops_retrying_autoconnect() -> None:
    """A node that boots before the router must rejoin once WiFi returns (power cut).

    NetworkManager's default gives up after 4 attempts for a while; 0 means forever.
    """
    assert nm_wifi_profile("s", "p", "h")["connection"]["autoconnect-retries"] == "0"


def test_profile_id_does_not_depend_on_the_site() -> None:
    """Re-provisioning must replace the old profile, not add a second one."""
    first = nm_wifi_profile("site-a", "p", "h")["connection"]["id"]
    second = nm_wifi_profile("site-b", "q", "i")["connection"]["id"]
    assert first == second


@pytest.mark.parametrize("psk", ["p@ss w0rd", "a;b#c=d", "100%(x)s", "back\\slash", "q'u\"ote"])
def test_special_characters_in_the_psk_are_preserved(psk: str) -> None:
    assert nm_wifi_profile("s", psk, "h")["wifi-security"]["psk"] == psk


def test_every_value_is_a_string_ready_for_a_keyfile() -> None:
    for section in nm_wifi_profile("s", "p", "h").values():
        assert all(isinstance(value, str) for value in section.values())


# --- tailscale_up_args ----------------------------------------------------------


def test_tailscale_args_are_exact() -> None:
    assert tailscale_up_args("/run/camera/ts.key", "edge-7") == [
        "tailscale",
        "up",
        "--auth-key=file:/run/camera/ts.key",
        "--hostname=edge-7",
    ]


def test_tailscale_args_accept_a_path_and_keep_spaces_in_one_argument() -> None:
    args = tailscale_up_args(Path("/run/my camera/ts.key"), "edge-7")
    assert "--auth-key=file:/run/my camera/ts.key" in args


def test_key_contents_never_reach_argv(tmp_path: Path) -> None:
    key_file = tmp_path / "ts.key"
    key_file.write_text("tskey-auth-s3cret\n", encoding="utf-8")

    args = tailscale_up_args(key_file, "edge-7")

    assert "tskey-auth-s3cret" not in " ".join(args)


# --- generate_ap_passphrase -----------------------------------------------------


def test_passphrase_is_valid_for_wpa2_and_at_least_16_characters() -> None:
    passphrase = generate_ap_passphrase()
    assert 16 <= len(passphrase) <= 63
    assert passphrase.isascii() and passphrase.isprintable()


def test_passphrase_is_readable_from_a_printed_label() -> None:
    allowed = set(string.ascii_letters + string.digits) - set("0O1lI")
    for _ in range(200):
        assert set(generate_ap_passphrase()) <= allowed


def test_passphrases_differ_between_calls() -> None:
    assert len({generate_ap_passphrase() for _ in range(200)}) == 200


def test_passphrase_does_not_come_from_the_seedable_random_module() -> None:
    random.seed(1234)
    first = generate_ap_passphrase()
    random.seed(1234)
    assert generate_ap_passphrase() != first
