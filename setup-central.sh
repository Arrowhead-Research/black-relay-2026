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
#   --with-bridge         also run the physical (IQ5) bridge on this server: its certs, config, broker
#                         login and containers (detection/physical-subsystem/deploy). Asks for sudo once.
#   --panel-ip IP         the IQ5 panel's address, with --with-bridge (needed the first time)
#   --with-rf             also run RF live analytics here (service rf-analytics): its own broker login,
#                         Kismet login from detection/rf-subsystem/central/.env, and the RF console
#                         pointed at this broker. Remembered in .env (COMPOSE_PROFILES=rf).
#   --kismet-url URL      the RF stack's Kismet, with --with-rf (default http://rf-kismet:2501: the RF
#                         stack on this host, on the shared Docker network "fusion")
#
# Safe to run again: existing logins, passwords, certificates and data are kept. Needs Docker with
# Compose v2 and bash. Prints where every login is at the end.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
store="$root/detection/central-store"
cd "$root"

address="" domain="" email="" token_file="" cron=1 bridge=0 panel_ip="" rf=0 kismet_url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="${2:?--domain needs a name}"; shift 2 ;;
    --email) email="${2:?--email needs an address}"; shift 2 ;;
    --cloudflare-token) token_file="${2:?--cloudflare-token needs a file}"; shift 2 ;;
    --no-cron) cron=0; shift ;;
    --with-bridge) bridge=1; shift ;;
    --panel-ip) panel_ip="${2:?--panel-ip needs an address}"; shift 2 ;;
    --with-rf) rf=1; shift ;;
    --kismet-url) kismet_url="${2:?--kismet-url needs a URL}"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) address="$1"; shift ;;
  esac
done
[ -n "$address" ] || { sed -n '2,26p' "$0"; exit 2; }
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

# Reads KEY from an env file (empty if missing).
envget() { grep "^$2=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
# Sets KEY=VALUE in an env file, replacing an existing line (values here hold no newlines).
envput() {
  touch "$1"
  (umask 077; { grep -v "^$2=" "$1" || true; printf '%s=%s\n' "$2" "$3"; } >"$1.new") && cat "$1.new" >"$1" && rm -f "$1.new"
}

[ "$rf" = 1 ] || [ "$(envget "$root/.env" RF_ENABLED)" != 1 ] || rf=1   # --with-rf is remembered
if [ "$rf" = 1 ]; then
  step "RF analytics: Kismet login, broker login, RF console"
  rf_env="$root/detection/rf-subsystem/central/.env"
  envput "$root/.env" RF_ENABLED 1
  profiles="$(envget "$root/.env" COMPOSE_PROFILES)"
  case ",$profiles," in *,rf,*) ;; *) envput "$root/.env" COMPOSE_PROFILES "${profiles:+$profiles,}rf" ;; esac
  [ -z "$kismet_url" ] || envput "$root/.env" RF_KISMET_URL "$kismet_url"
  if [ -f "$rf_env" ]; then
    # Kismet admin login, as the RF stack seeded it (rf-analytics only reads the REST API).
    [ -n "$(envget "$root/.env" RF_KISMET_USER)" ] || envput "$root/.env" RF_KISMET_USER "$(envget "$rf_env" KISMET_ADMIN_USER)"
    [ -n "$(envget "$root/.env" RF_KISMET_PASSWORD)" ] || envput "$root/.env" RF_KISMET_PASSWORD "$(envget "$rf_env" KISMET_ADMIN_PASSWORD)"
    # Site, rules and friendly files the RF stack's own rf-analytics used (file names in analytics/config).
    for k in RF_SITE RF_RULES RF_FRIENDLY; do
      v="$(envget "$rf_env" "$k")"
      [ -z "$v" ] || [ -n "$(envget "$root/.env" "$k")" ] || envput "$root/.env" "$k" "$v"
    done
    # The console (RF stack) publishes positions and node status here with the rf-console login.
    envput "$rf_env" CONSOLE_MQTT_HOST "$address"
    envput "$rf_env" CONSOLE_MQTT_USER "$(envget "$root/.env" BROKER_RF_USER)"
    envput "$rf_env" CONSOLE_MQTT_PASSWORD "$(envget "$root/.env" BROKER_RF_PASSWORD)"
    # Kismet joins the shared network as rf-kismet (the overlay) unless analytics polls a URL elsewhere.
    if [ -z "$(envget "$root/.env" RF_KISMET_URL)" ] && [ -z "$(envget "$rf_env" COMPOSE_FILE)" ]; then
      envput "$rf_env" COMPOSE_FILE docker-compose.yml:docker-compose.fusion.yml
    fi
    chmod 600 "$rf_env"
    echo "   RF console publishes to $address:1883 as $(envget "$root/.env" BROKER_RF_USER)"
    docker network inspect fusion >/dev/null 2>&1 || { docker network create fusion >/dev/null && echo "   created Docker network fusion"; }
    if [ -n "$(docker ps -q --filter label=com.docker.compose.project=rf-central)" ]; then
      echo "   applying it to the running RF stack (old broker removed)"
      (cd "$root/detection/rf-subsystem/central" && docker compose up -d --remove-orphans) 2>&1 | sed 's/^/   /'
    fi
  else
    echo "   NOTE: no $rf_env (RF stack not on this host): set RF_KISMET_USER and RF_KISMET_PASSWORD in"
    echo "   .env, and --kismet-url if Kismet is elsewhere."
  fi
  docker network inspect fusion >/dev/null 2>&1 || docker network create fusion >/dev/null
fi

# The store may have been started on its own before; the root project takes it over (same volumes).
if [ -n "$(docker ps -q --filter label=com.docker.compose.project=black-relay-central-store)" ]; then
  step "Moving the running store into the central project (data kept)"
  (cd "$store" && docker compose down)
fi

step "Starting registry, secrets store, broker and fusion$( [ "$rf" = 0 ] || echo ', RF analytics')"
docker compose up -d --wait --build
docker compose ps --format '   {{.Service}}: {{.Status}}'

if [ "$bridge" = 1 ]; then
  dep="$root/detection/physical-subsystem/deploy"
  as_root() { if [ "$(id -u)" = 0 ] || [ -n "${NO_SUDO:-}" ]; then "$@"; else sudo "$@"; fi; }
  # Sets KEY inside a top-level YAML block (keeps any trailing comment). The example config is flat enough.
  set_yaml() { # set_yaml <block> <key> <value>
    # A key that held a nested block (e.g. fusion.tls) loses its nested lines when set to a scalar.
    awk -v b="$1" -v k="$2" -v v="$3" '
      skip && /^    / { next }
      { skip = 0 }
      /^[a-z_]+:/ { blk = substr($1, 1, length($1) - 1) }
      blk == b && !done && $0 ~ "^  " k ":" { c = ""; i = index($0, " #"); if (i) c = "  " substr($0, i + 1)
                                              print "  " k ": " v c; done = 1; skip = 1; next }
      { print }' "$dep/config/config.yaml" >"$dep/config/config.yaml.new" && mv "$dep/config/config.yaml.new" "$dep/config/config.yaml"
  }
  envset() { touch "$dep/.env"; grep -q "^$1=" "$dep/.env" || printf '%s=%s\n' "$1" "$2" >>"$dep/.env"; }

  step "Physical bridge on this server"
  phys_pw="$(grep '^BROKER_PHYSICAL_PASSWORD=' "$root/.env" | cut -d= -f2-)"
  # The bridge's own broker (for Home Assistant / Node-RED) moves to 8884: the central broker has 8883.
  envset BRIDGE_MQTT_PORT 8884
  envset BRIDGE_HOSTS "$address${domain:+ $domain}"   # the bridge's cert names (+ 127.0.0.1, localhost always)
  envset IMAGE_PREFIX ""                     # bridge images are built here from this checkout
  if [ -n "$domain" ]; then envset CENTRAL_BROKER_NAME "$domain"; envset CENTRAL_BROKER_IP "$address"; fi
  if [ ! -f "$dep/certs/mosquitto/broker/broker.crt" ]; then
    echo "   generating the bridge's certificates and logins (sudo: files belong to the container users)"
    (cd "$dep" && as_root env FUSION_PASSWORD="$phys_pw" ./scripts/gen-certs.sh "$address" ${domain:+"$domain"}) | sed 's/^/   /'
  else
    printf '%s\n' "$phys_pw" | as_root sh -c "umask 077; mkdir -p '$dep/secrets/fusion' && cat >'$dep/secrets/fusion/fusion_password' && chown 10003 '$dep/secrets/fusion' '$dep/secrets/fusion/fusion_password'"
  fi
  if [ ! -f "$dep/config/config.yaml" ]; then
    [ -n "$panel_ip" ] || { echo "--with-bridge needs --panel-ip the first time" >&2; exit 2; }
    cp "$dep/config/config.example.yaml" "$dep/config/config.yaml"
  fi
  [ -z "$panel_ip" ] || set_yaml iq_remote panel_ip "$panel_ip"
  set_yaml mqtt port 8884
  set_yaml fusion enabled true
  if [ -n "$domain" ]; then
    set_yaml fusion host "$domain"; set_yaml fusion port 8883
  else  # no public certificate: same host, so plain 1883 to the central broker never leaves this machine
    set_yaml fusion host "$address"; set_yaml fusion port 1883; set_yaml fusion tls null
  fi
  if ! as_root sh -c "ls '$dep'/data/pki/panel_pin.json '$dep'/data/pki/*/panel_pin.json" >/dev/null 2>&1; then
    echo "   NOTE: no panel pairing in $dep/data/pki yet. Pair once (detection/physical-subsystem/README.md,"
    echo "   'Pairing'), or restore a sealed bundle: detection/central-store/secrets.sh fetch $dep"
  fi
  (cd "$dep" && docker compose --profile fusion up -d --build --wait normalizer outbound mosquitto fusion iq-remote)
  (cd "$dep" && docker compose --profile fusion ps --format '   {{.Service}}: {{.Status}}')
  echo "   Firewall: set PANEL_IP in $dep/nftables/blackrelay-egress.nft and load it (sudo nft -f ...)."
  [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0)" = 0 ] || \
    echo "   Note: IP forwarding is on (Docker needs it); keep the panel network out of the forward path with nftables."
fi

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
$( [ "$rf" = 1 ] && echo "   RF          rf-analytics polls $(envget "$root/.env" RF_KISMET_URL | grep . || echo http://rf-kismet:2501), publishes as $(envget "$root/.env" BROKER_RF_ANALYTICS_USER)" )
$( [ "$bridge" = 1 ] && echo "   Bridge      API https://$address:8443   local broker $address:8884   publishing to the central broker" )
EOF
