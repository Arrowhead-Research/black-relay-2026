#!/usr/bin/env bash
# Writes the repo-root .env for the root compose.yaml: the bind address, the fusion API token and
# one random broker password per client. Keys already in .env are kept, so re-running is safe.
#   detection/central/make-env.sh               (reads the address from detection/central-store/.env)
#   detection/central/make-env.sh <address>
#   BROKER_CAMERA_PASSWORD=... detection/central/make-env.sh    choose the password kits will type
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
store_env="$root/detection/central-store/.env"
bind="${1:-}"
[ -n "$bind" ] || bind="$(grep '^BIND_ADDRESS=' "$store_env" 2>/dev/null | cut -d= -f2 || true)"
[ -n "$bind" ] || { echo "usage: $0 <address> (or run detection/central-store/server.sh init first)" >&2; exit 1; }
out="$root/.env"
random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }
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
add BROKER_RF_ANALYTICS_USER rf-analytics
add BROKER_RF_ANALYTICS_PASSWORD "$(random 40)"
# Camera kits share this login and its password is typed on a phone, so it is shorter.
add BROKER_CAMERA_USER relay
add BROKER_CAMERA_PASSWORD "${BROKER_CAMERA_PASSWORD:-$(random 16)}"
echo "$out is complete (0600). Bridge password: grep BROKER_PHYSICAL_PASSWORD $out"
