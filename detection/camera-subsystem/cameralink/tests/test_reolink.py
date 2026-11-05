"""ReolinkClient and enable_services, driven by recorded RLC-520A traffic.

No test touches the network: the HTTP client gets a fake opener and
enable_services gets a fake Host.
"""

import asyncio
import copy
import http.client
import inspect
import io
import json
import logging
import traceback
import urllib.error
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from reolink_aio import exceptions as rl_exc
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType

from cameralink.reolink import (
    ApiError,
    AuthFailed,
    CameraError,
    LockedOut,
    ReolinkClient,
    Unreachable,
    enable_services,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reolink"
TOKEN = "tok3n-secret-abc"
PASSWORD = "s3cret-camera-pass"
NEW_PASSWORD = "n3w-camera-pass"
HOST = "10.77.0.10"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def login_ok() -> list:
    response = recorded("login_factory")["response"]
    response[0]["value"]["Token"]["name"] = TOKEN
    return response


def login_refused(remain_times: int, unlock_time: int) -> list:
    response = recorded("login_old_blank")["response"]
    response[0]["error"]["auth_warning_info"] = {
        "remain_times": remain_times,
        "unlock_time": unlock_time,
    }
    return response


class FakeOpener:
    """Answers by command with a recorded response, or raises what it is given."""

    def __init__(self, **by_cmd):
        self.by_cmd = {"Login": login_ok(), **by_cmd}
        self.calls = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        query = parse_qs(urlsplit(request.full_url).query)
        cmd = query["cmd"][0]
        self.calls.append(
            {
                "cmd": cmd,
                "url": request.full_url,
                "method": request.get_method(),
                "query": query,
                "body": json.loads(request.data),
            }
        )
        self.timeouts.append(timeout)
        answer = self.by_cmd[cmd]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BytesIO(json.dumps(answer).encode())

    def bodies(self, *skip):
        return [c["body"] for c in self.calls if c["cmd"] not in skip]

    def cmds(self):
        return [c["cmd"] for c in self.calls]


def logged_in(password: str = PASSWORD, **by_cmd) -> tuple[ReolinkClient, FakeOpener]:
    opener = FakeOpener(**by_cmd)
    client = ReolinkClient(HOST, opener=opener)
    client.login("admin", password)
    return client, opener


def assert_clean(exc: BaseException, *secrets: str) -> None:
    """The secret appears nowhere that a log line or traceback could show."""
    text = "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)
    for secret in secrets:
        assert secret not in text
    assert "api.cgi" not in text
    assert exc.__cause__ is None
    assert exc.__context__ is None


# --- login -----------------------------------------------------------------


def test_blank_login_stores_the_token():
    opener = FakeOpener(
        SetP2p=recorded("setp2p")["response"], SetUpnp=recorded("setupnp")["response"]
    )
    client = ReolinkClient(HOST, opener=opener)

    client.login("admin", "")

    expected = recorded("login_factory")["request"]
    expected[0]["param"]["User"]["password"] = ""
    login = opener.calls[0]
    assert login["body"] == expected
    assert login["method"] == "POST"
    assert urlsplit(login["url"]).netloc == HOST
    assert urlsplit(login["url"]).path == "/cgi-bin/api.cgi"
    assert "token" not in login["query"]

    client.disable_p2p()
    assert opener.calls[1]["query"]["token"] == [TOKEN]


def test_wrong_password_raises_auth_failed():
    opener = FakeOpener(Login=recorded("login_old_blank")["response"])
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(AuthFailed) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times == 10
    assert_clean(caught.value, PASSWORD)


def test_auth_failed_without_warning_info_has_no_remain_times():
    response = recorded("login_old_blank")["response"]
    del response[0]["error"]["auth_warning_info"]
    client = ReolinkClient(HOST, opener=FakeOpener(Login=response))

    with pytest.raises(AuthFailed) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times is None


@pytest.mark.parametrize(
    "remain_times, unlock_time",
    [(0, 0), (0, 300), (3, 300)],
)
def test_lockout_response_raises_locked_out(remain_times, unlock_time):
    # Inferred from the shape of the recorded wrong-password response: nobody
    # has provoked a real lockout.
    opener = FakeOpener(Login=login_refused(remain_times, unlock_time))
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(LockedOut) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times == remain_times
    assert caught.value.unlock_time == unlock_time
    assert not isinstance(caught.value, AuthFailed)


def test_other_login_error_is_an_api_error_not_an_auth_failure():
    response = [{"cmd": "Login", "code": 1, "error": {"detail": "max session", "rspCode": -5}}]
    client = ReolinkClient(HOST, opener=FakeOpener(Login=response))

    with pytest.raises(ApiError) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.rsp_code == -5


def test_login_success_without_a_token_is_an_api_error():
    response = [{"cmd": "Login", "code": 0, "value": {}}]
    client = ReolinkClient(HOST, opener=FakeOpener(Login=response))

    with pytest.raises(ApiError):
        client.login("admin", PASSWORD)


# --- unreachable is not a login attempt ------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError(111, "Connection refused"),
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        urllib.error.URLError("no route"),
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b""),
    ],
    ids=lambda f: type(f).__name__,
)
def test_connection_refused_raises_unreachable(failure):
    opener = FakeOpener(Login=failure)
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(Unreachable) as caught:
        client.login("admin", PASSWORD)

    assert not isinstance(caught.value, AuthFailed)
    assert not isinstance(caught.value, LockedOut)
    assert_clean(caught.value, PASSWORD)


def test_refused_during_boot_then_wrong_password_are_different_errors():
    opener = FakeOpener(Login=ConnectionRefusedError(111, "Connection refused"))
    client = ReolinkClient(HOST, opener=opener)
    with pytest.raises(Unreachable):
        client.login("admin", "")

    opener.by_cmd["Login"] = recorded("login_old_blank")["response"]
    with pytest.raises(AuthFailed):
        client.login("admin", "")


def test_http_error_status_is_an_api_error_and_hides_the_url():
    url = f"http://{HOST}/cgi-bin/api.cgi?cmd=SetP2p&token={TOKEN}"
    failure = urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b""))
    client, opener = logged_in(SetP2p=failure)

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert "502" in str(caught.value)
    assert_clean(caught.value, TOKEN, PASSWORD)
    assert opener.cmds() == ["Login", "SetP2p"]


@pytest.mark.parametrize("body", [b"<html>booting</html>", b"[]", b"{}", b"[1]"])
def test_unusable_response_body_is_an_api_error(body):
    client = ReolinkClient(HOST, opener=FakeOpener(Login=body))

    with pytest.raises(ApiError) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


def test_all_errors_are_camera_errors():
    for error in (AuthFailed, LockedOut, ApiError, Unreachable):
        assert issubclass(error, CameraError)


def test_timeout_is_passed_to_the_opener():
    opener = FakeOpener()
    ReolinkClient(HOST, opener=opener, timeout=3).login("admin", "")
    assert opener.timeouts == [3]


def test_default_timeout_is_8_seconds():
    opener = FakeOpener()
    ReolinkClient(HOST, opener=opener).login("admin", "")
    assert opener.timeouts == [8]


# --- secrets stay out of logs ----------------------------------------------


def exercise_every_call(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = logged_in(
        Logout=recorded("logout")["response"],
        GetDevInfo=[{"cmd": "GetDevInfo", "code": 0, "value": {"DevInfo": {"model": "RLC-520A"}}}],
        ModifyUser=recorded("modifyuser")["response"],
        SetP2p=recorded("setp2p")["response"],
        SetUpnp=recorded("setupnp")["response"],
        GetNetPort=recorded("getnetport")["response"],
        SetNtp=recorded("setntp")["response"],
        SetTime=recorded("settime")["response"],
        SetDevName=recorded("setdevname")["response"],
        GetOsd=recorded("getosd")["response"],
        SetOsd=recorded("setosd")["response"],
        SetEnc=recorded("setenc")["response"],  # the rejected form: covers the error path
        GetIsp=recorded("getisp")["response"],
        SetIsp=recorded("setisp_rotate")["response"],
    )
    client.device_info()
    client.set_password(NEW_PASSWORD)
    client.disable_p2p()
    client.enable_rtsp()
    client.set_clock(datetime(2026, 11, 5, tzinfo=UTC))
    client.set_name("cam-8da040")
    with pytest.raises(ApiError):
        client.set_streams()
    client.set_rotation(True)
    client.logout()


def log_text(caplog) -> str:
    return "\n".join(
        f"{r.getMessage()} {r.args!r} {r.__dict__!r}" for r in caplog.records
    )


def test_token_is_never_in_a_log_record(caplog):
    exercise_every_call(caplog)
    assert caplog.records, "the client should log something for this test to mean anything"
    text = log_text(caplog)
    assert TOKEN not in text
    assert "token=" not in text
    assert "api.cgi" not in text


def test_password_is_never_in_a_log_record(caplog):
    exercise_every_call(caplog)
    text = log_text(caplog)
    assert PASSWORD not in text
    assert NEW_PASSWORD not in text


def test_error_on_a_tokened_call_does_not_carry_the_token():
    client, _ = logged_in(SetP2p=ConnectionRefusedError(111, "refused"))
    with pytest.raises(Unreachable) as caught:
        client.disable_p2p()
    assert_clean(caught.value, TOKEN, PASSWORD)


def test_client_repr_holds_no_secrets():
    client, _ = logged_in()
    assert TOKEN not in repr(client)
    assert PASSWORD not in repr(client)


# --- writes match the recorded bodies --------------------------------------


def test_set_streams_sends_sub_640x480_at_7fps_and_main_at_15fps():
    client, opener = logged_in(SetEnc=recorded("setenc_ok")["response"])

    client.set_streams()

    body = opener.bodies("Login")[0]
    assert body == recorded("setenc_ok")["request"]
    enc = body[0]["param"]["Enc"]
    assert enc["subStream"]["size"] == "640*480"
    assert enc["subStream"]["frameRate"] == 7
    assert enc["mainStream"]["size"] == "2560*1920"
    assert enc["mainStream"]["frameRate"] == 15
    assert enc["mainStream"]["bitRate"] == 4096
    for stream in (enc["mainStream"], enc["subStream"]):
        assert not {"vType", "gop", "width", "height"} & stream.keys()


def test_rejected_write_raises_api_error_with_the_rsp_code():
    client, _ = logged_in(SetEnc=recorded("setenc")["response"])

    with pytest.raises(ApiError) as caught:
        client.set_streams()

    assert caught.value.rsp_code == -4
    assert "SetEnc" in str(caught.value)


def test_set_password_sends_old_and_new_and_tracks_the_new_one():
    client, opener = logged_in(ModifyUser=recorded("modifyuser")["response"])

    client.set_password(NEW_PASSWORD)
    client.set_password("third-pass")

    first, second = opener.bodies("Login")
    expected = recorded("modifyuser")["request"]
    expected[0]["param"]["User"]["oldPassword"] = PASSWORD
    expected[0]["param"]["User"]["newPassword"] = NEW_PASSWORD
    assert first == expected
    assert second[0]["param"]["User"]["oldPassword"] == NEW_PASSWORD
    assert second[0]["param"]["User"]["newPassword"] == "third-pass"


def test_rejected_password_change_keeps_the_old_password():
    rejected = [{"cmd": "ModifyUser", "code": 1, "error": {"detail": "x", "rspCode": -1}}]
    client, opener = logged_in(ModifyUser=rejected)

    with pytest.raises(ApiError):
        client.set_password(NEW_PASSWORD)
    with pytest.raises(ApiError):
        client.set_password(NEW_PASSWORD)

    assert opener.bodies("Login")[1][0]["param"]["User"]["oldPassword"] == PASSWORD


def test_disable_p2p_also_turns_off_upnp():
    client, opener = logged_in(
        SetP2p=recorded("setp2p")["response"], SetUpnp=recorded("setupnp")["response"]
    )

    client.disable_p2p()

    assert opener.bodies("Login") == [recorded("setp2p")["request"], recorded("setupnp")["request"]]


def test_set_clock_turns_ntp_off_then_sets_utc_time():
    client, opener = logged_in(
        SetNtp=recorded("setntp")["response"], SetTime=recorded("settime")["response"]
    )

    client.set_clock(datetime(2026, 11, 5, 16, 58, 42, tzinfo=UTC))

    assert opener.bodies("Login") == [recorded("setntp")["request"], recorded("settime")["request"]]


def test_set_clock_converts_to_utc():
    client, opener = logged_in(
        SetNtp=recorded("setntp")["response"], SetTime=recorded("settime")["response"]
    )
    plus_eight = timezone(timedelta(hours=8))

    client.set_clock(datetime(2026, 11, 6, 0, 58, 42, tzinfo=plus_eight))

    assert opener.bodies("Login")[1] == recorded("settime")["request"]


def test_set_name_sets_devname_and_changes_only_the_osd_name():
    client, opener = logged_in(
        SetDevName=recorded("setdevname")["response"],
        GetOsd=recorded("getosd")["response"],
        SetOsd=recorded("setosd")["response"],
    )

    client.set_name("cam-8da040")

    assert opener.cmds() == ["Login", "SetDevName", "GetOsd", "SetOsd"]
    bodies = opener.bodies("Login")
    assert bodies[0] == recorded("setdevname")["request"]
    assert bodies[1] == recorded("getosd")["request"]
    assert bodies[2] == recorded("setosd")["request"]


def test_set_rotation_flipped_changes_only_rotation():
    client, opener = logged_in(
        GetIsp=recorded("getisp")["response"], SetIsp=recorded("setisp_rotate")["response"]
    )

    client.set_rotation(True)

    assert opener.cmds() == ["Login", "GetIsp", "SetIsp"]
    bodies = opener.bodies("Login")
    assert bodies[0] == recorded("getisp")["request"]
    assert bodies[1] == recorded("setisp_rotate")["request"]


def test_set_rotation_upright_writes_zero_over_a_flipped_camera():
    flipped = recorded("getisp")["response"]
    flipped[0]["value"]["Isp"]["rotation"] = 1
    client, opener = logged_in(GetIsp=flipped, SetIsp=recorded("setisp_rotate")["response"])

    client.set_rotation(False)

    written = opener.bodies("Login")[1][0]["param"]["Isp"]
    expected = copy.deepcopy(recorded("getisp")["response"][0]["value"]["Isp"])
    assert written == expected
    assert written["rotation"] == 0


def test_read_without_the_expected_value_is_an_api_error():
    client, opener = logged_in(GetIsp=[{"cmd": "GetIsp", "code": 0, "value": {}}])

    with pytest.raises(ApiError):
        client.set_rotation(True)

    assert opener.cmds() == ["Login", "GetIsp"]


# --- enable_rtsp: SetNetPort has no recording, see the report ---------------


def test_enable_rtsp_does_not_write_when_rtsp_is_already_on():
    client, opener = logged_in(GetNetPort=recorded("getnetport")["response"])

    client.enable_rtsp()

    assert opener.cmds() == ["Login", "GetNetPort"]
    assert opener.bodies("Login") == [recorded("getnetport")["request"]]


def test_enable_rtsp_turns_on_only_rtsp_when_it_is_off():
    current = recorded("getnetport")["response"]
    current[0]["value"]["NetPort"]["rtspEnable"] = 0
    current[0]["value"]["NetPort"]["onvifEnable"] = 0
    ok = [{"cmd": "SetNetPort", "code": 0, "value": {"rspCode": 200}}]
    client, opener = logged_in(GetNetPort=current, SetNetPort=ok)

    client.enable_rtsp()

    written = opener.bodies("Login")[1][0]
    assert written["cmd"] == "SetNetPort"
    expected = dict(current[0]["value"]["NetPort"], rtspEnable=1)
    assert written["param"]["NetPort"] == expected
    assert written["param"]["NetPort"]["onvifEnable"] == 0
    assert written["param"]["NetPort"]["rtmpEnable"] == 0


# --- device_info, logout, ordering -----------------------------------------


def test_device_info_returns_the_devinfo_object():
    info = {"model": "RLC-520A", "firmVer": "v3"}
    client, opener = logged_in(
        GetDevInfo=[{"cmd": "GetDevInfo", "code": 0, "value": {"DevInfo": info}}]
    )

    assert client.device_info() == info
    assert opener.calls[1]["query"]["token"] == [TOKEN]


def test_logout_sends_the_token_and_forgets_the_session():
    client, opener = logged_in(
        Logout=recorded("logout")["response"], SetP2p=recorded("setp2p")["response"]
    )

    client.logout()

    assert opener.bodies("Login") == [recorded("logout")["request"]]
    assert opener.calls[1]["query"]["token"] == [TOKEN]
    with pytest.raises(ApiError):
        client.disable_p2p()
    assert opener.cmds() == ["Login", "Logout"]


def test_logout_forgets_the_session_even_when_the_camera_is_gone():
    client, opener = logged_in(Logout=ConnectionRefusedError(111, "refused"))

    with pytest.raises(Unreachable):
        client.logout()

    client.logout()  # nothing left to end: no second request
    assert opener.cmds() == ["Login", "Logout"]


def test_calls_before_login_never_reach_the_camera():
    opener = FakeOpener()
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(ApiError):
        client.set_password(NEW_PASSWORD)

    assert opener.calls == []


def test_relogin_does_not_send_the_old_token():
    client, opener = logged_in()

    client.login("admin", PASSWORD)

    assert "token" not in opener.calls[1]["query"]


# --- Baichuan --------------------------------------------------------------


class FakeBaichuan:
    def __init__(self, ports: dict, login_error=None, set_error=None, logout_error=None):
        self.ports = copy.deepcopy(ports)
        self.login_error = login_error
        self.set_error = set_error
        self.logout_error = logout_error
        self.calls = []

    async def login(self):
        self.calls.append("login")
        if self.login_error:
            raise self.login_error

    async def set_port_enabled(self, port, enable):
        self.calls.append(f"set:{port.name}")
        if self.set_error:
            raise self.set_error
        self.ports[port.name]["enable"] = enable

    async def logout(self):
        self.calls.append("logout")
        if self.logout_error:
            raise self.logout_error


class FakeHostFactory:
    def __init__(self, baichuan):
        self.baichuan = baichuan
        self.args = None
        self.closed = False

    def __call__(self, *args):
        self.args = args
        return self

    async def logout(self):
        self.closed = True


def run_enable(baichuan, password=""):
    factory = FakeHostFactory(baichuan)
    asyncio.run(enable_services(HOST, password, host_factory=factory))
    return factory


def test_baichuan_enables_http_and_rtsp_when_only_9000_is_open():
    ports = recorded("baichuan_factory_ports")
    baichuan = FakeBaichuan(ports["before"])

    factory = run_enable(baichuan, password="")

    assert factory.args == (HOST, "admin", "")
    assert baichuan.ports == ports["after_enabling_http_and_rtsp"]
    assert baichuan.calls == ["login", "set:http", "set:rtsp", "logout"]


@pytest.mark.parametrize(
    "failure",
    [
        rl_exc.ReolinkConnectionError("refused"),
        rl_exc.ReolinkTimeoutError("timed out"),
        ConnectionRefusedError(111, "refused"),
        TimeoutError("timed out"),
    ],
    ids=lambda f: type(f).__name__,
)
def test_baichuan_refused_is_unreachable_not_auth_failed(failure):
    baichuan = FakeBaichuan({}, login_error=failure)

    with pytest.raises(Unreachable) as caught:
        run_enable(baichuan, password=PASSWORD)

    assert not isinstance(caught.value, AuthFailed)
    assert_clean(caught.value, PASSWORD)
    assert baichuan.calls == ["login", "logout"]


def test_baichuan_bad_credentials_raise_auth_failed():
    failure = rl_exc.CredentialsInvalidError(f"bad login with {PASSWORD}")
    baichuan = FakeBaichuan({}, login_error=failure)

    with pytest.raises(AuthFailed) as caught:
        run_enable(baichuan, password=PASSWORD)

    assert caught.value.remain_times is None
    assert_clean(caught.value, PASSWORD)
    assert baichuan.calls == ["login", "logout"]


def test_baichuan_other_failure_is_an_api_error_and_still_logs_out():
    ports = recorded("baichuan_factory_ports")["before"]
    baichuan = FakeBaichuan(ports, set_error=rl_exc.ApiError(f"fail {PASSWORD}", rspCode=400))

    with pytest.raises(ApiError) as caught:
        run_enable(baichuan, password=PASSWORD)

    assert_clean(caught.value, PASSWORD)
    assert baichuan.calls == ["login", "set:http", "logout"]


def test_baichuan_logout_failure_does_not_hide_the_real_error():
    baichuan = FakeBaichuan(
        {},
        login_error=rl_exc.CredentialsInvalidError("bad"),
        logout_error=rl_exc.ReolinkConnectionError("gone"),
    )

    with pytest.raises(AuthFailed):
        run_enable(baichuan)


def test_baichuan_logout_failure_after_success_is_not_an_error(caplog):
    ports = recorded("baichuan_factory_ports")
    baichuan = FakeBaichuan(ports["before"], logout_error=rl_exc.ReolinkConnectionError("gone"))

    run_enable(baichuan)

    assert baichuan.ports == ports["after_enabling_http_and_rtsp"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_real_host_session_is_closed_after_enable_services():
    # The session is opened by Host's constructor and baichuan.logout leaves it
    # open. Only the Baichuan calls are stubbed, so nothing reaches the network.
    built = []

    async def do_nothing(*args, **kwargs):
        pass

    def factory(*args):
        host = Host(*args)
        host.baichuan.login = do_nothing
        host.baichuan.set_port_enabled = do_nothing
        built.append(host)
        return host

    asyncio.run(enable_services(HOST, "", host_factory=factory))

    assert built[0]._aiohttp_session.closed


def test_session_is_closed_even_when_baichuan_logout_fails():
    ports = recorded("baichuan_factory_ports")["before"]
    baichuan = FakeBaichuan(ports, logout_error=rl_exc.ReolinkConnectionError("gone"))

    factory = run_enable(baichuan)

    assert factory.closed


def test_enable_services_uses_the_real_host_by_default():
    assert inspect.signature(enable_services).parameters["host_factory"].default is Host


def test_real_baichuan_has_the_calls_we_make():
    # Constructing Host opens no connection, but needs a running loop. This catches
    # a reolink_aio API drift that the fake above could not.
    async def build():
        return Host(HOST, "admin", "").baichuan

    baichuan = asyncio.run(build())
    for name in ("login", "set_port_enabled", "logout"):
        assert inspect.iscoroutinefunction(getattr(baichuan, name))
    params = inspect.signature(baichuan.set_port_enabled).parameters
    assert list(params)[:2] == ["port", "enable"]
    assert {PortType.http.name, PortType.rtsp.name} == {"http", "rtsp"}
