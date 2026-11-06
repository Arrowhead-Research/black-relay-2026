"""The daemon that owns the camera link: one tick every few seconds, for as long as it runs.

Each tick looks at ``eth0`` and the DHCP lease, runs one ``adopt.step``, decides what Frigate
should be doing, and publishes the result to a world-readable state file.

The link generation is what limits a login to one per link or address change, so it only ever
rises, and it is on disk before a step can use it. A generation that went back, or a bump that a
restart forgot, could let the same physical link spend a second login. It rises on a carrier
change, a lease IP change and every boot. The last also voids a settle time from before a reboot.
A tick can block for two minutes, so the kernel's flap counter is read as well as the carrier.

dnsmasq has one address to give, and holds it for the MAC it gave it to until the lease ends, so a
swapped camera gets "no address available" for up to an hour (measured with these flags). That is
why a boot or a rising carrier wipes the lease and restarts dnsmasq, and why no lease is believed
until that is done: at those moments the file may describe a camera that has gone.

A node with no RTC boots on an old clock and NTP later steps it, which makes a live lease look
expired. So the expiry only decides whether to believe a lease not yet seen in this generation.
One already seen is held until the generation changes or the file names another MAC.

Errors from the camera, the store, render and systemd end the tick's work and are reported. Any
other exception is a bug and is left to crash into ``Restart=always``. The state file lives in the
service's runtime directory, so it goes too, and nothing can read a stale state as current.

A Hailo detector can crash and leave Frigate's stats looking normal, so the daemon also reads
Frigate's log, and publishes when it last managed to, so that a blind scan cannot pass for a clean
one. A failure found there stays reported until the container starts again, because no line in the
log proves the detector recovered. So a failure that outlasts DETECTOR_ERROR_RESTART_SECONDS gets a
restart, at most once per DETECTOR_RESTART_LIMIT_SECONDS across daemon restarts, and a permanent
fault stays reported at that pace. A daemon restart reads the whole log of the running container,
so the report survives it.

Frigate's recordings state is only a setting, so the daemon also reports whether segments are being
written. ffmpeg writes every segment to the cache as ``<camera>@<time>.mp4`` whatever the retention
mode, and retention is decided later. Frigate v0.18.2: frigate/config/camera/camera.py:282,293 and
frigate/const.py:16 for the path, and frigate/record/maintainer.py:63 for the later discard. The
cache is a host tmpfs so that this daemon can see it.
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import sys
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from cameralink.adopt import (
    FAILURE_CAP,
    FAILURE_WINDOW_SECONDS,
    SIGNATURE_FAILURE_CAP,
    Lease,
    LinkState,
    step,
)
from cameralink.frigate_config import FrigateConfigError, proxy_secret, render, write_env
from cameralink.reolink import CameraError, ReolinkClient
from cameralink.store import CameraRecord, CameraStore, StoreError

_log = logging.getLogger(__name__)

TICK_SECONDS = 5
FRIGATE_DOWN_TICKS = 6
FRIGATE_UNIT = "frigate.service"
DHCP_UNIT = "camera-link-dhcp.service"
FIREWALL_UNIT = "nftables.service"

_SYSTEMCTL = "/usr/bin/systemctl"
_SYSTEMCTL_TIMEOUT = 30
RUNNING_STATES = frozenset({"active", "activating", "reloading", "refreshing"})
_NOT_RUNNING_STATES = frozenset({"inactive", "failed", "deactivating"})
_RUNNABLE = frozenset({LinkState.ADOPTED, LinkState.ADOPTED_PENDING_SETTINGS})
_LIMITED = frozenset(
    {LinkState.ADOPTED_PENDING_SETTINGS, LinkState.NEEDS_RESET, LinkState.FAILED}
)
_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
# Frigate's files are from a camera nobody can name now, so any camera differs from it.
_UNKNOWN_MAC = "unknown"
_HIDDEN_REASON = (
    "Frigate's config could not be rendered: a setting was rejected, and the message is hidden "
    "because it would show a secret"
)
_NO_CENTRAL_PROXY = "central_proxy_unavailable"
_NEEDS_RESET = "the camera needs a factory reset"
_LIMIT_REACHED = (
    "the camera's login limit is reached, see retry_after, or run camera-link clear-limits"
)
_LOCAL_SECRET_RE = re.compile(rb"[0-9a-f]{64}\s*")

LOG_SCAN_SECONDS = 30
# Overlaps the last read, which docker timed in whole seconds. Reading a line twice is harmless.
_LOG_OVERLAP_SECONDS = 5
_DOCKER = "/usr/bin/docker"
_FRIGATE_CONTAINER = "frigate"
_DOCKER_TIMEOUT = 20
# Frigate v0.18.2: frigate/detectors/plugins/hailo8l.py:132, 264, 345, 351, frigate/watchdog.py:126.
# The HailoRT lines come from its native library.
_HAILO_FAILURE_RE = re.compile(
    r"\[HailoRT\] \[error\]"
    r"|HailoRTException"
    r"|Inference error:"
    r"|\[INIT\] Failed to initialize HailoAsyncInference"
    r"|Timeout waiting for inference results"
    r"|HailoRT inference thread has stopped"
    r"|Detection appears to be stuck"
)
_DOCKER_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")

DETECTOR_ERROR_RESTART_SECONDS = 120
DETECTOR_RESTART_LIMIT_SECONDS = 600
_LATEST_TIME = 2**33  # the year 2242
_DETECTOR_FAILING = (
    "Frigate's log shows a detector failure, and Frigate is restarted for it at most every 10 minutes"
)
_LOG_UNREADABLE = "Frigate's log cannot be read, so its detector cannot be vouched for"

SEGMENT_MAX_AGE_SECONDS = 60
MEDIA_MIN_FREE_FRACTION = 0.05


class SystemdError(Exception):
    """systemd did not do what was asked, or could not be reached."""


class ProvisioningError(Exception):
    """The saved config cannot be used. The message never quotes the file."""


class LinkStateError(Exception):
    """The link cannot be observed or its generation saved, so no step may run."""


@dataclass(frozen=True, kw_only=True)
class Paths:
    lease_file: Path = Path("/var/lib/camera-link/dnsmasq.leases")
    store: Path = Path("/var/lib/camera-link/cameras.json")
    link_state: Path = Path("/var/lib/camera-link/link.json")
    carrier: Path = Path("/sys/class/net/eth0/carrier")
    carrier_changes: Path = Path("/sys/class/net/eth0/carrier_changes")
    boot_id: Path = Path("/proc/sys/kernel/random/boot_id")
    bridge_config: Path = Path("/etc/camerabridge/bridge.yaml")
    node_file: Path = Path("/var/lib/camera-provision/node.json")
    frigate_config: Path = Path("/var/lib/frigate/config/config.yml")
    frigate_env: Path = Path("/etc/camerabridge/frigate.env")
    state_file: Path = Path("/run/camera-link/state.json")
    local_proxy_secret: Path = Path("/var/lib/camera-link/proxy.secret")
    frigate_cache: Path = Path("/run/frigate-cache")
    frigate_media: Path = Path("/var/lib/frigate/media")


class Systemctl:
    """Frigate's container can take a minute to stop, and a tick must not wait for it, so all
    but ``stop_and_wait`` only queue the job. A failed start is Restart=always's to retry."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def state(self, unit: str) -> str:
        state = self._systemctl("is-active", unit).stdout.strip()
        if state in RUNNING_STATES or state in _NOT_RUNNING_STATES:
            return state
        raise SystemdError(f"{unit}: unexpected state from systemctl")

    def is_failed(self, unit: str) -> bool:
        return self._systemctl("is-failed", unit).stdout.strip() == "failed"

    def start(self, unit: str) -> None:
        self._act("--no-block", "start", unit)

    def restart(self, unit: str) -> None:
        self._act("--no-block", "restart", unit)

    def stop(self, unit: str) -> None:
        self._act("--no-block", "stop", unit)

    def stop_and_wait(self, unit: str) -> None:
        self._act("stop", unit)

    def _act(self, *args: str) -> None:
        result = self._systemctl(*args)
        if result.returncode != 0:
            raise SystemdError(f"systemctl {' '.join(args)} failed: exit {result.returncode}")

    def _systemctl(self, *args: str):
        failure = None
        try:
            return self._run(
                [_SYSTEMCTL, *args],
                capture_output=True,
                text=True,
                timeout=_SYSTEMCTL_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failure = "timed out"
        except OSError as err:
            failure = type(err).__name__
        raise SystemdError(f"systemctl {' '.join(args)}: {failure}")


class FrigateLog:
    """Asks docker about Frigate's container. None means docker gave no answer. Nothing it
    returns is logged or reported, because a log line can quote a setting."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def started_at(self) -> str | None:
        # stderr stays out: a CLI warning must not reach --since, or look like a restart.
        output = self._docker(
            "inspect", "--format", "{{.State.StartedAt}}", _FRIGATE_CONTAINER,
            stderr=subprocess.DEVNULL,
        )
        started = output.strip() if output is not None else ""
        return started if _DOCKER_TIME_RE.fullmatch(started) else None

    def since_time(self, since: str) -> str | None:
        # HailoRT writes its own lines from native code, past Frigate's logger.
        return self._docker("logs", "--since", since, _FRIGATE_CONTAINER, stderr=subprocess.STDOUT)

    def _docker(self, *args: str, stderr: int) -> str | None:
        try:
            result = self._run(
                [_DOCKER, *args],
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                errors="replace",
                timeout=_DOCKER_TIMEOUT,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class _Link:
    """What must outlive a restart of the daemon or the node. Holds no secret."""

    generation: int = 0
    boot_id: str | None = None
    carrier: bool | None = None
    carrier_changes: str | None = None  # the kernel's count, which sees a flap between ticks
    ip: str | None = None  # the last lease's IP, kept while there is no lease
    held_mac: str | None = None  # the MAC of the lease seen in this generation
    frigate_mac: str | None = None  # the camera whose password is in Frigate's env file
    restart_owed: bool = False  # the files are ahead of the running Frigate
    dhcp_reset_owed: bool = False  # the lease file may describe a camera that has gone
    detector_restart_at: float | None = None  # keeps the restart limit across daemon restarts


_LINK_FIELDS = {item.name for item in fields(_Link)}
# Added after link files already existed on a kit. A missing generation is damage; this is not.
_LINK_FIELDS_OPTIONAL = {"detector_restart_at"}


@dataclass(frozen=True)
class _Provisioning:
    node_id: str
    covered_zones: object  # checked by render
    relay_user: str
    relay_password: str
    flipped: bool

    def __repr__(self) -> str:
        return f"_Provisioning({self.node_id!r})"


def read_lease(path: Path, now: float | None) -> Lease | None:
    """The newest lease in a dnsmasq lease file that has not ended, or of any age if ``now`` is
    None. Lines that make no sense are skipped. An expiry of 0 is how dnsmasq writes a lease that
    never ends."""
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    best = None
    best_expiry = -1.0
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdecimal():
            continue
        expiry = float(fields[0]) or float("inf")
        mac = fields[1].lower()
        if (now is not None and expiry <= now) or not _MAC_RE.fullmatch(mac) or not _is_ipv4(fields[2]):
            continue
        if expiry > best_expiry:
            best, best_expiry = Lease(mac, fields[2]), expiry
    return best


def clear_limits(store_path: Path, out=None) -> int:
    """Clear the rolling cap and the signature limit of the latest camera. Everything else in
    its record stays: passwords, the lockout flag, and the attempt of this generation."""
    say = out or _say
    store = CameraStore(store_path)
    say("Waiting for the camera link daemon to finish its tick, up to about 130 s...")
    failure = None
    try:
        with store.lock():
            record = store.load()
            if record is not None:
                record = store.save(replace(record, failures=(), signature_failures=()))
    except StoreError as err:
        failure = str(err)
    if failure is not None:
        print(f"camera-link: {failure}", file=sys.stderr)
        return 1
    if record is None:
        say("No camera on record, so there is nothing to clear.")
        return 0
    say(f"Cleared the login limits for camera {record.mac}.")
    say("If it still reports needs_reset, unplug its cable and plug it back in.")
    return 0


class Daemon:
    def __init__(
        self,
        paths: Paths,
        systemd,
        *,
        step_fn=step,
        clock=None,
        client_factory=ReolinkClient,
        frigate_log=None,
    ):
        self._paths = paths
        self._systemd = systemd
        self._step = step_fn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._store = CameraStore(paths.store)  # one per thread: its lock is per object
        self._link = self._load_link()
        self._published = None
        self._frigate_down_ticks = 0
        self._frigate_up_ticks = 0
        self._frigate_log = frigate_log or FrigateLog()
        self._frigate_started = None
        self._detector_error = False
        self._detector_error_since = None
        self._log_blind = False
        self._log_checked_at = None
        self._log_read_at = None
        self._camera = None  # the Frigate camera whose segments count

    def run(self, stop) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(TICK_SECONDS)

    def tick(self) -> None:
        self._scan_frigate_log()
        problems = []
        try:
            lease = self._observe_link(problems)
        except LinkStateError as err:
            self._publish(LinkState.FAILED, None, None, str(err))
            return

        provisioning = None
        try:
            provisioning = _read_provisioning(self._paths)
            self._camera = provisioning.node_id if provisioning else None
        except ProvisioningError as err:
            problems.append(str(err))
        mac = lease.mac if lease else None

        record = None
        try:
            with self._store.lock():
                state = self._step(
                    lease,
                    self._link.generation,
                    self._store,
                    self._client_factory,
                    self._clock,
                    node_id=provisioning.node_id if provisioning else None,
                    flipped=provisioning.flipped if provisioning else None,
                )
                if mac is not None:
                    record = self._store.load(mac)
        except StoreError as err:
            state = LinkState.FAILED
            problems.append(str(err))
        except CameraError as err:
            state = LinkState.FAILED
            problems.append(f"camera error: {type(err).__name__}")

        try:
            problem = self._drive_frigate(state, mac, record, provisioning)
            if state in _RUNNABLE and provisioning is not None:
                self._restart_for_detector()
        except (SystemdError, LinkStateError) as err:
            problem = str(err)
        if problem:
            problems.append(problem)
        if self._detector_error:
            problems.append(_DETECTOR_FAILING)
        elif self._log_blind and state in _RUNNABLE:
            problems.append(_LOG_UNREADABLE)
        if state is LinkState.NEEDS_RESET:
            problems.append(_NEEDS_RESET if _password_in_doubt(record) else _LIMIT_REACHED)
        if provisioning is not None and not provisioning.relay_password:
            problems.append(_NO_CENTRAL_PROXY)
        self._publish(state, mac, record, problems[0] if problems else None)

    # The link

    def _load_link(self) -> _Link:
        try:
            content = self._paths.link_state.read_bytes()
        except FileNotFoundError:
            return _Link()
        except OSError as err:
            _log.warning("link state cannot be read: %s", type(err).__name__)
            return self._damaged_link()
        link = _decode_link(content)
        return link if link is not None else self._damaged_link()

    def _damaged_link(self) -> _Link:
        """The count is gone. The clock is far above any real count, and even if it were not,
        a repeated generation only sends no login until the next change, which is safe."""
        _log.warning("link state is damaged: taking the generation from the clock")
        return _Link(
            generation=int(self._clock().timestamp()),
            frigate_mac=_UNKNOWN_MAC,
            dhcp_reset_owed=True,
        )

    def _observe_link(self, problems: list) -> Lease | None:
        try:
            boot_id = self._paths.boot_id.read_text(encoding="ascii").strip()
        except (OSError, ValueError) as err:
            raise LinkStateError(f"the boot id cannot be read: {type(err).__name__}") from None
        carrier = _read_carrier(self._paths.carrier)
        flaps = _read_or_blank(self._paths.carrier_changes)

        link = self._link
        booted = link.boot_id != boot_id
        changed = (link.carrier is not None and link.carrier != carrier) or (
            bool(flaps) and bool(link.carrier_changes) and flaps != link.carrier_changes
        )
        if booted or changed:
            self._save_link(
                generation=link.generation + 1,
                boot_id=boot_id,
                carrier=carrier,
                carrier_changes=flaps,
                held_mac=None,
                dhcp_reset_owed=link.dhcp_reset_owed or booted or (changed and carrier),
            )
        elif flaps != link.carrier_changes:
            self._save_link(carrier_changes=flaps)
        if not carrier:
            return None
        if self._link.dhcp_reset_owed:
            self._reset_dhcp(problems)
            return None

        lease = self._current_lease()
        if lease is not None and (lease.ip, lease.mac) != (self._link.ip, self._link.held_mac):
            moved = self._link.ip is not None and lease.ip != self._link.ip
            self._save_link(
                generation=self._link.generation + (1 if moved else 0),
                ip=lease.ip,
                held_mac=lease.mac,
            )
        return lease

    def _current_lease(self) -> Lease | None:
        lease = read_lease(self._paths.lease_file, self._clock().timestamp())
        held = self._link.held_mac
        if lease is not None or held is None:
            return lease
        named = read_lease(self._paths.lease_file, None)
        if named is not None and named.mac != held:
            self._save_link(held_mac=None)
            return None
        return Lease(held, self._link.ip)

    def _reset_dhcp(self, problems: list) -> None:
        failure = None
        try:
            self._systemd.stop_and_wait(DHCP_UNIT)
            self._paths.lease_file.unlink(missing_ok=True)
            self._systemd.start(DHCP_UNIT)
        except SystemdError as err:
            failure = str(err)
        except OSError as err:
            failure = f"the lease file cannot be removed: {type(err).__name__}"
        if failure is not None:
            problems.append(f"the DHCP server could not be reset: {failure}")
            return
        self._save_link(dhcp_reset_owed=False)

    def _save_link(self, **changes) -> None:
        link = replace(self._link, **changes)
        try:
            _write_file(self._paths.link_state, json.dumps(asdict(link)).encode(), 0o600)
        except OSError as err:
            raise LinkStateError(
                f"the link state cannot be saved: {type(err).__name__}"
            ) from None
        self._link = link

    # Frigate

    def _drive_frigate(
        self,
        state: LinkState,
        mac: str | None,
        record: CameraRecord | None,
        provisioning: _Provisioning | None,
    ) -> str | None:
        problem = None
        if state in _RUNNABLE:
            if provisioning is None:
                problem = "not provisioned, so Frigate's config is not updated"
            else:
                try:
                    return self._apply_config(mac, record, provisioning)
                except FrigateConfigError as err:
                    problem = _without_secrets(str(err), record, provisioning)
                except OSError as err:
                    problem = f"Frigate's files cannot be written: {type(err).__name__}"
        self._frigate_down_ticks = 0

        # The env file holds another camera's password, or this one's was refused, and Frigate
        # would go on sending it. A limit alone says nothing against a working password.
        foreign = (
            mac is not None
            and self._link.frigate_mac is not None
            and self._link.frigate_mac != mac
        )
        doubted = state is LinkState.NEEDS_RESET and _password_in_doubt(record)
        if (foreign or doubted) and self._systemd.state(FRIGATE_UNIT) in RUNNING_STATES:
            self._systemd.stop(FRIGATE_UNIT)
        return problem

    def _apply_config(
        self, mac: str, record: CameraRecord, provisioning: _Provisioning
    ) -> str | None:
        paths = self._paths
        secret = self._proxy_secret(provisioning.relay_password)
        config = render(
            provisioning.node_id,
            provisioning.covered_zones,
            provisioning.relay_user,
            _load_existing(paths.frigate_config),
            secret,
        ).encode()
        # Staged first, so a value it refuses changes nothing, and so it can be compared.
        staged = paths.frigate_env.with_name(paths.frigate_env.name + ".new")
        try:
            write_env(staged, record.password, provisioning.relay_password, secret)
            changed = (
                _read_or_none(paths.frigate_config) != config
                or _read_or_none(paths.frigate_env) != staged.read_bytes()
            )
            if changed or self._link.frigate_mac != mac:
                # Saved first: both files could match by the next tick with Frigate still old.
                self._save_link(frigate_mac=mac, restart_owed=True)
                _write_file(paths.frigate_config, config, 0o644)
                os.replace(staged, paths.frigate_env)
                _fsync_directory(paths.frigate_env.parent)
        finally:
            staged.unlink(missing_ok=True)

        if self._link.restart_owed:
            self._systemd.restart(FRIGATE_UNIT)
            self._save_link(restart_owed=False)
            self._frigate_down_ticks = 0
            return None
        return self._keep_frigate_up()

    def _keep_frigate_up(self) -> str | None:
        """A queued start returns 0 even when its job then fails, as on a failed firewall, and a
        missing /dev/hailo0 never gets past activating. So watch what actually comes of it."""
        state = self._systemd.state(FRIGATE_UNIT)
        if state not in RUNNING_STATES:
            self._systemd.start(FRIGATE_UNIT)
        if state in RUNNING_STATES and state != "activating":
            # A real recovery has to last, or a Frigate crash-looping with short active spells
            # would never be reported.
            self._frigate_up_ticks += 1
            if self._frigate_up_ticks >= FRIGATE_DOWN_TICKS:
                self._frigate_down_ticks = 0
            return None
        self._frigate_up_ticks = 0
        self._frigate_down_ticks += 1
        if self._frigate_down_ticks < FRIGATE_DOWN_TICKS:
            return None
        if self._systemd.is_failed(FIREWALL_UNIT):
            return f"Frigate is wanted but not running: {FIREWALL_UNIT} has failed, see its journal"
        return "Frigate is wanted but not running, see its journal"

    def _proxy_secret(self, broker_password: str) -> str:
        """Derived from the broker password, which the central API also holds. With no password
        there is nothing to derive from, so a random one nobody else knows keeps Frigate's proxy
        closed. The central API cannot reach this node's Frigate then, which the state file says."""
        if broker_password:
            return proxy_secret(broker_password)
        path = self._paths.local_proxy_secret
        saved = _read_or_none(path)
        if saved is not None and _LOCAL_SECRET_RE.fullmatch(saved):
            return saved.decode("ascii").strip()
        secret = secrets.token_hex(32)
        _write_file(path, f"{secret}\n".encode(), 0o600)
        return secret

    # Frigate's log

    def _scan_frigate_log(self) -> None:
        now = self._clock().timestamp()
        checked = self._log_checked_at
        if checked is not None and 0 <= now - checked < LOG_SCAN_SECONDS:
            return
        self._log_checked_at = now
        self._log_blind = True
        started = self._frigate_log.started_at()
        if started is None:
            return  # no container or no docker: no evidence either way, so nothing changes
        if started != self._frigate_started:
            self._frigate_started = started
            self._detector_error = False
            self._detector_error_since = None
            self._log_read_at = None
        text = self._frigate_log.since_time(self._log_since(started, now))
        if text is None:
            return
        self._log_blind = False
        self._log_read_at = now
        if _HAILO_FAILURE_RE.search(text):
            self._detector_error = True
            if self._detector_error_since is None:
                self._detector_error_since = now

    def _restart_for_detector(self) -> None:
        """Nothing else would ever clear a failure. It clears when the new container is seen."""
        since = self._detector_error_since
        if since is None:
            return
        now = self._clock().timestamp()
        if now < since:  # a clock stepped back must not hold this off until it catches up
            since = self._detector_error_since = now
        if now - since < DETECTOR_ERROR_RESTART_SECONDS:
            return
        last = self._link.detector_restart_at
        if last is not None and 0 <= now - last < DETECTOR_RESTART_LIMIT_SECONDS:
            return
        if self._systemd.state(FRIGATE_UNIT) != "active":
            return
        # Saved first: a crash here must not let the limit be spent twice.
        self._save_link(detector_restart_at=now)
        self._systemd.restart(FRIGATE_UNIT)

    def _log_since(self, started: str, now: float) -> str:
        """Where to read from. A clock stepped back would hide lines, so that reads it all."""
        read = self._log_read_at
        return started if read is None or now < read else str(int(read) - _LOG_OVERLAP_SECONDS)

    # Segments

    def _record_writing(self, now: float) -> bool:
        """True only for a segment of this camera under a minute old, with room on the disk."""
        if self._camera is None:
            return False
        newest = _newest_segment_time(self._paths.frigate_cache, self._camera)
        return (
            newest is not None
            and abs(now - newest) < SEGMENT_MAX_AGE_SECONDS
            and _media_has_room(self._paths.frigate_media)
        )

    # Reporting

    def _publish(self, state, mac, record, reason) -> None:
        now = self._clock().timestamp()
        content = {
            "state": state.value,
            "mac": mac,
            "updated": int(now),
            "retry_after": _retry_after(state, record, now),
            "reason": reason,
            "detector_error": self._detector_error,
            "log_read_at": self._log_read_at,
            "record_writing": self._record_writing(now),
        }
        if (state, mac, reason) != self._published:
            _log.info("link: %s, camera %s, reason: %s", state.value, mac, reason)
            self._published = (state, mac, reason)
        try:
            _write_file(self._paths.state_file, json.dumps(content).encode(), 0o644)
        except OSError as err:
            _log.warning("state file cannot be written: %s", type(err).__name__)


def _newest_segment_time(cache: Path, camera: str) -> float | None:
    """The newest modification time among the camera's segments, including the one in use."""
    newest = None
    try:
        entries = list(os.scandir(cache))
    except OSError:
        return None
    for entry in entries:
        if not (entry.name.startswith(f"{camera}@") and entry.name.endswith(".mp4")):
            continue
        try:
            modified = entry.stat().st_mtime
        except OSError:  # Frigate moves finished segments away at any time
            continue
        if newest is None or modified > newest:
            newest = modified
    return newest


def _media_has_room(media: Path) -> bool:
    try:
        disk = os.statvfs(media)
    except OSError:
        return False
    return disk.f_blocks > 0 and disk.f_bavail / disk.f_blocks >= MEDIA_MIN_FREE_FRACTION


def _read_provisioning(paths: Paths) -> _Provisioning | None:
    failure = None
    try:
        config = yaml.safe_load(paths.bridge_config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as err:
        failure = f"bridge.yaml cannot be read: {type(err).__name__}"
    except (yaml.YAMLError, ValueError):
        # A YAML error quotes the file, and the file holds the broker password.
        failure = "bridge.yaml is not valid YAML"
    if failure is not None:
        raise ProvisioningError(failure)

    broker = config.get("broker") if isinstance(config, dict) else None
    if (
        not isinstance(broker, dict)
        or not isinstance(config.get("node_id"), str)
        or not isinstance(broker.get("username"), str)
        or not isinstance(broker.get("password"), str)
    ):
        raise ProvisioningError("bridge.yaml lacks node_id, broker.username or broker.password")
    return _Provisioning(
        node_id=config["node_id"],
        covered_zones=config.get("covered_zones"),
        relay_user=broker["username"],
        relay_password=broker["password"],
        flipped=_read_flipped(paths.node_file),
    )


def _read_flipped(node_file: Path) -> bool:
    """False unless the saved provisioning state says plainly that the camera is upside down."""
    try:
        node = json.loads(node_file.read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(node, dict) and node.get("camera_upside_down") is True


def _load_existing(path: Path):
    """The saved config, for its drawn zones. One that cannot be parsed is an error and not
    "no config", because rendering over it would lose the drawings."""
    content = _read_or_none(path)
    if content is None:
        return None
    failure = None
    try:
        return yaml.safe_load(content)
    except (yaml.YAMLError, ValueError):
        failure = "the saved Frigate config cannot be parsed, so it was left as it is"
    raise FrigateConfigError(failure)


def _read_carrier(path: Path) -> bool:
    # Reading it fails with EINVAL while the interface is down.
    try:
        return path.read_text(encoding="ascii").strip() == "1"
    except (OSError, ValueError):
        return False


def _read_or_blank(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""


def _decode_link(content: bytes) -> _Link | None:
    try:
        values = json.loads(content)
        # Every field, because a missing generation must not read as a generation of zero.
        known = _LINK_FIELDS - _LINK_FIELDS_OPTIONAL <= set(values) <= _LINK_FIELDS
        link = _Link(**values) if known else None
    except (ValueError, TypeError):
        return None
    if link is None:
        return None
    valid = (
        type(link.generation) is int
        and link.generation >= 0
        and isinstance(link.boot_id, str | None)
        and isinstance(link.carrier, bool | None)
        and isinstance(link.carrier_changes, str | None)
        and isinstance(link.ip, str | None)
        and isinstance(link.held_mac, str | None)
        and isinstance(link.frigate_mac, str | None)
        and isinstance(link.restart_owed, bool)
        and isinstance(link.dhcp_reset_owed, bool)
        and (link.detector_restart_at is None or _is_time(link.detector_restart_at))
    )
    return link if valid else None


def _is_time(value) -> bool:
    """A possible Unix time. NaN fails the range too, and a huge int compares without overflow."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value < _LATEST_TIME
    )


def _password_in_doubt(record: CameraRecord | None) -> bool:
    """For a camera in NEEDS_RESET. The other way there is a limit holding back a login, which
    leaves the phase as it was and is no verdict on the saved password."""
    return (
        record is None
        or record.locked
        or not record.password
        or record.phase == LinkState.NEEDS_RESET.value
    )


def _retry_after(state: LinkState, record: CameraRecord | None, now: float) -> float | None:
    """When a 24 h limit stops holding this camera back. A locked camera waits for a reset and
    not for the rolling cap, but even a reset is still subject to the signature limit."""
    if record is None or state not in _LIMITED:
        return None
    ends = [_limit_end(record.signature_failures, SIGNATURE_FAILURE_CAP, now)]
    if not record.locked:
        ends.append(_limit_end(record.failures, FAILURE_CAP, now))
    known = [end for end in ends if end is not None]
    return max(known) if known else None


def _limit_end(stamps: tuple[float, ...], cap: int, now: float) -> float | None:
    recent = sorted(stamp for stamp in stamps if stamp > now - FAILURE_WINDOW_SECONDS)
    return recent[-cap] + FAILURE_WINDOW_SECONDS if len(recent) >= cap else None


def _without_secrets(reason: str, record, provisioning: _Provisioning) -> str:
    """render quotes zone names and node ids it refuses, and a password can be pasted into the
    wrong field. Only these messages quote a setting, so only they go through this. A very short
    password can hide one for nothing, and the text then still says what failed. render quotes
    with repr, which escapes backslashes and quotes, so those escapes are compared away."""
    hidden = [record.password, record.pending_password, provisioning.relay_password]
    if provisioning.relay_password:
        hidden.append(proxy_secret(provisioning.relay_password))
    unescaped = reason.replace("\\", "")
    shown = any(
        value and (value in reason or repr(value)[1:-1].replace("\\", "") in unescaped)
        for value in hidden
    )
    return _HIDDEN_REASON if shown else reason


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_file(path: Path, content: bytes, mode: int) -> None:
    """Replace ``path`` whole, with exactly ``mode`` whatever the umask."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            os.fchmod(fd, mode)
            file.write(content)
            file.flush()
            os.fsync(fd)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _say(line: str) -> None:
    print(line, flush=True)
