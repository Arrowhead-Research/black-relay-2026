"""Replacing a file so that a power cut leaves the old one or the new one, never a mixture."""

import os
from pathlib import Path


def write_file(path: Path, content: bytes, mode: int) -> None:
    """Replace ``path`` whole, with exactly ``mode`` whatever the umask, and whatever mode a
    temporary file left by a crash has."""
    temporary = path.with_name(f"{path.name}.tmp")
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
    fsync_directory(path.parent)


def fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
