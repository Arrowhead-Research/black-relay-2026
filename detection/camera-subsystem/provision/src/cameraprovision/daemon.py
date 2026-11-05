"""The provisioning daemon: runs the state machine against the real system.

``tick`` first turns what happened into state-machine events, then makes the system
match the state. Each side effect records only that it succeeded, so one that fails is
tried again on the next tick while the timers keep running. Enough failed ticks in a
row raise ``ProvisionerFailed``, and systemd restarts the process.

Request threads share ``_gate`` with the tick. A portal session has an ``open`` flag
that only changes under it, so once a state is left no late request can save a config,
move the AP window, or start a join.

Logs carry exception type names only, since messages can quote a secret. ``system.py``
logs its own safe description of a failed command.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol
from wsgiref.types import StartResponse, WSGIEnvironment

from cameraprovision.config import _RFC1123_LABEL_RE as RFC1123_LABEL_RE
from cameraprovision.config import _write_private_atomic
from cameraprovision.portal import PortalMode, WifiChange, make_app, make_server
from cameraprovision.state import ProvisionEvent as E
from cameraprovision.state import ProvisionState as S
from cameraprovision.state import next_state
from cameraprovision.store import NodeStore, Paths
from cameraprovision.uplink import should_relaunch_ap, uplink_up

__all__ = ["PortalClosed", "Provisioner", "ProvisionerFailed", "Settings", "System"]

logger = logging.getLogger(__name__)

_AP_STATES = (S.AP_UP, S.FALLBACK_AP)
_THREAD_JOIN_S = 2.0


class ProvisionerFailed(Exception):
    """Too many failed ticks in a row. The process should exit nonzero."""


class PortalClosed(Exception):
    """A request reached ``apply`` after its portal session ended."""


class System(Protocol):
    def boot_id(self) -> str: ...
    def gpio_reprovision_held(self) -> bool: ...
    def scan_ssids(self) -> list[str]: ...
    def start_ap(self) -> None: ...
    def ap_active(self) -> bool: ...
    def set_hostname(self, hostname: str) -> None: ...
    def stop_ap(self) -> None: ...
    def reload_connections(self) -> None: ...
    def join_uplink(self) -> None: ...
    def wifi_associated(self) -> bool: ...
    def ipv4_lease(self) -> bool: ...
    def tailscale_up(self, key_file: Path, hostname: str) -> None: ...
    def chown_to_service_user(self, path: Path) -> None: ...


@dataclass(frozen=True, kw_only=True)
class Settings:
    join_timeout_s: float = 120.0
    ap_window_s: float = 300.0
    ap_cap_s: float = 1800.0
    tick_s: float = 1.0
    max_consecutive_failures: int = 60
    # NetworkManager's default address for ipv4.method=shared. The portal is on port 80,
    # so the Host a browser sends is this bare address, and portal_host must equal it.
    ap_ip: str = "10.42.0.1"
    ap_port: int = 80
    interface: str = "wlan0"
    ap_check_s: float = 10.0
    rejoin_s: float = 60.0
    hostname_retry_s: float = 30.0
    tailscale_backoff_s: float = 30.0
    tailscale_backoff_max_s: float = 3600.0


@dataclass(eq=False)
class _RequestMark:
    """Set when this request saved, so another request ending cannot start the join."""

    saved: bool = False


@dataclass(eq=False)
class _PortalSession:
    mode: PortalMode
    server: Any = None
    thread: threading.Thread | None = None
    open: bool = True
    saved: bool = False  # a submission succeeded, and the join is due
    request: threading.local = field(default_factory=threading.local)


class _Body:
    """A response body that reports when wsgiref has finished sending it."""

    def __init__(self, body: Iterable[bytes], on_close: Callable[[], None]) -> None:
        self._body = body
        self._on_close = on_close

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._body)

    def close(self) -> None:
        self._on_close()


class Provisioner:
    def __init__(
        self,
        *,
        system: System,
        paths: Paths,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.monotonic,
        server_factory: Callable[..., Any] = make_server,
    ) -> None:
        self.paths = paths
        self.settings = settings or Settings()
        self._system = system
        self._clock = clock
        self._server_factory = server_factory
        self._store = NodeStore(
            paths, chown=system.chown_to_service_user, interface=self.settings.interface
        )

        self._gate = threading.Lock()
        self._state = S.UNPROVISIONED
        self._boot_id = ""
        self._entered_at = 0.0
        self._window_started = 0.0
        self._config_pending = False
        self._session: _PortalSession | None = None
        self._persisted: dict[str, Any] | None = None
        self._persist_failing = False
        self._bridge_config_valid = True
        # The window restarts at the first portal open after the AP starts, and not at a
        # later reopen or a daemon restart.
        self._window_awaits_first_open = False
        self._hostname_owed = False
        self._hostname_next = 0.0
        self._ap_check_next = 0.0
        self._rejoin_next = 0.0
        self._failures = 0

        self._ssids_lock = threading.Lock()
        self._ssids: list[str] = []
        self._scanned = False
        self._ap_started = False
        # The AP may be left over from a daemon that crashed.
        self._ap_stop_owed = True
        self._join_issued = False

        self._tailscale_settled = False
        self._tailscale_logged_in = False
        self._tailscale_next = 0.0
        self._tailscale_delay = self.settings.tailscale_backoff_s

    @property
    def state(self) -> S:
        return self._state

    # --- lifecycle ------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        try:
            self.start()
            while not stop.wait(self.settings.tick_s):
                self.tick()
        finally:
            self.shutdown()

    def start(self) -> None:
        self._store.recover()
        self._boot_id = self._system.boot_id()
        if not self._restore_state():
            self._fire_boot_events()
        self._check_bridge_config()
        self._persist()
        self._guarded("reconciling", self._reconcile)

    def tick(self) -> None:
        results = [
            self._guarded("checking events", self._poll),
            self._guarded("reconciling", self._reconcile),
        ]
        self._persist()
        self._failures = 0 if all(results) else self._failures + 1
        if self._failures >= self.settings.max_consecutive_failures:
            raise ProvisionerFailed(f"{self._failures} failed ticks in a row")

    def shutdown(self) -> None:
        """Leave no AP without a portal. A restart brings both back from the saved state."""
        self._persist()
        self._guarded("closing the portal", self._close_portal)
        if self._ap_stop_owed:
            if self._guarded("stopping the AP", self._system.stop_ap):
                self._ap_stop_owed = False
            self._ap_started = False

    @staticmethod
    def _guarded(what: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except Exception as exc:
            logger.error("%s failed: %s", what, type(exc).__name__)
            return False
        return True

    # --- state and events -----------------------------------------------------------

    def _restore_state(self) -> bool:
        """Take the saved state if it is from this kernel boot. False means a real boot."""
        try:
            saved = json.loads(self.paths.state_file.read_text(encoding="utf-8"))
            same_boot = saved["boot_id"] == self._boot_id
            state = S(saved["state"])
            # CLOCK_MONOTONIC runs across process restarts within one boot, so a crash
            # loop cannot keep extending the join timeout or the AP cap.
            entered_at = float(saved["entered_at"])
            window_started = float(saved["window_started"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if not same_boot or state is S.UNPROVISIONED:
            return False
        self._state = state
        self._entered_at = entered_at
        self._window_started = window_started
        self._hostname_owed = state is S.JOINING
        self._rejoin_next = self._clock() + self.settings.rejoin_s
        logger.info("restored state %s after a daemon restart", state.value)
        return True

    def _fire_boot_events(self) -> None:
        self._fire(E.BOOT)
        if self._button_held():
            self._fire(E.REPROVISION_BUTTON)

    def _button_held(self) -> bool:
        try:
            return self._system.gpio_reprovision_held()
        except Exception as exc:
            logger.error("reading the setup button failed: %s", type(exc).__name__)
            return False

    def _fire(self, event: E) -> None:
        has_config = self._store.has_config()
        new = next_state(self._state, event, has_config=has_config)
        if new is self._state and event not in (E.BOOT, E.REPROVISION_BUTTON):
            return
        logger.info("%s + %s -> %s", self._state.value, event.value, new.value)
        self._state = new
        now = self._clock()
        self._entered_at = now
        self._window_started = now
        self._config_pending = False
        if self._session is not None:
            self._session.open = False
        self._scanned = False
        self._ap_started = False
        self._join_issued = False
        self._window_awaits_first_open = True
        self._check_bridge_config()
        if new is S.JOINING:
            self._hostname_owed = True
            self._hostname_next = now
        if new is S.PROVISIONED:
            self._rejoin_next = now + self.settings.rejoin_s
            self._tailscale_settled = False
            self._tailscale_next = now
            self._tailscale_delay = self.settings.tailscale_backoff_s

    def _poll(self) -> None:
        with self._gate:
            if self._config_pending:
                self._fire(E.CONFIG_RECEIVED)
            elif self._state is S.JOINING:
                self._poll_join()
            elif self._state in _AP_STATES:
                self._poll_ap_window()

    def _poll_join(self) -> None:
        uplink = self._probe_uplink()
        if uplink:
            self._fire(E.UPLINK_UP)
        elif should_relaunch_ap(
            state=self._state,
            seconds_in_joining=self._clock() - self._entered_at,
            uplink=uplink,
            timeout_s=self.settings.join_timeout_s,
        ):
            self._fire(E.UPLINK_TIMEOUT)

    def _probe_uplink(self) -> bool:
        try:
            return uplink_up(self._system)
        except Exception as exc:
            logger.warning("the uplink probe failed: %s", type(exc).__name__)
            return False

    def _poll_ap_window(self) -> None:
        now = self._clock()
        settings = self.settings
        if (
            now - self._window_started < settings.ap_window_s
            and now - self._entered_at < settings.ap_cap_s
        ):
            return
        before = self._state
        self._fire(E.AP_WINDOW_ELAPSED)
        if self._state is before:
            # No config to rejoin with, so the AP stays. Look again a window from now.
            self._window_started = now
            self._entered_at = now

    def _check_bridge_config(self) -> None:
        """Report a bad bridge config. It stops the bridge, but never the join or the AP logic."""
        valid = self._store.load() is not None or not self._store.has_config()
        if valid != self._bridge_config_valid:
            if valid:
                logger.info("the bridge config is valid again")
            else:
                logger.error("the bridge config is missing or invalid; the bridge cannot run")
        self._bridge_config_valid = valid

    def _persist(self) -> None:
        """Save the state if it changed. A failure is only logged.

        Exiting over it would not help: a restart would find the old state file, fire boot
        again and restart the join timer, so a full disk could keep a node from ever falling
        back. This process carries on with its state in memory, so it fires boot only once.
        """
        record = {
            "boot_id": self._boot_id,
            "state": self._state.value,
            "entered_at": self._entered_at,
            "window_started": self._window_started,
            "bridge_config_valid": self._bridge_config_valid,
        }
        if record == self._persisted:
            return
        try:
            _write_private_atomic(self.paths.state_file, json.dumps(record))
        except Exception as exc:
            if not self._persist_failing:
                logger.error("saving state failed: %s", type(exc).__name__)
            self._persist_failing = True
            return
        self._persisted = record
        self._persist_failing = False

    # --- making the system match the state ------------------------------------------

    def _reconcile(self) -> None:
        if self._state in _AP_STATES:
            self._reconcile_ap()
            return
        self._close_portal()
        if self._ap_stop_owed:
            self._system.stop_ap()
            self._ap_stop_owed = False
        self._reconcile_hostname()
        if self._state is S.JOINING and not self._join_issued:
            self._system.reload_connections()
            # A link NetworkManager already joined must not be torn down and re-activated.
            if not self._probe_uplink():
                self._system.join_uplink()
            self._join_issued = True
        elif self._state is S.PROVISIONED:
            self._reconcile_rejoin()
            self._reconcile_tailscale()

    def _reconcile_hostname(self) -> None:
        """Apply the hostname on entering JOINING. A failure must not hold up the join."""
        now = self._clock()
        if not self._hostname_owed or now < self._hostname_next:
            return
        hostname = self._store.hostname()
        if hostname is None or not RFC1123_LABEL_RE.fullmatch(hostname):
            # node.json can be edited by hand, and "--static" would be read as a flag.
            logger.error("the saved hostname is not valid, so it was not applied")
            self._hostname_owed = False
            return
        try:
            self._system.set_hostname(hostname)
        except Exception as exc:
            logger.warning("setting the hostname failed: %s", type(exc).__name__)
            self._hostname_next = now + self.settings.hostname_retry_s
            return
        self._hostname_owed = False

    def _reconcile_rejoin(self) -> None:
        """Nudge NetworkManager, which can stop autoconnecting after a no-secrets failure.

        A provisioned node never opens the AP over a lost link, so this is all it does.
        """
        now = self._clock()
        if now < self._rejoin_next:
            return
        self._rejoin_next = now + self.settings.rejoin_s
        if self._probe_uplink():
            return
        try:
            self._system.join_uplink()
        except Exception as exc:
            logger.warning("re-issuing the join failed: %s", type(exc).__name__)

    def _reconcile_ap(self) -> None:
        self._check_ap_alive()
        # In this order: the radio cannot scan while serving the AP, and the listener's
        # address does not exist until the AP profile is active.
        if not self._scanned:
            self._scan()
            self._scanned = True
        if not self._ap_started:
            self._ap_stop_owed = True
            self._system.start_ap()
            self._ap_started = True
            self._ap_check_next = self._clock() + self.settings.ap_check_s
        with self._gate:
            mode = self._portal_mode()
            session = self._session
            stale = session is not None and (
                not session.open
                or session.thread is None
                or not session.thread.is_alive()
                # Once a submission saved, the node is leaving: keep its response's session.
                or (session.mode is not mode and not session.saved)
            )
        if stale:
            self._close_portal()
        if self._session is None:
            self._open_portal(mode)

    def _check_ap_alive(self) -> None:
        """Restart an AP NetworkManager dropped, which would otherwise leave a dead portal."""
        now = self._clock()
        if not self._ap_started or now < self._ap_check_next:
            return
        self._ap_check_next = now + self.settings.ap_check_s
        try:
            alive = self._system.ap_active()
        except Exception as exc:
            logger.warning("checking the AP failed: %s", type(exc).__name__)
            return
        if not alive:
            logger.error("the AP is no longer active, so restarting it")
            self._close_portal()
            self._ap_started = False

    def _portal_mode(self) -> PortalMode:
        """WiFi-only for a fallback AP with a config to repair, otherwise everything."""
        if self._state is S.FALLBACK_AP and self._store.has_config():
            return PortalMode.WIFI_ONLY
        return PortalMode.FULL

    def _scan(self) -> None:
        try:
            found = self._system.scan_ssids()
        except Exception as exc:
            logger.warning("scanning for WiFi failed: %s", type(exc).__name__)
            return
        if found:
            with self._ssids_lock:
                self._ssids = list(found)

    def _cached_ssids(self) -> list[str]:
        """What the portal lists. Called from its request threads, and never touches the radio."""
        with self._ssids_lock:
            return list(self._ssids)

    def _open_portal(self, mode: PortalMode) -> None:
        settings = self.settings
        session = _PortalSession(mode=mode)
        app = make_app(
            scan_ssids=self._cached_ssids,
            apply=lambda change: self._apply(session, change),
            portal_host=settings.ap_ip,
            mode=mode,
            on_form_request=lambda: self._restart_window(session),
        )
        session.server = self._server_factory(
            self._reporting_completion(session, app),
            settings.ap_ip,
            settings.ap_port,
            settings.interface,
        )
        session.thread = threading.Thread(
            target=session.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="portal",
            daemon=True,
        )
        session.thread.start()
        with self._gate:
            if self._window_awaits_first_open:
                self._window_awaits_first_open = False
                self._window_started = self._clock()
            self._session = session
        logger.info("portal listening in %s mode", mode.value)

    def _close_portal(self) -> None:
        session = self._session
        if session is None:
            return
        self._session = None
        with self._gate:
            session.open = False
        session.server.shutdown()
        session.server.server_close()
        if session.thread is not None:
            session.thread.join(_THREAD_JOIN_S)
        logger.info("portal closed")

    def _reconcile_tailscale(self) -> None:
        now = self._clock()
        if self._tailscale_settled or now < self._tailscale_next:
            return
        if not self._tailscale_logged_in:
            hostname = self._store.pending_login_hostname()
            if hostname is None:
                self._tailscale_settled = True
                return
            try:
                self._system.tailscale_up(self.paths.tailscale_key, hostname)
            except Exception as exc:
                logger.warning(
                    "tailscale login failed, retrying in %g s: %s",
                    self._tailscale_delay,
                    type(exc).__name__,
                )
                self._tailscale_back_off(now)
                return
            self._tailscale_logged_in = True
            self._tailscale_delay = self.settings.tailscale_backoff_s
            logger.info("tailscale login succeeded")
        try:
            self._store.delete_tailscale_key()
        except OSError as exc:
            logger.error("deleting the used tailscale key failed: %s", type(exc).__name__)
            self._tailscale_back_off(now)
            return
        self._tailscale_logged_in = False
        self._tailscale_settled = True

    def _tailscale_back_off(self, now: float) -> None:
        self._tailscale_next = now + self._tailscale_delay
        self._tailscale_delay = min(
            self._tailscale_delay * 2, self.settings.tailscale_backoff_max_s
        )

    # --- called from portal request threads -----------------------------------------

    def _apply(self, session: _PortalSession, change: Any) -> None:
        """Save a submission, and do nothing else: the join starts after the response."""
        with self._gate:
            if not session.open:
                raise PortalClosed
            if isinstance(change, WifiChange):
                self._store.save_wifi(change.wifi_ssid, change.wifi_psk)
            else:
                self._store.save(change)
            session.saved = True
            session.request.mark.saved = True

    def _restart_window(self, session: _PortalSession) -> None:
        with self._gate:
            if session.open:
                self._window_started = self._clock()

    def _reporting_completion(self, session: _PortalSession, app: Any) -> Any:
        """Wrap ``app`` to fire ``config_received`` once a saving POST's response has gone."""

        def wrapped(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
            mark = _RequestMark()
            session.request.mark = mark

            def finished() -> None:
                if mark.saved:
                    with self._gate:
                        if session.open:
                            self._config_pending = True

            return _Body(app(environ, start_response), finished)

        return wrapped
