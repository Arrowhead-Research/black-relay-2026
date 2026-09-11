#!/usr/bin/env bash
# Trusted operator workstation only. Labels a validated snapshot; never rebuilds production.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNAPSHOT_ID=''
DISCOVERY_COST=''
BUILDER_COST=''
VALIDATION_COST=''
SNAPSHOT_COST=''

usage() {
  cat <<'EOF'
Usage: promote-gold-image.sh --snapshot-id ID \
  --discovery-cost TEXT --builder-cost TEXT --validation-cost TEXT \
  --snapshot-storage-cost TEXT

Costs are evidence strings based on current Hetzner pricing, for example
"EUR 0.01". Requires matching successful build and validation JSON files in
packer-output/ and HCLOUD_TOKEN in the environment.
EOF
}

while (($#)); do
  case "$1" in
    --snapshot-id)
      SNAPSHOT_ID="${2:?--snapshot-id requires an ID}"
      shift 2
      ;;
    --discovery-cost)
      DISCOVERY_COST="${2:?--discovery-cost requires text}"
      shift 2
      ;;
    --builder-cost)
      BUILDER_COST="${2:?--builder-cost requires text}"
      shift 2
      ;;
    --validation-cost)
      VALIDATION_COST="${2:?--validation-cost requires text}"
      shift 2
      ;;
    --snapshot-storage-cost)
      SNAPSHOT_COST="${2:?--snapshot-storage-cost requires text}"
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
[[ "${SNAPSHOT_ID}" =~ ^[0-9]+$ ]] || { printf 'snapshot ID must be numeric\n' >&2; exit 2; }
for value_name in DISCOVERY_COST BUILDER_COST VALIDATION_COST SNAPSHOT_COST; do
  [[ -n "${!value_name}" ]] || { printf 'all four cost arguments are required\n' >&2; exit 2; }
  [[ "${!value_name}" != *REPLACE_WITH* ]] || { printf 'replace all cost placeholders before promotion\n' >&2; exit 2; }
done
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
- Discovery server cost: ${DISCOVERY_COST}
- Packer builder cost: ${BUILDER_COST}
- Validation server cost: ${VALIDATION_COST}
- Snapshot storage cost: ${SNAPSHOT_COST}
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
printf '\nRetain this snapshot and one previous validated snapshot:\n'
hcloud image list \
  --type snapshot \
  --selector project=survivability,role=identity-stack-base,status=validated \
  -o columns=id,name,created,description
