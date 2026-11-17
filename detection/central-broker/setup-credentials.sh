#!/usr/bin/env bash
# Writes .env with one random password per client, mode 0600. Prints no secret and refuses to
# overwrite an existing .env without --force. The container builds the password file from .env
# at every start (entrypoint.sh); nothing on the host needs mosquitto installed.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
output="$here/.env"
force=0
while (($#)); do
    case "$1" in
    --force) force=1 ;;
    -h | --help) echo "usage: $0 [--force]"; exit 0 ;;
    *) echo "usage: $0 [--force]" >&2; exit 2 ;;
    esac
    shift
done
if [[ -e "$output" && "$force" != 1 ]]; then
    echo "$output already exists; not overwriting it. Use --force to make new secrets." >&2
    exit 1
fi

random() {
    local value
    # Letters and digits only: nothing an env file or a YAML value treats specially.
    value="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c "$1" || true)"
    if ((${#value} != $1)); then
        echo "could not read random bytes" >&2
        return 1
    fi
    printf '%s' "$value"
}

fusion_password="$(random 40)"
physical_password="$(random 40)"
rf_password="$(random 40)"

umask 077
cat >"$output" <<ENV
# central-broker credentials, written by setup-credentials.sh. Never commit this file.
# Each publisher's lead gets exactly its own line; fusion gets the read-only one.

# The central node's address on the Detection network. The broker binds to this address only.
BROKER_BIND_ADDRESS=${BROKER_BIND_ADDRESS:-CHANGE-ME}
BROKER_PORT=1883

BROKER_FUSION_USER=fusion
BROKER_FUSION_PASSWORD=$fusion_password
BROKER_PHYSICAL_USER=physical-subsystem
BROKER_PHYSICAL_PASSWORD=$physical_password
BROKER_RF_USER=rf-console
BROKER_RF_PASSWORD=$rf_password
ENV
echo "wrote $output (0600). Set BROKER_BIND_ADDRESS, then: docker compose up -d"
