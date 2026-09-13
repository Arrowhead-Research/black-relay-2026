# Phase 6: declarative host baseline

Status: complete. The trusted operator applied the role and confirmed a final
no-change convergence run.

The `host_baseline` Ansible role owns routine Debian host configuration after
operator-access bootstrap. It is composed by `ansible/playbooks/site.yml`; there
is no separate production script or sequence of host commands.

The role:

- installs the reviewed baseline package set and applies safe package updates;
- enables Debian's daily apt and unattended-upgrade timers;
- explicitly prohibits unattended reboots;
- makes journald persistent and bounds it to 14 days, 512 MiB persistent, and
  128 MiB runtime storage;
- owns the Phase 6 sysctl hardening and removes the superseded gold-image file;
- reports a pending reboot in the Ansible output without performing one; and
- reports root-filesystem free space below 15% without deleting data.

Email delivery for reboot and disk-pressure conditions is assigned to Phase 11
monitoring after an external destination is approved; the conditions remain
visible during every convergence run. The guarded `host_firewall` and
Docker-DNAT policy completed Phase 6 separately. CrowdSec is assigned to Phase 8
alongside SSH/Caddy log integration.

## Apply and verify

**Location: trusted operator workstation only.**

The named operator bootstrap must already be complete. Review check mode, apply,
then rerun the same declarative entry point:

```bash
ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --check --diff \
  ansible/playbooks/site.yml

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  ansible/playbooks/site.yml

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  ansible/playbooks/site.yml
```

The first apply may update packages and report that a deliberate reboot is
required. Do not reboot automatically or add a reboot task. The final run must
report no changes unless package repositories changed between runs.

No additional inventory values or secrets are required for this slice.
