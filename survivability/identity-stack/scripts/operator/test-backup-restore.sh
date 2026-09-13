#!/usr/bin/env bash
# Trusted operator workstation only. Creates, tests, and always removes a
# disposable server for the isolated Phase 7 backup restore test. The restore
# writes only into a temporary directory on the disposable server and never
# touches production paths or the production server object.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"
SNAPSHOT_ID=''
SSH_KEY=''
IDENTITY_FILE=''
SERVER_TYPE='cx23'
OWNER="$(id -un | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63 | sed 's/-*$//')"
RUN_ID=''
TEST_SERVER_ID=''
TEST_HOST_NAME=''
TEST_INVENTORY=''
KNOWN_HOSTS=''
USER_DATA=''

usage() {
  cat <<'EOF'
Usage: test-backup-restore.sh --snapshot-id ID --ssh-key NAME_OR_ID
                             --identity-file PATH
                             [--server-type TYPE] [--owner LABEL]

Requires HCLOUD_TOKEN, SOPS_AGE_KEY_FILE, and ANSIBLE_INVENTORY pointing at
the protected production-hosts.yml (for the Storage Box placeholders). The
identity file is the dedicated non-interactive Ansible key; its public half is
injected through cloud-init, so no FIDO2 touch is needed. SOPS_AGE_KEY_FILE
must decrypt the committed backup secrets.
EOF
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${TEST_SERVER_ID}" ]]; then
    printf 'Removing restore-test server %s...\n' "${TEST_SERVER_ID}" >&2
    hcloud server delete "${TEST_SERVER_ID}" || true
  fi
  [[ -z "${USER_DATA}" ]] || rm -f "${USER_DATA}"
  [[ -z "${KNOWN_HOSTS}" ]] || rm -f "${KNOWN_HOSTS}"
  [[ -z "${TEST_INVENTORY}" ]] || rm -f "${TEST_INVENTORY}"
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

while (($#)); do
  case "$1" in
    --snapshot-id)
      SNAPSHOT_ID="${2:?--snapshot-id requires an ID}"
      shift 2
      ;;
    --ssh-key)
      SSH_KEY="${2:?--ssh-key requires a name or ID}"
      shift 2
      ;;
    --identity-file)
      IDENTITY_FILE="${2:?--identity-file requires a path}"
      shift 2
      ;;
    --server-type)
      SERVER_TYPE="${2:?--server-type requires a type}"
      shift 2
      ;;
    --owner)
      OWNER="${2:?--owner requires a label}"
      shift 2
      ;;
    -h|--help)
      trap - EXIT INT TERM
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

: "${HCLOUD_TOKEN:?Set HCLOUD_TOKEN from trusted secret storage first}"
: "${SOPS_AGE_KEY_FILE:?Set SOPS_AGE_KEY_FILE to the operator age key first}"
: "${ANSIBLE_INVENTORY:?Set ANSIBLE_INVENTORY to the protected production-hosts.yml first}"
[[ "${SNAPSHOT_ID}" =~ ^[0-9]+$ ]] || { printf 'snapshot ID must be numeric\n' >&2; exit 2; }
[[ -n "${SSH_KEY}" ]] || { printf '%s\n' '--ssh-key is required' >&2; exit 2; }
[[ -n "${IDENTITY_FILE}" ]] || { printf '%s\n' '--identity-file is required' >&2; exit 2; }
[[ "${IDENTITY_FILE}" = /* ]] || { printf '%s\n' 'identity-file path must be absolute; expand the home-directory path first' >&2; exit 2; }
[[ "${IDENTITY_FILE}" != *.pub ]] || { printf 'pass the private-key stub, not its .pub file\n' >&2; exit 2; }
[[ -f "${IDENTITY_FILE}" ]] || { printf 'identity file not found: %s\n' "${IDENTITY_FILE}" >&2; exit 2; }
[[ -f "${IDENTITY_FILE}.pub" ]] || { printf 'public key not found: %s.pub\n' "${IDENTITY_FILE}" >&2; exit 2; }
[[ "${ANSIBLE_INVENTORY}" = /* ]] || { printf 'ANSIBLE_INVENTORY must be an absolute path\n' >&2; exit 2; }
[[ -f "${ANSIBLE_INVENTORY}" ]] || { printf 'production inventory not found: %s\n' "${ANSIBLE_INVENTORY}" >&2; exit 2; }
[[ ${#OWNER} -le 63 && "${OWNER}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || {
  printf 'owner must be a lowercase alphanumeric/hyphen Hetzner label value\n' >&2
  exit 2
}
for command_name in hcloud jq ssh ssh-keygen ansible-playbook sops python3 date; do
  command -v "${command_name}" >/dev/null || { printf 'missing command: %s\n' "${command_name}" >&2; exit 1; }
done
python3 -c 'import yaml' 2>/dev/null || { printf 'python3 needs PyYAML\n' >&2; exit 1; }
sops -d ansible/inventory/production/group_vars/all/secrets.sops.yml >/dev/null || {
  printf 'the committed backup secrets file must decrypt with SOPS_AGE_KEY_FILE\n' >&2
  exit 1
}

image_json="$(hcloud image describe "${SNAPSHOT_ID}" -o json)"
jq -e --arg id "${SNAPSHOT_ID}" \
  '(.id | tostring) == $id and .type == "snapshot" and .architecture == "x86"' \
  <<<"${image_json}" >/dev/null || {
    printf 'image must be the requested x86 snapshot\n' >&2
    exit 1
  }

hcloud image describe "${SNAPSHOT_ID}"
printf 'This creates a chargeable disposable server from snapshot %s.\n' "${SNAPSHOT_ID}"
read -r -p "Type restore-test to continue: " confirmation
[[ "${confirmation}" == "restore-test" ]] || { printf 'cancelled\n' >&2; exit 1; }

RUN_ID="$(date -u +%Y%m%d%H%M%S)"
TEST_HOST_NAME="restore-test-${RUN_ID}"
expires_at="$(date -u -d '+4 hours' +%Y%m%dt%H%M%Sz)"
started_epoch="$(date -u +%s)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p operator-output
KNOWN_HOSTS="${ROOT}/operator-output/known-hosts-restore-test-${RUN_ID}"
USER_DATA="${ROOT}/operator-output/restore-test-user-data-${RUN_ID}.yml"
TEST_INVENTORY="${ROOT}/operator-output/phase7-restore-test-inventory-${RUN_ID}.yml"

printf '#cloud-config\nssh_pwauth: false\ndisable_root: false\nssh_authorized_keys:\n  - %s\n' \
  "$(cat "${IDENTITY_FILE}.pub")" >"${USER_DATA}"
chmod 0600 "${USER_DATA}"

hcloud server create \
  --name "${TEST_HOST_NAME}" \
  --type "${SERVER_TYPE}" \
  --image "${SNAPSHOT_ID}" \
  --location hel1 \
  --ssh-key "${SSH_KEY}" \
  --user-data-from-file "${USER_DATA}" \
  --without-ipv6 \
  --label project=survivability \
  --label purpose=backup-restore-test \
  --label owner="${OWNER}" \
  --label expires-at="${expires_at}"

test_json="$(hcloud server describe "${TEST_HOST_NAME}" -o json)"
TEST_SERVER_ID="$(jq -er '.id' <<<"${test_json}")"
test_ip="$(jq -er '.public_net.ipv4.ip' <<<"${test_json}")"

ssh_opts=(
  -o UserKnownHostsFile="${KNOWN_HOSTS}"
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=5
  -o IdentityFile="${IDENTITY_FILE}"
  -o IdentitiesOnly=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o PreferredAuthentications=publickey
)

printf 'Waiting up to five minutes for SSH on %s...\n' "${test_ip}" >&2
for attempt in {1..60}; do
  printf '  SSH attempt %d/60...\n' "${attempt}" >&2
  if ssh "${ssh_opts[@]}" "root@${test_ip}" true; then
    break
  fi
  sleep 5
done
ssh "${ssh_opts[@]}" "root@${test_ip}" true || { printf 'SSH did not become ready\n' >&2; exit 1; }

python3 - "${ANSIBLE_INVENTORY}" "${TEST_INVENTORY}" "${TEST_HOST_NAME}" "${test_ip}" \
  "${IDENTITY_FILE}" "${KNOWN_HOSTS}" <<'PYEOF'
import sys
import yaml

production_path, test_path, test_host, test_ip, identity, known_hosts = sys.argv[1:7]
with open(production_path, encoding="utf-8") as handle:
    inventory = yaml.safe_load(handle)
hosts = inventory["all"]["children"]["identity_stack"]["hosts"]
_, hostvars = next(iter(hosts.items()))
selected = {}
for key in (
    "backup_restic_storage_box_server",
    "backup_restic_storage_box_subaccount_username",
    "backup_restic_storage_box_known_hosts",
):
    value = hostvars.get(key)
    if not value or "REPLACE_WITH" in str(value):
        sys.exit(f"{production_path} is missing a real value for {key}")
    selected[key] = value
test_inventory = {
    "all": {
        "hosts": {
            test_host: {
                "ansible_host": test_ip,
                "ansible_user": "root",
                "ansible_python_interpreter": "/usr/bin/python3",
                "ansible_ssh_private_key_file": identity,
                "ansible_ssh_common_args": (
                    "-o UserKnownHostsFile="
                    + known_hosts
                    + " -o StrictHostKeyChecking=accept-new"
                    + " -o IdentitiesOnly=yes"
                    + " -o PasswordAuthentication=no"
                ),
                **selected,
            }
        }
    }
}
with open(test_path, "w", encoding="utf-8") as handle:
    yaml.safe_dump(test_inventory, handle, default_flow_style=False)
PYEOF

ansible-playbook \
  -i "${TEST_INVENTORY}" \
  --extra-vars "backup_restore_test_confirmation=CONFIRM_RESTORE_TEST_${TEST_HOST_NAME}" \
  ansible/playbooks/phase7-restore-test.yml

finished_epoch="$(date -u +%s)"
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n \
  --arg run_id "${RUN_ID}" \
  --arg snapshot_id "${SNAPSHOT_ID}" \
  --arg test_server_id "${TEST_SERVER_ID}" \
  --arg server_type "${SERVER_TYPE}" \
  --arg started "${started}" \
  --arg finished "${finished}" \
  --argjson duration_seconds "$((finished_epoch - started_epoch))" \
  '{run_id: $run_id, snapshot_id: $snapshot_id, test_server_id: $test_server_id, server_type: $server_type, started: $started, finished: $finished, duration_seconds: $duration_seconds, result: "PASS"}' \
  >operator-output/phase7-restore-test.json

printf '\nRestore test passed in %s seconds.\n' "$((finished_epoch - started_epoch))"
printf 'Restore evidence: %s\n' "${ROOT}/operator-output/phase7-restore-test.json"
