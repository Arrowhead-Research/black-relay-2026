#!/usr/bin/env bash
# First-time setup (or update) of the BlackRelay central server: writes .env with the logins, then
# starts the central MQTT broker, fusion and the RF services (Kismet, WBT Console, rf-analytics)
# from compose.yaml. Run on the central server, from a checkout of this repo:
#
#   ./setup-central.sh <address>
#   ./setup-central.sh <address> --domain reg.bulexa.com --email you@example.org
#
# <address> is this machine's address on the network the other hosts use. It is the same as:
#   detection/central/make-env.sh <address> && docker compose up -d --wait
# With --domain the broker also gets TLS on 8883 with a Let's Encrypt certificate for that name
# (detection/central-broker/cert.sh), renewed daily from this user's crontab.
#
# Options:
#   --domain NAME         public DNS name for <address> (DNS on Cloudflare), e.g. reg.bulexa.com:
#                         Let's Encrypt certificate for the broker's TLS listener on 8883. Later runs
#                         without --domain keep using the name given before.
#   --email ADDR          Let's Encrypt account email (expiry notices only), with --domain the first time
#   --cloudflare-token F  file holding a Cloudflare API token (Zone:Read + DNS:Edit on that zone);
#                         asked for once if missing, then kept in detection/central-broker/state/
#   --no-cron             do not add the daily certificate renewal to this user's crontab
#   --with-bridge         also run the physical (IQ5) bridge on this server: its certs, config, broker
#                         login and containers (detection/physical-subsystem/deploy). Asks for sudo once.
#   --panel-ip IP         the IQ5 panel's address, with --with-bridge (needed the first time)
#
# Safe to run again: existing logins, passwords and data are kept. Needs Docker with Compose v2 and
# bash. Images come from GitHub's registry (ghcr.io); there is no local registry to set up.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

address="" bridge=0 panel_ip="" domain="" email="" token_file="" cron=1
while [ $# -gt 0 ]; do
  case "$1" in
    --with-bridge) bridge=1; shift ;;
    --panel-ip) panel_ip="${2:?--panel-ip needs an address}"; shift 2 ;;
    --domain) domain="${2:?--domain needs a name}"; shift 2 ;;
    --email) email="${2:?--email needs an address}"; shift 2 ;;
    --cloudflare-token) token_file="${2:?--cloudflare-token needs a file}"; shift 2 ;;
    --no-cron) cron=0; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) address="$1"; shift ;;
  esac
done
[ -n "$address" ] || { sed -n '2,27p' "$0"; exit 2; }
step() { printf '\n== %s\n' "$*"; }

command -v docker >/dev/null || { echo "Docker is not installed" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (docker compose version)" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "cannot talk to Docker: add this user to the docker group, or run with sudo" >&2; exit 1; }

step "Broker logins and fusion token"
"$root/detection/central/make-env.sh" "$address" | sed 's/^/   /'

broker_dir="$root/detection/central-broker"
mkdir -p "$broker_dir/state/tls" && chmod 700 "$broker_dir/state"   # before Docker creates it as root
# The name from an earlier run (or from the old central store, which cert.sh migrates) when not given.
[ -n "$domain" ] || domain="$(cat "$broker_dir/state/acme/name" "$root/detection/central-store/state/acme/name" 2>/dev/null | head -1)" || true
if [ -n "$domain" ]; then
  step "TLS certificate for $domain (Let's Encrypt)"
  if [ ! -s "$broker_dir/state/cloudflare.token" ] && [ ! -s "$root/detection/central-store/state/cloudflare.token" ]; then
    if [ -n "$token_file" ]; then cp "$token_file" "$broker_dir/state/cloudflare.token"
    else read -r -s -p "Cloudflare API token (Zone:Read + DNS:Edit on the zone): " t </dev/tty; echo
         printf '%s' "$t" >"$broker_dir/state/cloudflare.token"; unset t; fi
    chmod 600 "$broker_dir/state/cloudflare.token"
  fi
  if "$broker_dir/cert.sh" "$domain" ${email:+"$email"}; then
    if [ "$cron" = 1 ] && command -v crontab >/dev/null; then
      line="0 4 * * * cd $broker_dir && ./cert.sh >> state/renew.log 2>&1"
      # Replaces the old central store's renewal (its server.sh is gone) and keeps everything else.
      { crontab -l 2>/dev/null | grep -vF './server.sh letsencrypt' | grep -vF "$broker_dir && ./cert.sh" || true
        echo "$line"; } | crontab -
      echo "   daily renewal in this user's crontab"
    fi
  else
    echo "   WARNING: no certificate; the broker starts without TLS (1883 only). Fix the above, then run this again." >&2
  fi
fi

# The RF services used to run as their own project (rf-central); the root project includes them now
# and uses the same named volumes, so Kismet logs, console sites and calibration are kept.
if [ -n "$(docker ps -aq --filter label=com.docker.compose.project=rf-central)" ]; then
  step "Moving the RF services into the central project (data kept)"
  docker compose -p rf-central down --remove-orphans
fi

step "Starting broker, fusion and the RF services"
# --remove-orphans: an earlier setup ran the local registry and secrets store in this project.
docker compose up -d --wait --build --remove-orphans
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
  # The bridge's own broker (for Home Assistant / Node-RED) stays off the central broker's port.
  envset BRIDGE_MQTT_PORT 8884
  envset BRIDGE_HOSTS "$address"             # the bridge's cert names (+ 127.0.0.1, localhost always)
  envset IMAGE_PREFIX ""                     # bridge images are built here from this checkout
  if [ ! -f "$dep/certs/mosquitto/broker/broker.crt" ]; then
    echo "   generating the bridge's certificates and logins (sudo: files belong to the container users)"
    (cd "$dep" && as_root env FUSION_PASSWORD="$phys_pw" ./scripts/gen-certs.sh "$address") | sed 's/^/   /'
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
  # Same host: plain 1883 to the central broker never leaves this machine.
  set_yaml fusion host "$address"; set_yaml fusion port 1883; set_yaml fusion tls null
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

step "Done"
cat <<EOF
   Broker      $address:1883 (a login per publisher)      Fusion API  http://$address:8090
   RF          WBT Console http://$address:8080   Kismet for the nodes $address:3501
$( [ -s "$broker_dir/state/tls/server.crt" ] && echo "   Broker TLS  ${domain:-<name>}:8883  (Let's Encrypt; clients resolve ${domain:-it} to $address)" )
   Logins      .env  (broker passwords, fusion token)
$( [ "$bridge" = 1 ] && echo "   Bridge      API https://$address:8443   local broker $address:8884   publishing to the central broker" )

   A bridge on another host: set FUSION_PASSWORD (BROKER_PHYSICAL_PASSWORD from .env here) in its deploy/.env.
EOF
