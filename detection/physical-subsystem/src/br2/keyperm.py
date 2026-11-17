"""Startup check for private key files: refuse a key that group or others can read."""
from __future__ import annotations

import logging
import os
import stat

log = logging.getLogger("br2.keyperm")


class KeyPermissionError(Exception):
    pass


def check_startup(service: str, cfg) -> None:
    """Refuse to start a service whose private key is group/world accessible. Secrets that are not keys (Control4 token,
    MQTT/fusion password files) only produce a warning: container secret mounts are often 0444."""
    keys: list[tuple[str | None, str]] = []
    secrets: list[tuple[str | None, str]] = []
    if service == "iq-remote":
        from .adapters.iq_remote import find_pki
        try:
            keys.append((find_pki(cfg.iq["pki_dir"], cfg.iq.get("ca_file"))[1], "IQ Remote pairing key"))
        except FileNotFoundError:
            pass                                    # the adapter reports a missing pairing itself
    elif service == "control4":
        secrets.append((cfg.c4.get("token_file"), "Control4 token"))
    elif service == "outbound":
        keys.append((cfg.api.get("tls_key"), "API TLS key"))
        secrets.append((cfg.mqtt.get("password_file"), "MQTT password file"))
    elif service == "fusion":
        tls = cfg.fusion.get("tls") or {}
        keys.append((tls.get("keyfile"), "fusion client TLS key"))
        secrets.append((cfg.fusion.get("password_file"), "fusion password file"))
    for path, what in keys:
        require_private_key(path, what)
    for path, what in secrets:
        try:
            if path and stat.S_IMODE(os.stat(path).st_mode) & 0o007:
                log.warning("%s %s is readable by everyone (mode %04o); restrict it", what, path,
                            stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass


def require_private_key(path: str | os.PathLike | None, what: str = "private key") -> None:
    """Raise KeyPermissionError if `path` is readable/writable by group or others (mode & 077). A missing file is the
    caller's problem (its own error follows), not ours."""
    if not path:
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & 0o077:
        raise KeyPermissionError(
            f"{what} {path} has mode {mode:04o}: it must not be accessible to group/others. Fix with: chmod 600 {path}")
