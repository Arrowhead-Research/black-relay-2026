#!/usr/bin/env bash
# For provisioning RF (WBT) nodes without SSH from the admin machine to this server. Run it here, on the
# central server, then hand its output to node/fleet/fleet.sh on the admin machine:
#
#   detection/central/rf-node-secrets.sh wbt-1 wbt-2 wbt-3 wbt-4 > rf-node-secrets
#
# It makes sure each node has a broker login (rf-node-logins.sh) and the WBT Console has a heartbeat
# token (CONSOLE_NODE_TOKEN in detection/rf-subsystem/central/.env; a new one restarts the console),
# then prints "token <value>" and one "<node> <broker password>" line per node. Existing values are
# kept, so re-running is safe. The output is secret: keep the file 0600 and delete it after provisioning.
# A new token is enforced at once, so nodes that already send heartbeats are rejected until provisioned.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
rf_env="$root/detection/rf-subsystem/central/.env"
[ $# -gt 0 ] || { sed -n '2,11p' "$0" >&2; exit 2; }

"$here/rf-node-logins.sh" "$@" >&2

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
"$here/rf-node-logins.sh" --get "$@"
