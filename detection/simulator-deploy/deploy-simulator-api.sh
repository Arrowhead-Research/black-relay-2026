#!/usr/bin/env bash
# Starts the simulators with their own broker and fusion on this machine, built from this checkout,
# and prints where the fusion API is. Safe to run again: the token and passwords in .env are kept.
#
#   ./deploy-simulator-api.sh                 the API on port 8090 of this machine
#   API_PORT=8091 ./deploy-simulator-api.sh   another port, e.g. beside a real central server
#
# Stop it with:  docker compose down          (add -v to also forget the stored events)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
[ $# -eq 0 ] || { echo "usage: [API_PORT=<port>] $0   (takes no arguments)" >&2; exit 2; }
env="$here/.env"

command -v docker >/dev/null || { echo "Docker is not installed" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (docker compose version)" >&2; exit 1; }

random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }
umask 077
touch "$env"
add() { grep -q "^$1=" "$env" || printf '%s=%s\n' "$1" "$2" >>"$env"; }
# A port given now replaces the one saved earlier; everything else is written once.
replace() { grep -v "^$1=" "$env" >"$env.new" || true; printf '%s=%s\n' "$1" "$2" >>"$env.new"; mv "$env.new" "$env"; }
[ -z "${API_PORT:-}" ] || replace API_PORT "$API_PORT"
add FUSION_API_TOKEN "$(random 48)"
add BROKER_FUSION_USER fusion
add BROKER_FUSION_PASSWORD "$(random 40)"
add BROKER_PHYSICAL_USER physical-subsystem
add BROKER_PHYSICAL_PASSWORD "$(random 40)"
add BROKER_RF_USER rf-console
add BROKER_RF_PASSWORD "$(random 40)"
add BROKER_CAMERA_USER relay
add BROKER_CAMERA_PASSWORD "$(random 16)"

docker compose up -d --build --wait
docker compose ps --format '   {{.Service}}: {{.Status}}'

value() { grep "^$1=" "$env" | cut -d= -f2- || true; }
# The address other machines reach this one on: the one the default route uses.
primary_ip() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}'
  else
    iface="$(route -n get default 2>/dev/null | awk '/interface:/ { print $2 }')"
    [ -z "$iface" ] || ipconfig getifaddr "$iface" 2>/dev/null || true
  fi
}
port="$(value API_PORT)"; port="${port:-8090}"
address="$(primary_ip)"; address="${address:-127.0.0.1}"
cat <<EOF

API                $address:$port
FUSION_API_TOKEN   $(value FUSION_API_TOKEN)

   curl -s -H "Authorization: Bearer $(value FUSION_API_TOKEN)" http://$address:$port/api/v1/health

Make events:  ./mock_rf.sh    ./mock-camera.sh
              docker compose exec physical-sim sh -c 'echo "zone 12 Open" > /app/fusion-service/bench/sim.in'
EOF
