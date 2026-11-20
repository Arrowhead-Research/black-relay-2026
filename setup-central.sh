#!/usr/bin/env bash
# One command to set up (or update) the BlackRelay central server: image registry, secrets store,
# central MQTT broker and fusion. Run on the central server from a checkout of this repo:
#
#   ./setup-central.sh <address>                                       own CA (hosts must trust it)
#   ./setup-central.sh <address> --domain reg.bulexa.com --email you@example.org
#                                                                      Let's Encrypt (hosts trust it as is)
# Options:
#   --domain NAME         public DNS name pointing at <address> (Cloudflare DNS); gets a Let's Encrypt cert
#   --email ADDR          Let's Encrypt account email (expiry notices only), with --domain
#   --cloudflare-token F  file holding a Cloudflare API token (Zone:Read + DNS:Edit on that zone);
#                         asked for once if missing, then kept in detection/central-store/state/
#   --no-cron             do not add the daily certificate renewal to this user's crontab
#
# Safe to run again: existing logins, passwords, certificates and data are kept. Needs Docker with
# Compose v2 and bash. Prints where every login is at the end.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
store="$root/detection/central-store"
cd "$root"

address="" domain="" email="" token_file="" cron=1
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="${2:?--domain needs a name}"; shift 2 ;;
    --email) email="${2:?--email needs an address}"; shift 2 ;;
    --cloudflare-token) token_file="${2:?--cloudflare-token needs a file}"; shift 2 ;;
    --no-cron) cron=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) address="$1"; shift ;;
  esac
done
[ -n "$address" ] || { sed -n '2,18p' "$0"; exit 2; }
[ -z "$domain" ] || [ -n "$email" ] || { echo "--domain needs --email (Let's Encrypt account)" >&2; exit 2; }
step() { printf '\n== %s\n' "$*"; }

command -v docker >/dev/null || { echo "Docker is not installed" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (docker compose version)" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "cannot talk to Docker: add this user to the docker group, or run with sudo" >&2; exit 1; }

step "Registry and secrets store: TLS and logins"
"$store/server.sh" init "$address" ${domain:+"$domain"} | sed 's/^/   /'

if [ -n "$domain" ]; then
  step "Let's Encrypt certificate for $domain"
  if [ ! -s "$store/state/cloudflare.token" ]; then
    if [ -n "$token_file" ]; then cp "$token_file" "$store/state/cloudflare.token"
    else read -r -s -p "Cloudflare API token (Zone:Read + DNS:Edit on the zone): " t </dev/tty; echo
         printf '%s' "$t" >"$store/state/cloudflare.token"; unset t; fi
    chmod 600 "$store/state/cloudflare.token"
  fi
  if "$store/server.sh" letsencrypt "$domain" "$email"; then
    if [ "$cron" = 1 ] && command -v crontab >/dev/null; then
      line="0 4 * * * cd $store && ./server.sh letsencrypt >> state/renew.log 2>&1"
      if ! crontab -l 2>/dev/null | grep -qF "$store && ./server.sh letsencrypt"; then
        { crontab -l 2>/dev/null || true; echo "$line"; } | crontab -
        echo "   daily renewal added to this user's crontab"
      fi
    fi
  else
    echo "   WARNING: no Let's Encrypt certificate yet; running with the store's own CA for now."
    echo "   Fix the cause above and run this command again."
  fi
fi

step "Broker logins and fusion token"
"$root/detection/central/make-env.sh" "$address" | sed 's/^/   /'

# The store may have been started on its own before; the root project takes it over (same volumes).
if [ -n "$(docker ps -q --filter label=com.docker.compose.project=black-relay-central-store)" ]; then
  step "Moving the running store into the central project (data kept)"
  (cd "$store" && docker compose down)
fi

step "Starting registry, secrets store, broker and fusion"
docker compose up -d --wait --build
docker compose ps --format '   {{.Service}}: {{.Status}}'

name="${domain:-$address}"
step "Done"
cat <<EOF
   Images      pull  $name:5000      push  $name:5001 (user publisher)
   Secrets     pull  $name:5002      push  $name:5003 (user keeper)
   Broker      $name:8883 (TLS)  and  $address:1883      Fusion API  http://$address:8090
   Logins      detection/central-store/state/credentials/  (registry)    .env  (broker, fusion token)
$( [ -z "$domain" ] && echo "   Hosts must trust detection/central-store/state/ca-public.crt (or use --domain)." )

   Add a host:        detection/central-store/server.sh add-client <name> [--secrets]
   Give a bridge its broker login:  detection/central/share-broker-login.sh <name>
EOF
