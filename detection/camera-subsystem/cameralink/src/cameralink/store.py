"""The node's durable memory of its cameras: passwords, attempts and finished steps.

One file, root-only, replaced atomically, holding one record per MAC address. Records
of earlier cameras stay, because a camera swapped back still has its own password and
its own failure count.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


class StoreError(Exception):
    """The file exists and cannot be read. Never treated as "no record", because that
    would forget the attempts already spent on the camera."""


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
    pending_rejected: bool = False  # the camera refused pending_password, so skip to blank
    locked: bool = False  # no more attempts until a generation shows the factory signature
    settle_key: str | None = None  # "<mac>/<link generation>" the settle time belongs to
    settle_since: float | None = None  # epoch time a port first accepted in that generation


class CameraStore:
    def __init__(self, path: Path):
        self._path = Path(path)

    def load(self, mac: str | None = None) -> CameraRecord | None:
        """The record for ``mac``, or by default for the camera saved last."""
        data = self._read()
        if data is None:
            return None
        fields = data["cameras"].get(data["current"] if mac is None else mac)
        return None if fields is None else _record(fields)

    def save(self, record: CameraRecord) -> None:
        data = self._read() or {"cameras": {}}
        data["current"] = record.mac
        data["cameras"][record.mac] = asdict(record)
        self._replace(json.dumps(data).encode())

    def _read(self) -> dict | None:
        failure = None
        try:
            data = json.loads(self._path.read_bytes())
            # Every record is decoded, so that damage is found now and not at a swap.
            for fields in data["cameras"].values():
                _record(fields)
            data["current"]
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, AttributeError):
            # Raised below: the file's content must not ride along as exception context.
            failure = StoreError(f"{self._path} is corrupt")
        if failure is not None:
            raise failure
        return data

    def _replace(self, content: bytes) -> None:
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            os.fchmod(fd, 0o600)  # a leftover temporary file may have any mode
            file.write(content)
            file.flush()
            os.fsync(fd)
        os.replace(temporary, self._path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _record(fields: dict) -> CameraRecord:
    return CameraRecord(**{**fields, "failures": tuple(fields.get("failures", ()))})
