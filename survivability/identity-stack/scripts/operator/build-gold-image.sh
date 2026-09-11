#!/usr/bin/env bash
# Trusted operator workstation only. Creates a temporary builder and snapshot.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VAR_FILE="packer/package-versions.auto.pkrvars.hcl"
SERVER_TYPE='cx23'
OWNER="$(id -un | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-*//; s/-*$//' | cut -c1-63 | sed 's/-*$//')"

usage() {
  cat <<'EOF'
Usage: build-gold-image.sh [--var-file PATH] [--server-type TYPE] [--owner LABEL]

Requires HCLOUD_TOKEN in the environment. The token is never read from a file.
EOF
}

while (($#)); do
  case "$1" in
    --var-file)
      VAR_FILE="${2:?--var-file requires a path}"
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
[[ ${#OWNER} -le 63 && "${OWNER}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || {
  printf 'owner must be a lowercase alphanumeric/hyphen Hetzner label value\n' >&2
  exit 2
}
for command_name in packer hcloud jq date; do
  command -v "${command_name}" >/dev/null || { printf 'missing command: %s\n' "${command_name}" >&2; exit 1; }
done

cd "${ROOT}"
[[ "${VAR_FILE}" = /* ]] || VAR_FILE="${ROOT}/${VAR_FILE}"
[[ -f "${VAR_FILE}" ]] || { printf 'variable file not found: %s\n' "${VAR_FILE}" >&2; exit 1; }
! grep -q 'REPLACE_WITH' "${VAR_FILE}" || { printf 'variable file still contains placeholders\n' >&2; exit 1; }
packer version | grep -q '1.16.0' || { printf 'Packer 1.16.0 is required\n' >&2; exit 1; }

if active_context="$(hcloud context active 2>/dev/null)"; then
  printf 'Active hcloud context: %s\n' "${active_context}"
else
  printf 'No named hcloud context; HCLOUD_TOKEN supplies authentication.\n'
fi
printf 'This creates a chargeable temporary builder and candidate snapshot.\n'
read -r -p 'Type BUILD_GOLD_IMAGE to continue: ' confirmation
[[ "${confirmation}" == "BUILD_GOLD_IMAGE" ]] || { printf 'cancelled\n' >&2; exit 1; }

run_id="$(date -u +%Y%m%d%H%M%S)"
build_expires="$(date -u -d '+4 hours' +%Y%m%dt%H%M%Sz)"
mkdir -p packer-output
rm -f packer-output/manifest.json

packer init packer
packer fmt -check -recursive packer
packer validate \
  -var-file="${VAR_FILE}" \
  -var "build_owner=${OWNER}" \
  -var "build_expires_at=${build_expires}" \
  -var "server_type=${SERVER_TYPE}" \
  packer

started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
packer build -on-error=cleanup \
  -var-file="${VAR_FILE}" \
  -var "build_owner=${OWNER}" \
  -var "build_expires_at=${build_expires}" \
  -var "server_type=${SERVER_TYPE}" \
  packer
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

snapshot_id="$(jq -er '.builds[-1].artifact_id | match("[0-9]+").string' packer-output/manifest.json)"
[[ "${snapshot_id}" =~ ^[0-9]+$ ]] || { printf 'invalid snapshot ID in manifest\n' >&2; exit 1; }
hcloud image describe "${snapshot_id}"

leaked_builders="$(hcloud server list --selector project=survivability,purpose=gold-image-builder -o json | jq 'length')"
if [[ "${leaked_builders}" != "0" ]]; then
  printf 'one or more gold-image builders remain; inspect and remove them by ID\n' >&2
  hcloud server list --selector project=survivability,purpose=gold-image-builder
  exit 1
fi

jq -n \
  --arg run_id "${run_id}" \
  --arg owner "${OWNER}" \
  --arg snapshot_id "${snapshot_id}" \
  --arg server_type "${SERVER_TYPE}" \
  --arg started "${started}" \
  --arg finished "${finished}" \
  --arg var_file "${VAR_FILE}" \
  '{run_id: $run_id, owner: $owner, snapshot_id: $snapshot_id, server_type: $server_type, started: $started, finished: $finished, var_file: $var_file}' \
  >packer-output/phase4-build.json

printf '\nCandidate snapshot ID: %s\n' "${snapshot_id}"
printf 'Build evidence: %s\n' "${ROOT}/packer-output/phase4-build.json"
printf 'Next: scripts/operator/validate-gold-image.sh --snapshot-id %s --ssh-key <HETZNER_KEY_NAME_OR_ID>\n' "${snapshot_id}"
