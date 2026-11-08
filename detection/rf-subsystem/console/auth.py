"""Sign-in for the WBT Console.

Two roles: ``operator`` (watch, mark devices, set up the site) and ``admin``
(also clear and wipe site data, set retention). Passwords come from the
environment (``CONSOLE_OPERATOR_PASSWORD``, ``CONSOLE_ADMIN_PASSWORD``, set in
the ignored ``central/.env``). With neither set, sign-in is off and everyone
is admin; the page then shows "sign-in off" so nobody mistakes it for a
secured deployment.

Sessions are signed cookies (HMAC-SHA256, 12 h). The signing key is random and
kept in the data directory, so sessions survive a restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

ROLES = {"operator": 1, "admin": 2}
SESSION_S = 12 * 3600
COOKIE = "wbt_session"


class Auth:
    def __init__(self, operator_password: str | None, admin_password: str | None, key_path: str | Path | None) -> None:
        self.passwords = {r: p for r, p in (("admin", admin_password), ("operator", operator_password)) if p}
        self.enabled = bool(self.passwords)
        self.key = self._load_key(Path(key_path)) if key_path else secrets.token_bytes(32)

    @staticmethod
    def _load_key(path: Path) -> bytes:
        try:
            key = path.read_bytes()
            if len(key) >= 32:
                return key
        except OSError:
            pass
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def login(self, password: str) -> str | None:
        """Role for this password (admin wins if both match), else None."""
        role = None
        for r, p in self.passwords.items():   # compare every one: no early exit timing
            if hmac.compare_digest(password.encode(), p.encode()) and role is None:
                role = r
        return role

    def _sign(self, payload: str) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self, role: str, now: float | None = None) -> str:
        exp = int((now or time.time()) + SESSION_S)
        payload = f"{role}.{exp}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None, now: float | None = None) -> str | None:
        """Role of a valid, unexpired session token, else None."""
        if not self.enabled:
            return "admin"
        if not token or token.count(".") != 2:
            return None
        role, exp, sig = token.split(".")
        if role not in ROLES or not hmac.compare_digest(sig, self._sign(f"{role}.{exp}")):
            return None
        try:
            if int(exp) < (now or time.time()):
                return None
        except ValueError:
            return None
        return role

    @staticmethod
    def allows(role: str | None, needed: str) -> bool:
        return role is not None and ROLES.get(role, 0) >= ROLES[needed]
