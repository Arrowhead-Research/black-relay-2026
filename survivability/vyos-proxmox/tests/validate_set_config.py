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
    if len(lines) != 159:
        fail(f"expected 159 commands from the routed VLAN baseline, got {len(lines)}")
    if len(lines) != len(set(lines)):
        fail("rendered configuration contains duplicate commands")

    normalized = "\n".join(lines) + "\n"
    expected_digest = (
        "d146e22e00e7b2e6d228669399d09cfd5264099e9403879d5ba29fd882e4d0fe"
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
        "set interfaces ethernet eth0 vif 200 address '10.73.200.1/24'",
        "set interfaces ethernet eth0 vif 210 address '10.73.210.1/29'",
        "set nat source rule 200 source address '10.73.200.0/24'",
        "set nat source rule 200 outbound-interface name 'eth1'",
        "set nat source rule 200 translation address 'masquerade'",
        "set nat source rule 210 source address '10.73.210.2/32'",
        "set nat source rule 210 outbound-interface name 'eth1'",
        "set nat source rule 210 translation address 'masquerade'",
        "set protocols static route 100.64.0.0/10 next-hop 10.73.210.2",
        "set service dhcp-server shared-network-name ISOLATED-TEST-VLAN-200 subnet 10.73.200.0/24 option default-router '10.73.200.1'",
        "set service dhcp-server shared-network-name ISOLATED-TEST-VLAN-200 subnet 10.73.200.0/24 option name-server '10.73.200.1'",
        "set service dns forwarding allow-from '10.73.200.0/24'",
        "set service dns forwarding listen-address '10.73.200.1'",
        "set firewall ipv4 input filter rule 20000 destination port '67'",
        "set firewall ipv4 input filter rule 20010 destination port '53'",
        "set firewall ipv4 input filter rule 20090 action 'drop'",
        "set firewall ipv4 input filter rule 21000 inbound-interface name 'eth0.210'",
        "set firewall ipv4 input filter rule 21000 source address '10.73.210.2/32'",
        "set firewall ipv4 input filter rule 21000 protocol 'icmp'",
        "set firewall ipv4 input filter rule 21090 inbound-interface name 'eth0.210'",
        "set firewall ipv4 input filter rule 21090 action 'drop'",
        "set firewall ipv4 forward filter rule 18000 state established",
        "set firewall ipv4 forward filter rule 18000 state related",
        "set firewall ipv4 forward filter rule 18010 inbound-interface name 'eth0.210'",
        "set firewall ipv4 forward filter rule 18010 outbound-interface name 'eth0.200'",
        "set firewall ipv4 forward filter rule 18010 source address '100.64.0.0/10'",
        "set firewall ipv4 forward filter rule 18010 destination address '10.73.200.0/24'",
        "set firewall ipv4 forward filter rule 18020 inbound-interface name 'eth0.200'",
        "set firewall ipv4 forward filter rule 18020 destination address '100.64.0.0/10'",
        "set firewall ipv4 forward filter rule 18030 inbound-interface name 'eth0.210'",
        "set firewall ipv4 forward filter rule 18030 source address '100.64.0.0/10'",
        "set firewall ipv4 forward filter rule 18040 source address '10.73.210.2/32'",
        "set firewall ipv4 forward filter rule 18040 outbound-interface name 'eth1'",
        "set firewall ipv4 forward filter rule 18090 inbound-interface name 'eth0.210'",
        "set firewall ipv4 forward filter rule 18090 action 'drop'",
        "set firewall ipv4 forward filter rule 20000 state established",
        "set firewall ipv4 forward filter rule 20000 state related",
        "set firewall ipv4 forward filter rule 20100 destination address '10.0.0.0/8'",
        "set firewall ipv4 forward filter rule 20110 destination address '172.16.0.0/12'",
        "set firewall ipv4 forward filter rule 20120 destination address '192.168.0.0/16'",
        "set firewall ipv4 forward filter rule 20200 source address '10.0.0.0/8'",
        "set firewall ipv4 forward filter rule 20210 source address '172.16.0.0/12'",
        "set firewall ipv4 forward filter rule 20220 source address '192.168.0.0/16'",
        "set firewall ipv4 forward filter rule 20300 outbound-interface name 'eth0.200'",
        "set firewall ipv4 forward filter rule 20300 action 'drop'",
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
        if (
            line.startswith("set nat source rule ")
            and "source address '100.64.0.0/10'" in line
        ):
            fail(f"tailnet client addresses must not be translated: {line}")

    expected_roots = {
        "firewall",
        "interfaces",
        "nat",
        "protocols",
        "service",
        "system",
    }
    if roots != expected_roots:
        fail(f"expected top-level paths {sorted(expected_roots)}, got {sorted(roots)}")

    print(f"validated {len(lines)} unique VyOS migration commands")


if __name__ == "__main__":
    main()
