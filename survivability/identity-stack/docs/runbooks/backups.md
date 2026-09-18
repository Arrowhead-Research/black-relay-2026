# Backups

**Every command here runs on a trusted operator workstation only.** Never run
this procedure in Pi or GitHub Actions; Pi may run only `make lint` and
`make validate`.

Restic backs up to a home-scoped SFTP subaccount on the protected BX11 Storage
Box. The `backup_restic` role renders root-only credentials from SOPS, installs
the `survivability-backup` wrapper, and schedules a daily 02:00 UTC backup and a
weekly Monday 03:30 UTC verification, each signalling its own Healthchecks.io
check. Retention is 7 daily / 5 weekly / 12 monthly, applied only by an explicit
operator command.

Work through steps 1-12 in order the first time. Steps 9-12 are also the
periodic verification procedure.

## 1. Generate the dedicated backup SSH key

One project-specific, non-interactive key. Do not reuse the operator or Ansible
keys:

```bash
umask 077
ssh-keygen -t ed25519 -N '' -C 'survivability-backup' \
  -f "$HOME/.ssh/survivability-backup"
chmod 0600 "$HOME/.ssh/survivability-backup"
chmod 0644 "$HOME/.ssh/survivability-backup.pub"
```

Keep the workstation copy for rotation. The key never belongs in this repository
outside SOPS.

## 2. Create the Healthchecks.io checks

Two checks with email alerts enabled:

- `survivability-backup`: period **1 day**, grace **2 hours**. This produces the
  26-hour staleness alert when no successful backup ping arrives.
- `survivability-backup-maintenance`: period **7 days**, grace **26 hours**.

Both ping URLs are secrets — anyone holding one can signal the check.

## 3. Create the SOPS-encrypted secrets file

SOPS finds your age identity automatically at
`${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt`; see
[`secrets.md`](secrets.md) if you hold it elsewhere or in 1Password.

```bash
sops ansible/inventory/production/group_vars/all/secrets.sops.yml
```

Fill four values, using the placeholder names in `secrets.example.yml`:

- `backup_restic_repository_password` — generated, at least 20 characters. Make
  an offline recovery copy; losing it renders the repository unreadable.
- `backup_restic_ssh_private_key` — contents of `~/.ssh/survivability-backup`
  (block scalar).
- `backup_restic_healthchecks_backup_url` and
  `backup_restic_healthchecks_maintenance_url` — the URLs from step 2.

Verify it decrypts, then commit it encrypted:

```bash
sops -d ansible/inventory/production/group_vars/all/secrets.sops.yml >/dev/null
git add ansible/inventory/production/group_vars/all/secrets.sops.yml
```

## 4. Apply the OpenTofu slice

**One operator at a time.** Generate the subaccount password (at least 20
characters) and store it in `secrets/tooling.sops.env` as
`TF_VAR_storage_box_subaccount_password`:

```bash
sops secrets/tooling.sops.env
```

Set these **temporary** bootstrap values in the private
`tofu/production.tfvars`:

```hcl
server_backups_enabled = true
storage_box_bootstrap_external_reachability = true
```

This is the only point at which the Storage Box is reachable from the operator
workstation.

```bash
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu plan -var-file=production.tfvars -out=bootstrap.tfplan
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu apply bootstrap.tfplan
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu output -raw storage_box_subaccount_username
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu output -raw storage_box_subaccount_server
```

The acceptable plan contains only the new
`hcloud_storage_box_subaccount.restic`, temporary external reachability, and the
server `backups` flag. It must not replace or destroy the server, Primary IPv4,
firewall, DNS records, or Storage Box.

## 5. Pin the Storage Box in the external inventory

Add three values to `~/.config/survivability/production-hosts.yml`:
`backup_restic_storage_box_server`,
`backup_restic_storage_box_subaccount_username`, and
`backup_restic_storage_box_known_hosts`.

Pin the host key, then **compare its fingerprint against Hetzner's published
Storage Box fingerprints before trusting it**. Storage Box hosts expose only
one of the published key types, so scan all supported types rather than assuming
ED25519:

```bash
STORAGE_BOX_SERVER="$(tofu -chdir=tofu output -raw storage_box_subaccount_server)"
ssh-keyscan -p 22 -t ed25519,ecdsa,rsa "${STORAGE_BOX_SERVER}" 2>/dev/null \
  | grep -v '^#' | tee /tmp/storage-box-host-key
ssh-keygen -lf /tmp/storage-box-host-key
```

The SHA256 fingerprint must match the corresponding value published in
[Hetzner's Storage Box documentation](https://docs.hetzner.com/storage/storage-box/general/#ssh-host-keys):

- ED25519: `SHA256:XqONwb1S0zuj5A1CDxpOSuD2hnAArV1A3wKY7Z3sdgM`
- RSA: `SHA256:EMlfI8GsRIfpVkoW1H2u0zYVpFGKkIMKHFZIRkf2ioI`
- ECDSA: `SHA256:oDHZqKXnoMtgvPBjjC57pcuFez28roaEuFcfwyg8O5c`
- DSA: `SHA256:RWkLouD9tfTwdboJOzjiWo5njZI59Hcta82ttAWxDA0`

Keep the resulting `host key-type key...` line exactly as printed as the single
known_hosts entry, then delete the temporary file. `ssh-keyscan` alone is not
verification.

## 6. Authorize the backup key on the subaccount

**One-time per key.** The provider cannot manage Storage Box SSH keys. SFTP port
22 requires an RFC4716 public-key file; restic keeps using the OpenSSH private
key.

```bash
BACKUP_IDENTITY="$HOME/.ssh/survivability-backup"
ssh-keygen -e -f "${BACKUP_IDENTITY}.pub" >"${BACKUP_IDENTITY}.rfc4716.pub"
sftp -P 22 "REPLACE_WITH_SUBACCOUNT_USERNAME@REPLACE_WITH_STORAGE_BOX_FQDN"
```

At the `sftp>` prompt, authenticate with the subaccount password, then:

```text
mkdir .ssh
put /ABSOLUTE/PATH/TO/survivability-backup.rfc4716.pub .ssh/authorized_keys
quit
```

Remove the temporary RFC4716 file after the first backup succeeds. For rotation:
download `authorized_keys`, append the converted new key, upload, test the new
key, then remove the old key in a second reviewed change — never remove the
currently authorized key first.

## 7. Close external reachability

Before deploying restic, return the variable to its steady-state value:

```hcl
storage_box_bootstrap_external_reachability = false
```

```bash
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu plan -var-file=production.tfvars -out=private.tfplan
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu apply private.tfplan
```

The second plan must contain only the in-place reachability change. If the VPS
then cannot reach the Storage Box, investigate the Hetzner network path — do not
leave external reachability enabled as a workaround.

## 8. Deploy and initialize

```bash
OPERATOR_USER="REPLACE_WITH_NAMED_OPERATOR"
PRODUCTION_HOST="REPLACE_WITH_PRODUCTION_HOST"
```

`backup_restic_init_repository=true` is opt-in and creates the repository only
if none exists; restic refuses to initialize over an existing one. Normal
convergence leaves it false and fails closed if the repository later disappears.

Ansible needs the age identity to decrypt the backup secrets, and no provider
credentials at all:

```bash
./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" --check --diff \
    --extra-vars '{"backup_restic_init_repository": true}' ansible/playbooks/site.yml

./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" \
    --extra-vars '{"backup_restic_init_repository": true}' ansible/playbooks/site.yml

./scripts/operator/with-secrets.sh --age -- \
  ansible-playbook -i "$ANSIBLE_INVENTORY" ansible/playbooks/site.yml
```

The final run must report no backup changes. Verify the timers:

```bash
ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo systemctl list-timers survivability-backup\*'
```

## 9. Run and verify the first backup

```bash
ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo systemctl start survivability-backup.service; \
   sudo journalctl -u survivability-backup.service -n 50 --no-pager'

ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  'sudo /usr/local/sbin/survivability-backup run snapshots'
```

Confirm the Healthchecks.io backup check received the `/start` and success
pings. A failed backup exits non-zero, so the systemd unit fails too.

## 10. Test the failure and staleness alerts

Ping the failure endpoint once and confirm the alert email arrives:

```bash
curl --max-time 10 "https://hc-ping.com/REPLACE_WITH_BACKUP_CHECK_UUID/fail"
```

Then, in the dashboard, temporarily set the backup check period to **1 minute**,
wait for the staleness alert, and restore it to **1 day** with **2 hours**
grace. The next successful ping clears both states.

## 11. Run the isolated restore test

This is the one script that needs both provider credentials and the age
identity, so it uses both scopes:

```bash
export ANSIBLE_INVENTORY="$HOME/.config/survivability/production-hosts.yml"
./scripts/operator/with-secrets.sh --tooling --age -- \
  ./scripts/operator/test-backup-restore.sh \
    --snapshot-id "$(cat packer-output/promoted-snapshot-id)" \
    --ssh-key "REPLACE_WITH_REGISTERED_HETZNER_SSH_KEY_NAME" \
    --identity-file "$HOME/.ssh/survivability-ansible"
```

`--ssh-key` is the name or numeric ID of the dedicated Ansible public key
registered in the Hetzner Cloud project, not the Storage Box backup key. List
registered keys and compare the chosen key with the local identity before
running the test:

```bash
./scripts/operator/with-secrets.sh --tooling -- hcloud ssh-key list
ssh-keygen -lf "$HOME/.ssh/survivability-ansible.pub"
./scripts/operator/with-secrets.sh --tooling -- \
  hcloud ssh-key describe "REPLACE_WITH_KEY_NAME_OR_ID" -o json \
  | jq -r .public_key | ssh-keygen -lf -
```

The two fingerprints must match. If the key is not registered, add only its
public half with `hcloud ssh-key create`; never upload the private key.

The script creates a disposable CX23 labelled with owner and expiry, injects the
dedicated Ansible key through cloud-init, runs `restore-test.yml`, verifies the
restored files and repository integrity, records the duration in
`operator-output/restore-test.json`, and always deletes the server. The restore
must complete well within the four-hour objective.

The playbook refuses to run against any host that already has a converged host
firewall policy, so it cannot be pointed at production even by mistake.

## 12. Apply confirmed retention when due

The weekly timer verifies the repository but never deletes snapshots. During a
monthly maintenance window, review the dry run, then apply:

```bash
ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  "sudo /usr/local/sbin/survivability-backup run forget --dry-run --keep-daily 7 --keep-weekly 5 --keep-monthly 12"

ssh "${OPERATOR_USER}@${PRODUCTION_HOST}" \
  "sudo /usr/local/sbin/survivability-backup prune 'PRUNE_RESTIC_identity_production'"
```

This is the one backup operation that destroys data, so it requires the exact
inventory-specific confirmation. Never automate it or place the value in a
timer, environment file, or SOPS secret.

## Failure and recovery

- **Repository lock (restic exit 11):** wait for the `--retry-lock` window, then
  run `survivability-backup run unlock` only after verifying no backup is active.
- **Storage Box unreachable:** check the pinned known_hosts entry and the
  Hetzner Console. The host firewall does not restrict outbound SFTP port 22.
- **Wrong repository password (restic exit 12):** correct the SOPS value and
  rerun. Verify the password before the first backup whenever it is regenerated.
- **Backup key rotation:** see step 6.
- **No recent snapshot:** the weekly verification job fails and pings `/fail`
  when the newest snapshot is older than 26 hours.
- **Missing repository:** normal convergence fails closed. Inspect the Storage
  Box, credentials, and recovery evidence before re-running with
  `backup_restic_init_repository=true`.

Hetzner server backups (enabled in step 4) are a secondary rollback path; restic
on the BX11 remains authoritative.
