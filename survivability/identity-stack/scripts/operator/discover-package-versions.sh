#!/usr/bin/env bash
# Trusted operator workstation only. Creates and always removes a discovery server.
#
# Frozen: the gold image is rebuilt only when the base image itself must change,
# not on a schedule. Ansible owns all host drift.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_KEY=''
IDENTITY_FILE=''
SERVER_TYPE='cx23'
OWNER="$(id -un | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63 | sed 's/-*$//')"
OUTPUT_FILE='packer/package-versions.auto.pkrvars.hcl'
DISCOVERY_ID=''
KNOWN_HOSTS=''
TEMP_OUTPUT=''

usage() {
  cat <<'EOF'
Usage: discover-package-versions.sh --ssh-key NAME_OR_ID --identity-file PATH
                                    [--server-type TYPE] [--owner LABEL]
                                    [--output PATH.pkrvars.hcl]

Run through scripts/operator/with-secrets.sh --tooling. Also needs the private-key stub matching the registered Hetzner
public key. FIDO2 keys visibly request PIN and touch during SSH operations.
Creates a disposable Debian 13 server and writes exact package pins locally.
EOF
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${DISCOVERY_ID}" ]]; then
    printf 'Removing package-discovery server %s...\n' "${DISCOVERY_ID}" >&2
    hcloud server delete "${DISCOVERY_ID}" || true
  fi
  [[ -z "${KNOWN_HOSTS}" ]] || rm -f "${KNOWN_HOSTS}"
  [[ -z "${TEMP_OUTPUT}" ]] || rm -f "${TEMP_OUTPUT}"
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

while (($#)); do
  case "$1" in
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
    --output)
      OUTPUT_FILE="${2:?--output requires a path}"
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

: "${HCLOUD_TOKEN:?Run through scripts/operator/with-secrets.sh --tooling}"
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
[[ "${OUTPUT_FILE}" == *.pkrvars.hcl ]] || { printf 'output must end in .pkrvars.hcl\n' >&2; exit 2; }
for command_name in hcloud jq ssh scp ssh-keygen date mktemp; do
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
[[ -x packer/scripts/discover-package-versions.sh ]] || { printf 'discovery payload is missing\n' >&2; exit 1; }
[[ "${OUTPUT_FILE}" = /* ]] || OUTPUT_FILE="${ROOT}/${OUTPUT_FILE}"
mkdir -p "$(dirname "${OUTPUT_FILE}")" packer-output

if active_context="$(hcloud context active 2>/dev/null)"; then
  printf 'Active hcloud context: %s\n' "${active_context}"
else
  printf 'No named hcloud context; HCLOUD_TOKEN supplies authentication.\n'
fi
printf 'This creates a chargeable disposable Debian 13 package-discovery server.\n'
read -r -p 'Type DISCOVER_PACKAGE_VERSIONS to continue: ' confirmation
[[ "${confirmation}" == "DISCOVER_PACKAGE_VERSIONS" ]] || { printf 'cancelled\n' >&2; exit 1; }

run_id="$(date -u +%Y%m%d%H%M%S)"
discovery_name="survivability-package-discovery-${run_id}"
discovery_expires="$(date -u -d '+2 hours' +%Y%m%dt%H%M%Sz)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
KNOWN_HOSTS="packer-output/known-hosts-discovery-${run_id}"
TEMP_OUTPUT="$(mktemp "packer-output/package-versions-${run_id}.XXXXXX.tmp")"
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
  --name "${discovery_name}" \
  --type "${SERVER_TYPE}" \
  --image debian-13 \
  --location hel1 \
  --ssh-key "${SSH_KEY}" \
  --without-ipv6 \
  --label project=survivability \
  --label purpose=package-discovery \
  --label owner="${OWNER}" \
  --label expires-at="${discovery_expires}"

discovery_json="$(hcloud server describe "${discovery_name}" -o json)"
DISCOVERY_ID="$(jq -er '.id' <<<"${discovery_json}")"
discovery_ip="$(jq -er '.public_net.ipv4.ip' <<<"${discovery_json}")"

printf 'Waiting up to five minutes for SSH on %s. Touch the security key if prompted.\n' "${discovery_ip}" >&2
ssh_ready=false
for _attempt in {1..60}; do
  printf '  SSH attempt %d/60...\n' "${_attempt}" >&2
  if ssh "${ssh_opts[@]}" "root@${discovery_ip}" true; then
    ssh_ready=true
    break
  fi
  sleep 5
done
[[ "${ssh_ready}" == true ]] || { printf 'SSH did not become ready within five minutes\n' >&2; exit 1; }

printf 'SSH is ready; copying the package-discovery payload...\n' >&2
scp "${ssh_opts[@]}" packer/scripts/discover-package-versions.sh "root@${discovery_ip}:/root/"
printf 'Running repository verification and package discovery remotely...\n' >&2
ssh "${ssh_opts[@]}" "root@${discovery_ip}" \
  'bash /root/discover-package-versions.sh' >"${TEMP_OUTPUT}"
printf 'Remote package discovery completed; validating its output...\n' >&2

! grep -q 'REPLACE_WITH' "${TEMP_OUTPUT}" || { printf 'discovery output contains placeholders\n' >&2; exit 1; }
[[ "$(wc -l <"${TEMP_OUTPUT}")" == 7 ]] || {
  printf 'discovery output contains unexpected lines; refusing to write invalid HCL\n' >&2
  cat "${TEMP_OUTPUT}" >&2
  exit 1
}
grep -Eq '^snapshot_epoch = "[0-9]{12}"$' "${TEMP_OUTPUT}" || {
  printf 'discovery output has an invalid snapshot_epoch\n' >&2
  exit 1
}
for variable_name in \
  docker_package_version \
  docker_cli_package_version \
  containerd_package_version \
  docker_buildx_package_version \
  docker_compose_package_version; do
  grep -Eq "^${variable_name} = \"[^\"]+\"$" "${TEMP_OUTPUT}" || {
    printf 'discovery output is missing %s\n' "${variable_name}" >&2
    exit 1
  }
done

mv "${TEMP_OUTPUT}" "${OUTPUT_FILE}"
TEMP_OUTPUT=''
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n \
  --arg run_id "${run_id}" \
  --arg owner "${OWNER}" \
  --arg discovery_server_id "${DISCOVERY_ID}" \
  --arg server_type "${SERVER_TYPE}" \
  --arg started "${started}" \
  --arg finished "${finished}" \
  --arg output_file "${OUTPUT_FILE}" \
  '{run_id: $run_id, owner: $owner, discovery_server_id: $discovery_server_id, server_type: $server_type, started: $started, finished: $finished, output_file: $output_file, result: "PASS"}' \
  >packer-output/phase4-discovery.json

hcloud server delete "${DISCOVERY_ID}"
DISCOVERY_ID=''

printf '\nPackage discovery passed. Exact pins: %s\n' "${OUTPUT_FILE}"
printf 'Discovery evidence: %s\n' "${ROOT}/packer-output/phase4-discovery.json"
cat "${OUTPUT_FILE}"
