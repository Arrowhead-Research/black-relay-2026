# Server rebuild

**This destroys everything on the production server.** It rebuilds the CPX32
from the promoted Debian 13 snapshot while preserving the server object and the
independent Primary IPv4.

Every command runs on a **trusted operator workstation only**. Never run this in
Pi or GitHub Actions, never give either environment Hetzner, B2, OpenTofu-state,
or SSH credentials, and never add this operation to a Make target.

## Safety properties

`scripts/operator/rebuild-production.sh` fails closed unless it can verify, before
changing any protection, that `CONFIRM_REBUILD` exactly equals the expected
numeric server ID; that the server is the OpenTofu-managed x86 CPX32 in `hel1`
with both protections enabled; that the independent non-auto-delete Primary IPv4
has the expected ID, address, assignment, and delete protection; that public IPv6
is absent; that the image ID matches local passing validation evidence and a
provider snapshot labelled validated Debian 13; and that the operator has
confirmed a fresh protected plan and tested console recovery.

It disables only rebuild protection. An exit trap restores both protections after
success, interruption, or failure -- the current API requires both enabled values
in one request. Delete protection stays enabled throughout and the script
contains no server or IP delete command. It passes a reviewed public SSH key as
one-time cloud-init data, suppresses any provider-generated root password, then
verifies provider identity, IPv4 preservation, protection, Debian 13 boot,
cloud-init, and key-only emergency SSH access.

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

# Confirm every required credential is present, by name, without decrypting
# anything into this shell. Each value is injected per-command below.
./scripts/operator/with-secrets.sh --tooling -- sh -c '
  : "${HCLOUD_TOKEN:?missing from secrets/tooling.sops.env}"
  : "${CLOUDFLARE_API_TOKEN:?missing from secrets/tooling.sops.env}"
  : "${AWS_ACCESS_KEY_ID:?missing from secrets/tooling.sops.env}"
  : "${AWS_SECRET_ACCESS_KEY:?missing from secrets/tooling.sops.env}"
  : "${AWS_ENDPOINT_URL_S3:?missing from secrets/tooling.sops.env}"
  : "${AWS_REGION:?missing from secrets/tooling.sops.env}"
  : "${TF_VAR_state_passphrase:?missing from secrets/tooling.sops.env}"
  : "${TF_VAR_storage_box_password:?missing from secrets/tooling.sops.env}"
  echo "all tooling credentials present"'
```

Credentials come from `secrets/tooling.sops.env`, decrypted with your age
identity into each command's own process; see [`secrets.md`](secrets.md). They
are never exported into your shell, so there is nothing to source and nothing to
`unset`.

Do not enable shell tracing. Do not place credentials in arguments, repository
files, logs, or chat.

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
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu init -reconfigure -backend-config=backend.hcl

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu state show hcloud_server.production
./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu state show hcloud_primary_ip.production

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu plan \
    -var-file=production.tfvars \
    -out=rebuild-preflight.tfplan

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu show -json rebuild-preflight.tfplan | \
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

./scripts/operator/with-secrets.sh --tooling -- \
  ./scripts/operator/rebuild-production.sh \
    --expected-server-id "$EXPECTED_SERVER_ID" \
    --expected-primary-ipv4-id "$EXPECTED_PRIMARY_IPV4_ID" \
    --expected-primary-ipv4 "$EXPECTED_PRIMARY_IPV4" \
    --snapshot-id "$SNAPSHOT_ID" \
    --identity-file "$IDENTITY_FILE"
```

`CONFIRM_REBUILD` is not a secret and is passed through to the script by the
wrapper along with the rest of your environment.

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
jq . "$(find operator-output -maxdepth 1 -name 'rebuild-*.json' -type f | sort | tail -1)"
hcloud server describe "$EXPECTED_SERVER_ID"
hcloud primary-ip describe "$EXPECTED_PRIMARY_IPV4_ID"
```

Confirm the server status is running, its image is the exact snapshot ID,
delete and rebuild protection are both enabled, and the Primary IPv4 object and
address are unchanged. Re-open the provider console and confirm the Debian 13
boot is visible.

Create a new plan rather than reusing the preflight plan:

```bash
rm -f tofu/rebuild-preflight.tfplan

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu plan \
    -var-file=production.tfvars \
    -out=rebuild-post.tfplan

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu show -json rebuild-post.tfplan | \
  jq -e 'all(.resource_changes[]?; .change.actions == ["no-op"])'

./scripts/operator/with-secrets.sh --tooling -- \
  tofu -chdir=tofu show rebuild-post.tfplan
rm -f tofu/rebuild-post.tfplan
```

Require `true` and a complete no-change human review. Record the final commit,
unchanged IDs, exact snapshot ID, evidence path, protection state, console/SSH
results, reviewer, and UTC completion. Then release the single-writer window.

Clear runtime authority. Provider credentials were never exported into this
shell, so only the confirmation value remains:

```bash
unset CONFIRM_REBUILD
```

### Re-enroll the tailnet node

A rebuild destroys `/var/lib/tailscale`, which backups deliberately do not
carry: a node key is cheap to reissue and worth nothing restored. The rebuilt
host therefore converges with `tailscaled` installed but unenrolled, and prints
that warning. Headscale still holds the old `blackrelay-vps` node, which is now
a machine that no longer exists.

Delete the stale node, then enroll the new one exactly as
`service-deployment.md` describes under "Enroll the VPS as `blackrelay-vps`":

```bash
ssh "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec -T headscale \
   headscale nodes list --output json' \
  | jq -r '.[] | select(.givenName == "blackrelay-vps") | .id'

ssh -t "REPLACE_WITH_OPERATOR@REPLACE_WITH_PRODUCTION_HOST" \
  'sudo docker compose --project-directory /srv/identity-stack \
   --file /srv/identity-stack/compose.yml exec headscale \
   headscale nodes delete --identifier REPLACE_WITH_STALE_NODE_ID'
```

That command prompts for confirmation and names the node it is about to remove,
which is why it runs on a terminal rather than through `exec -T`. Read the name
back before answering: the identifier is the only thing standing between this
and deleting a live node.

The stale entry does not expire on its own: tagged nodes are exempt from
`node.expiry`. Leaving it costs nothing operationally but makes `nodes list`
lie about what exists, so clear it as part of closing the rebuild.

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
