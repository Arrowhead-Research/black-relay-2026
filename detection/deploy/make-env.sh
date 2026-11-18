#!/usr/bin/env bash
# Writes .env for compose.yaml: the bind address and one random password per client. With an
# existing .env it only appends the keys that are missing, so secrets already in use stay put.
#   ./make-env.sh <bind address>        e.g. ./make-env.sh 172.22.41.10
#   BROKER_CAMERA_PASSWORD=... ./make-env.sh <bind address>   choose the password kits will type
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bind="${1:?usage: $0 <bind address>}"
out="$here/.env"
random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
umask 077
touch "$out"
add() { grep -q "^$1=" "$out" || printf '%s=%s\n' "$1" "$2" >>"$out"; }
add BIND_ADDRESS "$bind"
add FUSION_API_TOKEN "$(random 48)"
add BROKER_FUSION_USER fusion
add BROKER_FUSION_PASSWORD "$(random 40)"
add BROKER_PHYSICAL_USER physical-subsystem
add BROKER_PHYSICAL_PASSWORD "$(random 40)"
add BROKER_RF_USER rf-console
add BROKER_RF_PASSWORD "$(random 40)"
# Camera kits share this login and its password is typed on a phone, so it is shorter.
add BROKER_CAMERA_USER relay
add BROKER_CAMERA_PASSWORD "${BROKER_CAMERA_PASSWORD:-$(random 16)}"
echo "$out is complete (0600). API token: grep FUSION_API_TOKEN $out"
