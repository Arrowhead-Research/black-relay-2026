# Phase 5: explicitly confirmed CPX32 rebuild

This runbook destructively rebuilds the existing empty production CPX32 from
the explicit Debian 13 snapshot validated in Phase 4. It preserves the server
object and independent Primary IPv4. There is no application data to archive.

Every command below runs on a **trusted operator workstation only**. Never run
this procedure in Pi or GitHub Actions, and never provide either environment
with Hetzner, B2, OpenTofu-state, or SSH credentials. Do not add this operation
to a Make target.

## Safety properties

The committed `scripts/operator/rebuild-production.sh` fails closed unless it
can verify all of the following before changing protection:

- `CONFIRM_REBUILD` exactly equals the expected numeric server ID;
- the server is the OpenTofu-managed x86 CPX32 in `hel1`;
- delete and rebuild protection are enabled;
- the independent, non-auto-delete Primary IPv4 has the expected ID, address,
  assignment, and delete protection;
- public IPv6 remains absent;
- the image ID exactly matches local passing Phase 4 validation/promotion
  evidence and a provider snapshot labelled as validated Debian 13;
- the operator confirms a fresh protected OpenTofu plan and tested console
  recovery.

The script disables only rebuild protection. An exit trap retries restoration
of both protections after success, interruption, or failure; current
`hcloud`/API validation requires the two enabled values in the same request.
Delete protection remains enabled throughout, and the script contains no
server/IP delete command. It passes a reviewed
public SSH key as one-time cloud-init data, suppresses any provider-generated
root password, then verifies provider identity, IPv4 preservation, protection,
Debian 13 boot, cloud-init, and key-only emergency SSH access.

## 1. Claim the single-writer window

**Location: trusted operator process; no provider credentials required.**

1. Announce the change window and obtain acknowledgement that no other operator
   is using the production OpenTofu state.
2. Record operator, reviewer, UTC start time, repository commit, and purpose.
3. Keep this window through the final convergence plan.
4. Confirm the placeholder server still has no data or services to preserve.

The B2 backend has no dependable distributed lock. Local lock files do not
coordinate separate workstations.

## 2. Load tools and credentials safely

**Location: trusted operator workstation only.**

Use the same reviewed workstation setup and encrypted B2 backend from Phase 3.
The workstation needs OpenTofu 1.12.6, `hcloud`, `jq`, OpenSSH, and GNU `date`.
The `hcloud` CLI must expose `--user-data-from-file` on `server rebuild`.

```bash
cd /path/to/survivability/identity-stack

tofu version | grep 'OpenTofu v1.12.6'
hcloud version
hcloud server rebuild --help | grep -- '--user-data-from-file'
jq --version

# Reuse the protected file created during workstation bootstrap. This loads it
# into this shell; it does not require re-entering any value.
OPERATOR_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/survivability/operator.env"
test -f "$OPERATOR_ENV"
test "$(stat -c '%a' "$OPERATOR_ENV")" = '600'
set -a
. "$OPERATOR_ENV"
set +a

# OpenTofu uses these two aliases. They refer to values already loaded above.
export TF_VAR_state_passphrase="$TOFU_STATE_PASSPHRASE"
export TF_VAR_storage_box_password="$STORAGE_BOX_PASSWORD"

# Fail by variable name without displaying any value if the file is incomplete.
: "${HCLOUD_TOKEN:?missing from operator environment}"
: "${CLOUDFLARE_API_TOKEN:?missing from operator environment}"
: "${AWS_ACCESS_KEY_ID:?missing from operator environment}"
: "${AWS_SECRET_ACCESS_KEY:?missing from operator environment}"
: "${AWS_ENDPOINT_URL_S3:?missing from operator environment}"
: "${AWS_REGION:?missing from operator environment}"
: "${TF_VAR_state_passphrase:?missing from operator environment}"
: "${TF_VAR_storage_box_password:?missing from operator environment}"
```

If your protected file has a different path, change only `OPERATOR_ENV`. If your
workstation secret manager injects variables directly, use its normal session
command instead of sourcing a file. Do not retype already stored values.

Do not enable shell tracing. Do not place credentials in arguments, repository
files, logs, or chat. A config file is not loaded automatically by Bash; it must
be sourced once in each new operator shell because environment variables are
process-local.

Set non-secret expected values by independently comparing `production.tfvars`,
the Hetzner console, and the Phase 4 evidence:

```bash
EXPECTED_SERVER_ID='REPLACE_WITH_EXACT_NUMERIC_SERVER_ID'
EXPECTED_PRIMARY_IPV4_ID='REPLACE_WITH_EXACT_NUMERIC_PRIMARY_IPV4_ID'
EXPECTED_PRIMARY_IPV4='REPLACE_WITH_EXACT_IPV4_ADDRESS'
SNAPSHOT_ID="$(tr -d '[:space:]' < packer-output/promoted-snapshot-id)"
# Use the dedicated non-interactive Ansible identity for future rebuilds.
IDENTITY_FILE="$HOME/.ssh/survivability-ansible"

[[ "$EXPECTED_SERVER_ID" =~ ^[1-9][0-9]*$ ]]
[[ "$EXPECTED_PRIMARY_IPV4_ID" =~ ^[1-9][0-9]*$ ]]
[[ "$SNAPSHOT_ID" =~ ^[1-9][0-9]*$ ]]
test -f "$IDENTITY_FILE"
test -f "${IDENTITY_FILE}.pub"
ssh-keygen -lf "${IDENTITY_FILE}.pub"
```

Never set an ID by searching for `latest`, by a name prefix, or from an
unreviewed command result. The script reads the private-key stub only through
OpenSSH; it reads the `.pub` file to prepare cloud-init.

## 3. Prove imported state and protections are clean

**Location: trusted operator workstation only.**

Initialize the existing encrypted backend and inspect the two adopted resource
addresses:

```bash
tofu -chdir=tofu init -reconfigure -backend-config=backend.hcl

tofu -chdir=tofu state show hcloud_server.production
tofu -chdir=tofu state show hcloud_primary_ip.production

tofu -chdir=tofu plan \
  -var-file=production.tfvars \
  -out=phase5-preflight.tfplan

tofu -chdir=tofu show -json phase5-preflight.tfplan | \
  jq -e 'all(.resource_changes[]?; .change.actions == ["no-op"])'
```

The machine check must return `true`, and the complete human-readable plan must
show no changes. Confirm that state and provider metadata agree on:

- exact server ID, running status, `cpx32` type, x86 architecture, and `hel1`
  location;
- exact Primary IPv4 ID/address and assignment to that server;
- `auto_delete = false` for the Primary IPv4;
- server delete/rebuild protection and Primary IPv4 delete protection enabled;
- no public IPv6 allocation;
- the protected firewall remains attached.

Stop on drift, state uncertainty, an unexpected resource address, or any delete
or replace action. Do not weaken lifecycle or provider protection to proceed.
Have the reviewer compare the IDs with the console and saved plan.

## 4. Test console recovery immediately before rebuild

**Location: trusted operator workstation and Hetzner console only.**

Open the expected server by numeric ID in the Hetzner console. Verify account
MFA/recovery access and that the web console opens against that exact CPX32.
Record the successful check in the change record. Do not disable a protection or
start rescue mode during this test.

Also inspect the explicit snapshot and current resources without changing them:

```bash
hcloud server describe "$EXPECTED_SERVER_ID"
hcloud primary-ip describe "$EXPECTED_PRIMARY_IPV4_ID"
hcloud image describe "$SNAPSHOT_ID"
```

Stop unless every value matches Step 3 and the snapshot is labelled
`project=survivability`, `role=identity-stack-base`, `status=validated`, and
`os=debian-13`.

## 5. Run the guarded rebuild

**Location: trusted operator workstation only. Destructive.**

Set the exact resource-specific confirmation value. This value is not a secret,
but it must come from the independent checks above:

```bash
export CONFIRM_REBUILD="$EXPECTED_SERVER_ID"

scripts/operator/rebuild-production.sh \
  --expected-server-id "$EXPECTED_SERVER_ID" \
  --expected-primary-ipv4-id "$EXPECTED_PRIMARY_IPV4_ID" \
  --expected-primary-ipv4 "$EXPECTED_PRIMARY_IPV4" \
  --snapshot-id "$SNAPSHOT_ID" \
  --identity-file "$IDENTITY_FILE"
```

Review all displayed metadata. Enter `IMPORTED_STATE_VERIFIED` only for the
fresh no-change plan from Step 3. Enter `CONSOLE_RECOVERY_VERIFIED` only for the
console test from Step 4. Immediately before mutation, the script fetches the
server, Primary IPv4, and snapshot again and refuses to proceed if any guarded
metadata changed during review.

The script writes non-secret evidence under ignored `operator-output/`. It then:

1. records preflight IDs and key fingerprint;
2. creates temporary cloud-init and known-hosts files with mode `0600`;
3. disables only rebuild protection;
4. rebuilds the same server object with the exact numeric snapshot ID;
5. restores and verifies rebuild protection immediately;
6. proves the server and Primary IPv4 IDs/assignment are unchanged;
7. waits for key-only root SSH and validates Debian 13 and cloud-init; and
8. records `PASS` only after provider, boot, and access checks succeed.

The temporary root key is bootstrap access for Phases 5-6, not the final root
policy. Future rebuilds use the dedicated Ansible key here so Phase 6 can run
without touch/PIN prompts. The already-completed rebuild used the manual/FIDO2
key, so its Phase 6 transition authorizes the dedicated public key once through
that existing access. Phase 6 installs both public keys for the named operator,
reconnects with the dedicated key to prove sudo, and only then disables root SSH
and removes the temporary root authorization.

## 6. Verify convergence and close the window

**Location: trusted operator workstation only.**

Inspect the generated evidence and independently repeat provider checks:

```bash
jq . "$(find operator-output -maxdepth 1 -name 'phase5-*.json' -type f | sort | tail -1)"
hcloud server describe "$EXPECTED_SERVER_ID"
hcloud primary-ip describe "$EXPECTED_PRIMARY_IPV4_ID"
```

Confirm the server status is running, its image is the exact snapshot ID,
delete and rebuild protection are both enabled, and the Primary IPv4 object and
address are unchanged. Re-open the provider console and confirm the Debian 13
boot is visible.

Create a new plan rather than reusing the preflight plan:

```bash
rm -f tofu/phase5-preflight.tfplan

tofu -chdir=tofu plan \
  -var-file=production.tfvars \
  -out=phase5-post.tfplan

tofu -chdir=tofu show -json phase5-post.tfplan | \
  jq -e 'all(.resource_changes[]?; .change.actions == ["no-op"])'

tofu -chdir=tofu show phase5-post.tfplan
rm -f tofu/phase5-post.tfplan
```

Require `true` and a complete no-change human review. Record the final commit,
unchanged IDs, exact snapshot ID, evidence path, protection state, console/SSH
results, reviewer, and UTC completion. Then release the single-writer window.

Clear runtime authority:

```bash
unset CONFIRM_REBUILD HCLOUD_TOKEN CLOUDFLARE_API_TOKEN
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL_S3 AWS_REGION
unset TOFU_STATE_PASSPHRASE STORAGE_BOX_PASSWORD
unset TF_VAR_state_passphrase TF_VAR_storage_box_password
```

## Failure and recovery

- **Any preflight mismatch:** stop. Do not change protection and do not edit the
  script to bypass the failed check.
- **Script interruption or rebuild failure:** the exit trap retries rebuild
  protection restoration. Independently inspect the exact server immediately.
  If protection is not enabled, run
  `hcloud server enable-protection "$EXPECTED_SERVER_ID" delete rebuild`,
  verify both values in the console, and keep the change window open.
- **Uncertain rebuild action:** do not issue a second rebuild. Inspect server
  actions, image ID, status, console output, and the evidence file first.
- **SSH unavailable:** leave protections enabled and use the already-tested
  console path. Inspect cloud-init from the console. Do not enable password SSH
  or paste a private key into user data.
- **Primary IPv4 mismatch or unassignment:** do not delete/recreate the server or
  address. Re-enable all protections, keep the window open, and compare the
  independent Primary IP object and state before a reviewed reassignment.
- **Post-plan drift:** do not apply automatically. Review the refreshed provider
  metadata and plan with a second operator. Any replace/delete action is a hard
  stop.
- **Console recovery unavailable:** stop before rebuild. Phase 5 is not complete
  until console and emergency SSH access are both verified.
