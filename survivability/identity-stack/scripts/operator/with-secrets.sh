#!/usr/bin/env bash
# Trusted operator workstation only. Resolves the operator age identity and runs
# a command with SOPS-managed credentials injected into that child process only.
#
# Nothing is written to disk, no credential value is ever printed, and the age
# identity is scrubbed from the child environment unless the command itself
# needs to decrypt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOOLING_FILE="${ROOT}/secrets/tooling.sops.env"
OP_ENV_FILE="${ROOT}/.env.op.local"
DEFAULT_AGE_KEY="${XDG_CONFIG_HOME:-${HOME}/.config}/sops/age/keys.txt"
WANT_TOOLING=false
WANT_AGE=false

usage() {
  cat <<'EOF'
Usage: with-secrets.sh [--tooling] [--age] -- COMMAND [ARG...]

  --tooling  Inject secrets/tooling.sops.env (provider credentials and TF_VAR_*)
             into the command. Without --age the age identity is scrubbed, so
             OpenTofu and Packer receive provider credentials and nothing else.
  --age      Export the resolved age identity so the command can decrypt SOPS
             files itself. Required for ansible-playbook.

At least one scope is required. Examples:

  with-secrets.sh --tooling -- tofu -chdir=tofu plan -var-file=production.tfvars
  with-secrets.sh --age -- ansible-playbook ansible/playbooks/site.yml
  with-secrets.sh --tooling --age -- ./scripts/operator/test-backup-restore.sh ...

The age identity is resolved from, in order: an already-exported
SOPS_AGE_KEY_FILE; a 1Password reference in .env.op.local; or
${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt.
EOF
}

while (($#)); do
  case "$1" in
    --tooling)
      WANT_TOOLING=true
      shift
      ;;
    --age)
      WANT_AGE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -e /.dockerenv ]]; then
  printf 'refusing to run inside a container; this is a trusted-workstation command\n' >&2
  exit 1
fi
"${WANT_TOOLING}" || "${WANT_AGE}" || {
  printf 'specify --tooling, --age, or both\n' >&2
  usage >&2
  exit 2
}
(($#)) || { printf 'no command given after --\n' >&2; usage >&2; exit 2; }
command -v sops >/dev/null || { printf 'missing command: sops\n' >&2; exit 1; }

# Resolve exactly one age identity. First match wins; never print the material.
if [[ -n "${SOPS_AGE_KEY_FILE:-}" && -r "${SOPS_AGE_KEY_FILE}" ]]; then
  export SOPS_AGE_KEY_FILE
elif [[ -f "${OP_ENV_FILE}" ]] && command -v op >/dev/null; then
  op_reference="$(sed -n 's/^SOPS_AGE_KEY=//p' "${OP_ENV_FILE}" | head -n 1)"
  [[ -n "${op_reference}" ]] || {
    printf '%s must contain a single SOPS_AGE_KEY=op://... line\n' "${OP_ENV_FILE}" >&2
    exit 1
  }
  SOPS_AGE_KEY="$(op read "${op_reference}")"
  export SOPS_AGE_KEY
elif [[ -f "${DEFAULT_AGE_KEY}" ]]; then
  export SOPS_AGE_KEY_FILE="${DEFAULT_AGE_KEY}"
else
  printf 'no age identity found. Generate one with:\n' >&2
  printf '  mkdir -p -m 700 %s\n' "$(dirname "${DEFAULT_AGE_KEY}")" >&2
  printf '  age-keygen -o %s && chmod 600 %s\n' "${DEFAULT_AGE_KEY}" "${DEFAULT_AGE_KEY}" >&2
  printf 'then send the age-keygen -y output to an operator. See docs/runbooks/secrets.md.\n' >&2
  exit 1
fi

cd "${ROOT}"

if ! "${WANT_TOOLING}"; then
  exec "$@"
fi

[[ -f "${TOOLING_FILE}" ]] || {
  printf 'missing %s; create it with: sops secrets/tooling.sops.env\n' "${TOOLING_FILE}" >&2
  exit 1
}

# sops exec-env hands its argument to a shell, so quote the command explicitly.
quoted_command="$(printf '%q ' "$@")"
if "${WANT_AGE}"; then
  exec sops exec-env "${TOOLING_FILE}" "exec ${quoted_command}"
fi
exec sops exec-env "${TOOLING_FILE}" "exec env -u SOPS_AGE_KEY -u SOPS_AGE_KEY_FILE ${quoted_command}"
