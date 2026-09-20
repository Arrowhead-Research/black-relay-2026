#!/usr/bin/env python3
"""Perform lightweight structural validation of rendered VyOS set commands."""

from __future__ import annotations

import hashlib
import shlex
import sys


def fail(message: str) -> None:
    print(f"validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    lines = [line.strip() for line in sys.stdin if line.strip()]
    if not lines:
        fail("rendered configuration is empty")
    if len(lines) != 54:
        fail(f"expected 54 commands from the key-only baseline, got {len(lines)}")
    if len(lines) != len(set(lines)):
        fail("rendered configuration contains duplicate commands")

    normalized = "\n".join(lines) + "\n"
    expected_digest = (
        "69640fb3591c40c060b7bab3fbcb5da8caa81779643feb340e5d84ca0b02d9ba"
    )
    actual_digest = hashlib.sha256(normalized.encode()).hexdigest()
    if actual_digest != expected_digest:
        fail(f"rendered commands differ from the captured baseline: {actual_digest}")

    roots: set[str] = set()
    for number, line in enumerate(lines, start=1):
        if "{{" in line or "}}" in line:
            fail(f"line {number} contains an unresolved template expression")
        try:
            words = shlex.split(line)
        except ValueError as error:
            fail(f"line {number} is not valid shell-like syntax: {error}")
        if len(words) < 3 or words[0] not in {"set", "delete"}:
            fail(f"line {number} is not a VyOS set/delete command: {line}")
        roots.add(words[1])

    required_command = "set service ssh disable-password-authentication"
    if required_command not in lines:
        fail(f"missing key-only SSH command: {required_command}")

    expected_roots = {"interfaces", "nat", "protocols", "service", "system"}
    if roots != expected_roots:
        fail(f"expected top-level paths {sorted(expected_roots)}, got {sorted(roots)}")

    print(f"validated {len(lines)} unique VyOS set commands")


if __name__ == "__main__":
    main()
