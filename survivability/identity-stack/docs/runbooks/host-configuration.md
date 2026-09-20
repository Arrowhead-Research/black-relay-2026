# Host configuration

**Location: trusted operator workstation only.**

The `host_baseline` and `host_firewall` roles are composed by
`ansible/playbooks/site.yml`. There is no separate script or sequence of host
commands, and no additional inventory values or secrets are required.

## What the roles own

`host_baseline` installs the reviewed baseline package set, applies safe package
updates, enables Debian's daily apt and unattended-upgrade timers while
prohibiting unattended reboots, makes journald persistent and bounded (14 days,
512 MiB persistent, 128 MiB runtime), and applies sysctl hardening. It reports a
pending reboot and root-filesystem free space below 15% without acting on
either.

`host_firewall` adds an nftables layer behind the protected Hetzner Firewall. It
manages only the `inet survivability_filter` table and never flushes Docker's
tables or the complete ruleset. The policy drops inbound traffic by default;
permits loopback, established/related, required IPv4 ICMP, and public TCP 22, 80
and 443; drops IPv6 in the managed table; allows outbound host traffic; and
defaults non-DNAT forwarding to deny. Docker destination NAT is permitted only
when the original public TCP destination was 80 or 443, so a published backend
port cannot bypass host input policy. The role fails unless Docker still has
`icc=false` and `userland-proxy=false`.

## Activation safety

Firewall activation is guarded structurally, not by a typed confirmation.
Ansible validates the candidate with `nft --check`, preserves the active policy,
and schedules an automatic rollback 120 seconds out. It then loads the candidate
atomically, closes its existing SSH control connection, and proves a fresh
named-operator connection. Only after that succeeds does it persist the policy,
enable `nftables.service`, and cancel the rollback.

If fresh SSH fails, the timer restores the last-known-good policy. On a first
activation, where no prior host policy exists, rollback removes only the
`survivability_filter` table. The independently managed Hetzner Firewall remains
attached throughout, so console access and the provider-level policy are never
affected by a bad host policy.

## Apply and verify

Review check mode, apply, then rerun the same entry point:

`site.yml` converges `backup_restic`, which decrypts SOPS on the controller, so
run it through the wrapper with the `--age` scope; see
[`secrets.md`](secrets.md).

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --check --diff ansible/playbooks/site.yml
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

The first apply may update packages and report that a deliberate reboot is
required. Do not reboot automatically or add a reboot task. An apply that
activates a new firewall policy pauses briefly while Ansible discards its SSH
control connection and opens a fresh one.

The final run must report no changes unless package repositories changed between
runs.

## Failure recovery

- If candidate validation fails, no runtime firewall change occurs.
- If activation or fresh SSH fails, do not reboot. Wait at least 120 seconds for
  the automatic rollback, then retry normal named-operator SSH.
- If SSH returns, inspect the Ansible error, correct the policy, and rerun.
- If SSH does not return, use the already-tested Hetzner console. This role does
  not change the Hetzner Firewall.

Do not manually flush the complete nftables ruleset, and do not restart Docker as
a firewall-recovery shortcut.
