#!/usr/bin/env bash
# For provisioning fleet nodes without SSH from the admin machine to this server. Run it here, on the
# central server, from the repo root, then hand its output to node/fleet/fleet.sh on the admin machine
# (CENTRAL_SECRETS=<file>). One file can hold both kinds:
#
#   detection/central/node-secrets.sh rf wbt-1 wbt-2 wbt-3 wbt-4 > node-secrets
#   detection/central/node-secrets.sh camera cam-1 cam-2 >> node-secrets
#
# Both make sure each node has a broker login (node-logins.sh) and print one "<node> <broker password>"
# line per node.
#   rf      also makes sure the WBT Console has a heartbeat token (CONSOLE_NODE_TOKEN in
#           detection/rf-subsystem/central/.env; a new one restarts the console) and prints "token <value>".
#           A new token is enforced at once, so nodes that already send heartbeats are rejected until
#           provisioned.
#   camera  also prints the shared kit login from the root .env (BROKER_CAMERA_USER, BROKER_CAMERA_PASSWORD)
#           as "relay_user <user>" and "relay_password <password>". It never changes them.
# Existing values are kept, so re-running is safe. The output is secret: keep the file 0600 and delete it
# after provisioning. detection/central/rf-node-secrets.sh is the rf form under its old name.
set -euo pipefail
usage() { sed -n '2,/^set -/{/^set -/!p}' "$0" >&2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
case "${1:-}" in
rf | camera) kind="$1"; shift ;;
*) usage; exit 2 ;;
esac
[ $# -gt 0 ] || { usage; exit 2; }

"$here/node-logins.sh" "$kind" "$@" >&2

if [ "$kind" = rf ]; then
  rf_env="$root/detection/rf-subsystem/central/.env"
  token=""
  [ ! -f "$rf_env" ] || token="$(sed -n 's/^CONSOLE_NODE_TOKEN=//p' "$rf_env" | tail -n1)"
  if [ -z "$token" ]; then
    token="$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 48 || true)"
    umask 077
    { grep -v '^CONSOLE_NODE_TOKEN=' "$rf_env" 2>/dev/null || true; printf 'CONSOLE_NODE_TOKEN=%s\n' "$token"; } >"$rf_env.new"
    mv "$rf_env.new" "$rf_env"
    echo "new console heartbeat token" >&2
    if (cd "$root" && docker compose ps --status running --services 2>/dev/null | grep -qx rf-console); then
      (cd "$root" && docker compose up -d rf-console >/dev/null 2>&1) && echo "console restarted with it" >&2
    fi
  fi
  printf 'token %s\n' "$token"
else
  env_file="$root/.env"
  relay_user="$(sed -n 's/^BROKER_CAMERA_USER=//p' "$env_file" | tail -n1)"
  relay_password="$(sed -n 's/^BROKER_CAMERA_PASSWORD=//p' "$env_file" | tail -n1)"
  [ -n "$relay_password" ] || { echo "BROKER_CAMERA_PASSWORD is not set in $env_file: run detection/central/make-env.sh" >&2; exit 1; }
  printf 'relay_user %s\nrelay_password %s\n' "${relay_user:-relay}" "$relay_password"
fi
"$here/node-logins.sh" "$kind" --get "$@"
