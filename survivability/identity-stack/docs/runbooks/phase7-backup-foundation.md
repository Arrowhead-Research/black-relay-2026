# Phase 7: backup foundation

Status: pending trusted-operator execution. The implementation, tests, and
this runbook are merged; a trusted operator must complete the sequence below
and record completion in `ARCHITECTURE_ROADMAP.md` and `AGENTS.md`.

Phase 7 delivers the backup foundation on top of the protected BX11 Storage
Box provisioned in Phase 3:

- OpenTofu adds a home-scoped `hcloud_storage_box_subaccount` for restic over
  the always-on SFTP port 22. Optional interactive SSH on port 23, Samba,
  WebDAV, and ZFS snapshot browsing stay disabled on the BX11.
- The `backup_restic` Ansible role renders root-only restic credentials from
  the committed SOPS-encrypted secrets file, installs the
  `survivability-backup` wrapper, and schedules the daily `02:00 UTC` backup
  timer and the weekly Monday `03:30 UTC` verification timer.
- The daily job stages consistent SQLite copies transactionally, runs
  `restic backup`, and pings a Healthchecks.io check for start/success/failure
  signals.
- The weekly job verifies repository data and the 26-hour freshness objective.
  Retention (7 daily / 5 weekly / 12 monthly) is a short, explicit operator
  command requiring an inventory-specific confirmation before `forget --prune`.
- The isolated restore test runs only on a disposable server, restores into a
  temporary directory, verifies restored configuration, backup cryptographic
  material, and any staged SQLite copies, and requires an exact confirmation.

All commands below run on a **trusted operator workstation only**. Never run
this procedure in Pi or GitHub Actions. Pi may run only `make lint` and
`make validate`.

## 1. Generate the dedicated backup SSH key

**Location: trusted operator workstation only.**

Create one project-specific, non-interactive key for the backup subaccount.
Do not reuse the operator or Ansible keys:

```bash
umask 077
ssh-keygen \
  -t ed25519 \
  -N '' \
  -C 'survivability-backup' \
  -f "$HOME/.ssh/survivability-backup"
chmod 0600 "$HOME/.ssh/survivability-backup"
chmod 0644 "$HOME/.ssh/survivability-backup.pub"
```

The private key is deployed to the VPS root-only through SOPS in Step 3.
Keep the workstation copy for key rotation; the key never belongs in this
repository outside SOPS.

## 2. Create the Healthchecks.io checks

**Location: trusted operator workstation only.**

Create two checks in the project Healthchecks.io account with email alerts
enabled:

- `survivability-backup` with period **1 day** and grace **2 hours**. This
  yields the roadmap's 26-hour staleness alert when no successful backup ping
  arrives.
- `survivability-backup-maintenance` with period **7 days** and grace
  **26 hours**.

Record both ping URLs for Step 3. They are secrets: anyone holding a URL can
signal the check.

## 3. Create the SOPS-encrypted backup secrets file

**Location: trusted operator workstation only.**

From the repository root, create the committed encrypted file using the
placeholder names in
`ansible/inventory/production/group_vars/all/secrets.example.yml`:

```bash
export SOPS_AGE_KEY_FILE="/ABSOLUTE/PATH/TO/OPERATOR_AGE_KEY"
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Fill four values:

- `backup_restic_repository_password`: a generated restic repository password
  of at least 20 characters. Make an offline recovery copy.
- `backup_restic_ssh_private_key`: the contents of
  `~/.ssh/survivability-backup` from Step 1 (block scalar).
- `backup_restic_healthchecks_backup_url` and
  `backup_restic_healthchecks_maintenance_url`: the ping URLs from Step 2.

Verify the file decrypts, then commit it encrypted:

```bash
sops -d ansible/inventory/production/group_vars/all/secrets.sops.yml >/dev/null
git add ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Never paste decrypted values into chat, tickets, or Pi.

## 4. Apply the OpenTofu slice

**Location: trusted operator workstation only. One operator at a time.**

Generate the subaccount password in trusted secret storage (at least 20
characters), then load the OpenTofu environment as in Phase 3 and add:

```bash
export TF_VAR_storage_box_subaccount_password="$STORAGE_BOX_SUBACCOUNT_PASSWORD"
```

In the private `tofu/production.tfvars`, set the following **temporary**
bootstrap values:

```hcl
server_backups_enabled = true
storage_box_bootstrap_external_reachability = true
```

This is the only point at which the Storage Box is reachable from the operator
workstation. Review and apply the bootstrap plan:

```bash
tofu -chdir=tofu plan -var-file=tofu/production.tfvars -out=phase7-bootstrap.tfplan
tofu -chdir=tofu apply phase7-bootstrap.tfplan
tofu -chdir=tofu output -raw storage_box_subaccount_username
tofu -chdir=tofu output -raw storage_box_subaccount_server
```

The acceptable bootstrap plan contains only: the new
`hcloud_storage_box_subaccount.restic` resource, temporary external
reachability on the protected BX11/subaccount, and the server `backups` flag
change. It must not replace or destroy the server, Primary IPv4, firewall, DNS
records, or Storage Box.

Record the printed subaccount username and endpoint for Step 5. Do not deploy
restic or initialize its repository until Step 7 closes external reachability.

## 5. Pin the Storage Box in the external inventory

**Location: trusted operator workstation only.**

Add the three placeholders from `hosts.example.yml` to the protected
`~/.config/survivability/production-hosts.yml`:

- `backup_restic_storage_box_server` (the endpoint from Step 4);
- `backup_restic_storage_box_subaccount_username` (the username from Step 4);
- `backup_restic_storage_box_known_hosts` with the pinned host key.

Pin the host key with ssh-keyscan against SFTP port 22, then compare its
fingerprint with Hetzner's published Storage Box **ED25519** fingerprint
`SHA256:XqONwb1S0zuj5A1CDxpOSuD2hnAArV1A3wKY7Z3sdgM` before trusting it:

```bash
STORAGE_BOX_SERVER="$(tofu -chdir=tofu output -raw storage_box_subaccount_server)"
ssh-keyscan -p 22 -t ed25519 "${STORAGE_BOX_SERVER}" 2>/dev/null | tee /tmp/storage-box-host-key
ssh-keygen -lf /tmp/storage-box-host-key
```

Keep the resulting `host ssh-ed25519 AAAA...` line, exactly as printed, as
the single `backup_restic_storage_box_known_hosts` entry. Delete the temporary
file after the comparison. Do not treat ssh-keyscan alone as verification.

## 6. Authorize the backup key on the subaccount

**Location: trusted operator workstation only. One-time per key.**

The provider cannot manage Storage Box SSH keys. SFTP port 22 requires an
RFC4716 public-key file, while restic continues to use the original OpenSSH
private key. Create the converted public file and upload it using the
subaccount password from trusted secret storage:

```bash
BACKUP_IDENTITY="$HOME/.ssh/survivability-backup"
STORAGE_BOX_SERVER="REPLACE_WITH_STORAGE_BOX_FQDN"
STORAGE_BOX_USERNAME="REPLACE_WITH_SUBACCOUNT_USERNAME"

ssh-keygen -e -f "${BACKUP_IDENTITY}.pub" >"${BACKUP_IDENTITY}.rfc4716.pub"
sftp -P 22 "${STORAGE_BOX_USERNAME}@${STORAGE_BOX_SERVER}"
```

At the interactive `sftp>` prompt, authenticate with the subaccount password,
then run:

```text
mkdir .ssh
put /ABSOLUTE/PATH/TO/survivability-backup.rfc4716.pub .ssh/authorized_keys
quit
```

Remove the temporary RFC4716 file from the workstation after verifying the
first backup. Only the public key is transferred. For rotation, download the
existing `authorized_keys`, append the converted new key, upload it, test the
new key, then remove the old key in a second reviewed change.

## 7. Close external Storage Box reachability

**Location: trusted operator workstation only. One operator at a time.**

Before deploying restic, set the private OpenTofu variables back to their
steady-state value:

```hcl
storage_box_bootstrap_external_reachability = false
```

Review the second plan. It must contain only the in-place reachability change
from external to Hetzner-network-only access on the BX11 and its restic
subaccount:

```bash
tofu -chdir=tofu plan -var-file=tofu/production.tfvars -out=phase7-private.tfplan
tofu -chdir=tofu apply phase7-private.tfplan
unset TF_VAR_storage_box_subaccount_password
```

The production VPS must prove SFTP access in Step 8. If it cannot, do not leave
external reachability enabled as a workaround; investigate the Hetzner network
path and the reviewed configuration first.

## 8. Deploy and initialize

**Location: trusted operator workstation only.**

For the verification commands below, set the named operator and production
address from the external inventory:

```bash
OPERATOR_USER="REPLACE_WITH_NAMED_OPERATOR"
PRODUCTION_HOST="REPLACE_WITH_PRODUCTION_HOST"
```

Run check mode first with the exact initialization confirmation, then apply
with that same confirmation. This is the only time a missing repository may be
created; normal convergence fails closed if it later disappears. For the
committed `identity_production` inventory hostname, the confirmation is
`INITIALIZE_RESTIC_identity_production`.

```bash
INIT_CONFIRMATION="INITIALIZE_RESTIC_identity_production"

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --check --diff \
  --extra-vars "backup_restic_init_repository=true backup_restic_init_confirmation=${INIT_CONFIRMATION}" \
  ansible/playbooks/site.yml

ansible-playbook \
  -i "$ANSIBLE_INVENTORY" \
  --extra-vars "backup_restic_init_repository=true backup_restic_init_confirmation=${INIT_CONFIRMATION}" \
  ansible/playbooks/site.yml

unset INIT_CONFIRMATION
ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

The final run must report no backup changes. Verify the timers:

```bash
ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo systemctl list-timers survivability-backup\\*'
```

## 9. Run and verify the first backup

**Location: trusted operator workstation only.**

Trigger the daily job manually and watch its output:

```bash
ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo systemctl start survivability-backup.service; \\
   sudo journalctl -u survivability-backup.service -n 50 --no-pager'

ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo /usr/local/sbin/survivability-backup run snapshots'
```

Confirm in the Healthchecks.io dashboard that the backup check received the
`/start` and success pings for this run.

## 10. Apply confirmed retention when due

**Location: trusted operator workstation only.**

The weekly timer verifies the repository but never deletes snapshots. Run this
short command during a monthly maintenance window (and after the first week in
service) to apply the 7 daily / 5 weekly / 12 monthly policy. Review
`--dry-run` first; the second command requires the exact confirmation and runs
`forget --prune`:

```bash
PRUNE_CONFIRMATION="PRUNE_RESTIC_identity_production"

ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  "sudo /usr/local/sbin/survivability-backup run forget --dry-run --keep-daily 7 --keep-weekly 5 --keep-monthly 12"

ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  "sudo /usr/local/sbin/survivability-backup prune '${PRUNE_CONFIRMATION}'"

unset PRUNE_CONFIRMATION
```

Do not automate this confirmation or place it in a timer, environment file, or
SOPS secret. It is intentionally a visible operator decision.

## 11. Test the failure signal

**Location: trusted operator workstation only.**

Confirm the failure path alerts. Ping the failure endpoint once from the
workstation using the backup check URL:

```bash
curl --max-time 10 "https://hc-ping.com/REPLACE_WITH_BACKUP_CHECK_UUID/fail"
```

Healthchecks.io must send the failure alert email, and the check must show a
failure. The next successful scheduled ping clears the state.

## 12. Test the staleness alert

**Location: trusted operator workstation only.**

In the Healthchecks.io dashboard, temporarily set the backup check period to
**1 minute** (keep the grace at a few minutes), wait for the staleness alert
email, then restore the period to **1 day** with **2 hours** grace. Confirm
the check returns to the healthy state after the next successful ping.

## 13. Run the isolated restore test

**Location: trusted operator workstation only.**

Run the guarded script with the promoted snapshot, the registered Hetzner SSH
key, and the dedicated Ansible identity:

```bash
HCLOUD_TOKEN="$HCLOUD_TOKEN" \
SOPS_AGE_KEY_FILE="$SOPS_AGE_KEY_FILE" \
ANSIBLE_INVENTORY="$HOME/.config/survivability/production-hosts.yml" \
./scripts/operator/test-backup-restore.sh \
  --snapshot-id "$(cat packer-output/promoted-snapshot-id)" \
  --ssh-key "REPLACE_WITH_REGISTERED_HETZNER_SSH_KEY_NAME" \
  --identity-file "$HOME/.ssh/survivability-ansible"
```

The script creates a disposable CX23 labeled with owner and expiry, injects
the dedicated Ansible key through cloud-init, runs the
`phase7-restore-test.yml` playbook with its exact confirmation, verifies the
restored files and repository integrity, records the duration in
`operator-output/phase7-restore-test.json`, and always deletes the server.
The restore must complete well within the four-hour objective; on failure,
inspect the Ansible output and rerun after correcting the cause.

Never point the restore playbook at the production host. It restores only
into `/var/tmp/survivability-restore-test`, but running it against production
would still exercise the backup credentials unnecessarily.

## 14. Record completion

Rerun `site.yml` once more and require no changes. When every step above has
passed, update `ARCHITECTURE_ROADMAP.md` (Phase 7 status and the current
phase notes in `AGENTS.md` and `README.md`) to record operator completion, as
done for Phase 6.

## Failure and recovery

- **Repository lock (restic exit 11):** wait for the `--retry-lock` window;
  run `sudo /usr/local/sbin/survivability-backup run unlock` only after
  verifying no backup is genuinely active.
- **Storage Box unreachable:** check the pinned known_hosts entry and Hetzner
  Console; the host firewall does not restrict outbound SFTP port 22.
- **Wrong repository password (restic exit 12):** correct the SOPS value and
  rerun; a wrong password can render the repository unreadable, so verify
  before the first backup whenever the secret is regenerated.
- **Backup key rotation:** generate a new key, authorize it on the subaccount
  (Step 6), update the SOPS value, apply, and only then remove the old key
  from the subaccount. Never remove the currently authorized key first.
- **No recent snapshot:** the weekly verification job fails (and pings
  `/fail`) when the newest snapshot is older than 26 hours.
- **Missing repository:** normal `site.yml` convergence fails closed. Do not
  recreate it casually; inspect the Storage Box, credentials, and recovery
  evidence before using the one-time initialization confirmation again.

Hetzner server backups (enabled in Step 4) remain a secondary rollback path;
restic on the BX11 remains the authoritative backup.
