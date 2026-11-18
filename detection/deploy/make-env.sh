#!/usr/bin/env bash
# Writes .env for compose.yaml: the bind address and one random password per client.
#   ./make-env.sh <bind address>        e.g. ./make-env.sh 100.64.0.16
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bind="${1:?usage: $0 <bind address>}"
out="$here/.env"
[[ -e "$out" ]] && { echo "$out exists; remove it first to make new secrets" >&2; exit 1; }
random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
umask 077
cat >"$out" <<ENV
BIND_ADDRESS=$bind
FUSION_API_TOKEN=$(random 48)
BROKER_FUSION_USER=fusion
BROKER_FUSION_PASSWORD=$(random 40)
BROKER_PHYSICAL_USER=physical-subsystem
BROKER_PHYSICAL_PASSWORD=$(random 40)
BROKER_RF_USER=rf-console
BROKER_RF_PASSWORD=$(random 40)
ENV
echo "wrote $out (0600). The API token: grep FUSION_API_TOKEN $out"
