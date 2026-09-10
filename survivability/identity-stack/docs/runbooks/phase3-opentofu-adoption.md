# Phase 3: protected OpenTofu adoption

This runbook adopts the existing empty Helsinki CPX32 and its independent
Primary IPv4, then creates the production firewall, DNS records, and protected
BX11. It never rebuilds or replaces the server.

## Execution boundary

- **Pi or GitHub Actions:** formatting, validation, and credential-free tests
  only (`make lint` and `make validate`).
- **Trusted operator workstation only:** B2 bootstrap, provider inspection,
  backend initialization, imports, plans, and applies.
- Never provide Pi or GitHub Actions with provider credentials, state keys,
  state files, plans, SSH material, or populated variable files.

No command in this runbook authorizes deleting, rebuilding, or replacing a
protected resource. Stop if any plan contains a `delete` action.

## 1. Claim the single-writer window

**Location: trusted operator process; no provider credentials required.**

1. Announce the planned state-operation window in the team's agreed operations
   channel or change record.
2. Obtain explicit acknowledgement that no other operator is using this state.
3. Record the operator, UTC start time, repository commit, and purpose.
4. Keep the window until the final state upload and verification complete.

The B2 backend has no dependable distributed lock in this design.
`use_lockfile` is deliberately false. A local `.terraform.tfstate.lock.info`
does not coordinate separate workstations.

## 2. Bootstrap the B2 backend

**Location: trusted operator workstation and Backblaze console only.**

1. Create a dedicated private B2 bucket for this state. Confirm file-version
   history is retained and do not configure lifecycle rules that immediately
   remove prior versions.
2. Create a bucket-scoped application key with only the capabilities needed to
   list the bucket and read/write state objects. Store the key ID and
   application key in trusted secret storage.
3. Generate a unique high-entropy state-encryption passphrase of at least 20
   characters. Store it in trusted secret storage and make an offline recovery
   copy. Loss of this value makes the state unrecoverable.
4. Copy `tofu/backend.hcl.example` to `tofu/backend.hcl` and replace its
   non-secret bucket and region placeholders. `backend.hcl` is ignored by Git.
   Never put credentials or the encryption passphrase in it.
5. Record and independently verify the bucket name, region, endpoint, state key,
   and recovery-copy location without recording secret values.

Supply backend credentials at runtime as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`. The committed OpenTofu configuration encrypts state
and saved plans client-side with enforced PBKDF2/AES-GCM before backend upload.
Backblaze server-side encryption does not replace this control.

## 3. Prepare and verify inputs

**Location: trusted operator workstation only.**

Copy `tofu/production.tfvars.example` to `tofu/production.tfvars` and replace
all placeholders. The resulting file is ignored by Git, but still restrict it
to the operator:

```bash
chmod 0600 tofu/production.tfvars tofu/backend.hcl
```

Independently obtain the existing server ID, server name, Primary IPv4 ID, and
IPv4 address from the Hetzner console. Confirm all of the following before an
import:

- the server ID exactly matches `expected_server_id`;
- type is `cpx32`;
- location is Helsinki (`hel1`);
- `server_backups_enabled` matches the server's current backup setting so
  adoption does not silently enable or disable backups;
- the displayed Primary IPv4 ID and address exactly match the variables;
- no public IPv6 address or network is enabled on the server;
- the Primary IPv4 is an independent Primary IP, not an auto-delete address;
- the server contains no data or service requiring migration;
- provider console access and account MFA recovery are available.

Use Hetzner's location code rather than its display name for
`storage_box_location` (`hel1` for Helsinki, not `Helsinki`). Create the BX11
initial password and state passphrase in trusted secret storage. Load all
credentials without echoing them or placing values in shell history:

```bash
# Values come from the operator's trusted secret-storage workflow.
export HCLOUD_TOKEN
export CLOUDFLARE_API_TOKEN
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_REGION
export TOFU_STATE_PASSPHRASE
export STORAGE_BOX_PASSWORD

# Map secret-store names to the exact OpenTofu input names without printing.
export TF_VAR_state_passphrase="$TOFU_STATE_PASSPHRASE"
export TF_VAR_storage_box_password="$STORAGE_BOX_PASSWORD"
```

Do not enable shell tracing (`set -x`). Confirm the variables are set by name,
not by printing their values.

### One-time removal of a pre-existing public IPv6 allocation

**Location: trusted operator workstation and Hetzner console only.**

If the server identity postcondition reports a non-empty `ipv6_address` or
`ipv6_network`, stop before applying. Do not add a `public_net` block to the
imported `hcloud_server`: the provider can attempt an unplanned deletion of the
protected IPv4 on that path.

The placeholder server has no workload to preserve, but Primary IP assignment
changes still require a controlled power-off. With a second operator:

1. Record the exact server ID and the exact IPv6 Primary IP ID, address, and
   network. Use `CONFIRM_REMOVE_IPV6=<exact-ipv6-primary-ip-id>` in the change
   record and have the reviewer compare it with the console.
2. Verify the selected object is type IPv6, is assigned only to the expected
   CPX32, and is not the protected IPv4. Confirm there are no AAAA records or
   clients depending on it.
3. Verify console recovery, then power off the empty placeholder server through
   the Hetzner console. Hetzner requires an existing server to be off before
   removing a Primary IP assignment.
4. Unassign only that IPv6 Primary IP. If it has deletion protection, remove
   protection only from that exact IPv6 object after reconfirming its ID.
5. Delete the now-unassigned IPv6 Primary IP so it does not remain as an
   unmanaged allocation. Never alter the protected IPv4.
6. Power the server on and verify its ID, CPX32 type, `hel1` location, Primary
   IPv4 assignment, protection settings, and console reachability are unchanged.
7. Create a fresh OpenTofu plan. The IPv6 postcondition accepts the provider's
   empty-string and literal `<nil>` representations of an absent IPv6 value, but
   rejects every real IPv6 address or network.

Record the interruption and checks in the same single-writer change window.

## 4. Initialize and import exactly two resources

**Location: trusted operator workstation only.**

Run from the repository's `survivability/identity-stack` directory:

```bash
tofu -chdir=tofu init -reconfigure -backend-config=backend.hcl

tofu -chdir=tofu import \
  -var-file=production.tfvars \
  hcloud_server.production \
  "$SERVER_ID"

tofu -chdir=tofu import \
  -var-file=production.tfvars \
  hcloud_primary_ip.production \
  "$PRIMARY_IPV4_ID"

tofu -chdir=tofu state list
```

`PRIMARY_IPV4_ID` and `SERVER_ID` must be copied from the same independently
verified metadata used in `production.tfvars`; do not derive them from names.
Before the first plan, `state list` must contain exactly:

```text
hcloud_primary_ip.production
hcloud_server.production
```

Do not import the firewall, DNS records, or BX11. They are new resources. Stop
and investigate if either import rejects the configured ID/address/type/location
postconditions.

## 5. Create and inspect the protected plan

**Location: trusted operator workstation only.**

```bash
tofu -chdir=tofu plan \
  -var-file=production.tfvars \
  -out=phase3.tfplan

tofu -chdir=tofu show -json phase3.tfplan | \
  jq -e 'all(.resource_changes[]?; (.change.actions | index("delete")) == null)'

tofu -chdir=tofu show -json phase3.tfplan | \
  jq -e '[.resource_changes[]? | select(
    .address == "hcloud_server.production" or
    .address == "hcloud_primary_ip.production"
  )] | all(.change.actions == ["no-op"] or .change.actions == ["update"])'
```

The two `jq` checks must return `true`. They are supplements, not substitutes,
for reading the complete human-readable plan. Verify that:

- neither imported resource is deleted, replaced, or recreated;
- updates only establish expected names, labels, IPv4 assignment, and
  provider-side delete/rebuild protections; the backup setting does not change;
- the server has no `public_net` change (the provider's imported-server path can
  otherwise attempt an unplanned Primary IP deletion);
- the Primary IPv4 has `auto_delete = false` and remains assigned to the CPX32;
- the firewall permits only inbound IPv4 TCP 22/80/443 and required ICMP;
- Cloudflare records are DNS-only (`proxied = false`) A records with TTL 300;
- the only creates are the reviewed firewall, attachment, DNS records, and BX11;
- the BX11 has provider delete protection;
- no secret value is displayed.

Have a second operator review the saved plan and the independently verified
resource IDs. If any action is unexpected, do not apply it.

## 6. Apply only the reviewed plan and verify convergence

**Location: trusted operator workstation only.**

```bash
tofu -chdir=tofu apply phase3.tfplan

tofu -chdir=tofu plan \
  -var-file=production.tfvars \
  -detailed-exitcode
```

The second command must exit `0`; exit `2` means drift remains. Confirm in the
Hetzner console that delete and rebuild protection are enabled for the CPX32,
that delete protection is enabled for the Primary IPv4 and BX11, and that the
same Primary IPv4 remains assigned. Confirm DNS values independently.

Retain no plaintext exports after the window:

```bash
unset HCLOUD_TOKEN CLOUDFLARE_API_TOKEN
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL_S3 AWS_REGION
unset TOFU_STATE_PASSPHRASE STORAGE_BOX_PASSWORD
unset TF_VAR_state_passphrase TF_VAR_storage_box_password
rm -f tofu/phase3.tfplan
```

Verify a new encrypted state object and prior-version retention in B2. Record
the final commit, plan result, resource IDs, UTC completion time, and reviewer
in the change record, then release the single-writer window.

## Failure and recovery

- **Wrong resource imported:** stop before apply. Use `tofu state rm ADDRESS` only
  after a second operator confirms the state-only removal and the correct ID.
  This does not delete the remote object.
- **Interrupted import/apply:** keep the single-writer window, preserve B2 object
  versions, inspect state and provider metadata, and create a fresh plan. Never
  blindly rerun an old plan.
- **Storage Box location not found:** use a location code such as `hel1`, not a
  display name such as `Helsinki`. Verify that no Storage Box was created, fix
  `production.tfvars`, discard the saved plan, and create a fresh plan.
- **State upload uncertainty:** do not allow another operator to run OpenTofu.
  Compare the current B2 object/version with local command results before any
  further action.
- **Protected Primary IP deletion error on the server resource:** do not disable
  protection or rerun the saved plan. This indicates the provider's known
  imported-`public_net` behavior. Verify the address in the Hetzner console,
  use the configuration where `hcloud_primary_ip.production` owns
  `assignee_id`, and generate a fresh reviewed plan with no server `public_net`
  change.
- **IPv6 postcondition failure:** follow the one-time reviewed IPv6 procedure
  above. Do not weaken the postcondition or delete any IP while the server is
  running.
- **Any delete/replace action:** stop. Do not remove `prevent_destroy`, provider
  protections, or confirmation requirements to make a plan pass.
- **Lost encryption passphrase:** stop. Recover the offline copy; do not create a
  replacement state while the authoritative encrypted state exists.
