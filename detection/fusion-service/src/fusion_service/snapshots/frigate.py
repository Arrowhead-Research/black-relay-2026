"""Snapshots live on the kit, served by Frigate on port 8971 behind a shared-secret header.

The kit checks ``X-Proxy-Secret`` against a hash it computed at provisioning from the broker
password every kit holds (``camera-subsystem/cameralink/src/cameralink/frigate_config.py``,
``proxy_secret``). The formula here must stay equal to that one. Frigate's certificate is
self-signed on the kit, so verification is off; the kit's firewall decides who may connect.

A detection's ``snapshot_url`` names the kit by its mDNS hostname, which does not resolve from
the central node. Only its path is used; the address comes from the kit's latest heartbeat.
"""

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from fusion_service.domain.errors import NodeTimeout, NodeUnreachable

PROXY_SECRET_HEADER = "X-Proxy-Secret"
FRIGATE_PORT = 8971
_SNAPSHOT_PATH = re.compile(r"^/api/events/[A-Za-z0-9._-]{1,128}/snapshot\.jpg$")


def proxy_secret(broker_password: str) -> str:
    return hashlib.sha256(b"black-relay-frigate-proxy:" + broker_password.encode()).hexdigest()


def snapshot_path(snapshot_url: object) -> str | None:
    """The Frigate path inside a detection's snapshot_url, or None if it is not one."""
    if not isinstance(snapshot_url, str):
        return None
    path = urlsplit(snapshot_url).path
    return path if _SNAPSHOT_PATH.match(path) else None


def node_address(value: object) -> str | None:
    """A heartbeat's node_address as an IPv4 host address, or None if it cannot be one."""
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    if (
        address.is_loopback
        or address.is_multicast
        or address.is_unspecified
        or address.is_link_local
        or address.is_reserved
    ):
        return None
    return str(address)


@dataclass(frozen=True)
class Snapshot:
    media_type: str
    content: bytes


class FrigateSnapshots:
    """One HTTP client for every kit, holding the secret. Never logs it or the response."""

    def __init__(
        self,
        broker_password: str,
        *,
        timeout_s: float,
        max_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ):
        self._max_bytes = max_bytes
        self._client = httpx.Client(
            verify=False,
            timeout=timeout_s,
            transport=transport,
            headers={PROXY_SECRET_HEADER: proxy_secret(broker_password), "Accept": "image/jpeg"},
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, address: str, path: str) -> Snapshot | None:
        """The snapshot at `path` on the kit at `address`; None when the kit no longer has it."""
        url = httpx.URL(scheme="https", host=address, port=FRIGATE_PORT, path=path)
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code == 404:
                    return None
                if response.status_code in (401, 403):
                    raise NodeUnreachable("node_refused_secret")
                if response.status_code != 200:
                    raise NodeUnreachable(f"node_status_{response.status_code}")
                media_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
                if not media_type.startswith("image/"):
                    raise NodeUnreachable("node_sent_no_image")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise NodeUnreachable("snapshot_too_large")
                    chunks.append(chunk)
        except httpx.TimeoutException:
            raise NodeTimeout("node_timeout") from None
        except httpx.HTTPError:
            raise NodeUnreachable("node_unreachable") from None
        return Snapshot(media_type, b"".join(chunks))
