#!/usr/bin/env bash
# Trusted operator workstation only. Destructively rebuilds the protected production server.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXPECTED_SERVER_ID=''
EXPECTED_PRIMARY_IPV4_ID=''
EXPECTED_PRIMARY_IPV4=''
SNAPSHOT_ID=''
IDENTITY_FILE=''
EVIDENCE_DIR='operator-output'
USER_DATA=''
KNOWN_HOSTS=''
REBUILD_PROTECTION_DISABLED=false

usage() {
  cat <<'EOF'
Usage: rebuild-production.sh --expected-server-id ID \
  --expected-primary-ipv4-id ID --expected-primary-ipv4 ADDRESS \
  --snapshot-id ID --identity-file PATH [--evidence-dir PATH]

Trusted operator workstation only. Run through with-secrets.sh --tooling for
HCLOUD_TOKEN. Requires the exact
CONFIRM_REBUILD=<expected-server-id> environment value. The identity file must
be the private-key stub whose .pub key will be supplied through one-time
cloud-init user data. The script never reads the private key itself.
EOF
}

restore_rebuild_protection() {
  local attempt server_json
  for attempt in {1..24}; do
    # Current hcloud/API validation requires delete and rebuild to be sent with
    # the same value. Delete protection is already true, so enabling both
    # restores rebuild protection without weakening delete protection.
    if hcloud server enable-protection "${EXPECTED_SERVER_ID}" delete rebuild; then
      server_json="$(hcloud server describe "${EXPECTED_SERVER_ID}" -o json 2>/dev/null || true)"
      if [[ -n "${server_json}" ]] && jq -e '.protection.rebuild == true' <<<"${server_json}" >/dev/null; then
        return 0
      fi
    fi
    printf 'Rebuild protection restore attempt %d/24 failed; retrying...\n' "${attempt}" >&2
    sleep 5
  done
  printf 'CRITICAL: rebuild protection could not be verified. Run immediately:\n' >&2
  printf '  hcloud server enable-protection %s delete rebuild\n' "${EXPECTED_SERVER_ID}" >&2
  return 1
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  rm -f "${USER_DATA}" "${KNOWN_HOSTS}"
  if [[ "${REBUILD_PROTECTION_DISABLED}" == true ]]; then
    printf 'Restoring rebuild protection after interruption or failure...\n' >&2
    if ! restore_rebuild_protection; then
      exit_code=1
    fi
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

while (($#)); do
  case "$1" in
    --expected-server-id)
      EXPECTED_SERVER_ID="${2:?--expected-server-id requires an ID}"
      shift 2
      ;;
    --expected-primary-ipv4-id)
      EXPECTED_PRIMARY_IPV4_ID="${2:?--expected-primary-ipv4-id requires an ID}"
      shift 2
      ;;
    --expected-primary-ipv4)
      EXPECTED_PRIMARY_IPV4="${2:?--expected-primary-ipv4 requires an address}"
      shift 2
      ;;
    --snapshot-id)
      SNAPSHOT_ID="${2:?--snapshot-id requires an ID}"
      shift 2
      ;;
    --identity-file)
      IDENTITY_FILE="${2:?--identity-file requires a path}"
      shift 2
      ;;
    --evidence-dir)
      EVIDENCE_DIR="${2:?--evidence-dir requires a path}"
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
for value_name in EXPECTED_SERVER_ID EXPECTED_PRIMARY_IPV4_ID SNAPSHOT_ID; do
  [[ "${!value_name}" =~ ^[1-9][0-9]*$ ]] || {
    printf '%s must be a positive numeric ID\n' "${value_name}" >&2
    exit 2
  }
done
[[ "${EXPECTED_PRIMARY_IPV4}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
  printf 'expected Primary IPv4 must be an IPv4 address\n' >&2
  exit 2
}
[[ "${CONFIRM_REBUILD:-}" == "${EXPECTED_SERVER_ID}" ]] || {
  printf 'refusing rebuild: set CONFIRM_REBUILD to the exact expected server ID\n' >&2
  exit 2
}
[[ "${IDENTITY_FILE}" = /* ]] || {
  printf 'identity-file path must be absolute; expand the home-directory path first\n' >&2
  exit 2
}
[[ "${IDENTITY_FILE}" != *.pub ]] || {
  printf 'pass the private-key stub, not its .pub file\n' >&2
  exit 2
}
[[ -f "${IDENTITY_FILE}" && -f "${IDENTITY_FILE}.pub" ]] || {
  printf 'identity file and matching .pub file are required\n' >&2
  exit 2
}
for command_name in hcloud jq ssh ssh-keygen date mktemp; do
  command -v "${command_name}" >/dev/null || {
    printf 'missing command: %s\n' "${command_name}" >&2
    exit 1
  }
done
hcloud server rebuild --help | grep -q -- '--user-data-from-file' || {
  printf 'hcloud CLI lacks rebuild user-data support; install a current reviewed release\n' >&2
  exit 1
}

cd "${ROOT}"
[[ "${EVIDENCE_DIR}" = /* ]] || EVIDENCE_DIR="${ROOT}/${EVIDENCE_DIR}"
install -d -m 0700 "${EVIDENCE_DIR}"

promoted_id_file=packer-output/promoted-snapshot-id
validation_file=packer-output/phase4-validation.json
[[ -f "${promoted_id_file}" && -f "${validation_file}" ]] || {
  printf 'local Phase 4 promotion and validation evidence is required\n' >&2
  exit 1
}
[[ "$(tr -d '[:space:]' <"${promoted_id_file}")" == "${SNAPSHOT_ID}" ]] || {
  printf 'snapshot ID does not match the explicitly promoted snapshot\n' >&2
  exit 1
}
jq -e --arg id "${SNAPSHOT_ID}" \
  '.snapshot_id == $id and .result == "PASS"' "${validation_file}" >/dev/null || {
    printf 'snapshot ID does not have matching successful validation evidence\n' >&2
    exit 1
  }

public_key="$(awk 'NF { if (++lines == 1) key=$0 } END { if (lines == 1) print key }' "${IDENTITY_FILE}.pub")"
[[ "${public_key}" =~ ^(ssh-ed25519|sk-ssh-ed25519@openssh.com|ecdsa-sha2-nistp(256|384|521)|sk-ecdsa-sha2-nistp256@openssh.com|ssh-rsa)[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || {
  printf 'the .pub file must contain exactly one valid OpenSSH public-key line\n' >&2
  exit 2
}
ssh-keygen -lf "${IDENTITY_FILE}.pub" >/dev/null
key_fingerprint="$(ssh-keygen -lf "${IDENTITY_FILE}.pub" | awk '{print $2}')"

image_json="$(hcloud image describe "${SNAPSHOT_ID}" -o json)"
jq -e --arg id "${SNAPSHOT_ID}" '
  (.id | tostring) == $id and
  .type == "snapshot" and
  .architecture == "x86" and
  .labels.project == "survivability" and
  .labels.role == "identity-stack-base" and
  .labels.status == "validated" and
  .labels.os == "debian-13"
' <<<"${image_json}" >/dev/null || {
  printf 'provider image is not the requested validated Debian 13 x86 snapshot\n' >&2
  exit 1
}

server_json="$(hcloud server describe "${EXPECTED_SERVER_ID}" -o json)"
primary_ip_json="$(hcloud primary-ip describe "${EXPECTED_PRIMARY_IPV4_ID}" -o json)"
jq -e --arg id "${EXPECTED_SERVER_ID}" --arg ip "${EXPECTED_PRIMARY_IPV4}" '
  (.id | tostring) == $id and
  .status == "running" and
  .server_type.name == "cpx32" and
  .server_type.architecture == "x86" and
  .location.name == "hel1" and
  .public_net.ipv4.ip == $ip and
  (.public_net.ipv6.ip == null or .public_net.ipv6.ip == "" or .public_net.ipv6.ip == "<nil>") and
  .protection.delete == true and
  .protection.rebuild == true and
  .labels.project == "survivability" and
  .labels.environment == "production" and
  .labels.managed_by == "opentofu"
' <<<"${server_json}" >/dev/null || {
  printf 'server identity, location, type, IPv4, labels, IPv6 state, or protection check failed\n' >&2
  exit 1
}
jq -e \
  --arg id "${EXPECTED_PRIMARY_IPV4_ID}" \
  --arg server_id "${EXPECTED_SERVER_ID}" \
  --arg ip "${EXPECTED_PRIMARY_IPV4}" '
  (.id | tostring) == $id and
  .type == "ipv4" and
  .ip == $ip and
  (.assignee_id | tostring) == $server_id and
  .assignee_type == "server" and
  .auto_delete == false and
  .protection.delete == true
' <<<"${primary_ip_json}" >/dev/null || {
  printf 'independent protected Primary IPv4 identity or assignment check failed\n' >&2
  exit 1
}
server_guard="$(jq -c '[.id, .status, .server_type.name, .server_type.architecture, .location.name, .public_net.ipv4.ip, .public_net.ipv6.ip, .protection.delete, .protection.rebuild, .labels.project, .labels.environment, .labels.managed_by]' <<<"${server_json}")"
primary_ip_guard="$(jq -c '[.id, .type, .ip, .assignee_id, .assignee_type, .auto_delete, .protection.delete]' <<<"${primary_ip_json}")"
image_guard="$(jq -c '[.id, .type, .architecture, .labels.project, .labels.role, .labels.status, .labels.os]' <<<"${image_json}")"

if active_context="$(hcloud context active 2>/dev/null)"; then
  printf 'Active hcloud context: %s\n' "${active_context}"
else
  printf 'No named hcloud context; HCLOUD_TOKEN supplies authentication.\n'
fi
hcloud server describe "${EXPECTED_SERVER_ID}"
hcloud primary-ip describe "${EXPECTED_PRIMARY_IPV4_ID}"
hcloud image describe "${SNAPSHOT_ID}"
printf '\nWARNING: the next confirmed operation erases the server filesystem.\n'
printf 'Server ID: %s (CPX32, hel1)\n' "${EXPECTED_SERVER_ID}"
printf 'Primary IPv4 ID/address: %s / %s\n' "${EXPECTED_PRIMARY_IPV4_ID}" "${EXPECTED_PRIMARY_IPV4}"
printf 'Validated snapshot ID: %s\n' "${SNAPSHOT_ID}"
printf 'Bootstrap SSH key fingerprint: %s\n' "${key_fingerprint}"
read -r -p 'Type IMPORTED_STATE_VERIFIED after reviewing a fresh protected OpenTofu plan: ' state_confirmation
[[ "${state_confirmation}" == 'IMPORTED_STATE_VERIFIED' ]] || {
  printf 'cancelled: imported state was not confirmed\n' >&2
  exit 1
}
read -r -p 'Type CONSOLE_RECOVERY_VERIFIED after testing Hetzner console recovery: ' console_confirmation
[[ "${console_confirmation}" == 'CONSOLE_RECOVERY_VERIFIED' ]] || {
  printf 'cancelled: console recovery was not confirmed\n' >&2
  exit 1
}

# Close the review-to-mutation race: all guarded provider metadata must still
# exactly match the values that were displayed and confirmed above.
current_server_json="$(hcloud server describe "${EXPECTED_SERVER_ID}" -o json)"
current_primary_ip_json="$(hcloud primary-ip describe "${EXPECTED_PRIMARY_IPV4_ID}" -o json)"
current_image_json="$(hcloud image describe "${SNAPSHOT_ID}" -o json)"
[[ "$(jq -c '[.id, .status, .server_type.name, .server_type.architecture, .location.name, .public_net.ipv4.ip, .public_net.ipv6.ip, .protection.delete, .protection.rebuild, .labels.project, .labels.environment, .labels.managed_by]' <<<"${current_server_json}")" == "${server_guard}" ]] || {
  printf 'server metadata changed after review; refusing rebuild\n' >&2
  exit 1
}
[[ "$(jq -c '[.id, .type, .ip, .assignee_id, .assignee_type, .auto_delete, .protection.delete]' <<<"${current_primary_ip_json}")" == "${primary_ip_guard}" ]] || {
  printf 'Primary IPv4 metadata changed after review; refusing rebuild\n' >&2
  exit 1
}
[[ "$(jq -c '[.id, .type, .architecture, .labels.project, .labels.role, .labels.status, .labels.os]' <<<"${current_image_json}")" == "${image_guard}" ]] || {
  printf 'snapshot metadata changed after review; refusing rebuild\n' >&2
  exit 1
}

run_id="$(date -u +%Y%m%d%H%M%S)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
evidence_file="${EVIDENCE_DIR}/rebuild-${run_id}.json"
jq -n \
  --arg run_id "${run_id}" \
  --arg started "${started}" \
  --arg server_id "${EXPECTED_SERVER_ID}" \
  --arg primary_ipv4_id "${EXPECTED_PRIMARY_IPV4_ID}" \
  --arg primary_ipv4 "${EXPECTED_PRIMARY_IPV4}" \
  --arg snapshot_id "${SNAPSHOT_ID}" \
  --arg key_fingerprint "${key_fingerprint}" \
  '{run_id: $run_id, started: $started, server_id: $server_id, primary_ipv4_id: $primary_ipv4_id, primary_ipv4: $primary_ipv4, snapshot_id: $snapshot_id, bootstrap_key_fingerprint: $key_fingerprint, result: "PREFLIGHT_PASS"}' \
  >"${evidence_file}"

USER_DATA="$(mktemp "${TMPDIR:-/tmp}/rebuild-cloud-init.XXXXXX")"
KNOWN_HOSTS="$(mktemp "${TMPDIR:-/tmp}/rebuild-known-hosts.XXXXXX")"
chmod 0600 "${USER_DATA}" "${KNOWN_HOSTS}"
cat >"${USER_DATA}" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    lock_passwd: true
    ssh_authorized_keys:
      - >-
        ${public_key}
EOF

printf 'Disabling only rebuild protection for server %s...\n' "${EXPECTED_SERVER_ID}"
# Set the trap guard first: even an ambiguous CLI failure must attempt restore.
REBUILD_PROTECTION_DISABLED=true
hcloud server disable-protection "${EXPECTED_SERVER_ID}" rebuild

# --quiet and /dev/null ensure a provider-generated root password is never shown
# or persisted. One-time cloud-init installs only the reviewed public SSH key.
hcloud server rebuild --quiet \
  --image "${SNAPSHOT_ID}" \
  --user-data-from-file "${USER_DATA}" \
  "${EXPECTED_SERVER_ID}" >/dev/null

printf 'Rebuild action completed; immediately restoring rebuild protection...\n'
restore_rebuild_protection
REBUILD_PROTECTION_DISABLED=false

post_server_json=''
for attempt in {1..60}; do
  post_server_json="$(hcloud server describe "${EXPECTED_SERVER_ID}" -o json)"
  if jq -e --arg id "${EXPECTED_SERVER_ID}" --arg image_id "${SNAPSHOT_ID}" --arg ip "${EXPECTED_PRIMARY_IPV4}" '
    (.id | tostring) == $id and
    (.image.id | tostring) == $image_id and
    .status == "running" and
    .server_type.name == "cpx32" and
    .server_type.architecture == "x86" and
    .location.name == "hel1" and
    .public_net.ipv4.ip == $ip and
    (.public_net.ipv6.ip == null or .public_net.ipv6.ip == "" or .public_net.ipv6.ip == "<nil>") and
    .protection.delete == true and
    .protection.rebuild == true and
    .labels.project == "survivability" and
    .labels.environment == "production" and
    .labels.managed_by == "opentofu"
  ' <<<"${post_server_json}" >/dev/null; then
    break
  fi
  printf 'Waiting for verified provider state (%d/60)...\n' "${attempt}" >&2
  sleep 5
done
jq -e --arg id "${EXPECTED_SERVER_ID}" --arg image_id "${SNAPSHOT_ID}" --arg ip "${EXPECTED_PRIMARY_IPV4}" '
  (.id | tostring) == $id and
  (.image.id | tostring) == $image_id and
  .status == "running" and
  .server_type.name == "cpx32" and
  .server_type.architecture == "x86" and
  .location.name == "hel1" and
  .public_net.ipv4.ip == $ip and
  (.public_net.ipv6.ip == null or .public_net.ipv6.ip == "" or .public_net.ipv6.ip == "<nil>") and
  .protection.delete == true and
  .protection.rebuild == true and
  .labels.project == "survivability" and
  .labels.environment == "production" and
  .labels.managed_by == "opentofu"
' <<<"${post_server_json}" >/dev/null || {
  printf 'post-rebuild server identity, image, running state, IPv4, or protection check failed\n' >&2
  exit 1
}
post_primary_ip_json="$(hcloud primary-ip describe "${EXPECTED_PRIMARY_IPV4_ID}" -o json)"
jq -e \
  --arg id "${EXPECTED_PRIMARY_IPV4_ID}" \
  --arg server_id "${EXPECTED_SERVER_ID}" \
  --arg ip "${EXPECTED_PRIMARY_IPV4}" '
  (.id | tostring) == $id and
  .type == "ipv4" and
  .ip == $ip and
  (.assignee_id | tostring) == $server_id and
  .assignee_type == "server" and
  .auto_delete == false and
  .protection.delete == true
' <<<"${post_primary_ip_json}" >/dev/null || {
  printf 'Primary IPv4 changed or lost protection during rebuild\n' >&2
  exit 1
}

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
printf 'Waiting up to five minutes for emergency SSH access. Use FIDO2 PIN/touch if prompted.\n'
ssh_ready=false
for attempt in {1..60}; do
  if ssh "${ssh_opts[@]}" "root@${EXPECTED_PRIMARY_IPV4}" true; then
    ssh_ready=true
    break
  fi
  printf 'SSH attempt %d/60 failed; retrying...\n' "${attempt}" >&2
  sleep 5
done
[[ "${ssh_ready}" == true ]] || {
  printf 'emergency SSH access did not become ready; use the verified console path\n' >&2
  exit 1
}
ssh "${ssh_opts[@]}" "root@${EXPECTED_PRIMARY_IPV4}" '
  set -eu
  cloud-init status --wait
  . /etc/os-release
  test "${ID}" = debian
  test "${VERSION_ID}" = 13
  test "$(dpkg --print-architecture)" = amd64
  test -s /etc/machine-id
  sshd -t
'

finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n \
  --arg run_id "${run_id}" \
  --arg started "${started}" \
  --arg finished "${finished}" \
  --arg server_id "${EXPECTED_SERVER_ID}" \
  --arg primary_ipv4_id "${EXPECTED_PRIMARY_IPV4_ID}" \
  --arg primary_ipv4 "${EXPECTED_PRIMARY_IPV4}" \
  --arg snapshot_id "${SNAPSHOT_ID}" \
  --arg key_fingerprint "${key_fingerprint}" \
  '{run_id: $run_id, started: $started, finished: $finished, server_id: $server_id, primary_ipv4_id: $primary_ipv4_id, primary_ipv4: $primary_ipv4, snapshot_id: $snapshot_id, bootstrap_key_fingerprint: $key_fingerprint, server_type: "cpx32", location: "hel1", os: "debian-13", architecture: "amd64", delete_protection: true, rebuild_protection: true, emergency_ssh: "PASS", result: "PASS"}' \
  >"${evidence_file}"

printf '\nPhase 5 provider, boot, and emergency-access checks passed.\n'
printf 'Evidence: %s\n' "${evidence_file}"
printf 'Run the post-rebuild OpenTofu convergence plan before releasing the single-writer window.\n'
