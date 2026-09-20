# Agent Instructions

## Purpose

This workspace defines the Survivability project's VyOS virtual machine
configuration for a Proxmox node. Keep VM lifecycle concerns distinct from the
configuration applied inside VyOS, and document that boundary before adding
automation.

## Credential and authority boundary

The Pi container is an untrusted development environment. It may create and
validate configuration, documentation, tests, and non-sensitive fixtures, but
it must not receive authority over a live Proxmox node or VyOS router.

- Never request, mount, read, or persist Proxmox API tokens, passwords, session
  cookies, SSH agents, SSH private keys, or decrypted secrets.
- Never connect to, inspect, modify, reboot, or deploy to a live Proxmox or
  VyOS system from Pi.
- Never run an authoritative plan or apply against live infrastructure from Pi.
- Use placeholders and documented variable names for addresses, credentials,
  keys, and other deployment-specific values.
- Keep generated private keys, exported router configurations, state files, and
  packet captures out of version control.
- Clearly label commands that require a trusted operator workstation.

## Engineering rules

- Prefer declarative, reviewable, and repeatable configuration over console
  instructions.
- Preserve management access and default-deny boundaries when proposing
  firewall or routing changes.
- Require explicit operator review for changes that can interrupt routing,
  management access, or the Proxmox bridge.
- Add validation that executes or parses the configuration rather than tests
  that only assert on source text.
- Keep the first implementation small; do not choose an automation stack until
  the topology and ownership boundaries justify it.
