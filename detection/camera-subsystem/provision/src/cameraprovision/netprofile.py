"""Builders for the WiFi client profile, the Tailscale command, and the AP passphrase.

Pure: nothing here touches the system. The adapters that write the keyfile and run
the command are Task 6's, and they must treat the returned profile and keyfile text
as secret because they hold the WiFi PSK.
"""

from __future__ import annotations

import os
import secrets
import string
import uuid

__all__ = [
    "UPLINK_PROFILE_ID",
    "generate_ap_passphrase",
    "nm_wifi_profile",
    "render_nm_keyfile",
    "tailscale_up_args",
]

# Fixed, so re-provisioning replaces the profile instead of adding one per site.
UPLINK_PROFILE_ID = "black-relay-uplink"

# Printed on the kit label and typed by hand, so look-alike characters are left out.
_PASSPHRASE_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
# 57 symbols over 20 characters gives about 116 bits.
_PASSPHRASE_LENGTH = 20


def nm_wifi_profile(
    ssid: str, psk: str, hostname: str, *, interface: str = "wlan0"
) -> dict[str, dict[str, str]]:
    """Return the NetworkManager keyfile sections for the site WiFi client profile.

    Values are unescaped strings; :func:`render_nm_keyfile` turns them into a file.
    ``autoconnect-retries`` is 0 (retry forever) so a node that boots before the router
    rejoins by itself once WiFi returns. ``may-fail`` is false because uplink means a
    DHCP lease, so association alone must not count as connected.
    """
    return {
        "connection": {
            "id": UPLINK_PROFILE_ID,
            "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, UPLINK_PROFILE_ID)),
            "type": "wifi",
            "interface-name": interface,
            "autoconnect": "true",
            "autoconnect-retries": "0",
        },
        "wifi": {"ssid": ssid, "mode": "infrastructure"},
        "wifi-security": {"key-mgmt": "wpa-psk", "psk": psk},
        "ipv4": {"method": "auto", "may-fail": "false", "dhcp-hostname": hostname},
    }


def _escape_keyfile_value(value: str) -> str:
    """Apply GKeyFile string escaping, including each leading space."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    body = escaped.lstrip(" ")
    return "\\s" * (len(escaped) - len(body)) + body


def _encode_keyfile_ssid(ssid: str) -> str:
    """Encode an SSID as NetworkManager's keyfile writer does.

    The reader takes a bare ``;`` to mean a list of byte values, so ``12;34;56;`` would
    be read as three bytes. A literal one is stored as ``\\\\;``. Anything else outside
    printable ASCII is stored as byte values.
    """
    if not (ssid.isascii() and ssid.isprintable()):
        return "".join(f"{byte};" for byte in ssid.encode("utf-8"))
    return _escape_keyfile_value(ssid).replace(";", "\\\\;")


def render_nm_keyfile(profile: dict[str, dict[str, str]]) -> str:
    """Return ``profile`` as NetworkManager keyfile text, escaped so values round-trip."""
    blocks = []
    for section, settings in profile.items():
        lines = [f"[{section}]"]
        for key, value in settings.items():
            is_ssid = (section, key) == ("wifi", "ssid")
            encoded = _encode_keyfile_ssid(value) if is_ssid else _escape_keyfile_value(value)
            lines.append(f"{key}={encoded}")
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def tailscale_up_args(
    auth_key_file: str | os.PathLike[str], hostname: str, *, force_reauth: bool = False
) -> list[str]:
    """Return the ``tailscale up`` argv, naming the key by file so it stays out of argv.

    ``--accept-dns=false`` keeps the tailnet's DNS away from the node's resolver, because
    fusion must not depend on Tailscale. ``force_reauth`` makes a node that is already
    logged in take a new key.
    """
    return [
        "tailscale",
        "up",
        f"--auth-key=file:{os.fspath(auth_key_file)}",
        f"--hostname={hostname}",
        "--accept-dns=false",
        *(["--force-reauth"] if force_reauth else []),
    ]


def generate_ap_passphrase() -> str:
    """Return a new random WPA2 passphrase for one kit's setup AP.

    Random rather than derived from the serial, which is public in the SSID. Generated
    once when the image is seeded; storing it (0600) is the caller's job.
    """
    return "".join(secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(_PASSPHRASE_LENGTH))
