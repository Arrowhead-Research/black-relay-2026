"""Adopt a factory-fresh camera with no operator action, and never lock it out.

``step`` is one tick. The caller runs it repeatedly and it never sleeps, so "wait" means
returning ``ADOPTING`` and looking again on the next tick. Everything that has to outlive
a crash is on disk before the act it protects:

- ``attempted`` is written before any login. A daemon restart in the same link generation
  finds it, with no outcome beside it, and sends nothing.
- A failure is stamped along with ``attempted`` and removed on success, so a crash during
  a first login cannot dodge the rolling caps.
- A new password is saved as ``pending_password`` before the camera hears of it, and is
  promoted to ``password`` only once a login with it has worked.
- Each finished setting is recorded as it finishes, so a retry repeats only the rest.

The saved password of a known camera moves to ``pending_password`` before a blank login
over Baichuan. That call can turn HTTP on and then fail or time out, leaving the camera
blank or not, so from then on the record says what is true: either might be live, and the
next generation tries the saved password and then blank. Read ``password`` only after
``ADOPTED`` or ``ADOPTED_PENDING_SETTINGS``: until then it can be blank while the camera
is not.

``step`` holds ``store.lock()`` for the whole tick, including its network calls, because
the record it works from is in memory: another writer must not change the file under it.
"""

import asyncio
import logging
import secrets
import socket
import time
from dataclasses import dataclass, replace
from enum import StrEnum

from cameralink.reolink import (
    ApiError,
    AuthFailed,
    CameraError,
    LockedOut,
    LoginOutcomeUnknown,
    Unreachable,
    enable_services,
)
from cameralink.store import CameraRecord, CameraStore

_log = logging.getLogger(__name__)

_USER = "admin"
_BLANK = ""
_HTTP_PORT = 80
_BAICHUAN_PORT = 9000
_PROBE_TIMEOUT = 3.0

# The camera answers a port well before it is ready, and HTTP opens later than Baichuan,
# so a camera mid-boot looks factory-reset until this has passed. Measured: DHCP at about
# 13 s after going offline, port 9000 within about 40 s.
SETTLE_SECONDS = 45
FAILURE_CAP = 3
# The factory signature skips FAILURE_CAP, because a real reset must adopt at once, but a
# camera can show it without being reset. Baichuan reports no remain_times to warn of a
# lockout, so a failed try on the signature is limited apart from the cap. Success never is.
SIGNATURE_FAILURE_CAP = 1
FAILURE_WINDOW_SECONDS = 24 * 3600
# The camera locks at 10 failures, and one enable_services call can spend 3. Stopping here
# keeps the margin even if the camera's own counter does not fall over the cap's 24 hours.
MIN_REMAIN_TIMES = 3
_CANCEL_GRACE = 5.0

STEPS = (
    "disable_p2p",
    "enable_rtsp",
    "disable_onvif",
    "set_clock",
    "set_name",
    "set_streams",
    "set_rotation",
)


class LinkState(StrEnum):
    NO_CAMERA = "no_camera"
    ADOPTING = "adopting"
    ADOPTED = "adopted"
    # Secured and streaming, so Frigate may run, but a known node_id or rotation has not
    # reached the camera yet: the cap is holding it back, or the login was refused.
    ADOPTED_PENDING_SETTINGS = "adopted_pending_settings"
    NEEDS_RESET = "needs_reset"
    FAILED = "failed"


# Where a generation's attempt stands, besides the LinkState it ended in. Both mean that
# the next tick continues the same attempt, rather than that nothing more may be sent.
_PHASE_HTTP_FOLLOW_UP = "http_follow_up"  # enable_services proved blank; HTTP login is next
_PHASE_TRY_BLANK = "try_blank"  # the pending password was rejected; blank is next


@dataclass(frozen=True)
class Lease:
    mac: str
    ip: str


def step(
    lease: Lease | None,
    link_generation: int,
    store: CameraStore,
    client_factory,
    now,
    *,
    node_id: str | None = None,
    flipped: bool | None = None,
    enable=enable_services,
    probe=None,
    enable_timeout: float = 120,
    monotonic=time.monotonic,
) -> LinkState:
    """One tick. ``now`` is the wall clock: called with no arguments, it returns an aware
    datetime. ``monotonic`` times the settle, since the wall clock of a node with no RTC
    steps forward when it first syncs, which would skip the wait."""
    if lease is None:
        return LinkState.NO_CAMERA
    with store.lock():
        adoption = _Adoption(
            lease,
            link_generation,
            store,
            client_factory,
            now,
            monotonic,
            node_id,
            flipped,
            enable,
            probe or _port_open,
            enable_timeout,
        )
        try:
            state = adoption.run()
        finally:
            # Also after a StoreError, or the camera's session stays open until it expires.
            adoption.logout()
    _log.debug("camera %s: generation %s: %s", adoption.mac, link_generation, state)
    return state


def _port_open(host: str, port: int) -> bool | None:
    """True if it accepts and False if it refuses. Anything else says nothing about the
    port, and a port wrongly called closed would look like the factory signature."""
    try:
        socket.create_connection((host, port), _PROBE_TIMEOUT).close()
    except ConnectionRefusedError:
        return False
    except OSError:
        return None
    return True


class _Adoption:
    """One tick's work on one camera. All lasting state is in the record."""

    def __init__(
        self,
        lease,
        generation,
        store,
        client_factory,
        now,
        monotonic,
        node_id,
        flipped,
        enable,
        probe,
        enable_timeout,
    ):
        self.mac = lease.mac.lower()
        self.ip = lease.ip
        self.key = f"{self.mac}/{generation}"
        self.store = store
        self.client_factory = client_factory
        self.now = now
        self.monotonic = monotonic
        self.node_id = node_id
        self.flipped = flipped
        self.enable = enable
        self.probe = probe
        self.enable_timeout = enable_timeout
        self._client = None
        self._undo = None

        latest = store.load()
        self.rec = store.load(self.mac) or CameraRecord(self.mac, _BLANK, None, None)
        if latest is None or latest.mac != self.mac:
            # Now the latest camera, so nothing can read the previous camera's password
            # as this one's. A record of its own is what keeps that password off this wire.
            self.rec = store.save(self.rec)

    def run(self) -> LinkState:
        if self.rec.attempted == self.key:
            return self._continue_generation()

        http = self.probe(self.ip, _HTTP_PORT)
        baichuan = self.probe(self.ip, _BAICHUAN_PORT)
        if not (http or baichuan) or not self._settled():
            return LinkState.ADOPTING
        if http is None or baichuan is None:
            return LinkState.ADOPTING
        if baichuan and not http:
            return self._factory_signature()
        if self.rec.locked or self._capped():
            return LinkState.NEEDS_RESET
        if self.rec.pending_password is not None:
            return self._recover()
        if self.rec.password == _BLANK:
            return self._blank_login()
        return self._saved_login()

    def logout(self) -> None:
        if self._client is None:
            return
        try:
            self._client.logout()
        except CameraError as err:
            _log.debug("camera %s: logout failed: %s", self.mac, type(err).__name__)

    @property
    def client(self):
        if self._client is None:
            self._client = self.client_factory(self.ip)
        return self._client

    def _continue_generation(self) -> LinkState:
        phase = self.rec.phase
        if phase == _PHASE_HTTP_FOLLOW_UP:
            return self._http_follow_up()
        if phase == _PHASE_TRY_BLANK:
            return self._blank_after_pending()
        if phase == LinkState.ADOPTED:
            return self._finish_newly_runnable_steps()
        # No phase: a crash or kill during the attempt. Its outcome is unknown, and only
        # the next generation may try again.
        return LinkState.FAILED if phase is None else LinkState(phase)

    def _settled(self) -> bool:
        since = self.rec.settle_since
        current = self.monotonic()
        # A "since" later than now can only be from before a reboot.
        if self.rec.settle_key != self.key or since is None or current < since:
            self._update(settle_key=self.key, settle_since=current)
            return False
        return current - since >= SETTLE_SECONDS

    def _capped(self) -> bool:
        return len(self._recent(self.rec.failures)) >= FAILURE_CAP

    def _recent(self, stamps: tuple[float, ...]) -> tuple[float, ...]:
        # A stamp from the future is kept: this node can boot with an old clock.
        oldest = self.now().timestamp() - FAILURE_WINDOW_SECONDS
        return tuple(stamp for stamp in stamps if stamp > oldest)

    def _factory_signature(self) -> LinkState:
        if len(self._recent(self.rec.signature_failures)) >= SIGNATURE_FAILURE_CAP:
            return LinkState.NEEDS_RESET
        if self.rec.password != _BLANK:
            self._update(pending_password=self.rec.password, password=_BLANK)
        self._begin_attempt(signature=True)
        failure = None
        try:
            _run_enable(self.enable, self.ip, _BLANK, self.enable_timeout)
        except Unreachable:
            self._undo_attempt()
            return LinkState.ADOPTING
        except CameraError as err:
            failure = err
        if isinstance(failure, ApiError):
            # enable_services raises this only once logged in, so blank worked, and this
            # was no failed login. The attempt for this generation is still used.
            self._update(signature_failures=self.rec.signature_failures[:-1])
        if failure is not None:
            return self._login_failed(failure)
        # Blank worked, so this attempt did not fail. Whether it was a real reset, which
        # clears the caps, waits on HTTP agreeing: see _change_password.
        self._update(
            signature_failures=self.rec.signature_failures[:-1], phase=_PHASE_HTTP_FOLLOW_UP
        )
        return self._http_follow_up()

    def _http_follow_up(self) -> LinkState:
        """Blank again, over HTTP, which spends no new guess when the firmware is consistent.
        If it is not, the signature path has no cap of its own to stop this repeating, so
        the day's signature attempt pays for it."""
        state = self._try_login(_BLANK)
        if state is None:
            return self._guarded(self._change_password)
        if state is not LinkState.ADOPTING:
            stamp = self.now().timestamp()
            self._update(signature_failures=(*self._recent(self.rec.signature_failures), stamp))
        return state

    def _recover(self) -> LinkState:
        # A password the camera refused is skipped, to save one of its failures, until
        # blank is refused too. Then one verdict was wrong, or the camera is foreign, and
        # the pending password is asked again. It stays on disk until blank proves out.
        if self.rec.pending_rejected:
            return self._blank_login()
        pending = self.rec.pending_password
        state = self._try_login(pending, rejected=_PHASE_TRY_BLANK)
        if state is not None:
            if self.rec.pending_rejected:
                return self._blank_after_pending()
            return state
        self._update(password=pending, pending_password=None)
        return self._guarded(self._run_steps)

    def _blank_after_pending(self) -> LinkState:
        if self._capped():
            self._update(phase=LinkState.NEEDS_RESET.value)
            return LinkState.NEEDS_RESET
        return self._blank_login()

    def _blank_login(self) -> LinkState:
        state = self._try_login(_BLANK)
        return state if state is not None else self._guarded(self._change_password)

    def _saved_login(self) -> LinkState:
        state = self._try_login(self.rec.password)
        return state if state is not None else self._guarded(self._run_steps)

    def _finish_newly_runnable_steps(self) -> LinkState:
        """Provisioning arrived or changed while the link stayed up. Nothing else can
        bring this to the camera, and at worst the saved password fails as usual."""
        wanted = _wanted(self.node_id, self.flipped)
        unfinished = any(
            value is not None and self.rec.applied.get(name) != value
            for name, value in wanted.items()
        )
        if not unfinished:
            return LinkState.ADOPTED
        if self._capped():
            return LinkState.ADOPTED_PENDING_SETTINGS
        state = self._try_login(self.rec.password)
        if state is LinkState.ADOPTING:
            return LinkState.ADOPTED_PENDING_SETTINGS  # refused; the undo restored the phase
        return state if state is not None else self._guarded(self._run_steps)

    def _begin_attempt(self, signature: bool = False) -> None:
        rec = self.rec
        self._undo = {
            "attempted": rec.attempted,
            "phase": rec.phase,
            "failures": rec.failures,
            "signature_failures": rec.signature_failures,
        }
        field = "signature_failures" if signature else "failures"
        stamps = (*self._recent(getattr(rec, field)), self.now().timestamp())
        self._update(attempted=self.key, phase=None, **{field: stamps})

    def _undo_attempt(self) -> None:
        self._update(**self._undo)

    def _try_login(self, password: str, rejected: str | None = None) -> LinkState | None:
        """One counted HTTP login. None means it worked; otherwise the state to report."""
        self._begin_attempt()
        failure = None
        try:
            self.client.login(_USER, password)
        except Unreachable:
            self._undo_attempt()
            return LinkState.ADOPTING
        except CameraError as err:
            failure = err
        if failure is not None:
            return self._login_failed(failure, rejected)
        self._update(failures=self.rec.failures[:-1])
        return None

    def _login_failed(self, err: CameraError, rejected: str | None = None) -> LinkState:
        """Judge a login that was already stamped as a failure."""
        name = type(err).__name__
        if isinstance(err, LockedOut):
            _log.warning("camera %s: locked out, attempts stopped until a reset", self.mac)
            self._update(phase=LinkState.NEEDS_RESET.value, locked=True)
            return LinkState.NEEDS_RESET
        if isinstance(err, AuthFailed):
            remain = err.remain_times
            _log.warning("camera %s: login rejected, %s tries left", self.mac, remain)
            if remain is not None and remain <= MIN_REMAIN_TIMES:
                self._update(phase=LinkState.NEEDS_RESET.value, locked=True)
                return LinkState.NEEDS_RESET
            if rejected is None:
                # A saved password leaves the flag clear, so this is blank being refused.
                self._update(phase=LinkState.NEEDS_RESET.value, pending_rejected=False)
            else:
                self._update(phase=rejected, pending_rejected=True)
            return LinkState.NEEDS_RESET
        _log.warning("camera %s: login outcome unknown: %s", self.mac, name)
        self._update(phase=LinkState.FAILED.value)
        return LinkState.FAILED

    def _guarded(self, work) -> LinkState:
        """Work after a login. Errors here end the generation; the password stays on disk."""
        failure = None
        try:
            work()
        except CameraError as err:
            failure = err
        if failure is None:
            self._update(phase=LinkState.ADOPTED.value)
            return LinkState.ADOPTED
        # The client logs in again after some writes, so these can be login verdicts.
        if isinstance(failure, (AuthFailed, LockedOut, LoginOutcomeUnknown)):
            self._update(failures=(*self.rec.failures, self.now().timestamp()))
            return self._login_failed(failure)
        _log.warning("camera %s: adoption stopped: %s", self.mac, type(failure).__name__)
        self._update(phase=LinkState.FAILED.value)
        return LinkState.FAILED

    def _change_password(self) -> None:
        """Blank just worked over HTTP. That is a real reset: no earlier setting survives,
        and the camera's own failure count is gone, with any lockout."""
        password = self.rec.pending_password
        if password is None:
            password = secrets.token_urlsafe(15)[:20]
        self._update(
            pending_password=password,
            password=_BLANK,
            pending_rejected=False,
            applied={},
            failures=(),
            signature_failures=(),
            locked=False,
        )
        self.client.set_password(password)
        self.client.login(_USER, password)
        self._update(password=password, pending_password=None)
        _log.info("camera %s: password changed", self.mac)
        self._run_steps()

    def _run_steps(self) -> None:
        for name, value in _wanted(self.node_id, self.flipped).items():
            if value is None or self.rec.applied.get(name) == value:
                continue
            method = getattr(self.client, name)
            if name == "set_clock":
                method(self.now())
            elif name in ("set_name", "set_rotation"):
                method(value)
            else:
                method()
            self._update(applied={**self.rec.applied, name: value})

    def _update(self, **changes) -> None:
        self.rec = self.store.save(replace(self.rec, **changes))


def _wanted(node_id: str | None, flipped: bool | None) -> dict:
    """Each step and the value that finishes it, in order. None means not yet known."""
    wanted = dict.fromkeys(STEPS, True)
    wanted["set_name"] = node_id
    wanted["set_rotation"] = flipped
    return wanted


def _run_enable(enable, host: str, password: str, timeout: float) -> None:
    """Run the async enable_services from this synchronous tick, bounded in time.

    asyncio.wait_for and asyncio.run both wait for a cancelled task to finish, and
    enable_services does network I/O in a ``finally``, so neither gives a bound.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_bounded(enable(host, password), host, timeout))
    finally:
        loop.close()


async def _bounded(coroutine, host: str, timeout: float) -> None:
    task = asyncio.ensure_future(coroutine)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        # Twice, since the first cancel can land in the logout of a ``finally``.
        for _ in range(2):
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=_CANCEL_GRACE)
            if done:
                break
        if done and not task.cancelled():
            task.exception()  # retrieved, or asyncio logs it
        raise LoginOutcomeUnknown(f"camera {host}: enable_services timed out")
    if task.cancelled():
        raise LoginOutcomeUnknown(f"camera {host}: enable_services was cancelled")
    error = task.exception()
    if error is None:
        return
    if isinstance(error, CameraError) or not isinstance(error, Exception):
        raise error
    # Its message could hold anything, so only its type goes on.
    raise LoginOutcomeUnknown(f"camera {host}: enable_services failed: {type(error).__name__}")
