"""Tests for ``cameraprovision.netprofile`` — pure builders, nothing is executed."""

from __future__ import annotations

import random
import shutil
import string
import subprocess
import uuid
from pathlib import Path

import pytest

from cameraprovision.netprofile import (
    generate_ap_passphrase,
    nm_wifi_profile,
    render_nm_keyfile,
    tailscale_up_args,
)

needs_nmcli = pytest.mark.skipif(
    shutil.which("nmcli") is None, reason="nmcli not installed: keyfile round trips not run"
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


def test_profile_requires_ipv4_to_call_the_connection_up() -> None:
    """Uplink means a DHCP lease, so association alone must not count as connected."""
    assert nm_wifi_profile("s", "p", "h")["ipv4"]["may-fail"] == "false"


def test_profile_uuid_is_stable_and_derived_from_the_id() -> None:
    connection = nm_wifi_profile("site-a", "p", "h")["connection"]
    assert connection["uuid"] == nm_wifi_profile("site-b", "q", "i")["connection"]["uuid"]
    assert connection["uuid"] == str(uuid.uuid5(uuid.NAMESPACE_DNS, connection["id"]))


def test_every_value_is_a_string_ready_for_a_keyfile() -> None:
    for section in nm_wifi_profile("s", "p", "h").values():
        assert all(isinstance(value, str) for value in section.values())


# --- render_nm_keyfile ----------------------------------------------------------


def rendered_line(ssid: str, psk: str, key: str) -> str:
    text = render_nm_keyfile(nm_wifi_profile(ssid, psk, "h"))
    return next(line for line in text.splitlines() if line.startswith(f"{key}="))


def test_keyfile_has_every_section_and_key() -> None:
    assert render_nm_keyfile({"one": {"a": "1", "b": "2"}, "two": {"c": "3"}}) == (
        "[one]\na=1\nb=2\n\n[two]\nc=3\n"
    )


@pytest.mark.parametrize(
    ("psk", "line"),
    [
        ("back\\slash", r"psk=back\\slash"),
        ("abcdefg\\", r"psk=abcdefg\\"),
        (" lead", r"psk=\slead"),
        ("  two", r"psk=\s\stwo"),
        ("trail ", "psk=trail "),
        ("a;b#c=d", "psk=a;b#c=d"),  # only the SSID treats ; specially
        ("line\nbreak", r"psk=line\nbreak"),  # never a second keyfile line
    ],
)
def test_keyfile_escapes_the_psk(psk: str, line: str) -> None:
    assert rendered_line("s", psk, "psk") == line


@pytest.mark.parametrize(
    ("ssid", "line"),
    [
        (r"a\b", r"ssid=a\\b"),
        ("12;34;56;", r"ssid=12\\;34\\;56\\;"),
        (" lead", r"ssid=\slead"),
        ("é", "ssid=195;169;"),  # bytes, as NetworkManager's writer stores them
    ],
)
def test_keyfile_escapes_the_ssid(ssid: str, line: str) -> None:
    assert rendered_line(ssid, "p", "ssid") == line


def nmcli_offline(args: list[str], stdin: str | None = None) -> dict[str, dict[str, str]]:
    """Run ``nmcli --offline`` and parse the keyfile it prints. Touches no daemon."""
    done = subprocess.run(
        ["nmcli", "--offline", "connection", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    sections: dict[str, dict[str, str]] = {}
    for line in done.stdout.splitlines():
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
        elif "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    return sections


def through_nmcli_reader(ssid: str, psk: str) -> dict[str, dict[str, str]]:
    """Our keyfile, read by NetworkManager's parser and printed by its writer."""
    keyfile = render_nm_keyfile(nm_wifi_profile(ssid, psk, "edge-7"))
    return nmcli_offline(["modify", "connection.zone", "z"], stdin=keyfile)


def from_nmcli_writer(ssid: str, psk: str) -> dict[str, dict[str, str]]:
    """The same literal values, handed straight to NetworkManager's writer."""
    return nmcli_offline(
        [
            "add",
            "type",
            "wifi",
            "con-name",
            "x",
            "ifname",
            "*",
            "ssid",
            ssid,
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            psk,
        ]
    )


@needs_nmcli
@pytest.mark.parametrize(
    "psk",
    [
        "back\\slash",
        "abcdefg\\",
        " leadspace",
        "  twospaces",
        "trailspace ",
        "a;b#c=d",
        "100%(x)s",
        "q'u\"ote",
        "\\s-literal",
        "a1" * 32,
    ],
)
def test_psk_survives_networkmanagers_parser(psk: str) -> None:
    assert (
        through_nmcli_reader("s", psk)["wifi-security"]["psk"]
        == from_nmcli_writer("s", psk)["wifi-security"]["psk"]
    )


@needs_nmcli
@pytest.mark.parametrize(
    "ssid",
    [
        r"a\b",
        "12;34;56;",
        "1;2",
        "a;b",
        ";x",
        "x;",
        r"a\;b",
        r"a;b\c",
        "1234",
        " lead",
        "trail ",
        "#x",
        "a=b",
        "[x]",
        "é",
        "日本語",
    ],
)
def test_ssid_survives_networkmanagers_parser(ssid: str) -> None:
    assert (
        through_nmcli_reader(ssid, "password")["wifi"]["ssid"]
        == from_nmcli_writer(ssid, "password")["wifi"]["ssid"]
    )


@needs_nmcli
def test_every_setting_survives_networkmanagers_parser() -> None:
    profile = nm_wifi_profile("Site WiFi", "password", "edge-7")
    parsed = through_nmcli_reader("Site WiFi", "password")

    assert parsed["connection"]["id"] == profile["connection"]["id"]
    assert parsed["connection"]["uuid"] == profile["connection"]["uuid"]
    assert parsed["connection"]["autoconnect-retries"] == "0"
    assert parsed["wifi-security"]["key-mgmt"] == "wpa-psk"
    assert parsed["ipv4"]["method"] == "auto"
    assert parsed["ipv4"]["may-fail"] == "false"
    assert parsed["ipv4"]["dhcp-hostname"] == "edge-7"


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


def test_passphrase_has_the_documented_length() -> None:
    assert len(generate_ap_passphrase()) == 20


def test_passphrase_uses_the_full_label_safe_alphabet() -> None:
    """Exact, so a narrowed alphabet fails too. 4000 draws miss a symbol about 1 in 10^28."""
    allowed = set(string.ascii_letters + string.digits) - set("0O1lI")
    seen = {c for _ in range(200) for c in generate_ap_passphrase()}
    assert seen == allowed


def test_passphrases_differ_between_calls() -> None:
    assert len({generate_ap_passphrase() for _ in range(200)}) == 200


def test_passphrase_does_not_come_from_the_seedable_random_module() -> None:
    random.seed(1234)
    first = generate_ap_passphrase()
    random.seed(1234)
    assert generate_ap_passphrase() != first
