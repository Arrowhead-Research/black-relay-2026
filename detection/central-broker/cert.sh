#!/usr/bin/env bash
# The central broker's TLS certificate (port 8883): a publicly trusted Let's Encrypt certificate for a
# name such as reg.bulexa.com, issued over DNS-01 with Cloudflare, so the server never has to be
# reachable from the internet. Run ON the central server, as the user that runs Docker.
#
#   ./cert.sh <name> <email>     first time: issue (asks you to accept the Let's Encrypt terms)
#   ./cert.sh                    later: renew when due (daily from cron; setup-central.sh adds it)
#
# Needs a Cloudflare API token (Zone > Zone > Read and Zone > DNS > Edit, that zone only) in
# state/cloudflare.token (0600). The name only needs a DNS record (A <server LAN IP>, DNS only) in
# Cloudflare. Everything lives in state/ (0700, never committed): acme/ (account, certificates) and
# tls/server.crt + server.key, which the root compose.yaml mounts read-only into the broker.
#
# An earlier layout kept this in detection/central-store/state; the first run copies it from there
# (account, certificate, token), so the existing reg.bulexa.com certificate is reused, not re-issued.
#
# LEGO_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory tests issuance without
# installing anything (and without using up Let's Encrypt's rate limits).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
cd "$here"
LEGO_IMAGE=goacme/lego:v4.28.1@sha256:bc674054d11643fa8c248915574e59fb8856e4d1fd3d5026933521df1782cc47
umask 077

die() { echo "error: $*" >&2; exit 1; }

mkdir -p state/acme state/tls
chmod 700 state
[ -w state/tls ] || die "$here/state/tls is not writable by $(id -un) (Docker created it as root before cert.sh ran?): sudo chown -R $(id -un) $here/state"

old="$root/detection/central-store/state"
if [ ! -f state/acme/name ] && [ -f "$old/acme/name" ]; then
  echo "reusing the Let's Encrypt account and certificate from $old"
  cp -R "$old/acme/." state/acme/
fi
[ -s state/cloudflare.token ] || [ ! -s "$old/cloudflare.token" ] || cp "$old/cloudflare.token" state/cloudflare.token

name="${1:-}" email="${2:-}"
[ -n "$name" ] || name="$(cat state/acme/name 2>/dev/null)" || true
[ -n "$email" ] || email="$(cat state/acme/email 2>/dev/null)" || true
[ -n "$name" ] && [ -n "$email" ] || die "usage: $0 <name> <email> (first time)"
[[ "$name" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "not a DNS name: $name"
[ -s state/cloudflare.token ] || die "put a Cloudflare API token (Zone > Zone > Read and Zone > DNS > Edit, this zone only) in $here/state/cloudflare.token"
chmod 600 state/cloudflare.token
if [ ! -f state/acme/accepted ]; then
  [ "${LE_ACCEPT_TOS:-}" = yes ] || { read -r -p "Accept the Let's Encrypt Subscriber Agreement (https://letsencrypt.org/repository/)? [yes/no] " a </dev/tty; [ "$a" = yes ] || die "not accepted"; }
  touch state/acme/accepted
fi
printf '%s\n' "$name" >state/acme/name
printf '%s\n' "$email" >state/acme/email

action=(run)
[ -f "state/acme/certificates/$name.crt" ] && action=(renew --dynamic)
docker run --rm --user "$(id -u):$(id -g)" -v "$here/state:/state" \
  -e CLOUDFLARE_DNS_API_TOKEN_FILE=/state/cloudflare.token ${LEGO_SERVER:+-e LEGO_SERVER="$LEGO_SERVER"} \
  "$LEGO_IMAGE" --path /state/acme --email "$email" --accept-tos --dns cloudflare \
  --dns.resolvers 1.1.1.1:53 --domains "$name" "${action[@]}"

crt="state/acme/certificates/$name.crt" key="state/acme/certificates/$name.key"
[ -f "$crt" ] && [ -f "$key" ] || die "no certificate was issued; see the output above"
if [[ "${LEGO_SERVER:-}" == *staging* ]]; then
  echo "staging test passed: an (untrusted) test certificate was issued; not installing it"
  rm -rf state/acme/certificates
  exit 0
fi

if cmp -s "$crt" state/tls/server.crt && cmp -s "$key" state/tls/server.key; then
  echo "certificate for $name is current ($(openssl x509 -noout -enddate -in "$crt" 2>/dev/null | cut -d= -f2))"
  exit 0
fi
cp "$crt" state/tls/server.crt
cp "$key" state/tls/server.key
chmod 644 state/tls/server.crt
chmod 600 state/tls/server.key
echo "installed the Let's Encrypt certificate for $name"
# The broker copies the certificate at start: restart it if it is running (clients reconnect).
if [ -n "$(docker compose --project-directory "$root" ps -q broker 2>/dev/null)" ]; then
  docker compose --project-directory "$root" restart broker >/dev/null && echo "restarted the broker"
fi
