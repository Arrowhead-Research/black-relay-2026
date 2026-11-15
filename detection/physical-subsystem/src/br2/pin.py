"""Leaf pinning of the panel's TLS server certificate (IQ Remote link).

Why: the IQ Remote TLS context trusts the panel CA, and every paired device (phones, tablets, other virtual remotes) holds
a certificate signed by that same CA. Without a leaf pin, anything that can present ANY panel-CA-signed certificate on the
panel address would be accepted as "the panel". The pin is the SHA-256 of the server certificate's SubjectPublicKeyInfo
(SPKI), so it survives a re-issue that keeps the key and breaks on any different key.

Modes (`iq_remote.pin_mode`; the strict preset forces `preset`):
  required (default)  a pin must already be RECORDED in `<pki_dir>/panel_pin.json` (by `br2 pair`, which records it during
                      the operator-attended pairing, or by `br2 repin --yes`). With no pin the adapter REFUSES to connect
                      and says to run `br2 pair` / `br2 repin`. Nothing is ever recorded automatically, so a restart never
                      re-trusts whatever answers at the panel address.
  tofu                opt-in (dev/harness): with no pin at all, the first verified connection records it (trust on first use).
  preset              `iq_remote.panel_spki_sha256` is set: that value is the pin, the file is ignored, nothing is recorded.
FAIL CLOSED: a pin file that EXISTS but cannot be read and validated (empty, corrupt JSON, bad hash, unreadable by this uid)
is never treated as "no pin": the connection is refused (health reason `pin_unreadable`, loud log), in every mode but
`preset`. Only a truly absent file means "no pin yet".

Legitimate panel certificate rotation: REFUSED until the operator runs `br2 repin --yes`. (Rotation on a Qolsys panel is
rare; a surprise rotation is indistinguishable from an impostor.) The harness conformance suite rotates the panel
certificate on purpose, so `iq_remote.pin_accept_rotation: true` (or env BR2_IQ_PIN_ACCEPT_ROTATION=1, which
harness_start.sh sets) makes a STORED pin follow a rotation instead. It is off by default, is never honoured together with
the strict config preset, and must not be used in production.

On mismatch nothing is sent: the check runs inside the TLS handshake, before paho writes the MQTT CONNECT, and the
adapter reports link-down with reason `pin_mismatch`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("br2.pin")

PIN_FILE = "panel_pin.json"
_ID12 = re.compile(r"[A-Fa-f0-9]{12}")


def pki_folders(pki_dir: str | os.PathLike) -> tuple[Path, list[Path]]:
    """Where the pairing material lives. Looks in `<pki_dir>/<12hex>/`, then `<pki_dir>/pki/<12hex>/` (the layout
    qolsys-controller writes: given D it stores `D/pki/<12hex>/`), else the
    pki dir itself (flat). Returns (container, identity folders found in it); the container holds the identity folder
    and is where the pin file lives, so either spelling of the path finds the same pin."""
    root = Path(pki_dir)
    for c in (root, root / "pki"):
        try:
            found = sorted(d for d in c.iterdir() if d.is_dir() and _ID12.fullmatch(d.name))
        except OSError:
            continue
        if found:
            return c, found
    return root, []


class PinMismatch(ssl.SSLError):
    """The panel presented a certificate whose public key is not the pinned one."""


class PinUnreadable(PinMismatch):
    """A pin file exists but cannot be read and validated: refuse (never fall back to trusting)."""


class PinMissing(PinMismatch):
    """pin_mode is `required` and no pin has been recorded yet."""


MODES = ("required", "tofu", "preset")
DEFAULT_OWNER = "10001:10000"          # uid:gid the compose services run as (iq-remote); `iq_remote.owner` overrides


def parse_owner(spec: str | None, ref: str | os.PathLike | None = None) -> tuple[int, int] | None:
    """`uid:gid`, or `dir` = whoever owns `ref` (the pki dir). None/'' -> the default service owner."""
    spec = (str(spec).strip() if spec else "") or DEFAULT_OWNER
    if spec == "dir":
        try:
            st = os.stat(ref) if ref is not None else None
        except OSError:
            st = None
        return (st.st_uid, st.st_gid) if st is not None else None
    uid, _, gid = spec.partition(":")
    try:
        return int(uid), (int(gid) if gid else -1)
    except ValueError:
        raise ValueError(f"iq_remote.owner must be 'uid:gid' or 'dir', got {spec!r}") from None


def chown_to(path: str | os.PathLike, owner: tuple[int, int] | None) -> None:
    """When running as root, hand `path` to the service uid/gid, so that what a root-run command writes is readable by
    the (non-root, capability-less) service. Does nothing when not root: files then belong to the invoking user."""
    if owner is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    os.chown(path, owner[0], owner[1])


def norm_hex(s: str | None) -> str | None:
    if not s:
        return None
    h = re.sub(r"^sha-?256[:=]?", "", str(s).strip().lower())
    h = re.sub(r"[^0-9a-f]", "", h)
    return h if len(h) == 64 else None


# ---- minimal DER walker: SPKI out of an X.509 certificate without needing the `cryptography` package
def _tlv(b: bytes, i: int) -> tuple[int, int, int]:
    """(tag, header_len, content_len) of the element at b[i]."""
    tag = b[i]
    n = b[i + 1]
    if n < 0x80:
        return tag, 2, n
    k = n & 0x7F
    if k == 0 or k > 4:
        raise ValueError("bad DER length")
    return tag, 2 + k, int.from_bytes(b[i + 2:i + 2 + k], "big")


def spki_der(cert_der: bytes) -> bytes:
    tag, h, n = _tlv(cert_der, 0)                      # Certificate ::= SEQUENCE
    if tag != 0x30 or h + n > len(cert_der):
        raise ValueError("not a DER certificate")
    i = h
    tag, h, n = _tlv(cert_der, i)                      # tbsCertificate ::= SEQUENCE
    if tag != 0x30:
        raise ValueError("bad tbsCertificate")
    j, end = i + h, i + h + n
    tag, h, n = _tlv(cert_der, j)
    if tag == 0xA0:                                    # [0] version (optional)
        j += h + n
    for _ in range(5):                                 # serial, signature, issuer, validity, subject
        _, h, n = _tlv(cert_der, j)
        j += h + n
    tag, h, n = _tlv(cert_der, j)                      # subjectPublicKeyInfo
    if tag != 0x30 or j + h + n > end:
        raise ValueError("bad SPKI")
    return cert_der[j:j + h + n]


def spki_sha256(cert_der: bytes) -> str:
    return hashlib.sha256(spki_der(cert_der)).hexdigest()


def fmt(fp: str | None) -> str:
    return ":".join(fp[i:i + 2] for i in range(0, 64, 2)) if fp else "(none)"


class PanelPin:
    def __init__(self, pki_dir: str | os.PathLike, preset: str | None = None, accept_rotation: bool = False,
                 source: str = "tofu", mode: str = "required", owner: str | None = None):
        self.dir = pki_folders(pki_dir)[0]
        self.path = self.dir / PIN_FILE
        self.preset = norm_hex(preset)
        if preset and not self.preset:
            raise ValueError("iq_remote.panel_spki_sha256 must be 64 hex characters (a SHA-256)")
        mode = (mode or "required").strip().lower()
        if mode not in MODES:
            raise ValueError(f"iq_remote.pin_mode must be one of {', '.join(MODES)} (got {mode!r})")
        if self.preset:
            mode = "preset"
        elif mode == "preset":
            raise ValueError("iq_remote.pin_mode is 'preset' but iq_remote.panel_spki_sha256 is not set")
        self.mode = mode
        self.accept_rotation = bool(accept_rotation)
        self.source = source
        self.owner = owner
        self._mem: str | None = None                   # tofu only: pin held in memory when the file cannot be written
        self.mismatches = 0
        self.last_mismatch: dict | None = None
        self.last_problem: str | None = None           # "pin_unreadable" | "pin_missing" | "pin_mismatch" of the last check

    # ---- storage
    def stored(self) -> str | None:
        """The recorded pin. None ONLY when the file is truly absent (and nothing is held in memory). A file that exists
        but cannot be read, parsed or validated raises PinUnreadable: it must never read as "no pin"."""
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            return self._mem
        except OSError as e:
            raise PinUnreadable(f"{self.path} cannot be examined: {e}") from e
        # Something is at the pin path (a file, a symlink, a directory, a fifo...). Only a regular file (or a symlink that
        # resolves to one) can hold a pin; a dangling symlink, a directory, a fifo or a socket is a pin we cannot read:
        # fail closed, never "absent" (which, in tofu mode, would let the next connection re-record whatever answers).
        try:
            st = os.stat(self.path)
        except OSError as e:
            raise PinUnreadable(f"{self.path} exists but cannot be read (dangling symlink?): {e}") from e
        if not stat.S_ISREG(st.st_mode):
            raise PinUnreadable(f"{self.path} exists but is not a regular file")
        try:
            raw = self.path.read_bytes()
        except OSError as e:
            raise PinUnreadable(f"{self.path} exists but cannot be read: {e}") from e
        try:
            rec = json.loads(raw.decode("utf-8"))
            fp = norm_hex(rec.get("spki_sha256")) if isinstance(rec, dict) else None
        except (ValueError, UnicodeDecodeError, AttributeError) as e:
            raise PinUnreadable(f"{self.path} exists but is not valid JSON ({type(e).__name__})") from e
        if fp is None:
            raise PinUnreadable(f"{self.path} exists but holds no valid spki_sha256 (empty, truncated or bad hash)")
        return fp

    def startup_banner(self) -> str | None:
        """A prominent multi-line warning when the pin protection is weakened (tofu, or following rotations); None when
        the strict settings are in use. Logged once at adapter start."""
        why = []
        if self.mode == "tofu":
            why.append("pin_mode is 'tofu': with no pin recorded, the FIRST certificate presented at the panel address "
                       "is trusted and recorded")
        if self.accept_rotation:
            why.append("pin_accept_rotation is ON: a changed panel certificate is silently accepted and re-recorded")
        if not why:
            return None
        bar = "!" * 78
        return "\n".join([bar, "!! WEAKENED PANEL CERTIFICATE PINNING (development / harness use only)", *("!! " + w for w in why),
                          "!! An impostor on the panel's address would be trusted. Do NOT run like this in production:",
                          "!! use the strict defaults (record the pin with `br2 pair` / `br2 repin --yes`).", bar])

    def expected(self) -> str | None:
        return self.preset or self.stored()

    def persist(self, fp: str, source: str) -> bool:
        """Persist (0600, atomic). Returns False (and keeps the pin in memory) if the directory is not writable."""
        rec = json.dumps({"spki_sha256": fp, "source": source,
                          "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n"
        tmp = self.path.with_name(PIN_FILE + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(rec.encode())
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            chown_to(tmp, parse_owner(self.owner, self.dir))     # root-run command: the SERVICE uid must be able to read it
            os.replace(tmp, self.path)
            self._mem = None
            return True
        except (OSError, ValueError) as e:
            self._mem = fp if self.mode == "tofu" else None
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log.warning("panel pin: cannot persist %s (%s)%s", self.path, e,
                        "; holding it in memory for this process only (pin_mode tofu). Run `br2 repin --yes` on the host "
                        "or set iq_remote.panel_spki_sha256 to make it durable." if self.mode == "tofu" else "")
            return False

    # ---- the check (called from inside the TLS handshake)
    def check(self, cert_der: bytes | None) -> str:
        self.last_problem = None
        if not cert_der:
            raise PinMismatch("the panel presented no certificate")
        fp = spki_sha256(cert_der)
        if self.preset:
            if fp == self.preset:
                return "ok"
            self._mismatch(fp, self.preset, "config iq_remote.panel_spki_sha256")
        try:
            cur = self.stored()
        except PinUnreadable as e:
            self.last_problem = "pin_unreadable"
            log.error("PANEL PIN UNREADABLE: %s. Refusing to talk to the panel: a pin file that exists but cannot be "
                      "read is NOT treated as 'no pin'. NOTHING was sent. Fix the file's owner/mode (it must be readable "
                      "by the service uid) or, if the pin is damaged, verify the panel and re-record it with "
                      "`br2 repin --yes`.", e)
            raise
        if cur is None:
            if self.mode != "tofu":
                self.last_problem = "pin_missing"
                log.error("PANEL PIN MISSING: no pin has been recorded (%s) and iq_remote.pin_mode is 'required'. "
                          "Refusing to connect; NOTHING was sent. Pair this bridge with `br2 pair` (which records the "
                          "pin while you are at the panel) or record the panel's current certificate with "
                          "`br2 repin --yes`, then restart.", self.path)
                raise PinMissing(f"no panel pin recorded in {self.path}; run `br2 pair` or `br2 repin`")
            self.persist(fp, self.source)
            log.warning("panel pin: no pin yet; trusting the panel certificate presented now (SPKI sha256 %s) "
                        "and recording it (pin_mode tofu)", fmt(fp))
            return "recorded"
        if fp == cur:
            return "ok"
        if self.accept_rotation:
            log.warning("panel pin: panel certificate changed (%s -> %s); ACCEPTED because pin_accept_rotation is on "
                        "(harness/dev only)", fmt(cur), fmt(fp))
            self.persist(fp, "auto-rotation")
            return "rotated"
        self._mismatch(fp, cur, str(self.path))
        return "unreachable"

    def _mismatch(self, got: str, want: str, where: str) -> None:
        self.mismatches += 1
        self.last_problem = "pin_mismatch"
        self.last_mismatch = {"expected": want, "presented": got, "at": time.time()}
        log.error("PANEL PIN MISMATCH: the certificate presented at the panel address has SPKI sha256 %s but the pin "
                  "(%s) is %s. Refusing to talk to it; NOTHING was sent. If the panel's certificate was changed on "
                  "purpose, verify it and run `br2 repin --yes`; otherwise treat this as an impostor on your LAN.",
                  fmt(got), where, fmt(want))
        raise PinMismatch(f"panel certificate pin mismatch (presented {got[:16]}..., pinned {want[:16]}...)")


def pinned_socket_class(pin: PanelPin):
    """An SSLSocket whose handshake fails unless the server's SPKI matches. paho wraps with
    do_handshake_on_connect=False and calls do_handshake() itself, before it writes a single MQTT byte."""
    class PinnedSSLSocket(ssl.SSLSocket):
        def do_handshake(self, *a, **k):
            super().do_handshake(*a, **k)
            try:
                pin.check(self.getpeercert(binary_form=True))
            except PinMismatch:                        # includes PinUnreadable / PinMissing
                try:
                    self.close()
                except OSError:
                    pass
                raise
    return PinnedSSLSocket
