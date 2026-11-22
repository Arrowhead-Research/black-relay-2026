#!/usr/bin/env bash
# Gives each WBT (RF) node or camera node its own central broker login. RF nodes get rf-node-<node>,
# allowed to write detection/rf/nodes/<node>/# only. Camera nodes get cam-node-<node>, allowed to
# write detection/camera/# and read detection/rf/cues/camera. The passwords live in the repo-root
# .env as BROKER_RF_NODE_LOGINS or BROKER_CAMERA_NODE_LOGINS ("node:password ..."); existing ones are
# kept, so re-running is safe. Run on the central server:
#
#   detection/central/node-logins.sh rf wbt-1 wbt-2 ...       add missing logins, then re-create the broker
#   detection/central/node-logins.sh camera cam-1 ...         the same for camera nodes
#   detection/central/node-logins.sh <rf|camera> --get cam-1  print "<node> <password>" lines (for provisioning
#                                                             over SSH; the fleet playbooks read them, no_log)
# --no-restart leaves the broker running with its old logins.
# detection/central/rf-node-logins.sh is the rf form under its old name.
set -euo pipefail
usage() { sed -n '2,/^set -/{/^set -/!p}' "$0"; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
env_file="$root/.env"

case "${1:-}" in
rf) env_key=BROKER_RF_NODE_LOGINS login=rf-node- ;;
camera) env_key=BROKER_CAMERA_NODE_LOGINS login=cam-node- ;;
*) echo "first argument must be rf or camera, not '${1:-}'" >&2; usage; exit 2 ;;
esac
kind="$1"
shift

get=0 restart=1 nodes=()
for a in "$@"; do
  case "$a" in
    --get) get=1 ;;
    --no-restart) restart=0 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) [[ "$a" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "node names are lowercase letters, digits and dashes: $a" >&2; exit 2; }
       nodes+=("$a") ;;
  esac
done
[ ${#nodes[@]} -gt 0 ] || { usage; exit 2; }
[ -f "$env_file" ] || { echo "no $env_file: run detection/central/make-env.sh first" >&2; exit 1; }

current="$(sed -n "s/^$env_key=//p" "$env_file" | tail -n1)"
password_of() { tr ' ' '\n' <<<"$current" | sed -n "s/^$1://p" | sed -n 1p; }

if [ "$get" = 1 ]; then
  for n in "${nodes[@]}"; do
    pw="$(password_of "$n")"
    [ -n "$pw" ] || { echo "no broker login for $n: run $0 $kind $n" >&2; exit 1; }
    printf '%s %s\n' "$n" "$pw"
  done
  exit 0
fi

added=()
for n in "${nodes[@]}"; do
  [ -z "$(password_of "$n")" ] || continue
  pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 || true)"
  current="${current:+$current }$n:$pw"
  added+=("$n")
done
if [ ${#added[@]} -eq 0 ]; then echo "unchanged: every node has a login"; exit 0; fi

umask 077
{ grep -v "^$env_key=" "$env_file" || true; printf '%s=%s\n' "$env_key" "$current"; } >"$env_file.new"
mv "$env_file.new" "$env_file"
echo "added broker logins: ${added[*]/#/$login}"
if [ "$restart" = 1 ] && (cd "$root" && docker compose ps --status running --services 2>/dev/null | grep -qx broker); then
  (cd "$root" && docker compose up -d broker >/dev/null 2>&1) && echo "broker re-created with the new logins"
fi
