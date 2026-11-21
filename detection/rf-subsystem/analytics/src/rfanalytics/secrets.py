"""Secrets from the environment: NAME, or the file named by NAME_FILE (Docker convention).

The central compose stack generates its passwords into files on first start
(rf-subsystem/central/bootstrap.sh) and passes their paths as NAME_FILE.
"""

from __future__ import annotations

import os


def env_secret(name: str) -> str | None:
    """``$NAME`` if set and non-empty, else the contents of ``$NAME_FILE`` (trailing newline dropped)."""
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(f"{name}_FILE")
    if not path:
        return value
    with open(path) as f:
        return f.read().rstrip("\r\n")
