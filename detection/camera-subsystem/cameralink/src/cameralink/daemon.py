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

Errors from the camera, the store, render and systemd end the tick's work and are reported. Any
other exception is a bug and is left to crash into ``Restart=always``. The state file lives in the
service's runtime directory, so it goes too, and nothing can read a stale state as current.
"""

import ipaddress
import json
import logging
import os
import re
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
FRIGATE_UNIT = "frigate.service"
DHCP_UNIT = "camera-link-dhcp.service"

_SYSTEMCTL = "/usr/bin/systemctl"
_SYSTEMCTL_TIMEOUT = 30
_RUNNING = frozenset({"active", "activating", "reloading", "refreshing"})
_NOT_RUNNING = frozenset({"inactive", "failed", "deactivating"})
_RUNNABLE = frozenset({LinkState.ADOPTED, LinkState.ADOPTED_PENDING_SETTINGS})
_LIMITED = frozenset(
    {LinkState.ADOPTED_PENDING_SETTINGS, LinkState.NEEDS_RESET, LinkState.FAILED}
)
_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
# Frigate's files are from a camera nobody can name now, so any camera differs from it.
_UNKNOWN_MAC = "unknown"
_HIDDEN_REASON = "a setting was rejected, and its message would have shown a secret"


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


class Systemctl:
    """Frigate's container can take a minute to stop, and a tick must not wait for it, so all
    but ``stop_and_wait`` only queue the job. A failed start is Restart=always's to retry."""

    def __init__(self, run=subprocess.run):
        self._run = run

    def is_running(self, unit: str) -> bool:
        state = self._systemctl("is-active", unit).stdout.strip()
        if state in _RUNNING:
            return True
        if state in _NOT_RUNNING:
            return False
        raise SystemdError(f"{unit}: unexpected state from systemctl")

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


@dataclass(frozen=True)
class _Link:
    """What must outlive a restart of the daemon or the node. Holds no secret."""

    generation: int = 0
    boot_id: str | None = None
    carrier: bool | None = None
    carrier_changes: str | None = None  # the kernel's count, which sees a flap between ticks
    ip: str | None = None  # the last lease's IP, kept while there is no lease
    frigate_mac: str | None = None  # the camera whose password is in Frigate's env file
    restart_owed: bool = False  # the files are ahead of the running Frigate
    dhcp_reset_owed: bool = False  # the lease file may describe a camera that has gone


_LINK_FIELDS = {item.name for item in fields(_Link)}


@dataclass(frozen=True)
class _Provisioning:
    node_id: str
    covered_zones: object  # checked by render
    relay_user: str
    relay_password: str
    flipped: bool

    def __repr__(self) -> str:
        return f"_Provisioning({self.node_id!r})"


def read_lease(path: Path, now: float) -> Lease | None:
    """The newest lease in a dnsmasq lease file that has not ended. Lines that make no sense
    are skipped. An expiry of 0 is how dnsmasq writes a lease that never ends."""
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
        if expiry <= now or not _MAC_RE.fullmatch(mac) or not _is_ipv4(fields[2]):
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
    ):
        self._paths = paths
        self._systemd = systemd
        self._step = step_fn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._store = CameraStore(paths.store)  # one per thread: its lock is per object
        self._link = self._load_link()
        self._published = None

    def run(self, stop) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(TICK_SECONDS)

    def tick(self) -> None:
        problems = []
        try:
            lease = self._observe_link(problems)
        except LinkStateError as err:
            self._publish(LinkState.FAILED, None, None, str(err), None)
            return

        provisioning = None
        try:
            provisioning = _read_provisioning(self._paths)
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
            problem = self._drive_frigate(state, mac, record, provisioning, bool(problems))
        except (SystemdError, LinkStateError) as err:
            problem = str(err)
        if problem:
            problems.append(problem)
        if state is LinkState.NEEDS_RESET:
            problems.append("the camera needs a factory reset")
        self._publish(state, mac, record, problems[0] if problems else None, provisioning)

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
            restart_owed=True,
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
                dhcp_reset_owed=link.dhcp_reset_owed or booted or (changed and carrier),
            )
        elif flaps != link.carrier_changes:
            self._save_link(carrier_changes=flaps)
        if not carrier:
            return None
        if self._link.dhcp_reset_owed:
            self._reset_dhcp(problems)
            return None

        lease = read_lease(self._paths.lease_file, self._clock().timestamp())
        if lease is not None and lease.ip != self._link.ip:
            bump = 1 if self._link.ip is not None else 0
            self._save_link(generation=self._link.generation + bump, ip=lease.ip)
        return lease

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
        has_problem: bool,
    ) -> str | None:
        problem = None
        if state in _RUNNABLE and not has_problem:
            if provisioning is None:
                problem = "not provisioned yet, so Frigate is not started"
            else:
                try:
                    self._apply_config(mac, record, provisioning)
                    return None
                except FrigateConfigError as err:
                    problem = str(err)
                except OSError as err:
                    problem = f"Frigate's files cannot be written: {type(err).__name__}"

        foreign = (
            mac is not None
            and self._link.frigate_mac is not None
            and self._link.frigate_mac != mac
        )
        unreachable = state is LinkState.NEEDS_RESET and (record is None or not record.password)
        if (foreign or unreachable) and self._systemd.is_running(FRIGATE_UNIT):
            self._systemd.stop(FRIGATE_UNIT)
        if foreign:
            self._save_link(frigate_mac=None)
        return problem

    def _apply_config(
        self, mac: str, record: CameraRecord, provisioning: _Provisioning
    ) -> None:
        paths = self._paths
        secret = proxy_secret(provisioning.relay_password)
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
        elif not self._systemd.is_running(FRIGATE_UNIT):
            self._systemd.start(FRIGATE_UNIT)

    # Reporting

    def _publish(self, state, mac, record, reason, provisioning) -> None:
        now = self._clock().timestamp()
        reason = _without_secrets(reason, record, provisioning)
        content = {
            "state": state.value,
            "mac": mac,
            "updated": int(now),
            "retry_after": _retry_after(state, record, now),
            "reason": reason,
        }
        if (state, mac, reason) != self._published:
            _log.info("link: %s, camera %s, reason: %s", state.value, mac, reason)
            self._published = (state, mac, reason)
        try:
            _write_file(self._paths.state_file, json.dumps(content).encode(), 0o644)
        except OSError as err:
            _log.warning("state file cannot be written: %s", type(err).__name__)


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
        link = _Link(**values) if set(values) == _LINK_FIELDS else None
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
        and isinstance(link.frigate_mac, str | None)
        and isinstance(link.restart_owed, bool)
        and isinstance(link.dhcp_reset_owed, bool)
    )
    return link if valid else None


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


def _without_secrets(reason, record, provisioning):
    """render quotes the values it refuses, and a password can be pasted into the wrong field."""
    if reason is None:
        return None
    secrets = []
    if record is not None:
        secrets += [record.password, record.pending_password]
    if provisioning is not None:
        secrets += [provisioning.relay_password, proxy_secret(provisioning.relay_password)]
    return _HIDDEN_REASON if any(s and s in reason for s in secrets) else reason


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
