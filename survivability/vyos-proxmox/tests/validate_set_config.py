#!/usr/bin/env python3
"""Perform lightweight structural validation of rendered VyOS commands."""

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
    if len(lines) != 55:
        fail(f"expected 55 commands from the DHCP migration, got {len(lines)}")
    if len(lines) != len(set(lines)):
        fail("rendered configuration contains duplicate commands")

    normalized = "\n".join(lines) + "\n"
    expected_digest = (
        "6f1895e446fb9488920263b4974381cab2159d6d12cba21049935543af9a3f7e"
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

    required_commands = {
        "delete interfaces ethernet eth1 address '10.73.66.50/24'",
        "delete protocols static route 0.0.0.0/0 next-hop 10.73.66.1",
        "delete service ssh listen-address '10.73.66.50'",
        "set interfaces ethernet eth1 address 'dhcp'",
        "set service ssh disable-password-authentication",
    }
    missing_commands = required_commands.difference(lines)
    if missing_commands:
        fail(f"missing DHCP migration commands: {sorted(missing_commands)}")

    forbidden_prefixes = (
        "set protocols static route 0.0.0.0/0 ",
        "set service ssh listen-address ",
    )
    for line in lines:
        if line.startswith(forbidden_prefixes):
            fail(f"static WAN state remains in desired configuration: {line}")

    expected_roots = {"interfaces", "nat", "protocols", "service", "system"}
    if roots != expected_roots:
        fail(f"expected top-level paths {sorted(expected_roots)}, got {sorted(roots)}")

    print(f"validated {len(lines)} unique VyOS migration commands")


if __name__ == "__main__":
    main()
