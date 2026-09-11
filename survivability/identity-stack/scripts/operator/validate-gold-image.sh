#!/usr/bin/env bash
# Trusted operator workstation only. Creates and always removes a test server.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNAPSHOT_ID=''
SSH_KEY=''
IDENTITY_FILE=''
SERVER_TYPE='cx23'
OWNER="$(id -un | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63 | sed 's/-*$//')"
VALIDATION_ID=''
KNOWN_HOSTS=''

usage() {
  cat <<'EOF'
Usage: validate-gold-image.sh --snapshot-id ID --ssh-key NAME_OR_ID
                              --identity-file PATH
                              [--server-type TYPE] [--owner LABEL]

Requires HCLOUD_TOKEN and the private-key stub matching the registered Hetzner
public key. FIDO2 keys visibly request PIN and touch during SSH operations.
EOF
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${VALIDATION_ID}" ]]; then
    printf 'Removing validation server %s...\n' "${VALIDATION_ID}" >&2
    hcloud server delete "${VALIDATION_ID}" || true
  fi
  if [[ -n "${KNOWN_HOSTS}" ]]; then
    rm -f "${KNOWN_HOSTS}"
  fi
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
[[ "${SNAPSHOT_ID}" =~ ^[0-9]+$ ]] || { printf 'snapshot ID must be numeric\n' >&2; exit 2; }
[[ -n "${SSH_KEY}" ]] || { printf '%s\n' '--ssh-key is required' >&2; exit 2; }
[[ -n "${IDENTITY_FILE}" ]] || { printf '%s\n' '--identity-file is required' >&2; exit 2; }
[[ "${IDENTITY_FILE}" = /* ]] || { printf '%s\n' 'identity-file path must be absolute; expand the home-directory path first' >&2; exit 2; }
[[ "${IDENTITY_FILE}" != *.pub ]] || { printf 'pass the private-key stub, not its .pub file\n' >&2; exit 2; }
[[ -f "${IDENTITY_FILE}" ]] || { printf 'identity file not found: %s\n' "${IDENTITY_FILE}" >&2; exit 2; }
[[ -f "${IDENTITY_FILE}.pub" ]] || { printf 'public key not found: %s.pub\n' "${IDENTITY_FILE}" >&2; exit 2; }
[[ ${#OWNER} -le 63 && "${OWNER}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || {
  printf 'owner must be a lowercase alphanumeric/hyphen Hetzner label value\n' >&2
  exit 2
}
for command_name in hcloud jq ssh scp ssh-keygen date; do
  command -v "${command_name}" >/dev/null || { printf 'missing command: %s\n' "${command_name}" >&2; exit 1; }
done
registered_public_key="$(hcloud ssh-key describe "${SSH_KEY}" -o json | jq -er '.public_key')"
registered_fingerprint="$(printf '%s\n' "${registered_public_key}" | ssh-keygen -lf - | awk '{print $2}')"
local_fingerprint="$(ssh-keygen -lf "${IDENTITY_FILE}.pub" | awk '{print $2}')"
if [[ "${registered_fingerprint}" != "${local_fingerprint}" ]]; then
  printf 'identity file fingerprint %s does not match Hetzner SSH key %s (%s)\n' \
    "${local_fingerprint}" "${SSH_KEY}" "${registered_fingerprint}" >&2
  exit 1
fi

cd "${ROOT}"
[[ -x packer/scripts/validate-gold-image.sh ]] || { printf 'validation payload is missing\n' >&2; exit 1; }

image_json="$(hcloud image describe "${SNAPSHOT_ID}" -o json)"
jq -e --arg id "${SNAPSHOT_ID}" \
  '(.id | tostring) == $id and .type == "snapshot" and .architecture == "x86"' \
  <<<"${image_json}" >/dev/null || {
    printf 'image must be the requested x86 snapshot\n' >&2
    exit 1
  }

hcloud image describe "${SNAPSHOT_ID}"
printf 'This creates a chargeable disposable server from snapshot %s.\n' "${SNAPSHOT_ID}"
read -r -p "Type snapshot ID ${SNAPSHOT_ID} to continue: " confirmation
[[ "${confirmation}" == "${SNAPSHOT_ID}" ]] || { printf 'cancelled\n' >&2; exit 1; }

run_id="$(date -u +%Y%m%d%H%M%S)"
validation_name="survivability-image-validation-${run_id}"
validation_expires="$(date -u -d '+4 hours' +%Y%m%dt%H%M%Sz)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p packer-output
rm -f packer-output/phase4-validation.json
KNOWN_HOSTS="packer-output/known-hosts-validation-${run_id}"
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

hcloud server create \
  --name "${validation_name}" \
  --type "${SERVER_TYPE}" \
  --image "${SNAPSHOT_ID}" \
  --location hel1 \
  --ssh-key "${SSH_KEY}" \
  --without-ipv6 \
  --label project=survivability \
  --label purpose=gold-image-validation \
  --label owner="${OWNER}" \
  --label expires-at="${validation_expires}"

validation_json="$(hcloud server describe "${validation_name}" -o json)"
VALIDATION_ID="$(jq -er '.id' <<<"${validation_json}")"
validation_ip="$(jq -er '.public_net.ipv4.ip' <<<"${validation_json}")"

wait_for_ssh() {
  local _attempt
  printf 'Waiting up to five minutes for SSH on %s. Touch the security key if prompted.\n' "${validation_ip}" >&2
  for _attempt in {1..60}; do
    printf '  SSH attempt %d/60...\n' "${_attempt}" >&2
    if ssh "${ssh_opts[@]}" "root@${validation_ip}" true; then
      return 0
    fi
    sleep 5
  done
  printf 'SSH did not become ready within five minutes\n' >&2
  return 1
}

wait_for_ssh
scp "${ssh_opts[@]}" packer/scripts/validate-gold-image.sh "root@${validation_ip}:/root/"
ssh "${ssh_opts[@]}" "root@${validation_ip}" 'bash /root/validate-gold-image.sh'
boot_id_before="$(ssh "${ssh_opts[@]}" "root@${validation_ip}" 'cat /proc/sys/kernel/random/boot_id')"

hcloud server reboot "${VALIDATION_ID}"
boot_id_after=''
for _attempt in {1..60}; do
  boot_id_after="$(ssh "${ssh_opts[@]}" "root@${validation_ip}" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
  if [[ -n "${boot_id_after}" && "${boot_id_after}" != "${boot_id_before}" ]]; then
    break
  fi
  sleep 5
done
[[ -n "${boot_id_after}" && "${boot_id_after}" != "${boot_id_before}" ]] || {
  printf 'server did not complete a verifiable reboot within five minutes\n' >&2
  exit 1
}

ssh "${ssh_opts[@]}" "root@${validation_ip}" 'bash /root/validate-gold-image.sh'
ssh "${ssh_opts[@]}" "root@${validation_ip}" \
  'apt-mark showhold; systemctl --failed; docker info; docker compose version'
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n \
  --arg run_id "${run_id}" \
  --arg snapshot_id "${SNAPSHOT_ID}" \
  --arg validation_server_id "${VALIDATION_ID}" \
  --arg server_type "${SERVER_TYPE}" \
  --arg started "${started}" \
  --arg finished "${finished}" \
  '{run_id: $run_id, snapshot_id: $snapshot_id, validation_server_id: $validation_server_id, server_type: $server_type, started: $started, finished: $finished, result: "PASS"}' \
  >packer-output/phase4-validation.json

printf '\nValidation passed before and after reboot for snapshot %s.\n' "${SNAPSHOT_ID}"
printf 'Validation evidence: %s\n' "${ROOT}/packer-output/phase4-validation.json"
