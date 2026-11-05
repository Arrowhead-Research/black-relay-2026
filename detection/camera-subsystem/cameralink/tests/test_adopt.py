"""The adoption state machine, against a fake camera that holds real password state.

No test touches the network or sleeps: ports, the client and enable_services are fakes
sharing one FakeCamera, and the clock is passed in. PowerCut derives from BaseException,
so that no ``except`` in the code under test can survive it, as nothing survives a power cut.
"""

import asyncio
import errno
import fcntl
import inspect
import io
import json
import logging
import os
import socket
import stat
import tempfile
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cameralink import adopt
from cameralink.adopt import STEPS, Lease, LinkState, step
from cameralink.reolink import (
    ApiError,
    AuthFailed,
    LockedOut,
    LoginOutcomeUnknown,
    ReolinkClient,
    Unreachable,
    enable_services,
)
from cameralink.store import PHASES, CameraRecord, CameraStore, StoreError

MAC = "ec:71:db:8d:a0:40"
OTHER_MAC = "ec:71:db:00:00:02"
IP = "10.77.0.10"
LEASE = Lease(mac=MAC, ip=IP)
NODE = "cam-8da040"
SECRET = "known-secret-password"
LIMIT = 10


class PowerCut(BaseException):
    pass


def seed(path, record):
    """What the node would have on disk, however it got there."""
    store = CameraStore(path)
    with store.lock():
        existing = store.load(record.mac)
        store.save(replace(record, revision=existing.revision) if existing else record)


class Power:
    """Numbers every camera operation and store write, and cuts power at a chosen one."""

    def __init__(self, crash_at=None, when="before"):
        self.crash_at = crash_at
        self.when = when
        self.events = []

    def before(self, name):
        index = len(self.events)
        self.events.append(name)
        if index == self.crash_at and self.when == "before":
            raise PowerCut(name)
        return index

    def after(self, index):
        if index == self.crash_at and self.when == "after":
            raise PowerCut(self.events[index])


class FakeCamera:
    """A camera's password, lockout counter, open ports and settings."""

    def __init__(self, password="", http=True, mac=MAC, power=None):
        self.mac = mac
        self.password = password
        self.http = http
        self.up = True
        self.failures = 0
        self.power = power or Power()
        self.logins = []  # (channel, password) of every login the camera received
        self.calls = []  # every setting call, by name
        self.settings = {}
        self.script = {}  # operation name -> list of exceptions to raise, one per call
        self.after_applied = set()  # operation names whose exception comes after they apply

    def judge(self, channel, password, remain=True):
        self.logins.append((channel, password))
        if self.failures >= LIMIT:
            raise LockedOut("locked", 0, 300)
        if password != self.password:
            self.failures += 1
            remain_times = LIMIT - self.failures
            if remain_times == 0:
                raise LockedOut("locked", 0, 300)
            raise AuthFailed("rejected", remain_times if remain else None)
        self.failures = 0

    def scripted(self, name):
        queue = self.script.get(name)
        return queue.pop(0) if queue else None

    def factory_reset(self):
        self.password = ""
        self.http = False
        self.failures = 0
        self.settings = {}

    def ports(self):
        return {80: self.up and self.http, 9000: self.up}


class FakeClient:
    """Mirrors what ReolinkClient requires of its caller, and nothing more."""

    def __init__(self, camera):
        self.camera = camera
        self.token = False
        self.user = None
        self.current = None

    def _run(self, name, apply):
        camera = self.camera
        index = camera.power.before(name)
        if not self.token:
            raise ApiError(f"{name} needs a login first")
        if not (camera.up and camera.http):
            raise Unreachable(f"{name} not delivered")
        camera.calls.append(name)
        error = camera.scripted(name)
        if error is not None and name not in camera.after_applied:
            raise error
        apply()
        if error is not None:
            raise error  # the camera acted, and the reply was lost
        camera.power.after(index)

    def login(self, user, password):
        camera = self.camera
        index = camera.power.before("login")
        self.token = False
        if not (camera.up and camera.http):
            raise Unreachable("login not delivered")
        error = camera.scripted("login")
        if isinstance(error, Unreachable):
            raise error
        if error is not None:
            camera.logins.append(("http", password))
            raise error
        try:
            camera.judge("http", password)
        finally:
            camera.power.after(index)  # the cut can come with the verdict on its way back
        self.token, self.user, self.current = True, user, password

    def logout(self):
        if not self.token:
            return
        index = self.camera.power.before("logout")
        self.token = False
        error = self.camera.scripted("logout")
        if error is not None:
            raise error
        self.camera.power.after(index)

    def set_password(self, new):
        def apply():
            self.camera.password = new
            self.current = new
            self.token = False  # the camera ends the session

        self._run("set_password", apply)

    def disable_p2p(self):
        self._run("disable_p2p", lambda: self.camera.settings.update(p2p=False))

    def enable_rtsp(self):
        self._port_flag("enable_rtsp", "rtsp", True)

    def disable_onvif(self):
        self._port_flag("disable_onvif", "onvif", False)

    def _port_flag(self, name, flag, value):
        def apply():
            changed = self.camera.settings.get(flag) != value
            self.camera.settings[flag] = value
            if changed:
                self.login(self.user, self.current)

        self._run(name, apply)

    def set_clock(self, now):
        self._run("set_clock", lambda: self.camera.settings.update(clock=now))

    def set_name(self, name):
        self._run("set_name", lambda: self.camera.settings.update(name=name))

    def set_streams(self):
        self._run("set_streams", lambda: self.camera.settings.update(streams=True))

    def set_rotation(self, flipped):
        self._run("set_rotation", lambda: self.camera.settings.update(flipped=flipped))


class Clock:
    def __init__(self):
        self.time = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        self.since_boot = 1000.0

    def __call__(self):
        return self.time

    def monotonic(self):
        return self.since_boot

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)
        self.since_boot += seconds

    def wall_step(self, seconds):
        """NTP correcting the wall clock. Time since boot does not notice."""
        self.time += timedelta(seconds=seconds)


class TracedStore(CameraStore):
    def __init__(self, path, power):
        super().__init__(path)
        self.power = power

    def save(self, record):
        index = self.power.before("save")
        written = super().save(record)
        self.power.after(index)
        return written


class Bench:
    """A node, one camera at a time, and a clock. Restarts are a new Bench.store."""

    def __init__(self, path, camera, node_id=NODE, flipped=False):
        self.path = path
        self.camera = camera
        self.clock = Clock()
        self.store = TracedStore(path, camera.power)
        self.generation = 1
        self.node_id = node_id
        self.flipped = flipped
        self.enable_passwords = []

    async def enable(self, host, password):
        camera = self.camera
        if not camera.up:
            raise Unreachable("port 9000 closed")
        index = camera.power.before("enable_services")
        self.enable_passwords.append(password)
        error = camera.scripted("enable_services")
        if isinstance(error, Unreachable):
            raise error
        if error is not None:
            camera.logins.append(("baichuan", password))
            raise error
        try:
            camera.judge("baichuan", password, remain=False)
            camera.http = True
        finally:
            camera.power.after(index)

    def probe(self, host, port):
        return self.camera.ports()[port]

    def tick(self, generation=None, lease=None, **overrides):
        if generation is not None:
            self.generation = generation
        camera_lease = lease or Lease(mac=self.camera.mac, ip=IP)
        args = {
            "node_id": self.node_id,
            "flipped": self.flipped,
            "enable": self.enable,
            "probe": self.probe,
            "monotonic": self.clock.monotonic,
        }
        args.update(overrides)
        return step(
            camera_lease,
            self.generation,
            self.store,
            lambda host: FakeClient(self.camera),
            self.clock,
            **args,
        )

    def link_up(self, generation=None, **overrides):
        """A whole link generation: the port accepts, 45 s pass, the node acts."""
        self.tick(generation, **overrides)
        self.clock.advance(45)
        state = self.tick(generation, **overrides)
        for _ in range(3):  # HTTP may take a moment after enable_services
            if state is not LinkState.ADOPTING:
                break
            state = self.tick(generation, **overrides)
        return state

    def restart(self):
        self.store = TracedStore(self.path, self.camera.power)

    def record(self):
        return CameraStore(self.path).load(self.camera.mac)

    def known(self, password=SECRET, **fields):
        applied = {
            "disable_p2p": True,
            "enable_rtsp": True,
            "disable_onvif": True,
            "set_clock": True,
            "set_name": NODE,
            "set_streams": True,
            "set_rotation": False,
        }
        fields.setdefault("applied", applied)
        seed(self.path, CameraRecord(self.camera.mac, password, None, None, **fields))


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state" / "camera.json"


@pytest.fixture
def factory(path):
    return Bench(path, FakeCamera(password="", http=False))


@pytest.fixture
def blank_http(path):
    """A camera with HTTP on and a blank password, which an interrupted adoption leaves."""
    return Bench(path, FakeCamera(password="", http=True))


@pytest.fixture
def stranger(path):
    """A camera whose password this node has never known."""
    return Bench(path, FakeCamera(password="somebody-elses", http=True))


@pytest.fixture
def adopted(path):
    bench = Bench(path, FakeCamera(password=SECRET, http=True))
    bench.known()
    return bench


def assert_fully_adopted(bench):
    record = bench.record()
    camera = bench.camera
    assert record.password and record.password == camera.password
    assert record.pending_password is None
    assert camera.settings["p2p"] is False
    assert camera.settings["rtsp"] is True
    assert camera.settings["onvif"] is False
    assert "clock" in camera.settings
    assert camera.settings["name"] == NODE
    assert camera.settings["streams"] is True
    assert camera.settings["flipped"] is False
    assert set(record.applied) == set(STEPS)


# Tests named in the brief.


def test_factory_camera_is_adopted_and_password_saved_before_the_change(factory):
    seen = {}
    original = FakeClient.set_password

    def spying(self, new):
        on_disk = CameraStore(factory.path).load(MAC)
        seen.update(pending=on_disk.pending_password, new=new, camera=self.camera.password)
        original(self, new)

    FakeClient.set_password = spying
    try:
        assert factory.link_up() is LinkState.ADOPTED
    finally:
        FakeClient.set_password = original

    assert seen["pending"] == seen["new"]
    assert seen["camera"] == ""
    assert_fully_adopted(factory)
    assert len(factory.camera.password) == 20
    assert factory.camera.failures == 0


def test_power_cut_after_saving_pending_password_tries_it_then_factory(blank_http):
    bench = blank_http
    seed(bench.path, CameraRecord(MAC, "", "the-pending-one", "x/0"))

    assert bench.link_up() is LinkState.ADOPTED

    assert bench.camera.logins[:3] == [
        ("http", "the-pending-one"),
        ("http", ""),
        ("http", "the-pending-one"),
    ]
    assert bench.camera.password == "the-pending-one"
    assert_fully_adopted(bench)


def test_power_cut_after_the_change_recovers_with_the_pending_password(path):
    bench = Bench(path, FakeCamera(password="the-pending-one", http=True))
    seed(path, CameraRecord(MAC, "", "the-pending-one", "x/0"))

    assert bench.link_up() is LinkState.ADOPTED

    assert bench.camera.logins[0] == ("http", "the-pending-one")
    assert ("http", "") not in bench.camera.logins
    assert "set_password" not in bench.camera.calls
    assert bench.camera.failures == 0
    assert_fully_adopted(bench)


def test_refused_connection_does_not_use_the_attempt(blank_http):
    bench = blank_http
    bench.camera.up = False
    for _ in range(5):
        assert bench.tick() is LinkState.ADOPTING
        bench.clock.advance(60)
    assert bench.camera.logins == []
    assert bench.record().attempted != f"{MAC}/1"

    # Accepted by the probe, refused by the login: put the attempt back.
    bench.camera.up = True
    bench.tick()
    bench.clock.advance(45)
    bench.camera.script["login"] = [Unreachable("refused")]
    assert bench.tick() is LinkState.ADOPTING
    record = bench.record()
    assert record.attempted != f"{MAC}/1"
    assert record.failures == ()

    assert bench.tick() is LinkState.ADOPTED


def test_unknown_password_makes_one_attempt_and_reports_needs_reset(stranger):
    assert stranger.link_up() is LinkState.NEEDS_RESET
    assert stranger.camera.logins == [("http", "")]
    for _ in range(5):
        stranger.clock.advance(60)
        assert stranger.tick() is LinkState.NEEDS_RESET
    assert stranger.camera.failures == 1


def test_daemon_restart_does_not_retry_login_in_the_same_link_generation(stranger):
    assert stranger.link_up() is LinkState.NEEDS_RESET
    for _ in range(3):
        stranger.restart()
        stranger.clock.advance(60)
        assert stranger.tick() is LinkState.NEEDS_RESET
    assert len(stranger.camera.logins) == 1


def test_daemon_restart_during_a_login_does_not_retry_it(blank_http):
    bench = blank_http
    bench.tick()
    bench.clock.advance(45)
    # The attempt is on disk and no outcome ever was.
    original = bench.camera.judge

    def dies(*args, **kwargs):
        raise PowerCut

    bench.camera.judge = dies
    with pytest.raises(PowerCut):
        bench.tick()
    bench.camera.judge = original

    bench.restart()
    bench.clock.advance(60)
    assert bench.tick() is LinkState.FAILED
    assert bench.camera.logins == []
    assert bench.record().attempted == f"{MAC}/1"


def test_link_flap_allows_one_new_attempt(stranger):
    assert stranger.link_up() is LinkState.NEEDS_RESET
    assert stranger.link_up(2) is LinkState.NEEDS_RESET
    assert stranger.camera.logins == [("http", ""), ("http", "")]
    stranger.clock.advance(60)
    assert stranger.tick() is LinkState.NEEDS_RESET
    assert len(stranger.camera.logins) == 2


def test_new_mac_is_adopted_with_a_new_password(adopted, path):
    assert adopted.link_up() is LinkState.ADOPTED
    new = Bench(path, FakeCamera(password="", http=False, mac=OTHER_MAC))

    assert new.link_up(2) is LinkState.ADOPTED

    assert CameraStore(path).load().mac == OTHER_MAC
    assert new.camera.password != SECRET
    assert all(password != SECRET for _, password in new.camera.logins)
    assert SECRET not in new.enable_passwords
    assert_fully_adopted(new)


def test_known_camera_logs_in_with_its_password_and_is_not_reconfigured(adopted):
    assert adopted.link_up() is LinkState.ADOPTED
    assert adopted.camera.logins == [("http", SECRET)]
    assert adopted.camera.calls == []
    assert adopted.camera.password == SECRET
    assert adopted.enable_passwords == []


@pytest.mark.parametrize("failing", STEPS)
def test_each_failed_setting_leaves_failed_and_retries_on_next_generation(factory, failing):
    camera = factory.camera
    camera.script[failing] = [ApiError("no")]

    assert factory.link_up() is LinkState.FAILED
    position = STEPS.index(failing)
    assert set(factory.record().applied) == set(STEPS[:position])
    assert factory.record().password == camera.password != ""

    camera.calls.clear()
    camera.logins.clear()
    assert factory.link_up(2) is LinkState.ADOPTED

    assert camera.calls == list(STEPS[position:])
    assert camera.logins[0] == ("http", factory.record().password)
    assert_fully_adopted(factory)


def test_lockout_stops_attempts(stranger):
    camera = stranger.camera
    camera.failures = LIMIT - 1

    assert stranger.link_up() is LinkState.NEEDS_RESET
    assert stranger.record().locked
    count = len(camera.logins)

    for generation in (2, 3, 4):
        assert stranger.link_up(generation) is LinkState.NEEDS_RESET
    assert len(camera.logins) == count


# Factory signature.


def test_factory_signature_uses_enable_services_with_the_blank_password(factory):
    assert factory.link_up() is LinkState.ADOPTED
    assert factory.enable_passwords == [""]
    assert factory.camera.logins[:2] == [("baichuan", ""), ("http", "")]


def test_factory_signature_failure_adds_no_http_blank_login(factory):
    factory.camera.script["enable_services"] = [AuthFailed("rejected")]

    assert factory.link_up() is LinkState.NEEDS_RESET
    assert factory.camera.logins == [("baichuan", "")]


def test_http_slow_to_open_after_enable_services_waits_without_a_new_guess(factory):
    factory.tick()
    factory.clock.advance(45)
    factory.camera.script["login"] = [Unreachable("refused"), Unreachable("refused")]

    assert factory.tick() is LinkState.ADOPTING
    assert factory.tick() is LinkState.ADOPTING
    assert factory.enable_passwords == [""]
    assert factory.record().failures == ()
    assert factory.tick() is LinkState.ADOPTED
    assert factory.enable_passwords == [""]


def test_port_9000_opening_before_80_is_judged_only_after_the_settle(path):
    # A known camera mid-boot looks like a factory one until HTTP comes up.
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()

    assert bench.tick() is LinkState.ADOPTING
    bench.clock.advance(30)
    bench.camera.http = True
    bench.clock.advance(15)

    assert bench.tick() is LinkState.ADOPTED
    assert bench.enable_passwords == []
    assert bench.camera.logins == [("http", SECRET)]


def test_reset_of_a_known_camera_is_adopted_again_with_a_new_password(adopted):
    assert adopted.link_up() is LinkState.ADOPTED
    adopted.camera.factory_reset()
    adopted.camera.calls.clear()

    assert adopted.link_up(2) is LinkState.ADOPTED

    assert adopted.enable_passwords == [""]
    assert adopted.camera.calls[:2] == ["set_password", "disable_p2p"]
    assert_fully_adopted(adopted)


def test_false_factory_signature_keeps_the_saved_password(path):
    # HTTP stayed closed past the settle on a camera that was never reset.
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()

    assert bench.link_up() is LinkState.NEEDS_RESET
    assert bench.enable_passwords == [""]
    assert bench.record().pending_password == SECRET

    bench.camera.http = True
    bench.camera.calls.clear()
    assert bench.link_up(2) is LinkState.ADOPTED

    assert bench.record().password == SECRET
    assert bench.record().pending_password is None
    assert bench.camera.calls == []


@pytest.mark.parametrize(
    "error",
    [LoginOutcomeUnknown("?"), ApiError("after login")],
    ids=["unknown", "api_error"],
)
def test_interrupted_enable_on_a_reset_known_camera_can_still_reach_blank(path, error):
    # HTTP may be on and the password blank, while the node holds the old one.
    camera = FakeCamera(password="", http=False)
    bench = Bench(path, camera)
    bench.known()
    camera.script["enable_services"] = [error]

    assert bench.link_up() is LinkState.FAILED

    camera.http = True  # it did apply before the reply was lost
    assert bench.link_up(2) is LinkState.ADOPTED
    assert ("http", SECRET) in camera.logins
    assert_fully_adopted(bench)


# Settle.


def test_settle_waits_45_seconds_and_does_not_use_the_attempt(blank_http):
    bench = blank_http
    assert bench.tick() is LinkState.ADOPTING
    assert bench.camera.logins == []
    assert bench.record().attempted is None

    bench.clock.advance(44)
    assert bench.tick() is LinkState.ADOPTING
    assert bench.camera.logins == []
    assert bench.record().attempted is None

    bench.clock.advance(1)
    assert bench.tick() is LinkState.ADOPTED
    assert bench.camera.logins[0] == ("http", "")


def test_settle_starts_when_a_port_first_accepts_not_at_the_lease(blank_http):
    bench = blank_http
    bench.camera.up = False
    bench.tick()
    bench.clock.advance(100)
    bench.camera.up = True

    assert bench.tick() is LinkState.ADOPTING
    assert bench.camera.logins == []


def test_settle_runs_again_each_generation(adopted):
    assert adopted.link_up() is LinkState.ADOPTED
    adopted.camera.logins.clear()

    assert adopted.tick(2) is LinkState.ADOPTING
    assert adopted.camera.logins == []
    adopted.clock.advance(45)
    assert adopted.tick(2) is LinkState.ADOPTED


def test_settle_ignores_the_wall_clock_stepping_backward(blank_http):
    bench = blank_http
    bench.tick()
    bench.clock.wall_step(-3600)
    bench.clock.advance(45)
    assert bench.tick() is LinkState.ADOPTED


def test_forward_wall_clock_step_does_not_skip_the_settle(path):
    # A Pi with no RTC syncs after a mains restore, inside this very window. Without the
    # settle a healthy camera looks factory-reset and loses a blank login.
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()

    assert bench.tick() is LinkState.ADOPTING
    bench.clock.wall_step(3600)
    assert bench.tick() is LinkState.ADOPTING
    assert bench.camera.logins == []

    bench.clock.advance(40)
    bench.camera.http = True
    bench.clock.advance(5)
    assert bench.tick() is LinkState.ADOPTED
    assert bench.camera.logins == [("http", SECRET)]


def test_settle_continues_across_a_daemon_restart(blank_http):
    bench = blank_http
    bench.tick()
    bench.clock.advance(30)
    bench.restart()
    bench.tick()
    assert bench.camera.logins == []
    bench.clock.advance(15)
    assert bench.tick() is LinkState.ADOPTED


def test_settle_starts_over_when_time_since_boot_went_backward(blank_http):
    bench = blank_http
    bench.tick()
    bench.clock.since_boot = 5.0  # a reboot that kept the generation
    assert bench.tick() is LinkState.ADOPTING
    assert bench.camera.logins == []
    bench.clock.advance(44)
    assert bench.tick() is LinkState.ADOPTING
    bench.clock.advance(1)
    assert bench.tick() is LinkState.ADOPTED


def test_step_defaults_to_the_system_monotonic_clock():
    assert inspect.signature(step).parameters["monotonic"].default is time.monotonic


# Ports that cannot be judged.


@pytest.mark.parametrize(
    ("http", "baichuan"), [(None, True), (True, None), (None, None), (False, None)]
)
def test_unknown_port_is_neither_a_signature_nor_an_attempt(path, http, baichuan):
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()
    answers = {80: http, 9000: baichuan}

    def probe(host, port):
        return answers[port]

    bench.tick(probe=probe)
    bench.clock.advance(45)
    assert bench.tick(probe=probe) is LinkState.ADOPTING
    assert bench.camera.logins == []
    assert bench.record().attempted is None
    assert bench.record().password == SECRET

    bench.camera.http = True
    assert bench.link_up() is LinkState.ADOPTED
    assert bench.camera.logins == [("http", SECRET)]


def test_settle_starts_on_an_accepting_port_even_if_the_other_is_unknown(path):
    bench = Bench(path, FakeCamera(password=SECRET, http=True))
    bench.known()
    bench.tick(probe=lambda host, port: None if port == 80 else True)
    bench.clock.advance(45)
    assert bench.tick() is LinkState.ADOPTED


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), OSError(errno.EHOSTUNREACH, "no route"), ConnectionResetError()],
    ids=["timeout", "no_route", "reset"],
)
def test_default_probe_calls_anything_but_refused_unknown(monkeypatch, error):
    def connect(address, timeout):
        raise error

    monkeypatch.setattr(adopt.socket, "create_connection", connect)
    assert adopt._port_open("10.77.0.10", 80) is None


# Attempt accounting.


def test_attempted_is_on_disk_before_any_login(blank_http):
    bench = blank_http
    seen = []
    original = bench.camera.judge

    def spying(channel, password, remain=True):
        seen.append(CameraStore(bench.path).load(MAC).attempted)
        original(channel, password, remain)

    bench.camera.judge = spying
    bench.link_up()
    assert seen[0] == f"{MAC}/1"


def test_attempted_is_on_disk_before_enable_services(factory):
    seen = []
    original = factory.camera.judge

    def spying(channel, password, remain=True):
        seen.append(CameraStore(factory.path).load(MAC).attempted)
        original(channel, password, remain)

    factory.camera.judge = spying
    factory.link_up()
    assert seen[0] == f"{MAC}/1"


@pytest.mark.parametrize(
    "error", [LoginOutcomeUnknown("?"), ApiError("no")], ids=["unknown", "api_error"]
)
def test_undecided_login_is_the_attempt_and_ends_failed(blank_http, error):
    bench = blank_http
    bench.camera.script["login"] = [error]

    assert bench.link_up() is LinkState.FAILED
    assert len(bench.camera.logins) == 1
    assert len(bench.record().failures) == 1

    bench.clock.advance(60)
    assert bench.tick() is LinkState.FAILED
    assert len(bench.camera.logins) == 1

    assert bench.link_up(2) is LinkState.ADOPTED


def test_failed_saved_password_follows_the_same_rules(path):
    bench = Bench(path, FakeCamera(password="changed-behind-our-back", http=True))
    bench.known()

    assert bench.link_up() is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", SECRET)]
    assert bench.record().password == SECRET


def test_pending_password_undecided_stops_there_and_keeps_it(blank_http):
    bench = blank_http
    seed(bench.path, CameraRecord(MAC, "", "the-pending-one", None))
    bench.camera.script["login"] = [LoginOutcomeUnknown("?")]

    assert bench.link_up() is LinkState.FAILED
    assert bench.camera.logins == [("http", "the-pending-one")]
    assert bench.record().pending_password == "the-pending-one"


def test_neither_pending_nor_blank_keeps_pending_and_needs_reset(path):
    bench = Bench(path, FakeCamera(password="somebody-elses", http=True))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))

    assert bench.link_up() is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", "the-pending-one"), ("http", "")]
    assert bench.record().pending_password == "the-pending-one"
    assert len(bench.record().failures) == 2


def test_false_rejection_of_a_live_pending_recovers_by_a_later_generation(path):
    bench = Bench(path, FakeCamera(password="the-pending-one", http=True))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    bench.camera.script["login"] = [AuthFailed("busy", 9)]

    assert bench.link_up(1) is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", "the-pending-one"), ("http", "")]
    assert not bench.record().pending_rejected

    bench.camera.logins.clear()
    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.logins[0] == ("http", "the-pending-one")
    assert_fully_adopted(bench)


def test_false_rejection_recovers_when_the_cap_held_back_the_blank(path):
    bench = Bench(path, FakeCamera(password="the-pending-one", http=True))
    now = Clock()().timestamp()
    seed(path, CameraRecord(MAC, "", "the-pending-one", None, failures=(now, now)))
    bench.camera.script["login"] = [AuthFailed("busy", 9)]

    assert bench.link_up(1) is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", "the-pending-one")]

    bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
    bench.camera.logins.clear()
    assert bench.link_up(2) is LinkState.NEEDS_RESET  # blank's turn, and it is refused
    assert bench.camera.logins == [("http", "")]

    assert bench.link_up(3) is LinkState.ADOPTED
    assert_fully_adopted(bench)


def test_foreign_camera_with_a_pending_password_stays_within_the_cap(path):
    bench = Bench(path, FakeCamera(password="somebody-elses", http=True))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    for generation in range(1, 8):
        assert bench.link_up(generation) is LinkState.NEEDS_RESET
    assert len(bench.camera.logins) == 3
    assert bench.camera.failures == 3
    assert bench.record().pending_password == "the-pending-one"


def test_rejected_pending_goes_straight_to_blank_while_blank_is_undecided(path):
    bench = Bench(path, FakeCamera(password="", http=True))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    bench.camera.script["login"] = [AuthFailed("no", 9), LoginOutcomeUnknown("?")]

    assert bench.link_up(1) is LinkState.FAILED
    assert bench.record().pending_rejected

    bench.camera.logins.clear()
    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.logins[0] == ("http", "")
    assert bench.camera.password == "the-pending-one"


def test_blank_refused_on_the_signature_also_clears_the_flag(path):
    bench = Bench(path, FakeCamera(password="the-pending-one", http=False))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None, pending_rejected=True))

    assert bench.link_up(1) is LinkState.NEEDS_RESET
    assert not bench.record().pending_rejected

    bench.camera.http = True
    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.logins[-1] != ("http", "")


def test_blank_refused_after_pending_rejected_retries_only_blank(blank_http):
    bench = blank_http
    seed(bench.path, CameraRecord(MAC, "", "the-pending-one", None))
    bench.tick()
    bench.clock.advance(45)
    bench.camera.script["login"] = [AuthFailed("rejected", 9), Unreachable("refused")]

    assert bench.tick() is LinkState.ADOPTING
    assert len(bench.record().failures) == 1
    assert bench.camera.logins == [("http", "the-pending-one")]

    bench.camera.logins.clear()
    assert bench.tick() is LinkState.ADOPTED
    assert bench.camera.logins[0] == ("http", "")


def test_new_password_rejected_after_the_change_keeps_it_on_disk(blank_http):
    bench = blank_http
    bench.tick()
    bench.clock.advance(45)
    camera = bench.camera
    original = camera.judge

    def rejects_second(channel, password, remain=True):
        if password:
            camera.failures += 1
            raise AuthFailed("rejected", 9)
        original(channel, password, remain)

    camera.judge = rejects_second
    assert bench.tick() is LinkState.NEEDS_RESET
    camera.judge = original

    record = bench.record()
    assert record.pending_password == camera.password != ""
    assert bench.link_up(2) is LinkState.ADOPTED
    assert_fully_adopted(bench)


def test_low_remain_times_stops_attempts_before_the_camera_locks(stranger):
    stranger.camera.failures = LIMIT - adopt.MIN_REMAIN_TIMES - 1

    assert stranger.link_up() is LinkState.NEEDS_RESET
    assert stranger.record().locked
    count = len(stranger.camera.logins)

    assert stranger.link_up(2) is LinkState.NEEDS_RESET
    assert len(stranger.camera.logins) == count


def test_set_password_lost_reply_is_recovered_with_the_pending_password(blank_http):
    bench = blank_http
    bench.camera.script["set_password"] = [ApiError("reply lost")]
    bench.camera.after_applied.add("set_password")

    assert bench.link_up() is LinkState.FAILED
    record = bench.record()
    assert record.pending_password == bench.camera.password != ""

    bench.camera.logins.clear()
    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.logins[0] == ("http", record.pending_password)
    assert bench.camera.failures == 0
    assert_fully_adopted(bench)


def test_set_password_never_applied_resumes_the_same_change(blank_http):
    bench = blank_http
    bench.camera.script["set_password"] = [ApiError("no")]

    assert bench.link_up() is LinkState.FAILED
    pending = bench.record().pending_password
    assert bench.camera.password == ""

    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.password == pending
    assert_fully_adopted(bench)


def test_step_failures_do_not_count_against_the_cap(adopted):
    adopted.known(applied={})
    for generation in range(1, 7):
        adopted.camera.script["disable_p2p"] = [ApiError("no")]
        assert adopted.link_up(generation) is LinkState.FAILED
    assert adopted.record().failures == ()
    assert adopted.camera.logins == [("http", SECRET)] * 6


def test_login_failing_inside_a_step_is_judged_like_any_login(adopted):
    adopted.known(applied={"disable_p2p": True})
    original = FakeClient.login
    logins = []

    def second_fails(self, user, password):
        logins.append(password)
        if len(logins) == 2:
            self.camera.logins.append(("http", password))
            raise AuthFailed("rejected", 9)
        original(self, user, password)

    FakeClient.login = second_fails
    try:
        assert adopted.link_up() is LinkState.NEEDS_RESET
    finally:
        FakeClient.login = original
    assert len(adopted.record().failures) == 1


def test_logout_failure_changes_nothing(adopted):
    adopted.camera.script["logout"] = [ApiError("no")]
    assert adopted.link_up() is LinkState.ADOPTED


# Rolling cap.


def test_cap_allows_three_failures_per_24_hours_across_restarts(stranger):
    for generation in (1, 2, 3):
        assert stranger.link_up(generation) is LinkState.NEEDS_RESET
        stranger.restart()
    assert len(stranger.camera.logins) == 3

    for generation in (4, 5, 6):
        assert stranger.link_up(generation) is LinkState.NEEDS_RESET
        stranger.restart()
    assert len(stranger.camera.logins) == 3
    assert stranger.camera.failures == 3


def test_cap_rolls_after_24_hours(stranger):
    for generation in (1, 2, 3):
        stranger.link_up(generation)
    assert len(stranger.camera.logins) == 3

    stranger.clock.advance(24 * 3600 - 135)  # 45 s of settle were spent on each
    assert stranger.link_up(4) is LinkState.NEEDS_RESET
    assert len(stranger.camera.logins) == 4  # the oldest just expired, only that one


def test_cap_counts_a_login_cut_off_by_a_crash(stranger):
    def dies(*args, **kwargs):
        raise PowerCut

    original = stranger.camera.judge
    stranger.camera.judge = dies
    for generation in (1, 2, 3):
        with pytest.raises(PowerCut):
            stranger.link_up(generation)
        stranger.restart()
    stranger.camera.judge = original

    assert stranger.link_up(4) is LinkState.NEEDS_RESET
    assert stranger.camera.logins == []


def test_cap_treats_failures_stamped_in_the_future_as_recent(stranger):
    for generation in (1, 2, 3):
        stranger.link_up(generation)
    stranger.clock.wall_step(-48 * 3600)  # the node booted with an old clock

    assert stranger.link_up(4) is LinkState.NEEDS_RESET
    assert len(stranger.camera.logins) == 3


def test_cap_is_per_mac(stranger, path):
    for generation in (1, 2, 3):
        stranger.link_up(generation)

    other = Bench(path, FakeCamera(password="", http=True, mac=OTHER_MAC))
    assert other.link_up(4) is LinkState.ADOPTED


def test_factory_signature_is_exempt_from_the_cap(stranger):
    camera = stranger.camera
    for generation in (1, 2, 3):
        assert stranger.link_up(generation) is LinkState.NEEDS_RESET

    camera.factory_reset()
    assert stranger.link_up(4) is LinkState.ADOPTED
    assert stranger.enable_passwords == [""]
    assert stranger.record().failures == ()
    assert_fully_adopted(stranger)


def test_cap_reached_also_blocks_the_blank_after_a_rejected_pending(path):
    bench = Bench(path, FakeCamera(password="somebody-elses", http=True))
    now = Clock()().timestamp()
    seed(path, CameraRecord(MAC, "", "the-pending-one", None, failures=(now, now)))

    assert bench.link_up() is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", "the-pending-one")]


# The factory-signature limit.


@pytest.fixture
def closed_http_stranger(path):
    """Port 80 closed and a password this node does not know: the signature, falsely."""
    return Bench(path, FakeCamera(password="somebody-elses", http=False))


def test_false_signature_gets_one_baichuan_attempt_in_24_hours(closed_http_stranger):
    bench = closed_http_stranger
    camera = bench.camera
    for generation in range(1, 41):
        assert bench.link_up(generation) is LinkState.NEEDS_RESET
        bench.restart()
        bench.clock.advance(60)
        assert bench.tick() is LinkState.NEEDS_RESET
        bench.restart()
    assert bench.enable_passwords == [""]
    assert camera.logins == [("baichuan", "")]
    assert camera.failures == 1


def test_false_signature_on_a_known_camera_keeps_its_password_and_waits(path):
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()
    for generation in range(1, 11):
        assert bench.link_up(generation) is LinkState.NEEDS_RESET
        bench.restart()
    assert bench.camera.logins == [("baichuan", "")]
    assert bench.record().pending_password == SECRET


def test_signature_limit_rolls_after_24_hours(closed_http_stranger):
    bench = closed_http_stranger
    bench.link_up(1)

    bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS - 46)
    assert bench.link_up(2) is LinkState.NEEDS_RESET  # 1 s short after this settle
    assert len(bench.enable_passwords) == 1

    bench.clock.advance(1)
    assert bench.tick(2) is LinkState.NEEDS_RESET  # generation 2 is not yet attempted
    assert len(bench.enable_passwords) == 2


def test_signature_limit_treats_a_stamp_from_the_future_as_recent(closed_http_stranger):
    bench = closed_http_stranger
    bench.link_up(1)
    bench.clock.wall_step(-48 * 3600)

    assert bench.link_up(2) is LinkState.NEEDS_RESET
    assert len(bench.enable_passwords) == 1


def test_real_reset_after_the_signature_window_adopts(closed_http_stranger):
    bench = closed_http_stranger
    assert bench.link_up(1) is LinkState.NEEDS_RESET

    bench.camera.factory_reset()
    bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS)

    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.record().signature_failures == ()
    assert_fully_adopted(bench)


def test_real_reset_inside_the_signature_window_waits_for_it(closed_http_stranger):
    bench = closed_http_stranger
    assert bench.link_up(1) is LinkState.NEEDS_RESET

    bench.camera.factory_reset()
    assert bench.link_up(2) is LinkState.NEEDS_RESET
    assert len(bench.enable_passwords) == 1

    bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
    assert bench.link_up(3) is LinkState.ADOPTED


def test_real_reset_after_a_failed_http_guess_adopts_at_once(stranger):
    assert stranger.link_up(1) is LinkState.NEEDS_RESET
    assert stranger.camera.logins == [("http", "")]

    stranger.camera.factory_reset()

    assert stranger.link_up(2) is LinkState.ADOPTED
    assert stranger.enable_passwords == [""]
    assert_fully_adopted(stranger)


def test_successful_blank_logins_are_never_limited(factory):
    for generation in range(1, 6):
        assert factory.link_up(generation) is LinkState.ADOPTED
        assert factory.record().signature_failures == ()
        assert_fully_adopted(factory)
        factory.camera.factory_reset()
    assert len(factory.enable_passwords) == 5


def test_signature_failure_does_not_spend_the_http_cap(path):
    bench = Bench(path, FakeCamera(password=SECRET, http=False))
    bench.known()
    assert bench.link_up(1) is LinkState.NEEDS_RESET
    assert bench.record().failures == ()

    bench.camera.http = True
    assert bench.link_up(2) is LinkState.ADOPTED
    assert bench.camera.logins[-1] == ("http", SECRET)


def test_http_cap_does_not_spend_the_signature_limit(stranger):
    for generation in (1, 2, 3):
        stranger.link_up(generation)
    assert stranger.record().signature_failures == ()

    stranger.camera.http = False
    assert stranger.link_up(4) is LinkState.NEEDS_RESET
    assert stranger.enable_passwords == [""]


def test_lockout_on_a_signature_is_still_limited(closed_http_stranger):
    bench = closed_http_stranger
    bench.camera.failures = LIMIT
    for generation in range(1, 6):
        assert bench.link_up(generation) is LinkState.NEEDS_RESET
    assert len(bench.enable_passwords) == 1
    assert bench.record().locked


def test_enable_failing_after_its_login_does_not_spend_the_limit(factory):
    # enable_services raises ApiError only once logged in, which proves blank is live.
    factory.camera.script["enable_services"] = [ApiError("port write failed")]
    assert factory.link_up(1) is LinkState.FAILED
    assert factory.record().signature_failures == ()

    assert factory.link_up(2) is LinkState.ADOPTED
    assert_fully_adopted(factory)


def _http_disagrees_with_baichuan(bench):
    """Baichuan takes blank, HTTP refuses it, and port 80 closes again on each boot."""

    async def blank_works(host, password):
        bench.enable_passwords.append(password)
        bench.camera.http = True

    return blank_works


def test_baichuan_success_does_not_clear_the_caps_until_http_agrees(path):
    bench = Bench(path, FakeCamera(password="somebody-elses", http=False))
    now = Clock()().timestamp()
    seed(path, CameraRecord(MAC, "", None, None, failures=(now, now, now), locked=True))
    enable = _http_disagrees_with_baichuan(bench)

    assert bench.link_up(1, enable=enable) is LinkState.NEEDS_RESET

    record = bench.record()
    assert record.locked
    assert len(record.failures) == 4


def test_firmware_disagreeing_with_itself_cannot_run_past_the_limits(path):
    bench = Bench(path, FakeCamera(password="somebody-elses", http=False))
    enable = _http_disagrees_with_baichuan(bench)

    for generation in range(1, 31):
        bench.camera.http = False
        assert bench.link_up(generation, enable=enable) is LinkState.NEEDS_RESET
    assert bench.camera.logins == [("http", "")]
    assert bench.enable_passwords == [""]


def test_baichuan_success_does_not_spend_the_signature_limit(factory):
    factory.tick()
    factory.clock.advance(45)
    factory.camera.script["login"] = [Unreachable("refused")]
    assert factory.tick() is LinkState.ADOPTING
    assert factory.record().signature_failures == ()


def test_http_agreeing_clears_the_caps(path):
    bench = Bench(path, FakeCamera(password="", http=False))
    now = Clock()().timestamp()
    seed(path, CameraRecord(MAC, "", None, None, failures=(now, now, now), locked=True))

    assert bench.link_up() is LinkState.ADOPTED

    assert bench.record().failures == ()
    assert not bench.record().locked


def test_signature_limit_counts_an_attempt_cut_off_by_a_crash(closed_http_stranger):
    bench = closed_http_stranger
    original = bench.camera.judge

    def dies(*args, **kwargs):
        raise PowerCut

    bench.camera.judge = dies
    with pytest.raises(PowerCut):
        bench.link_up(1)
    bench.camera.judge = original
    bench.restart()

    assert bench.link_up(2) is LinkState.NEEDS_RESET
    assert bench.enable_passwords == [""]


# LockedOut.


def test_lockout_from_a_saved_password_stops_until_a_factory_signature(adopted):
    camera = adopted.camera
    camera.failures = LIMIT
    assert adopted.link_up() is LinkState.NEEDS_RESET
    assert adopted.record().locked
    assert len(camera.logins) == 1

    assert adopted.link_up(2) is LinkState.NEEDS_RESET
    assert len(camera.logins) == 1

    camera.factory_reset()
    assert adopted.link_up(3) is LinkState.ADOPTED
    assert not adopted.record().locked
    assert_fully_adopted(adopted)


def test_factory_signature_in_the_locked_generation_changes_nothing(adopted):
    adopted.camera.failures = LIMIT
    assert adopted.link_up() is LinkState.NEEDS_RESET
    count = len(adopted.camera.logins)

    adopted.camera.http = False
    adopted.clock.advance(60)
    assert adopted.tick() is LinkState.NEEDS_RESET
    assert len(adopted.camera.logins) == count
    assert adopted.enable_passwords == []


def test_lockout_on_enable_services_is_recorded(factory):
    factory.camera.script["enable_services"] = [LockedOut("locked", None, 0)]
    assert factory.link_up() is LinkState.NEEDS_RESET
    assert factory.record().locked


# Timeout and cancellation around enable_services.


def test_enable_services_default_timeout_is_120_seconds():
    assert inspect.signature(step).parameters["enable_timeout"].default == 120


def test_enable_services_timeout_counts_as_outcome_unknown(factory):
    async def hangs(host, password):
        await asyncio.sleep(3600)

    factory.tick()
    factory.clock.advance(45)
    assert factory.tick(enable=hangs, enable_timeout=0.01) is LinkState.FAILED
    assert factory.record().attempted == f"{MAC}/1"
    assert len(factory.record().signature_failures) == 1

    assert factory.tick() is LinkState.FAILED  # no second attempt in this generation
    assert factory.enable_passwords == []
    assert factory.link_up(2) is LinkState.NEEDS_RESET  # the day's signature attempt is spent
    assert factory.enable_passwords == []
    factory.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
    assert factory.link_up(3) is LinkState.ADOPTED


def test_enable_services_ignoring_cancellation_cannot_hang_the_daemon(factory, monkeypatch):
    monkeypatch.setattr(adopt, "_CANCEL_GRACE", 0.01)

    async def stubborn(host, password):
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

    factory.tick()
    factory.clock.advance(45)
    assert factory.tick(enable=stubborn, enable_timeout=0.01) is LinkState.FAILED


def test_enable_services_blocking_during_its_cleanup_is_cancelled_again(factory, monkeypatch):
    # enable_services logs out in a finally, so the first cancel can land in a new await.
    monkeypatch.setattr(adopt, "_CANCEL_GRACE", 0.01)
    reached = []

    async def slow_goodbye(host, password):
        try:
            await asyncio.sleep(3600)
        finally:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                reached.append("second cancel")
                raise

    factory.tick()
    factory.clock.advance(45)
    assert factory.tick(enable=slow_goodbye, enable_timeout=0.01) is LinkState.FAILED
    assert reached == ["second cancel"]


def test_enable_services_being_cancelled_does_not_crash_the_daemon(factory):
    async def cancelled(host, password):
        raise asyncio.CancelledError

    factory.tick()
    factory.clock.advance(45)
    assert factory.tick(enable=cancelled) is LinkState.FAILED


def test_enable_services_raising_something_unexpected_counts_as_unknown(factory):
    async def surprised(host, password):
        raise RuntimeError(f"bad frame for {password!r}")

    factory.tick()
    factory.clock.advance(45)
    assert factory.tick(enable=surprised) is LinkState.FAILED
    assert len(factory.record().signature_failures) == 1


def test_enable_services_refused_does_not_use_the_attempt(factory):
    factory.tick()
    factory.clock.advance(45)
    factory.camera.script["enable_services"] = [Unreachable("closed")]

    assert factory.tick() is LinkState.ADOPTING
    record = factory.record()
    assert record.attempted != f"{MAC}/1"
    assert record.failures == ()
    assert record.signature_failures == ()

    assert factory.tick() is LinkState.ADOPTED


# Resume and provisioning.


def test_resume_repeats_only_the_unfinished_steps(adopted):
    adopted.known(applied={"disable_p2p": True, "enable_rtsp": True, "set_clock": True})

    assert adopted.link_up() is LinkState.ADOPTED
    assert adopted.camera.calls == ["disable_onvif", "set_name", "set_streams", "set_rotation"]
    assert set(adopted.record().applied) == set(STEPS)


def test_steps_run_in_the_required_order(factory):
    assert factory.link_up() is LinkState.ADOPTED
    assert factory.camera.calls == ["set_password", *STEPS]
    assert STEPS == (
        "disable_p2p",
        "enable_rtsp",
        "disable_onvif",
        "set_clock",
        "set_name",
        "set_streams",
        "set_rotation",
    )


def test_set_name_and_rotation_receive_what_step_was_given(factory):
    factory.flipped = True
    assert factory.link_up() is LinkState.ADOPTED
    assert factory.camera.settings["name"] == NODE
    assert factory.camera.settings["flipped"] is True
    assert factory.camera.settings["clock"] == factory.clock()


def test_unprovisioned_node_runs_password_and_services_and_leaves_the_rest(factory):
    factory.node_id = None
    factory.flipped = None

    assert factory.link_up() is LinkState.ADOPTED

    record = factory.record()
    assert record.password == factory.camera.password != ""
    assert "set_name" not in record.applied
    assert "set_rotation" not in record.applied
    assert factory.camera.calls == [
        "set_password",
        "disable_p2p",
        "enable_rtsp",
        "disable_onvif",
        "set_clock",
        "set_streams",
    ]


def test_unprovisioned_steps_run_next_generation_once_provisioned(factory):
    factory.node_id = None
    factory.flipped = None
    factory.link_up()
    factory.camera.calls.clear()

    factory.node_id = NODE
    factory.flipped = False
    assert factory.link_up(2) is LinkState.ADOPTED
    assert factory.camera.calls == ["set_name", "set_rotation"]


def test_provisioning_in_the_same_generation_finishes_with_one_login(factory):
    factory.node_id = None
    factory.flipped = None
    factory.link_up()
    factory.camera.calls.clear()
    factory.camera.logins.clear()

    assert factory.tick() is LinkState.ADOPTED  # still nothing to do
    assert factory.camera.logins == []

    factory.node_id = NODE
    factory.flipped = False
    assert factory.tick() is LinkState.ADOPTED
    assert factory.tick() is LinkState.ADOPTED
    assert factory.camera.calls == ["set_name", "set_rotation"]
    assert len(factory.camera.logins) == 1


def test_provisioning_refused_in_the_same_generation_is_pending_settings(factory):
    factory.node_id = None
    factory.flipped = None
    factory.link_up()
    factory.node_id = NODE
    factory.camera.script["login"] = [Unreachable("refused")]

    assert factory.tick() is LinkState.ADOPTED_PENDING_SETTINGS
    assert "set_name" not in factory.record().applied
    assert factory.tick() is LinkState.ADOPTED
    assert factory.record().applied["set_name"] == NODE


def test_changed_setting_while_capped_is_pending_settings_with_no_login(adopted):
    assert adopted.link_up() is LinkState.ADOPTED
    now = adopted.clock().timestamp()
    seed(adopted.path, replace(adopted.record(), failures=(now, now, now)))
    adopted.camera.logins.clear()

    adopted.node_id = "cam-new"
    for _ in range(3):
        assert adopted.tick() is LinkState.ADOPTED_PENDING_SETTINGS
    assert adopted.camera.logins == []
    assert adopted.record().applied["set_name"] == NODE

    adopted.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
    assert adopted.tick() is LinkState.ADOPTED
    assert adopted.camera.settings["name"] == "cam-new"


def test_changed_rotation_is_pending_settings_too(adopted):
    assert adopted.link_up() is LinkState.ADOPTED
    adopted.flipped = True
    adopted.camera.script["login"] = [Unreachable("refused")]
    assert adopted.tick() is LinkState.ADOPTED_PENDING_SETTINGS
    assert adopted.tick() is LinkState.ADOPTED
    assert adopted.camera.settings["flipped"] is True


def test_unprovisioned_node_is_plainly_adopted(factory):
    factory.node_id = None
    factory.flipped = None
    assert factory.link_up() is LinkState.ADOPTED
    assert factory.tick() is LinkState.ADOPTED


def test_provisioning_failure_in_the_same_generation_does_not_retry(factory):
    factory.node_id = None
    factory.link_up()
    factory.node_id = NODE
    factory.camera.script["set_name"] = [ApiError("no")]
    factory.camera.logins.clear()

    assert factory.tick() is LinkState.FAILED
    assert factory.tick() is LinkState.FAILED
    assert len(factory.camera.logins) == 1


def test_changed_node_id_or_rotation_is_applied_again(adopted):
    adopted.node_id = "cam-new"
    adopted.flipped = True

    assert adopted.link_up() is LinkState.ADOPTED
    assert adopted.camera.calls == ["set_name", "set_rotation"]
    assert adopted.camera.settings["name"] == "cam-new"


# Passwords and swaps.


def test_password_comes_from_token_urlsafe_15_trimmed_to_20(factory, monkeypatch):
    asked = []

    def token(n):
        asked.append(n)
        return "A" * 25

    monkeypatch.setattr(adopt.secrets, "token_urlsafe", token)
    factory.link_up()
    assert asked == [15]
    assert factory.camera.password == "A" * 20


def test_camera_swapped_back_gets_its_own_password(adopted, path):
    adopted.link_up()
    other = Bench(path, FakeCamera(password="", http=False, mac=OTHER_MAC))
    other.link_up(2)

    assert adopted.link_up(3) is LinkState.ADOPTED
    assert adopted.camera.logins[-1] == ("http", SECRET)
    assert CameraStore(path).load().mac == MAC
    assert CameraStore(path).load().password == SECRET


def test_mac_case_does_not_make_a_new_camera(adopted):
    lease = Lease(mac=MAC.upper(), ip=IP)
    adopted.tick(lease=lease)
    adopted.clock.advance(45)
    assert adopted.tick(lease=lease) is LinkState.ADOPTED
    assert adopted.camera.logins == [("http", SECRET)]


def test_no_lease_is_no_camera_and_touches_nothing(factory):
    assert step(None, 1, factory.store, None, factory.clock) is LinkState.NO_CAMERA
    assert not factory.path.exists()


# Store.


def test_store_is_empty_until_saved(path):
    assert CameraStore(path).load() is None


def test_store_round_trips_every_field(path):
    record = CameraRecord(
        MAC,
        "",
        "pending",
        f"{MAC}/3",
        phase="failed",
        applied={"set_name": NODE, "set_rotation": False},
        failures=(1.5, 2.5),
        signature_failures=(3.5,),
        pending_rejected=True,
        locked=True,
        settle_key=f"{MAC}/3",
        settle_since=9.0,
    )
    store = CameraStore(path)
    with store.lock():
        written = store.save(record)
    assert written == replace(record, revision=1)
    assert CameraStore(path).load() == written


def test_every_phase_the_machine_writes_is_a_valid_one():
    written = {
        LinkState.ADOPTED,
        LinkState.NEEDS_RESET,
        LinkState.FAILED,
        adopt._PHASE_HTTP_FOLLOW_UP,
        adopt._PHASE_TRY_BLANK,
    }
    assert {str(phase) for phase in written} == PHASES


def _raw(path, **changes):
    """A file whose record differs from a good one only by ``changes``."""
    fields = asdict(CameraRecord(MAC, SECRET, None, None))
    fields.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"current": MAC, "cameras": {MAC: fields}}))


WRONG_TYPES = [
    ("mac", 7),
    ("mac", OTHER_MAC),
    ("password", None),
    ("password", 123),
    ("pending_password", 123),
    ("attempted", 5),
    ("phase", "bogus"),
    ("phase", 1),
    ("applied", []),
    ("applied", {"set_name": None}),
    ("applied", {"set_name": 3}),
    ("failures", ["x"]),
    ("failures", [True]),
    ("failures", "abc"),
    ("failures", None),
    ("signature_failures", ["x"]),
    ("signature_failures", 1),
    ("pending_rejected", "yes"),
    ("pending_rejected", 0),
    ("locked", 1),
    ("locked", None),
    ("settle_key", 1),
    ("settle_since", "x"),
    ("settle_since", True),
    ("revision", "1"),
    ("revision", True),
    ("revision", -1),
    ("surprise", 1),
]


@pytest.mark.parametrize(("name", "value"), WRONG_TYPES, ids=lambda v: str(v)[:12])
def test_wrong_field_type_is_a_corrupt_store_and_keeps_the_file(path, name, value):
    _raw(path, **{name: value})
    before = path.read_bytes()

    store = CameraStore(path)
    with pytest.raises(StoreError):
        store.load()
    with store.lock(), pytest.raises(StoreError):
        store.save(CameraRecord(OTHER_MAC, "x", None, None))
    assert path.read_bytes() == before


def test_missing_field_is_corrupt(path):
    fields = asdict(CameraRecord(MAC, SECRET, None, None))
    del fields["password"]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"current": MAC, "cameras": {MAC: fields}}))
    with pytest.raises(StoreError):
        CameraStore(path).load()


def test_both_passwords_at_once_is_corrupt(path):
    _raw(path, pending_password="other")
    with pytest.raises(StoreError):
        CameraStore(path).load()


def test_current_naming_no_camera_is_corrupt(path):
    fields = asdict(CameraRecord(MAC, SECRET, None, None))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"current": OTHER_MAC, "cameras": {MAC: fields}}))
    with pytest.raises(StoreError):
        CameraStore(path).load()


def test_wrong_type_stops_step_before_any_login(path):
    bench = Bench(path, FakeCamera(password="", http=True))
    _raw(path, password=None)
    with pytest.raises(StoreError):
        bench.link_up()
    assert bench.camera.logins == []


def test_saving_an_invalid_record_is_refused_and_writes_nothing(path):
    store = CameraStore(path)
    with store.lock(), pytest.raises(StoreError):
        store.save(CameraRecord(MAC, None, None, None))
    assert not path.exists()


def test_store_file_is_0600_and_replaces_a_wider_one(path):
    seed(path, CameraRecord(MAC, "pw", None, None))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    os.chmod(path, 0o644)
    seed(path, CameraRecord(MAC, "pw", None, None))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_new_state_directory_is_0700(path):
    seed(path, CameraRecord(MAC, "pw", None, None))
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_temporary_file_is_0600_from_creation_under_any_umask(path, monkeypatch):
    seen = []
    real_open = os.open

    def spying(file, flags, mode=0o777, **kwargs):
        fd = real_open(file, flags, mode, **kwargs)
        if str(file).endswith(".tmp"):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    monkeypatch.setattr(os, "open", spying)
    old = os.umask(0)
    try:
        seed(path, CameraRecord(MAC, "pw", None, None))
    finally:
        os.umask(old)
    assert seen == [0o600]


def test_leftover_wide_temporary_file_is_narrowed_before_the_password_lands(path, monkeypatch):
    seed(path, CameraRecord(MAC, "pw", None, None))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("left over")
    os.chmod(temporary, 0o644)
    modes = []
    real_fsync = os.fsync

    def spying(fd):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spying)
    seed(path, CameraRecord(MAC, "pw2", None, None))
    assert modes[0] == 0o600


def test_symlink_at_the_temporary_path_is_not_followed(path, tmp_path):
    seed(path, CameraRecord(MAC, "pw", None, None))
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    path.with_name(path.name + ".tmp").symlink_to(target)

    with pytest.raises(StoreError):
        seed(path, CameraRecord(MAC, "pw2", None, None))
    assert target.read_text() == "untouched"
    assert CameraStore(path).load().password == "pw"


def test_store_syncs_file_then_renames_then_syncs_directory(path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        order.append(f"fsync {kind}")
        real_fsync(fd)

    def replace_file(src, dst):
        order.append("rename")
        real_replace(src, dst)

    store = CameraStore(path)
    with store.lock():
        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace_file)
        store.save(CameraRecord(MAC, "pw", None, None))
    assert order == ["fsync file", "rename", "fsync dir"]


def test_every_newly_created_directory_has_its_parent_synced(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    deep = tmp_path / "one" / "two" / "camera.json"
    with CameraStore(deep).lock():
        pass
    assert synced == [str(tmp_path), str(tmp_path / "one")]

    synced.clear()
    with CameraStore(deep).lock():
        pass
    assert synced == []


def test_store_failure_mid_save_leaves_the_old_record(path, monkeypatch):
    seed(path, CameraRecord(MAC, "old", None, None))

    def fail(src, dst):
        raise OSError(errno.ENOSPC, "disk")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StoreError) as caught:
        seed(path, CameraRecord(MAC, "new", None, None))
    monkeypatch.undo()
    assert caught.value.__context__ is None

    assert CameraStore(path).load().password == "old"
    seed(path, CameraRecord(MAC, "newer", None, None))  # the leftover temp file is no obstacle
    assert CameraStore(path).load().password == "newer"


def test_store_keeps_each_mac_and_load_returns_the_latest(path):
    seed(path, CameraRecord(MAC, "a", None, None))
    seed(path, CameraRecord(OTHER_MAC, "b", None, None))
    store = CameraStore(path)
    assert store.load().mac == OTHER_MAC
    assert store.load(MAC).password == "a"
    assert store.load("ff:ff:ff:ff:ff:ff") is None


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"current": "x", "cameras": {"x": {}}}', "[" * 100000, "", "\xff"],
    ids=["bad_json", "list", "empty_record", "deep", "empty", "bad_utf8"],
)
def test_corrupt_store_raises_rather_than_forgetting_the_accounting(path, content):
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("latin-1"))
    with pytest.raises(StoreError) as caught:
        CameraStore(path).load()
    assert content[:50] not in str(caught.value) or not content
    # A JSONDecodeError's .doc holds the whole file, passwords included.
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_unreadable_store_raises_store_error(path):
    path.mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(StoreError) as caught:
        CameraStore(path).load()
    assert caught.value.__context__ is None


def test_lock_that_cannot_be_taken_raises_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StoreError) as caught, CameraStore(blocker / "camera.json").lock():
        pass
    assert caught.value.__context__ is None


def test_body_errors_inside_the_lock_are_not_converted(path):
    store = CameraStore(path)
    with pytest.raises(OSError, match="from the body"), store.lock():
        raise OSError("from the body")
    with store.lock():  # and it was released
        pass


def test_corrupt_store_stops_step_before_any_login(blank_http):
    bench = blank_http
    bench.path.parent.mkdir(parents=True)
    bench.path.write_text("{not json")
    with pytest.raises(StoreError):
        bench.tick()
    assert bench.camera.logins == []


# One writer at a time.


def _is_locked(path):
    """Whether another process could take the store's lock right now."""
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _clear_limits(path):
    """What ``camera-link clear-limits`` has to do."""
    store = CameraStore(path)
    with store.lock():
        store.save(replace(store.load(MAC), failures=(), signature_failures=(), locked=False))


def test_lock_file_is_a_0600_sibling_and_excludes_others(path):
    store = CameraStore(path)
    with store.lock():
        lock_file = path.with_name(path.name + ".lock")
        assert stat.S_IMODE(os.stat(lock_file).st_mode) == 0o600
        assert _is_locked(path)
    assert not _is_locked(path)


def test_lock_can_be_nested_on_one_store(path):
    store = CameraStore(path)
    with store.lock():
        with store.lock():
            store.save(CameraRecord(MAC, "pw", None, None))
        assert _is_locked(path)  # the inner exit did not release it
        store.save(replace(store.load(), password="pw2"))
    assert not _is_locked(path)


def test_save_outside_the_lock_is_refused(path):
    with pytest.raises(StoreError):
        CameraStore(path).save(CameraRecord(MAC, "pw", None, None))
    assert not path.exists()


def test_step_holds_the_lock_from_first_read_to_logout(blank_http):
    bench = blank_http
    held = []
    real_load = bench.store.load
    real_logout = FakeClient.logout

    def load(mac=None):
        held.append(_is_locked(bench.path))
        return real_load(mac)

    def logout(self):
        held.append(_is_locked(bench.path))
        real_logout(self)

    bench.store.load = load
    FakeClient.logout = logout
    try:
        bench.link_up()
    finally:
        FakeClient.logout = real_logout
    assert held and all(held)
    assert not _is_locked(bench.path)


def test_step_inside_a_callers_lock_does_not_deadlock(blank_http):
    with blank_http.store.lock():
        blank_http.tick()
        assert _is_locked(blank_http.path)


def test_lock_is_released_when_power_is_cut_in_the_process(tmp_path):
    bench = Bench(tmp_path / "camera.json", FakeCamera(password="", http=True))
    bench.camera.power.crash_at = 3
    with pytest.raises(PowerCut):
        bench.link_up()
    assert not _is_locked(bench.path)


def test_writer_waits_for_a_step_and_cannot_erase_the_pending_password(blank_http):
    # The review's interleaving, with the second process taken as a thread on its own
    # open file: it reaches for the record while the password change is on the wire.
    bench = blank_http
    original = FakeClient.set_password
    writers = []

    def spying(self, new):
        writer = threading.Thread(target=_clear_limits, args=(bench.path,))
        writer.start()
        writer.join(0.2)
        assert writer.is_alive()  # shut out for as long as the step runs
        writers.append(writer)
        original(self, new)

    FakeClient.set_password = spying
    try:
        bench.tick()
        bench.clock.advance(45)
        bench.camera.script["disable_p2p"] = [ApiError("stop here")]
        assert bench.tick() is LinkState.FAILED
    finally:
        FakeClient.set_password = original
    writers[0].join(5)
    assert not writers[0].is_alive()

    assert bench.record().password == bench.camera.password != ""


def test_stale_copy_saved_later_cannot_erase_a_pending_password(blank_http):
    # A writer that read before the step, however it holds the lock when it writes.
    bench = blank_http
    bench.tick()
    stale = CameraStore(bench.path).load(MAC)
    bench.clock.advance(45)
    bench.camera.script["set_password"] = [ApiError("reply lost")]
    bench.camera.after_applied.add("set_password")
    assert bench.tick() is LinkState.FAILED
    assert bench.record().pending_password == bench.camera.password != ""

    writer = CameraStore(bench.path)
    with writer.lock(), pytest.raises(StoreError):
        writer.save(replace(stale, failures=()))

    assert bench.record().pending_password == bench.camera.password


def test_new_record_cannot_overwrite_one_that_already_exists(path):
    seed(path, CameraRecord(MAC, "pw", None, None))
    store = CameraStore(path)
    with store.lock(), pytest.raises(StoreError):
        store.save(CameraRecord(MAC, "", None, None))
    assert store.load().password == "pw"


# Saved steps, promoted passwords and relogins.


@pytest.mark.parametrize("recovering", [False, True], ids=["change", "recovery"])
def test_password_is_promoted_on_disk_before_the_first_setting(path, recovering):
    bench = Bench(path, FakeCamera(password="the-pending-one" if recovering else "", http=False))
    if recovering:
        bench.camera.http = True
        seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    seen = []
    original = FakeClient.disable_p2p

    def spying(self):
        seen.append(CameraStore(path).load(MAC))
        original(self)

    FakeClient.disable_p2p = spying
    try:
        bench.link_up()
    finally:
        FakeClient.disable_p2p = original
    assert seen[0].password == bench.camera.password != ""
    assert seen[0].pending_password is None


@pytest.mark.parametrize(
    ("error", "state", "stamps"),
    [
        (AuthFailed("no", 9), LinkState.NEEDS_RESET, 1),
        (LoginOutcomeUnknown("?"), LinkState.FAILED, 1),
        (ApiError("no"), LinkState.FAILED, 0),
        (Unreachable("refused"), LinkState.FAILED, 0),
    ],
    ids=["rejected", "unknown", "api_error", "refused"],
)
def test_relogin_inside_a_step_is_stamped_only_if_it_may_be_a_failed_login(
    adopted, error, state, stamps
):
    adopted.known(applied={"disable_p2p": True})
    real_login = FakeClient.login
    calls = []

    def second_fails(self, user, password):
        calls.append(password)
        if len(calls) == 2:
            raise error
        real_login(self, user, password)

    FakeClient.login = second_fails
    try:
        assert adopted.link_up() is state
    finally:
        FakeClient.login = real_login
    assert len(adopted.record().failures) == stamps


# Secrets.


def test_record_repr_and_str_never_hold_a_password():
    record = CameraRecord(MAC, "the-real-secret", "the-pending-secret", None)
    for text in (repr(record), str(record), f"{record}"):
        assert "the-real-secret" not in text
        assert "the-pending-secret" not in text
        assert MAC in text


def test_no_log_line_holds_a_password(path, caplog):
    caplog.set_level(logging.DEBUG)
    bench = Bench(path, FakeCamera(password="", http=False))
    bench.link_up()
    password = bench.record().password
    bench.camera.factory_reset()
    bench.camera.password = "somebody-elses"
    bench.camera.http = True
    bench.link_up(2)
    bench.camera.password = ""
    bench.camera.script["disable_p2p"] = [ApiError("no")]
    bench.known(password)
    bench.link_up(3)

    assert caplog.records
    for record in caplog.records:
        assert password not in record.getMessage()
        assert "somebody-elses" not in record.getMessage()


def test_unexpected_enable_error_does_not_log_its_message(factory, caplog):
    caplog.set_level(logging.DEBUG)

    async def surprised(host, password):
        raise RuntimeError("frame held PASSWORD-IN-MESSAGE")

    factory.tick()
    factory.clock.advance(45)
    factory.tick(enable=surprised)
    assert "PASSWORD-IN-MESSAGE" not in caplog.text


# The fakes must stay true to what they stand in for.


@pytest.mark.parametrize(
    "name",
    [
        "login",
        "logout",
        "set_password",
        "disable_p2p",
        "enable_rtsp",
        "disable_onvif",
        "set_clock",
        "set_name",
        "set_streams",
        "set_rotation",
    ],
)
def test_fake_client_matches_the_real_signature(name):
    real = list(inspect.signature(getattr(ReolinkClient, name)).parameters)
    fake = list(inspect.signature(getattr(FakeClient, name)).parameters)
    assert fake == real


def test_default_enable_is_the_real_enable_services():
    assert inspect.signature(step).parameters["enable"].default is enable_services
    assert list(inspect.signature(enable_services).parameters)[:2] == ["host", "password"]


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_adoption_order_on_the_wire_with_the_real_client(path):
    commands = []
    ports = {"rtspEnable": 0, "onvifEnable": 1}

    def opener(request, timeout=None):
        body = json.loads(request.data)[0]
        command = body["cmd"]
        commands.append(command)
        value = {}
        if command == "Login":
            value = {"Token": {"name": "tok"}}
        elif command == "GetNetPort":
            value = {"NetPort": dict(ports)}
        elif command == "SetNetPort":
            ports.update(body["param"]["NetPort"])
        elif command == "GetOsd":
            value = {"Osd": {"osdChannel": {"name": "x"}}}
        elif command == "GetIsp":
            value = {"Isp": {"rotation": 0}}
        return _Reply(json.dumps([{"cmd": command, "code": 0, "value": value}]).encode())

    camera = FakeCamera(password="", http=True)
    bench = Bench(path, camera)
    client_factory = lambda host: ReolinkClient(host, opener=opener)

    for _ in range(2):
        state = step(
            LEASE,
            1,
            bench.store,
            client_factory,
            bench.clock,
            node_id=NODE,
            flipped=True,
            probe=bench.probe,
            monotonic=bench.clock.monotonic,
        )
        bench.clock.advance(45)

    assert state is LinkState.ADOPTED
    assert commands == [
        "Login",
        "ModifyUser",
        "Login",
        "SetP2p",
        "SetUpnp",
        "GetNetPort",
        "SetNetPort",
        "Login",
        "GetNetPort",
        "SetNetPort",
        "Login",
        "SetNtp",
        "SetTime",
        "GetOsd",
        "SetDevName",
        "SetOsd",
        "SetEnc",
        "GetIsp",
        "SetIsp",
        "Logout",
    ]


def test_default_probe_reports_accepting_and_refused_ports():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert adopt._port_open("127.0.0.1", port) is True
    assert adopt._port_open("127.0.0.1", port) is False


# The crash matrix.
#
# A power cut lands before and after every camera operation and every store write of an
# adoption, as recorded from a clean run, so a new step or save joins the matrix by itself.


def _reset_known(path, power):
    camera = FakeCamera(password="", http=False, power=power)
    bench = Bench(path, camera)
    bench.known()
    return bench


def _pending_but_camera_blank(path, power):
    bench = Bench(path, FakeCamera(password="", http=True, power=power))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    return bench


def _pending_and_camera_changed(path, power):
    bench = Bench(path, FakeCamera(password="the-pending-one", http=True, power=power))
    seed(path, CameraRecord(MAC, "", "the-pending-one", None))
    return bench


SCENARIOS = {
    "factory": lambda path, power: Bench(path, FakeCamera(password="", http=False, power=power)),
    "blank_http": lambda path, power: Bench(path, FakeCamera(password="", http=True, power=power)),
    "reset_known": _reset_known,
    "pending_blank": _pending_but_camera_blank,
    "pending_changed": _pending_and_camera_changed,
}


def _clean_trace(scenario):
    with tempfile.TemporaryDirectory() as directory:
        power = Power()
        bench = SCENARIOS[scenario](Path(directory) / "camera.json", power)
        assert bench.link_up() is LinkState.ADOPTED
        return list(power.events)


def pytest_generate_tests(metafunc):
    if "crash_point" in metafunc.fixturenames:
        points, ids = [], []
        for scenario in SCENARIOS:
            for index, name in enumerate(_clean_trace(scenario)):
                for when in ("before", "after"):
                    points.append((scenario, index, when))
                    ids.append(f"{scenario}-{index:02d}-{when}-{name}")
        metafunc.parametrize("crash_point", points, ids=ids)


def assert_loginable_from_disk(bench):
    """The one promise that matters: the node can always still reach this camera."""
    record = bench.record()
    on_disk = {""}
    if record is not None:
        on_disk |= {record.password, record.pending_password}
    assert bench.camera.password in on_disk


def _adopt_after_crash(bench, generation):
    """Adopts now, unless a cut stamped the signature attempt, which then holds for a day."""
    state = bench.link_up(generation)
    if state is LinkState.NEEDS_RESET and bench.record().signature_failures:
        assert bench.enable_passwords.count("") <= 1
        assert_loginable_from_disk(bench)
        bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
        state = bench.link_up(generation + 1)
    return state


def _after_the_cut(bench):
    bench.restart()
    bench.camera.power = Power()
    bench.store.power = bench.camera.power


def _steps_finished_at(scenario, index, when):
    """The settings the record must hold at a cut: each is saved as it finishes."""
    trace = _clean_trace(scenario)
    completed = index if when == "before" else index + 1
    finished = set(STEPS) if scenario == "reset_known" else set()
    for position, name in enumerate(trace[:completed]):
        if name == "save" and trace[position + 1] == "set_password":
            finished = set()  # blank proved a reset, so no setting survived
        if name in STEPS:
            save = trace.index("save", position)
            if save < completed:
                finished.add(name)
    return finished


def test_crash_matrix_covers_every_adoption_step():
    trace = _clean_trace("factory")
    for name in STEPS:
        following = [event for event in trace[trace.index(name) + 1 :] if event != "login"]
        assert following[0] == "save", f"{name} is not saved as it finishes"
    assert "enable_services" in trace
    assert trace.count("login") == 4  # blank, new password, and one each after two port writes
    assert "set_password" in trace
    for name in STEPS:
        assert name in trace
    assert trace.index("set_password") > trace.index("enable_services")


def test_crash_at_every_point_never_strands_the_camera(tmp_path, crash_point):
    scenario, index, when = crash_point
    power = Power(crash_at=index, when=when)
    bench = SCENARIOS[scenario](tmp_path / "camera.json", power)

    with pytest.raises(PowerCut):
        bench.link_up()
    assert_loginable_from_disk(bench)

    # A daemon restart in the same link generation.
    _after_the_cut(bench)
    bench.clock.advance(60)
    bench.tick()
    assert_loginable_from_disk(bench)

    # A reboot, which is a new link generation.
    assert _adopt_after_crash(bench, 2) is LinkState.ADOPTED
    assert_loginable_from_disk(bench)
    assert_fully_adopted(bench)
    assert bench.camera.failures < LIMIT
    assert len(bench.record().failures) <= 3


def test_steps_finished_before_the_cut_are_not_repeated(tmp_path, crash_point):
    scenario, index, when = crash_point
    power = Power(crash_at=index, when=when)
    bench = SCENARIOS[scenario](tmp_path / "camera.json", power)
    with pytest.raises(PowerCut):
        bench.link_up()
    finished = set(bench.record().applied) if bench.record() else set()
    assert finished == _steps_finished_at(scenario, index, when)
    _after_the_cut(bench)
    bench.camera.calls.clear()

    assert _adopt_after_crash(bench, 2) is LinkState.ADOPTED

    if bench.camera.calls.count("set_password"):
        return  # blank proved live, which proves a reset and starts the settings over
    assert not finished & set(bench.camera.calls)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_two_crashes_in_a_row_never_strand_the_camera(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(os, "fsync", lambda fd: None)  # thousands of runs; order is tested above
    for first in range(len(_clean_trace(scenario))):
        for second in range(60):
            path = tmp_path / f"{first}-{second}.json"
            bench = SCENARIOS[scenario](path, Power(crash_at=first))
            with pytest.raises(PowerCut):
                bench.link_up()
            assert_loginable_from_disk(bench)
            _after_the_cut(bench)
            bench.camera.power.crash_at = second
            bench.store.power = bench.camera.power

            try:
                state = _adopt_after_crash(bench, 2)
            except PowerCut:
                assert_loginable_from_disk(bench)
                _after_the_cut(bench)
                if _adopt_after_crash(bench, 5) is not LinkState.ADOPTED:
                    # Each cut mid-login stamps a failure, so the cap can hold it for a day.
                    assert len(bench.record().failures) >= adopt.FAILURE_CAP
                    assert_loginable_from_disk(bench)
                    bench.clock.advance(adopt.FAILURE_WINDOW_SECONDS)
                    assert bench.link_up(9) is LinkState.ADOPTED
            else:
                assert state is LinkState.ADOPTED
                path.unlink()
                break  # the second crash point lies past the end of this run
            assert_fully_adopted(bench)
            assert bench.camera.failures < LIMIT
            path.unlink()


def test_crash_while_the_change_is_mid_flight_keeps_both_candidates(path):
    # The exact window the design worries about: the camera has it, the record only as pending.
    power = Power()
    bench = Bench(path, FakeCamera(password="", http=True, power=power))
    original = FakeClient.set_password

    def applied_then_dies(self, new):
        original(self, new)
        raise PowerCut

    FakeClient.set_password = applied_then_dies
    try:
        with pytest.raises(PowerCut):
            bench.link_up()
    finally:
        FakeClient.set_password = original

    record = bench.record()
    assert record.pending_password == bench.camera.password != ""
    assert record.password == ""
    _after_the_cut(bench)
    assert bench.link_up(2) is LinkState.ADOPTED
    assert_fully_adopted(bench)
