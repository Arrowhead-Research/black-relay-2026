"""Builders for the WiFi client profile, the Tailscale command, and the AP passphrase.

Pure: nothing here touches the system. The adapters that write the profile and run
the command are Task 6's, and they must treat the returned profile as secret because
it holds the WiFi PSK.
"""

from __future__ import annotations

import os
import secrets
import string

__all__ = ["generate_ap_passphrase", "nm_wifi_profile", "tailscale_up_args"]

# Fixed, so re-provisioning replaces the profile instead of adding one per site.
_PROFILE_ID = "black-relay-uplink"

# Printed on the kit label and typed by hand, so look-alike characters are left out.
_PASSPHRASE_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
# 57 symbols over 20 characters gives about 116 bits.
_PASSPHRASE_LENGTH = 20


def nm_wifi_profile(ssid: str, psk: str, hostname: str) -> dict[str, dict[str, str]]:
    """Return the NetworkManager keyfile sections for the site WiFi client profile.

    Values are unescaped strings. ``autoconnect-retries`` is 0 (retry forever) so a
    node that boots before the router rejoins by itself once WiFi returns.
    """
    return {
        "connection": {
            "id": _PROFILE_ID,
            "type": "wifi",
            "autoconnect": "true",
            "autoconnect-retries": "0",
        },
        "wifi": {"ssid": ssid, "mode": "infrastructure"},
        "wifi-security": {"key-mgmt": "wpa-psk", "psk": psk},
        "ipv4": {"method": "auto", "dhcp-hostname": hostname},
    }


def tailscale_up_args(auth_key_file: str | os.PathLike[str], hostname: str) -> list[str]:
    """Return the ``tailscale up`` argv, naming the key by file so it stays out of argv."""
    return [
        "tailscale",
        "up",
        f"--auth-key=file:{os.fspath(auth_key_file)}",
        f"--hostname={hostname}",
    ]


def generate_ap_passphrase() -> str:
    """Return a new random WPA2 passphrase for one kit's setup AP.

    Random rather than derived from the serial, which is public in the SSID. Generated
    once when the image is seeded; storing it (0600) is the caller's job.
    """
    return "".join(secrets.choice(_PASSPHRASE_ALPHABET) for _ in range(_PASSPHRASE_LENGTH))
