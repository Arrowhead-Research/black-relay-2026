"""Turn on a factory camera's HTTP and RTSP over Baichuan, the only port it opens.

reolink_aio and aiohttp cost 20 MB of memory (measured), which a node would keep for nothing once
its camera is adopted, so ``adopt`` imports this module only for a camera that needs it. Importing
it also gives everything in ``camera_api``.

The URL and secrecy rules are the ones in ``camera_api``.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET

from reolink_aio import exceptions as reolink_errors
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType
from reolink_aio.enums import ConnectionEnum

from cameralink.camera_api import (
    _LOCKED_WORDING,
    _REJECTED_WORDINGS,
    ApiError,
    AuthFailed,
    CameraError,
    LockedOut,
    LoginOutcomeUnknown,
    ReolinkClient,
    Unreachable,
)

__all__ = [
    "ApiError",
    "AuthFailed",
    "CameraError",
    "LockedOut",
    "LoginOutcomeUnknown",
    "ReolinkClient",
    "Unreachable",
    "enable_services",
]

_log = logging.getLogger(__name__)

_BAICHUAN_PORT = 9000
_PROBE_TIMEOUT = 3.0

# Everything reolink_aio 0.22.0 was seen or could be expected to raise, including
# XML and text decoding of a bad frame, which it does not wrap.
_BAICHUAN_ERRORS = (
    reolink_errors.ReolinkError,
    OSError,
    EOFError,
    ET.ParseError,
    ValueError,  # includes UnicodeDecodeError
    LookupError,  # includes KeyError and IndexError
    TypeError,
    AttributeError,
)


async def _probe_port(host: str, port: int = _BAICHUAN_PORT) -> None:
    """Connect and hang up. Raises OSError if nothing is listening."""
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _PROBE_TIMEOUT)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def enable_services(host: str, password: str, host_factory=Host, probe=_probe_port) -> None:
    """Turn on HTTP and RTSP over Baichuan, the only port a factory camera opens.

    A closed port raises Unreachable before any Baichuan frame is sent. Past that,
    one call can send up to 3 login frames, because reolink_aio resends on connection
    errors and has no way to turn that off.
    """
    failure = None
    try:
        await probe(host)
    except OSError as err:
        failure = Unreachable(f"camera {host}: port {_BAICHUAN_PORT} closed: {type(err).__name__}")
    if failure is not None:
        raise failure

    # Forced to TCP: the default also tries UDP, which takes 90 s to fail on a closed port.
    api = host_factory(host, "admin", password, bc_connection=ConnectionEnum.tcp)
    baichuan = api.baichuan
    logged_in = False
    try:
        try:
            await baichuan.login()
            logged_in = True
            await baichuan.set_port_enabled(PortType.http, True)
            await baichuan.set_port_enabled(PortType.rtsp, True)
        finally:
            await _logout_quietly(host, api)
    except _BAICHUAN_ERRORS as err:
        # reolink_aio's messages decide the class and are never passed on.
        failure = _baichuan_failure(host, err, logged_in)
    if failure is not None:
        raise failure


async def _logout_quietly(host: str, api) -> None:
    # Must not replace the error that brought us here, and once the ports are
    # on, a failed goodbye does not undo the work. Host opens an aiohttp session
    # when constructed and only its own logout closes it. With no HTTP token, that
    # logout sends nothing to the camera. Baichuan's logout closes the socket only
    # if the login succeeded, and has no public way to close it otherwise, so a
    # test against the real Host pins the private attribute.
    async def drop_connection() -> None:
        connection = getattr(api.baichuan, "_connection", None)
        if connection is not None:
            await connection.drop_connection()

    # Looked up inside the guard too: a renamed call must not replace the real outcome.
    for step in (lambda: api.baichuan.logout(), lambda: api.logout(), drop_connection):
        try:
            await step()
        except _BAICHUAN_ERRORS as err:
            _log.warning("camera %s: logout failed: %s", host, type(err).__name__)


def _baichuan_failure(host: str, err: Exception, logged_in: bool) -> CameraError:
    name = type(err).__name__
    text = str(err)
    if _LOCKED_WORDING in text:
        return LockedOut(f"camera {host}: Baichuan login locked out", None, 0)
    if isinstance(err, reolink_errors.CredentialsInvalidError) or any(
        wording in text for wording in _REJECTED_WORDINGS
    ):
        return AuthFailed(f"camera {host}: Baichuan login rejected")
    if logged_in:
        return ApiError(f"camera {host}: Baichuan failed after login: {name}")
    return LoginOutcomeUnknown(f"camera {host}: Baichuan login failed: {name}")
