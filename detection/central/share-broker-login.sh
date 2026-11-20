#!/usr/bin/env bash
# Hands a bridge its central broker login without anyone copying a password: seals
# BROKER_PHYSICAL_PASSWORD (repo-root .env) into the secrets store as the device's `broker` bundle,
# encrypted to that device's own key. Run on the central server, from anywhere:
#   detection/central/share-broker-login.sh <device> [--to age1...]
# --to is only needed if the device has never had a bundle sealed (its key is not on record yet).
# On the bridge, detection/physical-subsystem/deploy/scripts/fetch-broker-login.sh picks it up.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
store="$root/detection/central-store"
device="${1:?usage: $0 <device> [--to age1...]}"; shift
pw="$(grep '^BROKER_PHYSICAL_PASSWORD=' "$root/.env" 2>/dev/null | cut -d= -f2- || true)"
[ -n "$pw" ] || { echo "no BROKER_PHYSICAL_PASSWORD in $root/.env: run detection/central/make-env.sh" >&2; exit 1; }
keeper="$(sed -n 's/^password: //p' "$store/state/credentials/keeper.txt" 2>/dev/null || true)"
[ -n "$keeper" ] || keeper="${BR_KEEPER_PASSWORD:-}"
[ -n "$keeper" ] || { echo "keeper password not found (state/credentials/keeper.txt or BR_KEEPER_PASSWORD)" >&2; exit 1; }
bind="$(grep '^BIND_ADDRESS=' "$store/.env" | cut -d= -f2)"

umask 077
base=/dev/shm; [ -d "$base" ] && [ -w "$base" ] || base="${TMPDIR:-/tmp}"
work="$(mktemp -d "$base/br-share.XXXXXX")"; trap 'rm -rf "$work"' EXIT
# A throwaway secrets.sh home that talks to this server's own store.
mkdir -p "$work/home" "$work/src/secrets/fusion"
if [ -s "$store/state/acme/name" ]; then
  name="$(cat "$store/state/acme/name")"
  echo "$name" >"$work/home/server"
  export BR_RESOLVE="$name:5003:$bind"     # reach it here even if the name does not resolve on this host
else
  echo "$bind" >"$work/home/server"; cp "$store/state/ca-public.crt" "$work/home/ca.crt"
fi
echo central >"$work/home/name"
[ ! -f "$HOME/.black-relay/admin-recipients" ] || cp "$HOME/.black-relay/admin-recipients" "$work/home/"
printf '%s\n' "$pw" >"$work/src/secrets/fusion/fusion_password"
# No admin recovery copy needed: the password also lives in the central .env.
BR_NO_ADMIN_WARNING=1 BR_HOME="$work/home" BR_KEEPER_PASSWORD="$keeper" "$store/secrets.sh" seal "$device" "$work/src" secrets/fusion/fusion_password --bundle broker "$@"
echo "On $device: detection/physical-subsystem/deploy/scripts/fetch-broker-login.sh"
