"""src/pytak_gateway/mission_package.py

The ATAK mission package API interface: a site's mission package is a mission on TAK Server, made and removed
through its Marti REST API (port 8443, mutual TLS) with the gateway's own client certificate.

Like fusion_client.py, this module only speaks the wire contract and says what each answer means. A password is
never logged or put in an error, and neither is a URL: an error carries a status or an exception class name.
"""

import logging
import os
import ssl
from typing import Any
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

# The creatorUid TAK records as the mission's owner, and the client uid every gateway CoT connection names itself
# (cot.identify_event): TAK subscribes a mission's creator, so the gateway's CoT reaches the missions it made.
GATEWAY_UID = "pytak-gateway"
DEFAULT_CERT = "/certs/pytak-gateway.pem"
DEFAULT_KEY = "/certs/pytak-gateway.key"


class MissionPackageError(Exception):
    """The mission package could not be made or removed; what happened is only in the message, never a password."""


class MissionPackageExists(MissionPackageError):
    """The mission package is already on TAK Server. Whoever made it, this site cannot claim it."""


class AtakMissionPackages:
    """TAK Server's mission API. `create`, `remove` and `remove_items` either do what they say or raise
    MissionPackageError, so a site is never built or removed without its mission package actually being made or
    removed."""

    def __init__(
        self,
        base_url: str | None,
        ssl_context: ssl.SSLContext | None = None,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 10.0,
    ):
        """
        Args:
            base_url: TAK Server's API, e.g. https://takserver:8443. None: every call refuses (TAK_API_URL unset)
            ssl_context: trusts the TAK CA and presents the gateway's client certificate
            session: use this session instead of making one on first use (tests); close() leaves it open
            timeout: seconds for one call
        """
        self.base_url = base_url.rstrip("/") if base_url else None
        self.ssl_context = ssl_context
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._owns_session = session is None

    @classmethod
    def from_env(cls) -> "AtakMissionPackages":
        """TAK_API_URL is TAK Server's API. The client certificate is TAK_API_CERT and TAK_API_KEY (the PEM and key
        the pki service publishes), unlocked with TLS_PASSWORD and checked against TLS_CAFILE, as for CoT.
        TLS_DONT_CHECK_HOSTNAME=true skips the hostname check, as for CoT. Unset TAK_API_URL gives a client that
        refuses every call; a URL with the certificate unreadable raises, so a typo shows at start-up."""
        url = os.getenv("TAK_API_URL") or None
        if url is None:
            logger.warning("TAK_API_URL is not set: no site can be built or removed (its mission package cannot be)")
            return cls(None)
        context = ssl.create_default_context(cafile=os.getenv("TLS_CAFILE") or None)
        if os.getenv("TLS_DONT_CHECK_HOSTNAME", "").lower() == "true":
            context.check_hostname = False
        context.load_cert_chain(
            os.getenv("TAK_API_CERT", DEFAULT_CERT),
            os.getenv("TAK_API_KEY", DEFAULT_KEY),
            os.getenv("TLS_PASSWORD") or None,
        )
        return cls(url, context)

    async def create(self, name: str, password: str) -> None:
        """Make the mission `name` on TAK, protected by `password`. MissionPackageExists: it is already there."""
        status = await self._call("PUT", name, {"password": password, "creatorUid": GATEWAY_UID})
        if status == 409:
            raise MissionPackageExists("the mission package already exists")
        self._check(status, "create", ok=(200, 201))

    async def remove(self, name: str, password: str) -> None:
        """Remove the mission `name` from TAK. One that is already gone counts as removed."""
        status = await self._call("DELETE", name, {"password": password, "creatorUid": GATEWAY_UID})
        if status == 404:
            logger.info("mission package %r was already gone from TAK", name)
            return
        self._check(status, "remove", ok=(200, 204))

    async def remove_items(self, name: str, uids: list[str]) -> None:
        """Take these map items (CoT uids) out of the mission `name`, so a phone that syncs it later does not get
        them back. A uid that is not in it counts as taken out (TAK answers 200), and so does every uid of a
        mission that is gone (404). No owner token is needed: the mission's default role may do it."""
        for uid in uids:
            status = await self._call("DELETE", name, {"uid": uid, "creatorUid": GATEWAY_UID}, path="/contents")
            if status == 404:
                return
            self._check(status, "take an item out of", ok=(200, 204))

    async def close(self) -> None:
        """Close the session this client opened (never one it was given)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    @staticmethod
    def _check(status: int, action: str, ok: tuple[int, ...]) -> None:
        if status in ok:
            return
        if status in (401, 403):
            raise MissionPackageError(f"TAK Server did not allow the gateway to {action} it (HTTP {status})")
        raise MissionPackageError(f"TAK Server could not {action} it (HTTP {status})")

    async def _call(self, method: str, name: str, params: dict[str, Any], path: str = "") -> int:
        """The HTTP status of one call. No answer at all is a MissionPackageError carrying the class name only: an
        aiohttp message can carry the URL, and the password is in the query."""
        if self.base_url is None:
            raise MissionPackageError("TAK_API_URL is not set")
        if self._session is None:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.ssl_context))
        url = f"{self.base_url}/Marti/api/missions/{quote(name, safe='')}{path}"
        try:
            async with self._session.request(method, url, params=params, timeout=self.timeout) as response:
                await response.read()
                return response.status
        except (TimeoutError, aiohttp.ClientError) as e:
            raise MissionPackageError(f"TAK Server did not answer ({type(e).__name__})") from None
