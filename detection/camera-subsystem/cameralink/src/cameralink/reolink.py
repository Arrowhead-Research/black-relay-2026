"""Client for the Reolink RLC-520A API, used over the point-to-point camera link.

Request bodies and response shapes come from the traffic recorded in
tests/fixtures/reolink/. The camera requires the session token in the query
string, so a URL is as secret as a password: URLs, tokens and passwords never
reach a log record or an exception. Errors are raised outside their ``except``
blocks so that the original exception, which may hold the URL, is not attached
to what the caller sees.

What a failure means for the one login attempt allowed per link or address change:

- ``Unreachable``: the request certainly never left. No attempt was used.
- ``LoginOutcomeUnknown``: a login may have reached the camera. Count it as the attempt.
- ``AuthFailed``, ``LockedOut``: the camera judged the login.
- ``ApiError``: the camera gave an answer that says nothing about the password, or a
  call other than login failed after its request may have been sent.
"""

import asyncio
import http.client
import json
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urlencode

from reolink_aio import exceptions as reolink_errors
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType
from reolink_aio.enums import ConnectionEnum

_log = logging.getLogger(__name__)

_RSP_LOGIN_FAILED = -7
_BAICHUAN_PORT = 9000
_PROBE_TIMEOUT = 3.0

# reolink_aio's api.py treats these camera wordings as credential errors. No
# exception type separates a lockout, and the HTTP API has no types at all.
_LOCKED_WORDING = "Login has been locked"
_REJECTED_WORDINGS = ("invalid user", "login failed", "password wrong")

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


class CameraError(Exception):
    """Base class for everything this module raises about a camera."""


class Unreachable(CameraError):
    """The request certainly never reached the camera. Never a login attempt."""


class LoginOutcomeUnknown(CameraError):
    """A login may have reached the camera, and no verdict came back."""


class AuthFailed(CameraError):
    """The camera rejected the credentials."""

    def __init__(self, message: str, remain_times: int | None = None):
        super().__init__(message)
        self.remain_times = remain_times


class LockedOut(CameraError):
    """The camera is refusing logins because of earlier failures."""

    def __init__(self, message: str, remain_times: int | None, unlock_time: int):
        super().__init__(message)
        self.remain_times = remain_times
        self.unlock_time = unlock_time


class ApiError(CameraError):
    """The camera answered, but with an error or something unusable."""

    def __init__(self, message: str, rsp_code: int | None = None):
        super().__init__(message)
        self.rsp_code = rsp_code


class _ConnectFailed(OSError):
    """TCP connect failed, so nothing was sent. urllib wraps this in a URLError."""


class _Connection(http.client.HTTPConnection):
    def connect(self):
        # urllib reports connect and send failures as the same URLError. This is
        # what tells "never sent" apart from "may have been sent".
        try:
            super().connect()
        except OSError as err:
            raise _ConnectFailed(type(err).__name__) from None


class _DirectHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Connection, req)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # Returning None hands the 3xx to urllib's default handler, which raises
    # HTTPError. Location is never parsed.
    def _refuse(self, *args):
        return None

    http_error_301 = http_error_302 = http_error_303 = _refuse
    http_error_307 = http_error_308 = _refuse


def _direct_opener():
    """Straight to the camera link: no proxy from the environment, no redirects."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _DirectHandler, _NoRedirects
    ).open


def _never_delivered(err: Exception) -> bool:
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    # TCP only refuses at connect. A timeout or reset could be from either side
    # of the send, so without _ConnectFailed those are not certain.
    return isinstance(reason, (_ConnectFailed, ConnectionRefusedError))


class ReolinkClient:
    def __init__(self, host: str, opener=None, timeout: float = 8):
        self._host = host
        self._opener = opener or _direct_opener()
        self._timeout = timeout
        self._user: str | None = None
        self._password: str | None = None
        self._token: str | None = None

    def login(self, user: str, password: str) -> None:
        self._token = None
        entry = self._exchange(
            "Login",
            {"User": {"Version": "0", "userName": user, "password": password}},
        )
        if entry.get("code") != 0:
            _raise_login_failure(self._host, entry)
        token = _child(_child(entry, "value"), "Token").get("name")
        if not (isinstance(token, str) and token):
            raise ApiError(f"camera {self._host}: Login returned no token")
        self._token = token
        self._user = user
        self._password = password

    def logout(self) -> None:
        if self._token is None:
            return
        try:
            self._call("Logout", {})
        finally:
            self._token = None

    def device_info(self) -> dict:
        return self._read("GetDevInfo", "DevInfo", {}, action=0)

    def set_password(self, new: str) -> None:
        self._call(
            "ModifyUser",
            {"User": {"userName": self._user, "oldPassword": self._password, "newPassword": new}},
        )
        self._password = new

    def disable_p2p(self) -> None:
        self._call("SetP2p", {"P2p": {"enable": 0}})
        self._call("SetUpnp", {"Upnp": {"enable": 0}})

    def enable_rtsp(self) -> None:
        self._set_port_flag("rtspEnable", 1)

    def disable_onvif(self) -> None:
        self._set_port_flag("onvifEnable", 0)

    def _set_port_flag(self, flag: str, value: int) -> None:
        ports = self._read("GetNetPort", "NetPort", {})
        if ports.get(flag) == value:
            return
        self._call("SetNetPort", {"NetPort": {**ports, flag: value}})
        # reolink_aio expires its session here because this write invalidates tokens.
        self.login(self._user, self._password)

    def set_clock(self, now: datetime) -> None:
        utc = now.astimezone(UTC)
        self._call(
            "SetNtp",
            {"Ntp": {"enable": 0, "interval": 1440, "port": 123, "server": "pool.ntp.org"}},
        )
        self._call(
            "SetTime",
            {
                "Dst": {"enable": 0},
                "Time": {
                    "year": utc.year,
                    "mon": utc.month,
                    "day": utc.day,
                    "hour": utc.hour,
                    "min": utc.minute,
                    "sec": utc.second,
                    "timeZone": 0,
                },
            },
        )

    def set_name(self, name: str) -> None:
        # Read first, so that a bad reply cannot leave the name half written.
        osd = self._read("GetOsd", "Osd", {"channel": 0})
        if not isinstance(osd.get("osdChannel"), dict):
            raise ApiError(f"camera {self._host}: GetOsd returned no osdChannel")
        osd["osdChannel"]["name"] = name
        self._call("SetDevName", {"DevName": {"name": name}})
        self._call("SetOsd", {"Osd": osd})

    def set_streams(self) -> None:
        # The keys of fixtures/reolink/setenc_ok.json, a body the camera accepted. setenc.json
        # also sent vType, gop, width and height and got rspCode -4, but it had an out-of-range
        # sub frameRate too, so which field caused that is unknown. The values here are in
        # getenc.json's range.
        self._call(
            "SetEnc",
            {
                "Enc": {
                    "channel": 0,
                    "audio": 0,
                    "mainStream": {
                        "size": "2304*1296",
                        "frameRate": 25,
                        "bitRate": 6144,
                        "profile": "High",
                    },
                    "subStream": {
                        "size": "640*480",
                        "frameRate": 15,
                        "bitRate": 512,
                        "profile": "High",
                    },
                }
            },
        )

    def set_rotation(self, flipped: bool) -> None:
        isp = self._read("GetIsp", "Isp", {"channel": 0})
        isp["rotation"] = 1 if flipped else 0
        self._call("SetIsp", {"Isp": isp})

    def _read(self, cmd: str, key: str, param: dict, action: int = 1) -> dict:
        value = _child(self._call(cmd, param, action), "value").get(key)
        if not isinstance(value, dict):
            raise ApiError(f"camera {self._host}: {cmd} returned no {key}")
        return value

    def _call(self, cmd: str, param: dict, action: int = 0) -> dict:
        if self._token is None:
            raise ApiError(f"camera {self._host}: {cmd} needs a login first")
        entry = self._exchange(cmd, param, action)
        if entry.get("code") != 0:
            rsp_code = _child(entry, "error").get("rspCode")
            raise ApiError(f"camera {self._host}: {cmd} failed, rspCode {rsp_code}", rsp_code)
        return entry

    def _exchange(self, cmd: str, param: dict, action: int = 0) -> dict:
        query = {"cmd": cmd}
        if self._token is not None:
            query["token"] = self._token
        request = urllib.request.Request(
            f"http://{self._host}/cgi-bin/api.cgi?{urlencode(query)}",
            data=json.dumps([{"cmd": cmd, "action": action, "param": param}]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _log.debug("camera %s: %s", self._host, cmd)

        raw = None
        failure = None
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            # The camera answered, so nothing here is about delivery. HTTPError
            # holds the URL, and is a URLError, so it has to be caught first.
            err.close()
            failure = ApiError(f"camera {self._host}: {cmd} got HTTP {err.code}")
        except (OSError, http.client.HTTPException, ValueError) as err:
            # OSError covers URLError, refused connections and timeouts.
            name = type(err).__name__
            if _never_delivered(err):
                failure = Unreachable(f"camera {self._host}: {cmd} not delivered: {name}")
            else:
                failure = self._after_send(cmd, f"failed: {name}")
        if failure is not None:
            raise failure

        return self._parse(cmd, raw)

    def _parse(self, cmd: str, raw: bytes) -> dict:
        try:
            decoded = json.loads(raw)
        except (ValueError, RecursionError):
            decoded = None
        if not (isinstance(decoded, list) and decoded and isinstance(decoded[0], dict)):
            raise self._after_send(cmd, "returned an unusable response")
        return decoded[0]

    def _after_send(self, cmd: str, what: str) -> CameraError:
        message = f"camera {self._host}: {cmd} {what}"
        return LoginOutcomeUnknown(message) if cmd == "Login" else ApiError(message)


def _child(parent: dict, key: str) -> dict:
    child = parent.get(key)
    return child if isinstance(child, dict) else {}


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _raise_login_failure(host: str, entry: dict) -> None:
    error = entry.get("error")
    if not isinstance(error, dict):
        raise LoginOutcomeUnknown(f"camera {host}: Login failed with no usable error")
    rsp_code = error.get("rspCode")
    detail = error.get("detail")
    detail = detail if isinstance(detail, str) else ""
    warning = _child(error, "auth_warning_info")
    remain_times = _int_or_none(warning.get("remain_times"))
    unlock_time = _int_or_none(warning.get("unlock_time")) or 0
    # No lockout has been recorded. This reads the fields of the recorded
    # wrong-password response and reolink_aio's wording, the only evidence available.
    # `detail` decides the class and never goes in a message.
    if remain_times == 0 or unlock_time > 0 or _LOCKED_WORDING in detail:
        raise LockedOut(
            f"camera {host}: locked out, unlock_time {unlock_time}", remain_times, unlock_time
        )
    if rsp_code == _RSP_LOGIN_FAILED or detail in _REJECTED_WORDINGS:
        raise AuthFailed(f"camera {host}: login rejected, {remain_times} tries left", remain_times)
    raise ApiError(f"camera {host}: Login failed, rspCode {rsp_code}", rsp_code)


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
    connection = getattr(api.baichuan, "_connection", None)
    steps = [api.baichuan.logout, api.logout]
    if connection is not None:
        steps.append(connection.drop_connection)
    for step in steps:
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
