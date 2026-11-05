"""The node's durable memory of its cameras: passwords, attempts and finished steps.

One file, root-only, replaced atomically, holding one record per MAC address. Records
of earlier cameras stay, because a camera swapped back still has its own password and
its own failure count.

Every writer holds ``CameraStore.lock()`` from before it reads until after it writes, and
``save`` refuses to run without it. A record also carries the revision it was read at, so
a copy read before someone else's write cannot replace that write: the password it would
erase may be the only copy of what is on the camera. Reading alone needs no lock, because
the file is only ever replaced whole.

The public methods raise only ``StoreError``. They raise it outside any ``except``, so
that a JSONDecodeError, whose ``doc`` holds the whole file, never rides along as context.
"""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

PHASES = frozenset({"adopted", "needs_reset", "failed", "http_follow_up", "try_blank"})


class StoreError(Exception):
    """The file cannot be read or written, or a write was refused. Never treated as "no
    record", because that would forget a password and the attempts spent on the camera."""


@dataclass(frozen=True)
class CameraRecord:
    """What the node knows about one camera. ``repr`` leaves out both passwords.

    ``password`` is the password last proven live, or blank if none is known. At most
    one of ``password`` and ``pending_password`` is ever set. A pending password may or
    may not be on the camera: only a login can tell, and only then does it go away.
    """

    mac: str
    password: str = field(repr=False)
    pending_password: str | None = field(repr=False)
    attempted: str | None  # "<mac>/<link generation>" of the last login attempt
    phase: str | None = None  # how that attempt stands; None while its outcome is unknown
    applied: dict = field(default_factory=dict)  # finished step name -> the value it applied
    failures: tuple[float, ...] = ()  # epoch times of failed attempts, for the rolling cap
    signature_failures: tuple[float, ...] = ()  # the same for logins on the factory signature
    pending_rejected: bool = False  # the camera refused pending_password, so skip to blank
    locked: bool = False  # no more attempts until a generation shows the factory signature
    settle_key: str | None = None  # "<mac>/<link generation>" the settle time belongs to
    settle_since: float | None = None  # monotonic time a port first accepted in that generation
    revision: int = 0  # how many times this record has been written


class CameraStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock_fd: int | None = None

    def load(self, mac: str | None = None) -> CameraRecord | None:
        """The record for ``mac``, or by default for the camera saved last."""
        data = self._read()
        if data is None:
            return None
        record = data["cameras"].get(data["current"] if mac is None else mac)
        return None if record is None else _record(record)

    def save(self, record: CameraRecord) -> CameraRecord:
        """Write ``record`` and return it as written, which is what to save next time."""
        if self._lock_fd is None:
            raise StoreError(f"{self._path}: save outside the lock")
        data = self._read() or {"cameras": {}}
        stored = data["cameras"].get(record.mac)
        if record.revision != (stored["revision"] if stored else 0):
            raise StoreError(f"{self._path}: stale record for {record.mac} refused")
        written = replace(record, revision=record.revision + 1)
        fields_to_write = asdict(written)
        _record(fields_to_write)
        data["current"] = record.mac
        data["cameras"][record.mac] = fields_to_write
        failure = None
        try:
            self._replace(json.dumps(data).encode())
        except OSError as err:
            failure = StoreError(f"{self._path} cannot be written: {type(err).__name__}")
        if failure is not None:
            raise failure
        return written

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive across processes. Blocks, and can be nested on one store object."""
        if self._lock_fd is not None:
            yield
            return
        self._lock_fd = self._acquire()
        try:
            yield
        finally:
            fd, self._lock_fd = self._lock_fd, None
            os.close(fd)  # which also releases the flock

    def _acquire(self) -> int:
        failure = None
        try:
            self._make_directory()
            lock_file = self._path.with_name(self._path.name + ".lock")
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
        except OSError as err:
            failure = StoreError(f"{self._path} cannot be locked: {type(err).__name__}")
        if failure is not None:
            raise failure
        return fd

    def _make_directory(self) -> None:
        missing = []
        directory = self._path.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        if not missing:
            return
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A new directory is only as durable as its entry in its parent.
        for created in reversed(missing):
            _fsync_directory(created.parent)

    def _read(self) -> dict | None:
        failure = None
        try:
            data = json.loads(self._path.read_bytes())
            # Every record is checked, so that damage is found now and not at a swap.
            for mac, record in data["cameras"].items():
                if record["mac"] != mac:
                    raise ValueError
            data["cameras"][data["current"]]
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            failure = StoreError(f"{self._path} is corrupt")
        except OSError as err:
            failure = StoreError(f"{self._path} cannot be read: {type(err).__name__}")
        if failure is not None:
            raise failure
        for record in data["cameras"].values():
            _record(record)
        return data

    def _replace(self, content: bytes) -> None:
        temporary = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            os.fchmod(fd, 0o600)  # a leftover temporary file may have any mode
            file.write(content)
            file.flush()
            os.fsync(fd)
        os.replace(temporary, self._path)
        _fsync_directory(self._path.parent)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_text(value) -> bool:
    return isinstance(value, str)


def _is_text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _is_flag(value) -> bool:
    return isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_stamps(value) -> bool:
    return isinstance(value, list | tuple) and all(_is_number(stamp) for stamp in value)


def _is_applied(value) -> bool:
    return isinstance(value, dict) and all(isinstance(v, bool | str) for v in value.values())


_CHECKS = {
    "mac": _is_text,
    "password": _is_text,
    "pending_password": _is_text_or_none,
    "attempted": _is_text_or_none,
    "phase": lambda value: value is None or (isinstance(value, str) and value in PHASES),
    "applied": _is_applied,
    "failures": _is_stamps,
    "signature_failures": _is_stamps,
    "pending_rejected": _is_flag,
    "locked": _is_flag,
    "settle_key": _is_text_or_none,
    "settle_since": lambda value: value is None or _is_number(value),
    "revision": lambda value: _is_number(value) and isinstance(value, int) and value >= 0,
}
_REQUIRED = {item.name for item in fields(CameraRecord)}
assert _REQUIRED == set(_CHECKS)


def _record(values: dict) -> CameraRecord:
    """Decode one record. A value of the wrong type is damage, and damage is never guessed
    around: one null password would otherwise be sent to the camera as a login. The error
    names the field and never its value."""
    failure = None
    if not isinstance(values, dict) or set(values) != _REQUIRED:
        failure = "fields"
    else:
        for name, check in _CHECKS.items():
            if not check(values[name]):
                failure = name
                break
        else:
            if values["password"] and values["pending_password"] is not None:
                failure = "password"
    if failure is not None:
        raise StoreError(f"camera record is corrupt: {failure}")
    return CameraRecord(
        **{
            **values,
            "failures": tuple(values["failures"]),
            "signature_failures": tuple(values["signature_failures"]),
        }
    )
