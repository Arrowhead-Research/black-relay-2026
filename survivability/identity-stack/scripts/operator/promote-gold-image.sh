#!/usr/bin/env bash
# Trusted operator workstation only. Labels a validated snapshot; never rebuilds production.
#
# Frozen: the gold image is rebuilt only when the base image itself must change,
# not on a schedule. Ansible owns all host drift.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNAPSHOT_ID=''

usage() {
  cat <<'EOF'
Usage: promote-gold-image.sh --snapshot-id ID

Requires matching successful build and validation JSON files in packer-output/
and HCLOUD_TOKEN, supplied by scripts/operator/with-secrets.sh --tooling.
EOF
}

while (($#)); do
  case "$1" in
    --snapshot-id)
      SNAPSHOT_ID="${2:?--snapshot-id requires an ID}"
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

: "${HCLOUD_TOKEN:?Run through scripts/operator/with-secrets.sh --tooling}"
[[ "${SNAPSHOT_ID}" =~ ^[0-9]+$ ]] || { printf 'snapshot ID must be numeric\n' >&2; exit 2; }
for command_name in hcloud jq date; do
  command -v "${command_name}" >/dev/null || { printf 'missing command: %s\n' "${command_name}" >&2; exit 1; }
done

cd "${ROOT}"
DISCOVERY_EVIDENCE=packer-output/phase4-discovery.json
BUILD_EVIDENCE=packer-output/phase4-build.json
VALIDATION_EVIDENCE=packer-output/phase4-validation.json
for evidence_file in "${DISCOVERY_EVIDENCE}" "${BUILD_EVIDENCE}" "${VALIDATION_EVIDENCE}"; do
  [[ -f "${evidence_file}" ]] || { printf 'missing %s\n' "${evidence_file}" >&2; exit 1; }
done

var_file="$(jq -er '.var_file' "${BUILD_EVIDENCE}")"
jq -e --arg output_file "${var_file}" '.result == "PASS" and .output_file == $output_file' "${DISCOVERY_EVIDENCE}" >/dev/null || {
  printf 'passing discovery evidence does not match the build variable file\n' >&2
  exit 1
}
jq -e --arg id "${SNAPSHOT_ID}" '.snapshot_id == $id' "${BUILD_EVIDENCE}" >/dev/null || {
  printf 'build evidence does not match snapshot ID\n' >&2
  exit 1
}
jq -e --arg id "${SNAPSHOT_ID}" '.snapshot_id == $id and .result == "PASS"' "${VALIDATION_EVIDENCE}" >/dev/null || {
  printf 'passing validation evidence does not match snapshot ID\n' >&2
  exit 1
}

image_json="$(hcloud image describe "${SNAPSHOT_ID}" -o json)"
jq -e --arg id "${SNAPSHOT_ID}" \
  '(.id | tostring) == $id and .type == "snapshot" and .architecture == "x86"' \
  <<<"${image_json}" >/dev/null || {
    printf 'provider image is not the requested x86 snapshot\n' >&2
    exit 1
  }
hcloud image describe "${SNAPSHOT_ID}"
printf '\nThis marks snapshot %s as validated. It does not rebuild production.\n' "${SNAPSHOT_ID}"
read -r -p "Type snapshot ID ${SNAPSHOT_ID} to promote it: " confirmation
[[ "${confirmation}" == "${SNAPSHOT_ID}" ]] || { printf 'cancelled\n' >&2; exit 1; }

run_id="$(jq -er '.run_id' "${BUILD_EVIDENCE}")"
operator="$(jq -er '.owner' "${BUILD_EVIDENCE}")"
build_started="$(jq -er '.started' "${BUILD_EVIDENCE}")"
build_finished="$(jq -er '.finished' "${BUILD_EVIDENCE}")"
builder_server_type="$(jq -er '.server_type' "${BUILD_EVIDENCE}")"
discovery_started="$(jq -er '.started' "${DISCOVERY_EVIDENCE}")"
discovery_finished="$(jq -er '.finished' "${DISCOVERY_EVIDENCE}")"
discovery_server_type="$(jq -er '.server_type' "${DISCOVERY_EVIDENCE}")"
validation_started="$(jq -er '.started' "${VALIDATION_EVIDENCE}")"
validation_finished="$(jq -er '.finished' "${VALIDATION_EVIDENCE}")"
server_type="$(jq -er '.server_type' "${VALIDATION_EVIDENCE}")"
evidence="packer-output/phase4-${run_id}.md"

cat >"${evidence}" <<EOF
# Phase 4 operator evidence

- Operator: ${operator}
- Snapshot ID: ${SNAPSHOT_ID}
- Discovery started: ${discovery_started}
- Discovery finished: ${discovery_finished}
- Discovery server type: ${discovery_server_type}
- Build started: ${build_started}
- Build finished: ${build_finished}
- Builder server type: ${builder_server_type}
- Validation started: ${validation_started}
- Validation finished: ${validation_finished}
- Location: hel1
- Architecture: x86
- Validation server type: ${server_type}
- Package-pin file used: ${var_file}
- Validation result: PASS
EOF

hcloud image add-label --overwrite "${SNAPSHOT_ID}" status=validated
hcloud image update --description \
  "Validated Survivability Debian 13 gold image ${run_id}" \
  "${SNAPSHOT_ID}"
hcloud image describe "${SNAPSHOT_ID}"
printf '%s\n' "${SNAPSHOT_ID}" >packer-output/promoted-snapshot-id

printf '\nPromoted explicit snapshot ID: %s\n' "${SNAPSHOT_ID}"
printf 'Operator evidence: %s/%s\n' "${ROOT}" "${evidence}"
printf '\nValidated snapshots:\n'
hcloud image list \
  --type snapshot \
  --selector project=survivability,role=identity-stack-base,status=validated \
  -o columns=id,name,created,description
