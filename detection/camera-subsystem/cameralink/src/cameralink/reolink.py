"""Client for the Reolink RLC-520A API, used over the point-to-point camera link.

Request bodies and response shapes come from the traffic recorded in
tests/fixtures/reolink/. The camera requires the session token in the query
string, so a URL is as secret as a password: URLs, tokens and passwords never
reach a log record or an exception. Errors are raised outside their ``except``
blocks so that the original exception, which may hold the URL, is not attached
to what the caller sees.
"""

import http.client
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

from reolink_aio import exceptions as reolink_errors
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType

_log = logging.getLogger(__name__)

_RSP_LOGIN_FAILED = -7


class CameraError(Exception):
    """Base class for everything this module raises about a camera."""


class Unreachable(CameraError):
    """No answer from the camera. This never counts as a login attempt."""


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


class ReolinkClient:
    def __init__(self, host: str, opener=urllib.request.urlopen, timeout: float = 8):
        self._host = host
        self._opener = opener
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
        try:
            self._token = entry["value"]["Token"]["name"]
        except (KeyError, TypeError):
            raise ApiError(f"camera {self._host}: Login returned no token") from None
        self._user = user
        self._password = password

    def logout(self) -> None:
        if self._token is None:
            return
        try:
            self._call("Logout", {})
        finally:
            self._token = None
            self._password = None

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
        ports = self._read("GetNetPort", "NetPort", {})
        if ports.get("rtspEnable") == 1:
            return
        self._call("SetNetPort", {"NetPort": {**ports, "rtspEnable": 1}})

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
        self._call("SetDevName", {"DevName": {"name": name}})
        osd = self._read("GetOsd", "Osd", {"channel": 0})
        osd["osdChannel"]["name"] = name
        self._call("SetOsd", {"Osd": osd})

    def set_streams(self) -> None:
        # SetEnc rejects vType, gop, width and height (rspCode -4). See
        # fixtures/reolink/setenc.json and setenc_ok.json.
        self._call(
            "SetEnc",
            {
                "Enc": {
                    "channel": 0,
                    "audio": 0,
                    "mainStream": {
                        "size": "2560*1920",
                        "frameRate": 15,
                        "bitRate": 4096,
                        "profile": "High",
                    },
                    "subStream": {
                        "size": "640*480",
                        "frameRate": 7,
                        "bitRate": 256,
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
        entry = self._call(cmd, param, action)
        try:
            return entry["value"][key]
        except (KeyError, TypeError):
            raise ApiError(f"camera {self._host}: {cmd} returned no {key}") from None

    def _call(self, cmd: str, param: dict, action: int = 0) -> dict:
        if self._token is None:
            raise ApiError(f"camera {self._host}: {cmd} needs a login first")
        entry = self._exchange(cmd, param, action)
        if entry.get("code") != 0:
            rsp_code = _error_of(entry).get("rspCode")
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
            # The camera answered, so this is not Unreachable. HTTPError holds
            # the URL, and is a URLError, so it has to be caught first.
            err.close()
            failure = ApiError(f"camera {self._host}: {cmd} got HTTP {err.code}")
        except (OSError, http.client.HTTPException) as err:
            # OSError covers URLError, refused connections and timeouts.
            failure = Unreachable(f"camera {self._host}: {cmd} failed: {type(err).__name__}")
        if failure is not None:
            raise failure

        return self._parse(cmd, raw)

    def _parse(self, cmd: str, raw: bytes) -> dict:
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if not (isinstance(decoded, list) and decoded and isinstance(decoded[0], dict)):
            raise ApiError(f"camera {self._host}: {cmd} returned an unusable response")
        return decoded[0]


def _error_of(entry: dict) -> dict:
    error = entry.get("error")
    return error if isinstance(error, dict) else {}


def _raise_login_failure(host: str, entry: dict) -> None:
    error = _error_of(entry)
    rsp_code = error.get("rspCode")
    warning = error.get("auth_warning_info") or {}
    remain_times = warning.get("remain_times")
    unlock_time = warning.get("unlock_time") or 0
    # No lockout has been recorded. This reads the fields of the recorded
    # wrong-password response, which is the only evidence available.
    if remain_times == 0 or unlock_time > 0:
        raise LockedOut(
            f"camera {host}: locked out, unlock_time {unlock_time}", remain_times, unlock_time
        )
    if rsp_code == _RSP_LOGIN_FAILED or warning:
        raise AuthFailed(f"camera {host}: login rejected, {remain_times} tries left", remain_times)
    raise ApiError(f"camera {host}: Login failed, rspCode {rsp_code}", rsp_code)


async def enable_services(host: str, password: str, host_factory=Host) -> None:
    """Turn on HTTP and RTSP over Baichuan, the only port a factory camera opens."""
    api = host_factory(host, "admin", password)
    baichuan = api.baichuan
    failure = None
    try:
        try:
            await baichuan.login()
            await baichuan.set_port_enabled(PortType.http, True)
            await baichuan.set_port_enabled(PortType.rtsp, True)
        finally:
            await _logout_quietly(host, api)
    except (reolink_errors.ReolinkError, OSError) as err:
        # Only the type name is kept: reolink_aio's messages are not ours to vouch for.
        failure = _baichuan_failure(host, err)
    if failure is not None:
        raise failure


async def _logout_quietly(host: str, api) -> None:
    # Must not replace the error that brought us here, and once the ports are
    # on, a failed goodbye does not undo the work. Host opens an aiohttp session
    # when constructed and only its own logout closes it. With no HTTP token, that
    # logout sends nothing to the camera.
    for logout in (api.baichuan.logout, api.logout):
        try:
            await logout()
        except (reolink_errors.ReolinkError, OSError) as err:
            _log.warning("camera %s: logout failed: %s", host, type(err).__name__)


def _baichuan_failure(host: str, err: Exception) -> CameraError:
    name = type(err).__name__
    # ReolinkTimeoutError is a TimeoutError, and so an OSError.
    if isinstance(err, (reolink_errors.ReolinkConnectionError, OSError)):
        return Unreachable(f"camera {host}: Baichuan failed: {name}")
    if isinstance(err, reolink_errors.CredentialsInvalidError):
        return AuthFailed(f"camera {host}: Baichuan login rejected")
    return ApiError(f"camera {host}: Baichuan failed: {name}")
