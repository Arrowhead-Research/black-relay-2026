#!/usr/bin/env bash
# Writes the repo-root .env for the root compose.yaml: the bind address, the fusion API token and
# one random broker password per client. Keys already in .env are kept, so re-running is safe.
#   detection/central/make-env.sh               (reads the address from detection/central-store/.env)
#   detection/central/make-env.sh <address>
#   BROKER_CAMERA_PASSWORD=... detection/central/make-env.sh    choose the password kits will type
# It also writes the camera relay's settings (camera-relay-service, included by the root compose):
# RELAY_SITE_SUBNET=... picks the site LAN the camera nodes are on (default: BIND_ADDRESS's /24).
# Then it makes a broker login for each fleet node in detection/rf-subsystem/node/fleet/ansible/inventory.yml
# (RF_NODES="wbt-1 ..." and CAMERA_NODES="cam-1 ..." override the lists), with node-logins.sh --no-restart:
# the broker reads them at its next (re)start, which `docker compose up -d` after this does.
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
# Camera kits share this login and its password is typed on a phone, so it is shorter.
add BROKER_CAMERA_USER relay
add BROKER_CAMERA_PASSWORD "${BROKER_CAMERA_PASSWORD:-$(random 16)}"
# The camera relay's API reads the camera topics it stores with a login of its own.
add BROKER_RELAY_API_USER relay-api
add BROKER_RELAY_API_PASSWORD "$(random 40)"

# The camera relay (camera-relay-service), which the root compose includes with this file as its .env.
# Values in ${...} are references that Compose resolves from the lines above, so the kit login, the
# relay API's login and the address each have one source here. Its own .env is only for running it alone.
bind_now="$(sed -n 's/^BIND_ADDRESS=//p' "$out" | tail -n1)"
add RELAY_SITE_SUBNET "${RELAY_SITE_SUBNET:-$(printf '%s' "$bind_now" | awk -F. 'NF == 4 { print $1 "." $2 "." $3 ".0/24" }')}"
add RELAY_CENTRAL_IP '${BIND_ADDRESS}'
add RELAY_BIND_ADDRESS '${BIND_ADDRESS}'
add RELAY_WEBRTC_HOSTS '${BIND_ADDRESS}'
# The WBT Console has 8080 on this host.
add RELAY_API_PORT 8091
add RELAY_API_TOKEN "$(random 48)"
# Nodes and MediaMTX use the shared kit login; fusion derives the Frigate proxy secret from the same password.
add RELAY_BROKER_HOST '${BIND_ADDRESS}'
add RELAY_BROKER_PORT 1883
add RELAY_BROKER_USER '${BROKER_CAMERA_USER}'
add RELAY_BROKER_PASSWORD '${BROKER_CAMERA_PASSWORD}'
add RELAY_API_BROKER_USER '${BROKER_RELAY_API_USER}'
add RELAY_API_BROKER_PASSWORD '${BROKER_RELAY_API_PASSWORD}'
add RELAY_MQTT_CLIENT_ID "relay-api-$(random 32)"
add RELAY_READ_USER viewer
add RELAY_READ_PASSWORD "$(random 16)"
add RELAY_MEDIAMTX_API_PASSWORD "$(random 32)"

# A broker login per fleet node, so provisioning (over SSH or with node-secrets.sh) finds them ready.
inventory="$root/detection/rf-subsystem/node/fleet/ansible/inventory.yml"
# inventory_hosts <group>: the host names listed under that group in the inventory.
inventory_hosts() {
  [ -f "$inventory" ] || return 0
  awk -v g="$1" '
    { match($0, /^ */); ind = RLENGTH; line = $0; sub(/^ +/, "", line); sub(/ *#.*$/, "", line) }
    line == "" { next }
    in_g && ind <= g_ind { in_g = 0 }
    !in_g && line == g ":" { in_g = 1; g_ind = ind; next }
    in_g && line ~ /^[a-z0-9][a-z0-9-]*:$/ && line != "hosts:" { sub(/:$/, "", line); print line }
  ' "$inventory"
}
rf_nodes="${RF_NODES-$(inventory_hosts rf_nodes | xargs)}"
camera_nodes="${CAMERA_NODES-$(inventory_hosts camera_nodes | xargs)}"
# shellcheck disable=SC2086 # node names are words
[ -z "$rf_nodes" ] || "$here/node-logins.sh" rf --no-restart $rf_nodes
# shellcheck disable=SC2086
[ -z "$camera_nodes" ] || "$here/node-logins.sh" camera --no-restart $camera_nodes
echo "$out is complete (0600). Bridge password: grep BROKER_PHYSICAL_PASSWORD $out"
