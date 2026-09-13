# Phase 6: guarded host firewall

Status: complete. The trusted operator activated the policy, confirmed the
fresh SSH proof and Docker-DNAT boundary, and obtained a final no-change
`site.yml` run. The automatic rollback was cancelled only after verification.

The semantic `host_firewall` role adds an nftables layer behind the existing
protected Hetzner Firewall. It is part of normal `ansible/playbooks/site.yml`
convergence and manages only the `inet survivability_filter` table. It never
flushes Docker's tables or the complete nftables ruleset.

The policy:

- drops inbound traffic by default;
- permits loopback and established/related traffic;
- permits required IPv4 ICMP;
- permits public TCP 22, 80, and 443 only;
- drops IPv6 traffic in the managed table because IPv6 is deliberately disabled;
- allows outbound host traffic;
- defaults non-DNAT forwarding to deny;
- allows containers to initiate outbound traffic while retaining Docker's later
  bridge-isolation processing; and
- permits Docker destination NAT only when the original public TCP destination
  was 80 or 443, preventing a published backend port from bypassing host input
  policy.

The role also fails unless Docker still has `icc=false` and
`userland-proxy=false`.

## Activation safety

A changed, missing, or inactive policy requires the exact confirmation
`APPLY_HOST_FIREWALL_<inventory hostname>`. An unchanged active policy does not,
so this is not a recurring flag for normal convergence.

Before activation, Ansible validates the candidate with `nft --check`, preserves
the active policy, and schedules an automatic rollback for 120 seconds. It then
loads the candidate atomically, closes its existing SSH control connection, and
proves a fresh named-operator connection. Only after that succeeds does it
persist the policy, enable `nftables.service`, and cancel rollback.

If fresh SSH fails, the timer restores the last-known-good policy. During first
activation, where no prior host policy exists, rollback removes only the
`survivability_filter` table. The independently managed Hetzner Firewall remains
attached throughout.

## Review and activate

**Location: trusted operator workstation only.**

For the committed production inventory hostname, set the exact non-secret
confirmation and use the normal declarative entry point:

```bash
CONFIRM_HOST_FIREWALL='APPLY_HOST_FIREWALL_identity_production'

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --check --diff \
  --extra-vars \
  "host_firewall_apply_confirmation=$CONFIRM_HOST_FIREWALL" \
  ansible/playbooks/site.yml

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --extra-vars \
  "host_firewall_apply_confirmation=$CONFIRM_HOST_FIREWALL" \
  ansible/playbooks/site.yml

unset CONFIRM_HOST_FIREWALL
```

The apply may pause briefly while Ansible discards its existing SSH control
connection and opens a fresh one. A successful recap confirms that rollback was
cancelled only after fresh SSH succeeded.

Run normal convergence again without confirmation:

```bash
ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

It must report no firewall changes. A future reviewed firewall-policy change
will require the same resource-specific confirmation again.

## Failure recovery

- If candidate validation fails, no runtime firewall change occurs.
- If activation or fresh SSH fails, do not reboot. Wait at least 120 seconds for
  automatic rollback, then retry normal named-operator SSH.
- If SSH returns, inspect the Ansible error, correct the policy, and rerun with
  confirmation.
- If SSH does not return, use the already-tested Hetzner console. The Hetzner
  Firewall was not changed by this role.

Do not manually flush the complete nftables ruleset and do not restart Docker as
a firewall-recovery shortcut.
