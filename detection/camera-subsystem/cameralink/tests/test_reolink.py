"""ReolinkClient and enable_services, driven by recorded RLC-520A traffic.

No test touches the network: the HTTP client gets a fake opener and
enable_services gets a fake Host.
"""

import asyncio
import copy
import errno
import http.client
import inspect
import io
import json
import logging
import socket
import struct
import threading
import traceback
import urllib.error
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from reolink_aio import exceptions as rl_exc
from reolink_aio.api import Host
from reolink_aio.baichuan.util import PortType, encrypt_baichuan
from reolink_aio.enums import ConnectionEnum

from cameralink import reolink
from cameralink.reolink import (
    ApiError,
    AuthFailed,
    CameraError,
    LockedOut,
    LoginOutcomeUnknown,
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


class FailingBody:
    """A response that opens fine and then fails while its body is read."""

    def __init__(self, failure):
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise self.failure


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
                "content_type": request.get_header("Content-type"),
            }
        )
        self.timeouts.append(timeout)
        answer = self.by_cmd[cmd]
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, FailingBody):
            return answer
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
    assert login["content_type"] == "application/json"
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


def test_warning_block_on_another_rsp_code_is_not_an_auth_failure():
    error = {"auth_warning_info": {"remain_times": 3, "unlock_time": 0}, "rspCode": -5}
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": error}]))

    with pytest.raises(ApiError):
        client.login("admin", PASSWORD)


@pytest.mark.parametrize("detail", ["invalid user", "login failed", "password wrong"])
def test_credential_wording_raises_auth_failed_whatever_the_rsp_code(detail):
    error = {"detail": detail, "rspCode": -26}
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": error}]))

    with pytest.raises(AuthFailed) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times is None
    assert detail not in str(caught.value)


def test_rsp_code_minus_7_alone_raises_auth_failed():
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": {"rspCode": -7}}]))

    with pytest.raises(AuthFailed):
        client.login("admin", PASSWORD)


@pytest.mark.parametrize("detail", [7, ["login failed"], {"x": 1}, None], ids=repr)
def test_non_text_detail_is_an_api_error(detail):
    error = {"detail": detail, "rspCode": -5}
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": error}]))

    with pytest.raises(ApiError):
        client.login("admin", PASSWORD)


@pytest.mark.parametrize("rsp_code", [-31, -7])
def test_locked_wording_raises_locked_out_whatever_the_rsp_code(rsp_code):
    error = {"detail": "Login has been locked", "rspCode": rsp_code}
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": error}]))

    with pytest.raises(LockedOut) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times is None
    assert caught.value.unlock_time == 0
    assert "Login has been locked" not in str(caught.value)


@pytest.mark.parametrize(
    "warning",
    ["not a dict", 7, ["x"], {"remain_times": "many", "unlock_time": "soon"}, {"unlock_time": None}],
    ids=repr,
)
def test_malformed_warning_block_on_a_wrong_password_is_still_auth_failed(warning):
    error = {"auth_warning_info": warning, "detail": "login failed", "rspCode": -7}
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[{"code": 1, "error": error}]))

    with pytest.raises(AuthFailed) as caught:
        client.login("admin", PASSWORD)

    assert caught.value.remain_times is None


@pytest.mark.parametrize("entry", [{"code": 1}, {"code": 1, "error": "x"}, {}], ids=repr)
def test_failed_login_with_no_usable_error_is_outcome_unknown(entry):
    client = ReolinkClient(HOST, opener=FakeOpener(Login=[entry]))

    with pytest.raises(LoginOutcomeUnknown):
        client.login("admin", PASSWORD)


@pytest.mark.parametrize(
    "value",
    [
        {},
        None,
        "x",
        {"Token": None},
        {"Token": "x"},
        {"Token": {}},
        {"Token": {"name": None}},
        {"Token": {"name": ""}},
        {"Token": {"name": 7}},
    ],
    ids=repr,
)
def test_login_success_without_a_token_is_an_api_error(value):
    opener = FakeOpener(Login=[{"cmd": "Login", "code": 0, "value": value}])
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(ApiError) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)
    with pytest.raises(ApiError):
        client.disable_p2p()
    assert opener.cmds() == ["Login"]


# --- what a failed request means ------------------------------------------

NEVER_DELIVERED = [
    ConnectionRefusedError(111, "Connection refused"),
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    # Holds what a message must never quote, so that quoting the exception fails the test.
    ConnectionRefusedError(111, f"refused, token={TOKEN}, {PASSWORD}"),
]
MAY_HAVE_BEEN_DELIVERED = [
    urllib.error.URLError(f"no route, token={TOKEN}, {PASSWORD}"),
    urllib.error.URLError(TimeoutError("timed out")),
    urllib.error.URLError(ConnectionResetError(104, "reset")),
    urllib.error.URLError(BrokenPipeError(32, "broken pipe")),
    TimeoutError("timed out"),
    ConnectionResetError(104, "reset"),
    http.client.RemoteDisconnected("closed"),
    http.client.BadStatusLine(f"token={TOKEN}"),
    ValueError(f"token={TOKEN}"),
]
READ_FAILURES = [
    TimeoutError("timed out"),
    ConnectionResetError(104, "reset"),
    http.client.IncompleteRead(b"[{"),
]


def failure_id(failure) -> str:
    return type(failure).__name__


@pytest.mark.parametrize("failure", NEVER_DELIVERED, ids=failure_id)
def test_connection_refused_raises_unreachable(failure):
    opener = FakeOpener(Login=failure)
    client = ReolinkClient(HOST, opener=opener)

    with pytest.raises(Unreachable) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD, TOKEN)


@pytest.mark.parametrize("failure", MAY_HAVE_BEEN_DELIVERED, ids=failure_id)
def test_login_failure_after_the_request_may_have_gone_is_outcome_unknown(failure):
    client = ReolinkClient(HOST, opener=FakeOpener(Login=failure))

    with pytest.raises(LoginOutcomeUnknown) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD, TOKEN)


@pytest.mark.parametrize("failure", READ_FAILURES, ids=failure_id)
def test_login_failure_during_read_is_outcome_unknown(failure):
    client = ReolinkClient(HOST, opener=FakeOpener(Login=FailingBody(failure)))

    with pytest.raises(LoginOutcomeUnknown) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize("failure", MAY_HAVE_BEEN_DELIVERED, ids=failure_id)
def test_other_call_failure_after_the_request_may_have_gone_is_api_error(failure):
    client, _ = logged_in(SetP2p=failure)

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert_clean(caught.value, PASSWORD, TOKEN)


@pytest.mark.parametrize("failure", READ_FAILURES, ids=failure_id)
def test_other_call_failure_during_read_is_api_error(failure):
    client, _ = logged_in(SetP2p=FailingBody(failure))

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert_clean(caught.value, PASSWORD, TOKEN)


def test_refused_other_call_is_unreachable():
    client, _ = logged_in(SetP2p=ConnectionRefusedError(111, "refused"))

    with pytest.raises(Unreachable) as caught:
        client.disable_p2p()

    assert_clean(caught.value, TOKEN, PASSWORD)


def test_refused_during_boot_then_wrong_password_are_different_errors():
    opener = FakeOpener(Login=ConnectionRefusedError(111, "Connection refused"))
    client = ReolinkClient(HOST, opener=opener)
    with pytest.raises(Unreachable):
        client.login("admin", "")

    opener.by_cmd["Login"] = recorded("login_old_blank")["response"]
    with pytest.raises(AuthFailed):
        client.login("admin", "")


def test_unreachable_and_unknown_are_not_auth_outcomes():
    for error in (Unreachable, LoginOutcomeUnknown):
        assert not issubclass(error, (AuthFailed, LockedOut, ApiError))
    assert not issubclass(Unreachable, LoginOutcomeUnknown)
    assert not issubclass(LoginOutcomeUnknown, Unreachable)


def test_http_error_status_is_an_api_error_and_hides_the_url():
    url = f"http://{HOST}/cgi-bin/api.cgi?cmd=SetP2p&token={TOKEN}"
    body = io.BytesIO(b"")
    failure = urllib.error.HTTPError(url, 502, "Bad Gateway", {}, body)
    client, opener = logged_in(SetP2p=failure)

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert "502" in str(caught.value)
    assert body.closed
    assert_clean(caught.value, TOKEN, PASSWORD)
    assert opener.cmds() == ["Login", "SetP2p"]


def test_login_http_error_status_is_an_api_error():
    url = f"http://{HOST}/cgi-bin/api.cgi?cmd=Login"
    failure = urllib.error.HTTPError(url, 503, "Busy", {}, io.BytesIO(b""))
    client = ReolinkClient(HOST, opener=FakeOpener(Login=failure))

    with pytest.raises(ApiError):
        client.login("admin", PASSWORD)


UNUSABLE_BODIES = [
    b"<html>booting</html>",
    b"[]",
    b"{}",
    b"[1]",
    b'[{"code": 0',
    b"\xff\xfe\x80\x81",
    b"[" * 200_000,
]


@pytest.mark.parametrize("body", UNUSABLE_BODIES, ids=lambda b: repr(b[:12]))
def test_unusable_login_body_is_outcome_unknown(body):
    client = ReolinkClient(HOST, opener=FakeOpener(Login=body))

    with pytest.raises(LoginOutcomeUnknown) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize("body", UNUSABLE_BODIES, ids=lambda b: repr(b[:12]))
def test_unusable_body_on_another_call_is_an_api_error(body):
    client, _ = logged_in(SetP2p=body)

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert_clean(caught.value, PASSWORD, TOKEN)


def test_rejected_call_with_a_non_dict_error_is_an_api_error():
    client, _ = logged_in(SetP2p=[{"cmd": "SetP2p", "code": 1, "error": "nope"}])

    with pytest.raises(ApiError) as caught:
        client.disable_p2p()

    assert caught.value.rsp_code is None


def test_all_errors_are_camera_errors():
    for error in (AuthFailed, LockedOut, ApiError, Unreachable, LoginOutcomeUnknown):
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
        SetNetPort=[{"cmd": "SetNetPort", "code": 0, "value": {"rspCode": 200}}],
        SetNtp=recorded("setntp")["response"],
        SetTime=recorded("settime")["response"],
        GetTime=camera_time(datetime(2026, 11, 5, tzinfo=UTC)),
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
    client.disable_onvif()
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


# --- writes match the recorded bodies --------------------------------------


def test_set_streams_sends_main_2304x1296_at_25fps_and_sub_640x480_at_15fps():
    client, opener = logged_in(SetEnc=recorded("setenc_ok")["response"])

    client.set_streams()

    enc = opener.bodies("Login")[0][0]["param"]["Enc"]
    assert enc["mainStream"] == {"size": "2304*1296", "frameRate": 25, "bitRate": 6144, "profile": "High"}
    assert enc["subStream"] == {"size": "640*480", "frameRate": 15, "bitRate": 512, "profile": "High"}


def test_set_streams_uses_the_body_shape_the_camera_accepted():
    # The recorded values are older, so only the keys are compared.
    client, opener = logged_in(SetEnc=recorded("setenc_ok")["response"])

    client.set_streams()

    sent = opener.bodies("Login")[0][0]
    accepted = recorded("setenc_ok")["request"][0]
    assert {k: v for k, v in sent.items() if k != "param"} == {k: v for k, v in accepted.items() if k != "param"}
    sent_enc, accepted_enc = sent["param"]["Enc"], accepted["param"]["Enc"]
    assert sent_enc.keys() == accepted_enc.keys()
    assert (sent_enc["channel"], sent_enc["audio"]) == (accepted_enc["channel"], accepted_enc["audio"])
    for stream in ("mainStream", "subStream"):
        assert sent_enc[stream].keys() == accepted_enc[stream].keys()
        assert sent_enc[stream]["profile"] == accepted_enc[stream]["profile"]


def test_set_streams_values_are_in_the_range_the_camera_reports():
    client, opener = logged_in(SetEnc=recorded("setenc_ok")["response"])

    client.set_streams()

    enc = opener.bodies("Login")[0][0]["param"]["Enc"]
    modes = recorded("getenc")["response"][0]["range"]["Enc"]
    mode = next(m for m in modes if m["mainStream"]["size"] == enc["mainStream"]["size"])
    for stream in ("mainStream", "subStream"):
        allowed = mode[stream]
        assert enc[stream]["size"] == allowed["size"]
        assert enc[stream]["frameRate"] in allowed["frameRate"]
        assert enc[stream]["bitRate"] in allowed["bitRate"]
        assert enc[stream]["profile"] in allowed["profile"]


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


SET_AT = datetime(2026, 11, 5, 16, 58, 42, tzinfo=UTC)


def camera_time(when: datetime, zone: int = 0, **changed) -> list:
    """A GetTime reply in which the camera's clock reads ``when``."""
    time = {
        "year": when.year,
        "mon": when.month,
        "day": when.day,
        "hour": when.hour,
        "min": when.minute,
        "sec": when.second,
        "hourFmt": 0,
        "timeFmt": "DD/MM/YYYY",
        "timeZone": zone,
        **changed,
    }
    return [{"cmd": "GetTime", "code": 0, "value": {"Dst": {"enable": 0}, "Time": time}}]


def clock_client(*readings) -> tuple[ReolinkClient, FakeOpener]:
    """A camera that takes every write, and answers GetTime with each reading in turn."""
    answers = iter(readings)
    return logged_in(
        SetNtp=recorded("setntp")["response"],
        SetTime=recorded("settime")["response"],
        GetTime=lambda: next(answers),
    )


def test_set_clock_turns_ntp_off_then_sets_utc_time_and_reads_it_back():
    client, opener = clock_client(camera_time(SET_AT))

    client.set_clock(SET_AT)

    assert opener.bodies("Login") == [
        recorded("setntp")["request"],
        recorded("settime")["request"],
        recorded("gettime")["request"],
    ]


def test_set_clock_converts_to_utc():
    client, opener = clock_client(camera_time(SET_AT))
    plus_eight = timezone(timedelta(hours=8))

    client.set_clock(datetime(2026, 11, 6, 0, 58, 42, tzinfo=plus_eight))

    assert opener.bodies("Login")[1] == recorded("settime")["request"]


def test_set_clock_writes_again_when_a_zone_change_left_the_clock_hours_out():
    """Kit 1, 2026-11-13: a factory camera at timeZone 28800 took the write and read 8 h ahead.
    The same write again, with its zone now 0, read right."""
    client, opener = clock_client(camera_time(SET_AT + timedelta(hours=8)), camera_time(SET_AT))

    client.set_clock(SET_AT)

    assert opener.cmds() == ["Login", "SetNtp", "SetTime", "GetTime", "SetTime", "GetTime"]
    assert opener.bodies("Login")[3] == recorded("settime")["request"]


def test_set_clock_takes_a_few_seconds_difference_as_set():
    client, opener = clock_client(camera_time(SET_AT + timedelta(seconds=9)))

    client.set_clock(SET_AT)

    assert opener.cmds() == ["Login", "SetNtp", "SetTime", "GetTime"]


def test_set_clock_writes_again_when_the_zone_did_not_take():
    client, opener = clock_client(camera_time(SET_AT, zone=28800), camera_time(SET_AT))

    client.set_clock(SET_AT)

    assert opener.cmds().count("SetTime") == 2


def test_a_clock_that_still_reads_wrong_is_logged_and_does_not_stop_the_adoption(caplog):
    """It costs the overlay its time. Raising would stop the name, rotation and streams too."""
    caplog.set_level(logging.WARNING)
    wrong = camera_time(SET_AT + timedelta(hours=8))
    client, opener = clock_client(wrong, wrong)

    client.set_clock(SET_AT)

    assert opener.cmds().count("SetTime") == 2
    assert "the clock does not read back as it was set" in caplog.text
    assert PASSWORD not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "changed",
    [{"mon": 0}, {"year": "2026"}, {"hour": 24}, {"sec": None}, {"day": True}, {"timeZone": "0"}],
)
def test_set_clock_takes_an_unusable_reading_as_not_set(changed):
    """The factory reading is year 0, month 0. It must not crash the step."""
    unusable = camera_time(SET_AT, **changed)
    client, opener = clock_client(unusable, unusable)

    client.set_clock(SET_AT)

    assert opener.cmds().count("SetTime") == 2


def test_set_clock_takes_a_reply_with_no_time_as_not_set():
    reply = [{"cmd": "GetTime", "code": 0, "value": {}}]
    client, opener = clock_client(reply, reply)

    client.set_clock(SET_AT)

    assert opener.cmds().count("SetTime") == 2


def test_set_clock_still_fails_when_the_camera_refuses_get_time():
    refused = [{"cmd": "GetTime", "code": 1, "error": {"rspCode": -6}}]
    client, _ = clock_client(refused)

    with pytest.raises(ApiError) as info:
        client.set_clock(SET_AT)

    assert info.value.rsp_code == -6


@pytest.mark.parametrize("code", ["evil\ninjected line", {"a": 1}, 1.5, True, None])
def test_a_code_that_is_not_a_number_never_reaches_the_message(code):
    """The message is logged now, and the camera chooses the code."""
    client, _ = logged_in(SetP2p=[{"cmd": "SetP2p", "code": 1, "error": {"rspCode": code}}])

    with pytest.raises(ApiError) as info:
        client.disable_p2p()

    assert str(info.value) == f"camera {HOST}: SetP2p failed, rspCode None"
    assert info.value.rsp_code is None


def test_set_name_sets_devname_and_changes_only_the_osd_name():
    client, opener = logged_in(
        SetDevName=recorded("setdevname")["response"],
        GetOsd=recorded("getosd")["response"],
        SetOsd=recorded("setosd")["response"],
    )

    client.set_name("cam-8da040")

    assert opener.cmds() == ["Login", "GetOsd", "SetDevName", "SetOsd"]
    bodies = opener.bodies("Login")
    assert bodies[0] == recorded("getosd")["request"]
    assert bodies[1] == recorded("setdevname")["request"]
    assert bodies[2] == recorded("setosd")["request"]


@pytest.mark.parametrize(
    "osd",
    [None, [], {"channel": 0}, {"osdChannel": None}, {"osdChannel": "x"}],
    ids=repr,
)
def test_set_name_with_a_malformed_osd_writes_nothing(osd):
    client, opener = logged_in(GetOsd=[{"cmd": "GetOsd", "code": 0, "value": {"Osd": osd}}])

    with pytest.raises(ApiError):
        client.set_name("cam-8da040")

    assert opener.cmds() == ["Login", "GetOsd"]


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


@pytest.mark.parametrize(
    "value",
    [{}, None, "x", {"Isp": None}, {"Isp": []}, {"Isp": "x"}],
    ids=repr,
)
def test_read_without_the_expected_object_is_an_api_error(value):
    client, opener = logged_in(GetIsp=[{"cmd": "GetIsp", "code": 0, "value": value}])

    with pytest.raises(ApiError):
        client.set_rotation(True)

    assert opener.cmds() == ["Login", "GetIsp"]


# --- network ports: SetNetPort has no recording, see the report -------------

SET_NET_PORT_OK = [{"cmd": "SetNetPort", "code": 0, "value": {"rspCode": 200}}]


def net_ports(**fields) -> list:
    response = recorded("getnetport")["response"]
    response[0]["value"]["NetPort"].update(fields)
    return response


def two_token_login():
    """The first Login returns TOKEN and later ones return a different token."""
    answers = iter([TOKEN, "second-token"])

    def answer():
        response = login_ok()
        response[0]["value"]["Token"]["name"] = next(answers)
        return response

    return answer


def test_enable_rtsp_does_not_write_when_rtsp_is_already_on():
    client, opener = logged_in(GetNetPort=recorded("getnetport")["response"])

    client.enable_rtsp()

    assert opener.cmds() == ["Login", "GetNetPort"]
    assert opener.bodies("Login") == [recorded("getnetport")["request"]]


def test_disable_onvif_does_not_write_when_onvif_is_already_off():
    client, opener = logged_in(GetNetPort=net_ports(onvifEnable=0))

    client.disable_onvif()

    assert opener.cmds() == ["Login", "GetNetPort"]


def test_enable_rtsp_turns_on_only_rtsp_and_logs_in_again():
    current = net_ports(rtspEnable=0, onvifEnable=0)
    client, opener = logged_in(
        GetNetPort=current, SetNetPort=SET_NET_PORT_OK, Login=two_token_login(),
        SetP2p=recorded("setp2p")["response"], SetUpnp=recorded("setupnp")["response"],
    )

    client.enable_rtsp()

    assert opener.cmds() == ["Login", "GetNetPort", "SetNetPort", "Login"]
    written = opener.calls[2]["body"][0]
    assert written["param"]["NetPort"] == dict(current[0]["value"]["NetPort"], rtspEnable=1)
    assert written["param"]["NetPort"]["onvifEnable"] == 0
    assert written["param"]["NetPort"]["rtmpEnable"] == 0
    relogin = opener.calls[3]
    assert "token" not in relogin["query"]
    assert relogin["body"][0]["param"]["User"]["password"] == PASSWORD

    client.disable_p2p()
    assert opener.calls[4]["query"]["token"] == ["second-token"]


def test_disable_onvif_turns_off_only_onvif_and_logs_in_again():
    current = net_ports(onvifEnable=1, rtspEnable=1)
    client, opener = logged_in(
        GetNetPort=current, SetNetPort=SET_NET_PORT_OK, Login=two_token_login(),
        SetP2p=recorded("setp2p")["response"], SetUpnp=recorded("setupnp")["response"],
    )

    client.disable_onvif()

    assert opener.cmds() == ["Login", "GetNetPort", "SetNetPort", "Login"]
    written = opener.calls[2]["body"][0]["param"]["NetPort"]
    assert written == dict(current[0]["value"]["NetPort"], onvifEnable=0)
    assert written["rtspEnable"] == 1

    client.disable_p2p()
    assert opener.calls[4]["query"]["token"] == ["second-token"]


def test_relogin_after_a_port_write_uses_the_current_password():
    client, opener = logged_in(
        ModifyUser=recorded("modifyuser")["response"],
        GetNetPort=net_ports(onvifEnable=1),
        SetNetPort=SET_NET_PORT_OK,
    )

    client.set_password(NEW_PASSWORD)
    client.disable_onvif()

    assert opener.calls[-1]["cmd"] == "Login"
    assert opener.calls[-1]["body"][0]["param"]["User"]["password"] == NEW_PASSWORD


def test_rejected_port_write_does_not_log_in_again():
    rejected = [{"cmd": "SetNetPort", "code": 1, "error": {"rspCode": -4}}]
    client, opener = logged_in(GetNetPort=net_ports(onvifEnable=1), SetNetPort=rejected)

    with pytest.raises(ApiError):
        client.disable_onvif()

    assert opener.cmds() == ["Login", "GetNetPort", "SetNetPort"]


# --- device_info, logout, ordering -----------------------------------------


def test_device_info_returns_the_devinfo_object():
    info = {"model": "RLC-520A", "firmVer": "v3"}
    client, opener = logged_in(
        GetDevInfo=[{"cmd": "GetDevInfo", "code": 0, "value": {"DevInfo": info}}]
    )

    assert client.device_info() == info
    assert opener.calls[1]["body"] == [{"cmd": "GetDevInfo", "action": 0, "param": {}}]
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

    with pytest.raises(ApiError) as caught:
        client.set_password(NEW_PASSWORD)

    assert_clean(caught.value, NEW_PASSWORD)
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
        self.kwargs = None
        self.closed = False

    def __call__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return self

    async def logout(self):
        self.closed = True


async def port_is_open(_host):
    pass


def run_enable(baichuan, password=""):
    factory = FakeHostFactory(baichuan)
    asyncio.run(enable_services(HOST, password, host_factory=factory, probe=port_is_open))
    return factory


def test_baichuan_enables_http_and_rtsp_when_only_9000_is_open():
    ports = recorded("baichuan_factory_ports")
    baichuan = FakeBaichuan(ports["before"])

    factory = run_enable(baichuan, password="")

    assert factory.args == (HOST, "admin", "")
    assert factory.kwargs == {"bc_connection": ConnectionEnum.tcp}
    assert baichuan.ports == ports["after_enabling_http_and_rtsp"]
    assert baichuan.calls == ["login", "set:http", "set:rtsp", "logout"]


def test_closed_baichuan_port_is_unreachable_and_never_builds_a_host():
    async def closed(_host):
        raise ConnectionRefusedError(111, "refused")

    factory = FakeHostFactory(FakeBaichuan({}))

    with pytest.raises(Unreachable) as caught:
        asyncio.run(enable_services(HOST, PASSWORD, host_factory=factory, probe=closed))

    assert_clean(caught.value, PASSWORD)
    assert factory.args is None


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError(111, "refused"),
        OSError(errno.EHOSTUNREACH, "no route"),
        TimeoutError("timed out"),
    ],
    ids=failure_id,
)
def test_default_probe_connects_to_port_9000_and_reports_unreachable(monkeypatch, failure):
    targets = []

    async def open_connection(host, port):
        targets.append((host, port))
        raise failure

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    factory = FakeHostFactory(FakeBaichuan({}))

    with pytest.raises(Unreachable):
        asyncio.run(enable_services(HOST, "", host_factory=factory))

    assert targets == [(HOST, 9000)]
    assert factory.args is None


def test_probe_gives_up_after_a_short_timeout(monkeypatch):
    async def never_connects(host, port):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "open_connection", never_connects)
    monkeypatch.setattr(reolink, "_PROBE_TIMEOUT", 0.01)

    with pytest.raises(Unreachable):
        asyncio.run(enable_services(HOST, "", host_factory=FakeHostFactory(FakeBaichuan({}))))


def test_default_probe_closes_its_socket_and_lets_baichuan_run():
    async def scenario():
        peer_saw_eof = asyncio.Event()

        async def handle(reader, writer):
            await reader.read()
            peer_saw_eof.set()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        await reolink._probe_port("127.0.0.1", port)
        await asyncio.wait_for(peer_saw_eof.wait(), 2)
        server.close()

    asyncio.run(scenario())


def test_probe_tolerates_a_reset_while_hanging_up(monkeypatch):
    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            raise ConnectionResetError(104, "reset")

    async def open_connection(host, port):
        return None, Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    asyncio.run(reolink._probe_port(HOST))


@pytest.mark.parametrize(
    "failure",
    [
        rl_exc.ReolinkConnectionError("refused"),
        rl_exc.ReolinkTimeoutError("timed out"),
        rl_exc.ApiError("status 500", rspCode=500),
        rl_exc.UnexpectedDataError(f"bad {PASSWORD}"),
        ConnectionResetError(104, "reset"),
        TimeoutError("timed out"),
        ET.ParseError("truncated"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        KeyError("x"),
        TypeError("x"),
        AttributeError("x"),
        IndexError("x"),
        EOFError("x"),
        ValueError("x"),
    ],
    ids=failure_id,
)
def test_baichuan_login_failure_past_the_probe_is_outcome_unknown(failure):
    baichuan = FakeBaichuan({}, login_error=failure)

    with pytest.raises(LoginOutcomeUnknown) as caught:
        run_enable(baichuan, password=PASSWORD)

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


@pytest.mark.parametrize(
    "failure",
    [
        rl_exc.CredentialsInvalidError("Login has been locked"),
        rl_exc.LoginError("Login has been locked"),
        rl_exc.ApiError("Login has been locked", rspCode=500),
    ],
    ids=failure_id,
)
def test_baichuan_locked_wording_raises_locked_out(failure):
    with pytest.raises(LockedOut) as caught:
        run_enable(FakeBaichuan({}, login_error=failure))

    assert caught.value.remain_times is None
    assert caught.value.unlock_time == 0


@pytest.mark.parametrize("wording", ["invalid user", "login failed", "password wrong"])
def test_baichuan_credential_wording_without_the_type_raises_auth_failed(wording):
    failure = rl_exc.LoginError(f"host said: {wording}")

    with pytest.raises(AuthFailed) as caught:
        run_enable(FakeBaichuan({}, login_error=failure))

    assert caught.value.remain_times is None


@pytest.mark.parametrize(
    "failure",
    [
        rl_exc.ApiError(f"fail {PASSWORD}", rspCode=400),
        rl_exc.ReolinkConnectionError("gone"),
        ET.ParseError("truncated"),
        KeyError("x"),
    ],
    ids=failure_id,
)
def test_baichuan_failure_after_login_is_an_api_error_and_still_logs_out(failure):
    ports = recorded("baichuan_factory_ports")["before"]
    baichuan = FakeBaichuan(ports, set_error=failure)

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


@pytest.mark.parametrize(
    "failure",
    [rl_exc.ReolinkConnectionError("gone"), ConnectionResetError(104, "gone"), ET.ParseError("x")],
    ids=failure_id,
)
def test_baichuan_logout_failure_after_success_is_not_an_error(caplog, failure):
    ports = recorded("baichuan_factory_ports")
    baichuan = FakeBaichuan(ports["before"], logout_error=failure)

    run_enable(baichuan)

    assert baichuan.ports == ports["after_enabling_http_and_rtsp"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_logout_failure_logs_its_type_and_not_its_text(caplog):
    ports = recorded("baichuan_factory_ports")["before"]
    baichuan = FakeBaichuan(ports, logout_error=rl_exc.ReolinkConnectionError(f"gone {PASSWORD}"))

    run_enable(baichuan, password=PASSWORD)

    assert "ReolinkConnectionError" in caplog.text
    assert PASSWORD not in caplog.text


class _ConnectionWithNoDropCall:
    """What a reolink_aio upgrade that renames the private call would leave."""


def test_a_renamed_drop_connection_does_not_turn_success_into_an_error(caplog):
    ports = recorded("baichuan_factory_ports")
    baichuan = FakeBaichuan(ports["before"])
    baichuan._connection = _ConnectionWithNoDropCall()

    factory = run_enable(baichuan)

    assert baichuan.ports == ports["after_enabling_http_and_rtsp"]
    assert baichuan.calls[-1] == "logout"
    assert factory.closed
    assert "AttributeError" in caplog.text


def test_a_renamed_drop_connection_does_not_hide_a_rejected_login():
    baichuan = FakeBaichuan({}, login_error=rl_exc.CredentialsInvalidError("bad"))
    baichuan._connection = _ConnectionWithNoDropCall()
    factory = FakeHostFactory(baichuan)

    with pytest.raises(AuthFailed):
        asyncio.run(enable_services(HOST, "", host_factory=factory, probe=port_is_open))

    assert factory.closed


def test_real_host_session_is_closed_after_enable_services():
    # The session is opened by Host's constructor and baichuan.logout leaves it
    # open. Only the Baichuan calls are stubbed, so nothing reaches the network.
    built = []

    async def do_nothing(*args, **kwargs):
        pass

    def factory(*args, **kwargs):
        host = Host(*args, **kwargs)
        host.baichuan.login = do_nothing
        host.baichuan.set_port_enabled = do_nothing
        built.append(host)
        return host

    asyncio.run(enable_services(HOST, "", host_factory=factory, probe=port_is_open))

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
        host = Host(HOST, "admin", "")
        await host.logout()  # closes the session the constructor opened
        return host.baichuan

    baichuan = asyncio.run(build())
    for name in ("login", "set_port_enabled", "logout"):
        assert inspect.iscoroutinefunction(getattr(baichuan, name))
    params = inspect.signature(baichuan.set_port_enabled).parameters
    assert list(params)[:2] == ["port", "enable"]
    assert {PortType.http.name, PortType.rtsp.name} == {"http", "rtsp"}


# --- the real Host against a fake Baichuan peer on loopback -----------------
#
# These show what this module and reolink_aio do. They say nothing about how an
# RLC-520A counts login frames.

BC_MAGIC = bytes.fromhex("f0debc0a")
BC_NONCE = b'<?xml version="1.0"?><body><Encryption version="1.1"><type>md5</type><nonce>abc</nonce></Encryption></body>'
BC_LOGIN_OK = b'<?xml version="1.0"?><body><DeviceInfo version="1.1"><type>ipc</type></DeviceInfo></body>'
BC_TRUNCATED = b'<?xml version="1.0"?><body><Device'


def bc_reply(cmd, mess_id, status=200, body=b""):
    fields = struct.pack("<II", cmd, len(body)) + mess_id + struct.pack("<H", status)
    return BC_MAGIC + fields + bytes(2) + bytes(4) + body


def bc_encrypted_reply(cmd, mess_id, xml):
    body = encrypt_baichuan(xml, mess_id[0])
    return BC_MAGIC + struct.pack("<II", cmd, len(body)) + mess_id + bytes.fromhex("00dd1466") + body


class BaichuanPeer:
    def __init__(self, mode):
        self.mode = mode
        self.login_frames = 0
        self.port_frames = 0
        self.saw_eof = asyncio.Event()

    def nonce_reply(self, mess_id):
        if self.mode == "nonce_truncated":
            return bc_encrypted_reply(1, mess_id, BC_TRUNCATED)
        if self.mode == "nonce_not_utf8":
            return BC_MAGIC + struct.pack("<II", 1, 4) + mess_id + bytes.fromhex("00dd1466") + b"\xff\xfe\x80\x81"
        return bc_encrypted_reply(1, mess_id, BC_NONCE)

    def login_reply(self, mess_id):
        if self.mode == "wrong":
            return bc_reply(1, mess_id, 401)
        if self.mode == "login_500":
            return bc_reply(1, mess_id, 500)
        if self.mode == "login_empty":
            return bc_reply(1, mess_id)
        xml = BC_TRUNCATED if self.mode == "login_truncated" else BC_LOGIN_OK
        return bc_reply(1, mess_id, 200, encrypt_baichuan(xml, mess_id[0]))

    async def handle(self, reader, writer):
        try:
            while True:
                header = await reader.readexactly(20)
                cmd, length = struct.unpack("<II", header[4:12])
                mess_id = header[12:16]
                message_class = header[18:20].hex()
                if message_class == "1464":
                    await reader.readexactly(4)
                if length:
                    await reader.readexactly(length)
                if cmd == 1 and message_class == "1465":
                    writer.write(self.nonce_reply(mess_id))
                elif cmd == 1:
                    self.login_frames += 1
                    if self.mode == "login_close":
                        return
                    writer.write(self.login_reply(mess_id))
                elif cmd == 36:
                    self.port_frames += 1
                    status = 500 if self.mode == "set_500" else 200
                    writer.write(bc_reply(36, mess_id, status))
                else:
                    writer.write(bc_reply(cmd, mess_id))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.saw_eof.set()
        finally:
            writer.close()


def run_against_peer(mode):
    """Run enable_services through the real Host. Returns the peer and any CameraError."""

    async def scenario():
        peer = BaichuanPeer(mode)
        server = await asyncio.start_server(peer.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        def factory(host, user, password, **kwargs):
            return Host(host, user, password, bc_port=port, **kwargs)

        error = None
        try:
            await enable_services("127.0.0.1", PASSWORD, host_factory=factory, probe=port_is_open)
        except CameraError as err:
            error = err
        if mode != "login_close":
            await asyncio.wait_for(peer.saw_eof.wait(), 2)
        server.close()
        return peer, error

    return asyncio.run(scenario())


def test_real_host_enables_both_ports_and_closes_the_socket():
    peer, error = run_against_peer("ok")

    assert error is None
    assert peer.login_frames == 1
    assert peer.port_frames == 2


def test_real_host_wrong_password_is_auth_failed_with_one_frame_and_closes_the_socket():
    peer, error = run_against_peer("wrong")

    assert isinstance(error, AuthFailed)
    assert error.remain_times is None
    assert peer.login_frames == 1
    assert_clean(error, PASSWORD)


def test_real_host_resends_the_login_frame_at_most_three_times():
    # reolink_aio retries cmd_id 1 on connection errors. Pinned because the report
    # gives this number to weigh against the camera's limit.
    peer, error = run_against_peer("login_close")

    assert isinstance(error, LoginOutcomeUnknown)
    assert peer.login_frames == 3
    assert_clean(error, PASSWORD)


@pytest.mark.parametrize(
    "mode", ["nonce_truncated", "nonce_not_utf8", "login_truncated", "login_empty", "login_500"]
)
def test_real_host_malformed_replies_are_outcome_unknown_and_close_the_socket(mode):
    _, error = run_against_peer(mode)

    assert isinstance(error, LoginOutcomeUnknown)
    assert_clean(error, PASSWORD)


def test_real_host_port_failure_after_login_is_an_api_error():
    peer, error = run_against_peer("set_500")

    assert isinstance(error, ApiError)
    assert peer.login_frames == 1


# --- the real opener against an HTTP server in this process ------------------


def read_request(conn):
    conn.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    while len(body) < length:
        body += conn.recv(65536)
    return head + b"\r\n\r\n" + body


class LoopbackServer:
    """Accepts on 127.0.0.1 and hands each request to `behave(conn)`."""

    def __init__(self, behave):
        self.behave = behave
        self.requests = []
        self.release = threading.Event()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.host = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self.requests.append(read_request(conn))
                self.behave(self, conn)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        # shutdown() is what wakes a thread blocked in accept() on Linux; close() alone does not.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(5)


def http_reply(status, body=b"", headers=b"", content_length=None):
    length = len(body) if content_length is None else content_length
    return (
        f"HTTP/1.1 {status} X\r\nContent-Length: {length}\r\nConnection: close\r\n".encode()
        + headers
        + b"\r\n"
        + body
    )


def replies(payload):
    def behave(_server, conn):
        conn.sendall(payload)

    return behave


def close_without_replying(_server, _conn):
    pass


def reset(_server, conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def hang(server, _conn):
    server.release.wait(5)


LOGIN_OK_BODY = json.dumps(login_ok()).encode()
SHORT_WITH_LENGTH = http_reply(200, LOGIN_OK_BODY[:10], content_length=len(LOGIN_OK_BODY))
SHORT_WITHOUT_LENGTH = (
    b"HTTP/1.1 200 X\r\nConnection: close\r\n\r\n" + LOGIN_OK_BODY[:10]
)
AFTER_SEND = {
    "no_reply": close_without_replying,
    "reset": reset,
    "read_timeout": hang,
    "short_with_length": replies(SHORT_WITH_LENGTH),
    "short_without_length": replies(SHORT_WITHOUT_LENGTH),
    "garbage_status": replies(b"NOT HTTP AT ALL\r\n\r\n"),
    "not_json": replies(http_reply(200, b"<html>booting</html>")),
}


def closed_port_host():
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return f"127.0.0.1:{sock.getsockname()[1]}"


def test_default_opener_logs_in_and_sends_the_recorded_request():
    with LoopbackServer(replies(http_reply(200, LOGIN_OK_BODY))) as server:
        client = ReolinkClient(server.host)
        client.login("admin", "")

    request = server.requests[0]
    assert request.startswith(b"POST /cgi-bin/api.cgi?cmd=Login HTTP/1.1\r\n")
    assert b"content-type: application/json" in request.lower()


def test_default_opener_refused_is_unreachable():
    client = ReolinkClient(closed_port_host())

    with pytest.raises(Unreachable) as caught:
        client.login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), OSError(errno.EHOSTUNREACH, "no route"), ConnectionRefusedError(111, "x")],
    ids=failure_id,
)
def test_default_opener_connect_failure_is_unreachable(monkeypatch, failure):
    def cannot_connect(*args, **kwargs):
        raise failure

    monkeypatch.setattr(socket, "create_connection", cannot_connect)

    with pytest.raises(Unreachable) as caught:
        ReolinkClient(HOST).login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize(
    "failure",
    [BrokenPipeError(32, "x"), ConnectionResetError(104, "x"), TimeoutError("x")],
    ids=failure_id,
)
def test_default_opener_failure_while_sending_is_outcome_unknown(monkeypatch, failure):
    # urllib wraps this in the same URLError as a connect failure.
    def cannot_send(self, data):
        raise failure

    monkeypatch.setattr(http.client.HTTPConnection, "send", cannot_send)
    with LoopbackServer(close_without_replying) as server, pytest.raises(
        LoginOutcomeUnknown
    ) as caught:
        ReolinkClient(server.host).login("admin", PASSWORD)

    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize("behaviour", AFTER_SEND)
def test_default_opener_login_after_send_is_outcome_unknown(behaviour):
    with LoopbackServer(AFTER_SEND[behaviour]) as server:
        client = ReolinkClient(server.host, timeout=0.2)
        with pytest.raises(LoginOutcomeUnknown) as caught:
            client.login("admin", PASSWORD)

    assert server.requests, "the request must have reached the server"
    assert_clean(caught.value, PASSWORD)


@pytest.mark.parametrize("behaviour", AFTER_SEND)
def test_default_opener_other_call_after_send_is_an_api_error(behaviour):
    answers = iter([replies(http_reply(200, LOGIN_OK_BODY)), AFTER_SEND[behaviour]])

    def behave(server, conn):
        next(answers)(server, conn)

    with LoopbackServer(behave) as server:
        client = ReolinkClient(server.host, timeout=0.2)
        client.login("admin", PASSWORD)
        with pytest.raises(ApiError) as caught:
            client.disable_p2p()

    assert f"token={TOKEN}".encode() in server.requests[1]
    assert_clean(caught.value, PASSWORD, TOKEN)


@pytest.mark.parametrize(
    "location",
    [b"http://127.0.0.1:1/elsewhere", b"/cgi-bin/api.cgi?cmd=Login", b"http://[bad", b"ftp://x/"],
)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_default_opener_does_not_follow_a_redirect(status, location):
    answer = http_reply(status, headers=b"Location: " + location + b"\r\n")
    with LoopbackServer(replies(answer)) as server:
        client = ReolinkClient(server.host, timeout=1)
        with pytest.raises(ApiError) as caught:
            client.login("admin", PASSWORD)

    assert len(server.requests) == 1
    assert str(status) in str(caught.value)
    assert_clean(caught.value, PASSWORD)


def test_default_opener_http_500_is_an_api_error_with_a_clean_chain():
    answers = iter([http_reply(200, LOGIN_OK_BODY), http_reply(500, b"x")])

    def behave(_server, conn):
        conn.sendall(next(answers))

    with LoopbackServer(behave) as server:
        client = ReolinkClient(server.host)
        client.login("admin", PASSWORD)
        with pytest.raises(ApiError) as caught:
            client.disable_p2p()

    assert_clean(caught.value, TOKEN, PASSWORD)


def test_default_opener_ignores_a_proxy_in_the_environment(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    with LoopbackServer(replies(http_reply(200, LOGIN_OK_BODY))) as server:
        ReolinkClient(server.host).login("admin", "")

    assert len(server.requests) == 1
