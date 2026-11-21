"""src/pytak_gateway/tls.py

Mutual TLS for the gateway's HTTP listener, with certificates that can be rotated while it runs.

A client must present a certificate signed by the TAK CA, the same one ATAK holds for TAK Server, and
must not be on that CA's revocation list. The check happens in the TLS handshake, before any route runs,
so it covers every route.

uvicorn builds one SSLContext at startup and has no reload hook of its own. We hand it one context through
`ssl_context_factory` and keep a reference, so a watcher can load changed files into that same context.
New handshakes see the change at once, open connections are untouched, and nothing restarts:

  * the server certificate and key are swapped (gateway cert rotation)
  * the CRL is loaded again (a device was revoked); the newest CRL wins, so this takes effect at once
  * new CA certificates are added (CA rollover); a CA is never removed from a live context, so dropping
    one takes a restart

With the CRL check on, a missing or expired CRL refuses every client (fail closed), so the watcher
warns well before the CRL expires.

The reload runs on the event loop, the same thread that does the handshakes, so a handshake never sees a
half-loaded context.
"""

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509

logger = logging.getLogger(__name__)

CRL_WARN_BEFORE = timedelta(days=14)


@dataclass(frozen=True)
class TLSConfig:
    cert: str  # the gateway's server certificate (PEM, may include the chain)
    key: str
    ca: str  # PEM bundle of the CA(s) client certs must chain to
    key_password: str | None = None
    crl: str | None = None  # PEM CRL of the CA that signs client certs; turns on revocation checks
    reload_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "TLSConfig | None":
        """None when GATEWAY_TLS_CERT is unset (plain HTTP). A half-set configuration raises, so a typo
        can never quietly leave the listener without client authentication."""
        cert = os.getenv("GATEWAY_TLS_CERT")
        if not cert:
            return None
        key, ca = os.getenv("GATEWAY_TLS_KEY"), os.getenv("GATEWAY_TLS_CA")
        if not key or not ca:
            raise ValueError("GATEWAY_TLS_CERT is set, so GATEWAY_TLS_KEY and GATEWAY_TLS_CA must be set too")
        return cls(
            cert=cert,
            key=key,
            ca=ca,
            key_password=os.getenv("GATEWAY_TLS_KEY_PASSWORD") or None,
            crl=os.getenv("GATEWAY_TLS_CRL") or None,
            reload_seconds=float(os.getenv("GATEWAY_TLS_RELOAD_SECONDS", "60")),
        )


def _stamp(*paths: str) -> tuple:
    """Changes when any file is replaced or rewritten; None for a file that is not there."""
    stamp = []
    for path in paths:
        try:
            st = os.stat(path)
            stamp.append((st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


class TLSReloader:
    """Owns the one SSLContext uvicorn serves with, and loads changed files into it."""

    def __init__(self, config: TLSConfig):
        self.config = config
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.verify_mode = ssl.CERT_REQUIRED
        if config.crl:
            self.context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        self._crl_next_update: datetime | None = None
        self._crl_warned = False
        # the first load raises on a bad file, so the gateway refuses to start rather than serve half-secured
        self._load_identity()
        self._load_ca()
        self._load_crl()
        self._stamps = self._current_stamps()

    def factory(self, config, default_factory) -> ssl.SSLContext:
        """uvicorn's `ssl_context_factory`: always the same context object, so later loads reach it."""
        return self.context

    def _load_identity(self) -> None:
        self.context.load_cert_chain(self.config.cert, self.config.key, self.config.key_password)

    def _load_ca(self) -> None:
        self.context.load_verify_locations(cafile=self.config.ca)

    def _load_crl(self) -> None:
        if not self.config.crl:
            return
        self.context.load_verify_locations(cafile=self.config.crl)
        with open(self.config.crl, "rb") as f:
            crl = x509.load_pem_x509_crl(f.read())
        self._crl_next_update = getattr(crl, "next_update_utc", None) or crl.next_update.replace(tzinfo=timezone.utc)
        self._crl_warned = False
        logger.info(f"loaded CRL {self.config.crl}, next update {self._crl_next_update:%Y-%m-%d}")

    def _current_stamps(self) -> dict[str, tuple]:
        return {
            "server certificate": _stamp(self.config.cert, self.config.key),
            "CA bundle": _stamp(self.config.ca),
            "CRL": _stamp(self.config.crl) if self.config.crl else (),
        }

    def reload_if_changed(self) -> None:
        """Load whatever changed since the last pass. A failed load keeps what is already loaded and is
        retried on the next pass (a cert and its key are replaced one after the other, so a pass can
        briefly see a mismatched pair)."""
        loaders = {
            "server certificate": self._load_identity,
            "CA bundle": self._load_ca,
            "CRL": self._load_crl,
        }
        now = self._current_stamps()
        for name, load in loaders.items():
            if now[name] == self._stamps[name]:
                continue
            try:
                load()
            except Exception as err:  # keep serving with what is loaded
                logger.error(f"reloading the {name} failed, still serving the previous one: {err}")
                continue
            self._stamps[name] = now[name]
            logger.info(f"reloaded the {name}")
        self._warn_if_crl_expiring()

    def _warn_if_crl_expiring(self) -> None:
        if self._crl_next_update is None or self._crl_warned:
            return
        left = self._crl_next_update - datetime.now(timezone.utc)
        if left < CRL_WARN_BEFORE:
            self._crl_warned = True
            what = (
                "has EXPIRED, every client is being refused"
                if left.total_seconds() <= 0
                else f"expires in {left.days} days"
            )
            logger.warning(
                f"the CRL {self.config.crl} {what}: regenerate it with revokeCert.sh (see README, Certificates and rotation) and publish it"
            )

    async def watch(self) -> None:
        """Poll the files for the life of the server (started by the app's lifespan)."""
        logger.info(
            f"mTLS on: client certs must chain to {self.config.ca}; reloading files every {self.config.reload_seconds:g}s"
        )
        while True:
            await asyncio.sleep(self.config.reload_seconds)
            self.reload_if_changed()
