#!/usr/bin/env bash
# Gives each RF (WBT) node its own central broker login: rf-node-<node>, allowed to write only
# detection/rf/nodes/<node>/#. Logins live in the repo-root .env as BROKER_RF_NODE_LOGINS
# ("node:password ..."); existing ones are kept, so re-running is safe. Run on the central server:
#
#   detection/central/rf-node-logins.sh wbt-1 wbt-2 ...    add missing logins, then re-create the broker
#   detection/central/rf-node-logins.sh --get wbt-5 ...    print "<node> <password>" lines (for provisioning
#                                                          over SSH; node/fleet/ansible reads it, no_log)
# --no-restart leaves the broker running with its old logins.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
env_file="$root/.env"
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
[ ${#nodes[@]} -gt 0 ] || { sed -n '2,9p' "$0"; exit 2; }
[ -f "$env_file" ] || { echo "no $env_file: run detection/central/make-env.sh first" >&2; exit 1; }

current="$(sed -n 's/^BROKER_RF_NODE_LOGINS=//p' "$env_file" | tail -n1)"
password_of() { tr ' ' '\n' <<<"$current" | sed -n "s/^$1://p" | head -n1; }

if [ "$get" = 1 ]; then
  for n in "${nodes[@]}"; do
    pw="$(password_of "$n")"
    [ -n "$pw" ] || { echo "no broker login for $n: run $0 $n" >&2; exit 1; }
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
{ grep -v '^BROKER_RF_NODE_LOGINS=' "$env_file" || true; printf 'BROKER_RF_NODE_LOGINS=%s\n' "$current"; } >"$env_file.new"
mv "$env_file.new" "$env_file"
echo "added broker logins: ${added[*]/#/rf-node-}"
if [ "$restart" = 1 ] && (cd "$root" && docker compose ps --status running --services 2>/dev/null | grep -qx broker); then
  (cd "$root" && docker compose up -d broker >/dev/null 2>&1) && echo "broker re-created with the new logins"
fi
