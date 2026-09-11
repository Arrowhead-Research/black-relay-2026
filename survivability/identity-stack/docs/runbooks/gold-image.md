# Phase 4: gold-image build, validation, promotion, and pruning

Run all commands from the repository root in one Bash shell on a **trusted
operator workstation**. Never run them in Pi or GitHub Actions. Never paste the
Hetzner token or SSH private key into chat, files, command-line arguments, or
shell history. This phase must not rebuild or modify the production CPX32; that
is a separately confirmed Phase 5 operation.

The committed operator scripts contain no credentials or fixed resource IDs.
They read `HCLOUD_TOKEN` from the environment, require explicit confirmation,
and fail closed. Do not add them to a default Make target.

## 1. Check prerequisites and select the Hetzner project

The workstation needs Packer 1.16.0, the `hcloud` CLI, `jq`, OpenSSH, and GNU
`date`. The operator's FIDO2 private-key stub and matching `.pub` file must be
present locally, and the public key must be registered in the intended Hetzner
project.

```sh
# trusted operator workstation only
cd /path/to/survivability/identity-stack
packer version | grep '1.16.0'
hcloud version
jq --version

# Use an absolute path to the private-key stub, never the .pub file.
IDENTITY_FILE="$HOME/.ssh/REPLACE_WITH_FIDO_PRIVATE_KEY_STUB"
test -f "${IDENTITY_FILE}"
test -f "${IDENTITY_FILE}.pub"
ssh-keygen -lf "${IDENTITY_FILE}.pub"

# Avoid putting the token in shell history.
read -rsp 'Hetzner API token: ' HCLOUD_TOKEN; printf '\n'
export HCLOUD_TOKEN
hcloud context active 2>/dev/null || printf '%s\n' 'No named context; HCLOUD_TOKEN supplies authentication'
read -rp 'Expected production server name: ' PRODUCTION_SERVER_NAME
hcloud server describe "${PRODUCTION_SERVER_NAME}"
```

Stop unless the active context and harmless server metadata identify the
expected project and existing imported CPX32. `describe` is read-only. The
current Ubuntu installation will be erased only during Phase 5; the server
object itself will not be deleted.

Set the non-secret Hetzner SSH-key name or ID used by disposable servers. It
must identify the public key matching `IDENTITY_FILE`:

```sh
# trusted operator workstation only
read -rp 'Registered Hetzner SSH key name or ID: ' HCLOUD_SSH_KEY
OPERATOR_LABEL="$(id -un | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63 | sed 's/-*$//')"
mkdir -p packer-output
```

`packer-output/` and private `*.pkrvars.hcl` files are ignored by Git.

## 2. Discover exact package versions with the operator script

Run the committed discovery orchestrator:

```sh
# trusted operator workstation only
scripts/operator/discover-package-versions.sh \
  --ssh-key "${HCLOUD_SSH_KEY}" \
  --identity-file "${IDENTITY_FILE}" \
  --server-type cx23 \
  --owner "${OPERATOR_LABEL}" \
  --output packer/package-versions.auto.pkrvars.hcl
```

Before creating anything, the script verifies that `IDENTITY_FILE.pub` matches
the selected Hetzner public key. SSH is forced to use only that private-key stub;
password fallback is disabled, while FIDO2 PIN and touch interaction remains
visible. Expect PIN/touch requests while testing SSH, copying the payload, and
running it. The script displays the active context and
requires the exact confirmation `DISCOVER_PACKAGE_VERSIONS`. It then:

1. Creates a labelled, expiring Debian 13 CX23 server in `hel1`.
2. Waits for SSH using the operator key from `ssh-agent`.
3. Copies and runs `packer/scripts/discover-package-versions.sh` remotely.
4. Verifies the Docker and Fluent Bit repository signing-key fingerprints.
5. Validates all six discovered package-version values.
6. Writes the exact pins to `packer/package-versions.auto.pkrvars.hcl`.
7. Writes timing evidence to `packer-output/phase4-discovery.json`.
8. Deletes the discovery server through an exit trap on success or failure.

Review the generated pins and confirm cleanup:

```sh
# trusted operator workstation only
cat packer/package-versions.auto.pkrvars.hcl
jq . packer-output/phase4-discovery.json
hcloud server list --selector project=survivability,purpose=package-discovery
```

The server list must be empty. Never bypass a failed fingerprint check. Record
this server's cost for Step 5; its start and finish times are in the evidence
file.

## 3. Build the candidate with the operator script

Run the committed build orchestrator instead of copying individual Packer
commands:

```sh
# trusted operator workstation only
scripts/operator/build-gold-image.sh \
  --var-file packer/package-versions.auto.pkrvars.hcl \
  --server-type cx23 \
  --owner "${OPERATOR_LABEL}"
```

The script will:

1. Check the token, tools, package-pin file, and Packer version.
2. Display the active Hetzner context.
3. Require the exact confirmation `BUILD_GOLD_IMAGE`.
4. Initialize and validate Packer.
5. Build with `-on-error=cleanup` and expiry/owner labels.
6. Extract and verify the numeric snapshot ID.
7. Fail if any gold-image builder remains.
8. Write `packer-output/phase4-build.json`.

Read the resulting ID into the current shell and inspect it:

```sh
# trusted operator workstation only
SNAPSHOT_ID="$(jq -er '.snapshot_id' packer-output/phase4-build.json)"
hcloud image describe "${SNAPSHOT_ID}"
```

Stop unless it is the expected x86 candidate snapshot. Record the builder's
elapsed time and cost for Step 5. Never select an image using `latest` or a name
prefix.

## 4. Validate with the operator script

Run the validation orchestrator using the exact numeric snapshot ID:

```sh
# trusted operator workstation only
scripts/operator/validate-gold-image.sh \
  --snapshot-id "${SNAPSHOT_ID}" \
  --ssh-key "${HCLOUD_SSH_KEY}" \
  --identity-file "${IDENTITY_FILE}" \
  --server-type cx23 \
  --owner "${OPERATOR_LABEL}"
```

Type the displayed snapshot ID when prompted. Expect multiple visible FIDO2
PIN/touch requests before and after reboot. The script will:

1. Verify that the provider object is the requested x86 snapshot.
2. Create a labelled, expiring Debian validation server in `hel1`.
3. Run `packer/scripts/validate-gold-image.sh` over SSH.
4. Reboot and verify that the kernel boot ID changed.
5. Run the complete validation again after reboot.
6. Inspect held packages, failed units, Docker, and Compose.
7. Write `packer-output/phase4-validation.json` only after success.
8. Delete the validation server through an exit trap on success or failure.

Confirm success evidence and verify that no disposable server remains:

```sh
# trusted operator workstation only
jq . packer-output/phase4-validation.json
hcloud server list --selector project=survivability,purpose=gold-image-validation
```

The server list must be empty. Do not use production DNS, the production Primary
IPv4, or production secrets during validation. Record validation elapsed time
and cost for Step 5.

## 5. Record costs and promote with the operator script

Consult current Hetzner pricing. Prepare four non-secret evidence strings for
the actual discovery server, Packer builder, validation server, and ongoing
snapshot storage costs. Then run:

```sh
# trusted operator workstation only
scripts/operator/promote-gold-image.sh \
  --snapshot-id "${SNAPSHOT_ID}" \
  --discovery-cost 'REPLACE_WITH_ACTUAL_AMOUNT_AND_CURRENCY' \
  --builder-cost 'REPLACE_WITH_ACTUAL_AMOUNT_AND_CURRENCY' \
  --validation-cost 'REPLACE_WITH_ACTUAL_AMOUNT_AND_CURRENCY' \
  --snapshot-storage-cost 'REPLACE_WITH_ACTUAL_RATE_AND_CURRENCY'
```

Replace every placeholder before running it. The script refuses to continue
without all four values and matching successful build/validation evidence. It
shows the provider image and requires the exact snapshot ID as confirmation.
It then:

- writes `packer-output/phase4-<run-id>.md`;
- changes the snapshot's `status` label from `candidate` to `validated`;
- adds a validated description;
- writes the numeric ID to `packer-output/promoted-snapshot-id`; and
- lists validated snapshots for the retention decision.

Verify the promoted ID:

```sh
# trusted operator workstation only
test "$(cat packer-output/promoted-snapshot-id)" = "${SNAPSHOT_ID}"
hcloud image describe "${SNAPSHOT_ID}"
```

That numeric ID is the only image reference to carry into Phase 5. The promotion
script does not rebuild or modify production.

## 6. Retain current and previous validated snapshots

List validated snapshots and identify the new current snapshot and exactly one
previous validated rollback snapshot:

```sh
# trusted operator workstation only
hcloud image list \
  --type snapshot \
  --selector project=survivability,role=identity-stack-base,status=validated \
  -o columns=id,name,created,description
```

For each older snapshot, inspect it and require its exact ID before deletion:

```sh
# trusted operator workstation only
CURRENT_SNAPSHOT_ID="${SNAPSHOT_ID}"
PREVIOUS_SNAPSHOT_ID='REPLACE_WITH_PREVIOUS_VALIDATED_ID'
OLD_SNAPSHOT_ID='REPLACE_WITH_ONE_OLDER_ID'

test "${OLD_SNAPSHOT_ID}" != "${CURRENT_SNAPSHOT_ID}"
test "${OLD_SNAPSHOT_ID}" != "${PREVIOUS_SNAPSHOT_ID}"
hcloud image describe "${OLD_SNAPSHOT_ID}"
read -rp "Type old snapshot ID ${OLD_SNAPSHOT_ID} to delete it: " CONFIRM_DELETE
[ "${CONFIRM_DELETE}" = "${OLD_SNAPSHOT_ID}" ]
hcloud image delete "${OLD_SNAPSHOT_ID}"
```

Repeat only for snapshots older than the retained pair. Never delete a snapshot
referenced by a pending rebuild, rollback record, or recovery exercise.

## Final cleanup

```sh
# trusted operator workstation only
hcloud server list --selector project=survivability
unset HCLOUD_TOKEN HCLOUD_SSH_KEY
```

Confirm there are no package-discovery, gold-image-builder, or image-validation
servers left. The production CPX32, Primary IPv4, firewall, and BX11 must remain
unchanged. If build or validation failed, do not run the promotion script and do
not use that candidate in Phase 5.
