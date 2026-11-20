#!/usr/bin/env bash
# First-start setup for the bridge, run by the one-shot `bootstrap` compose service before anything else,
# so a new machine needs only `docker compose up`. Everything it makes is kept; later starts change nothing.
#   1. this machine's key (BR_HOME/identity.key), made once; its public half is printed for sealing
#   2. local certificates, logins and secrets (scripts/gen-certs.sh), once
#   3. config/config.yaml from the example, with PANEL_IP, once
#   4. the panel pairing: fetched from the secrets registry and decrypted with this machine's key, when
#      data/pki has none; waits (polling) until Jayden has sealed and published it
#   5. the central broker login (secrets/fusion/fusion_password) from FUSION_PASSWORD, when given
# Runs as root inside the container only to hand files to the container users (uids 10001-10004, 1883).
set -euo pipefail
cd /deploy
say() { printf '[bootstrap] %s\n' "$*"; }
BR=/br
SECRETS_BASE="${SECRETS_BASE:-ghcr.io/arrowhead-research/blackrelay-secrets}"
PAIRING_BUNDLE="${PAIRING_BUNDLE:-}"          # empty = the main bundle (Wi-Fi pairing); `eth` = the Ethernet one
WAIT_S="${BOOTSTRAP_WAIT_S:-0}"                 # 0 = wait for the pairing bundle forever
umask 077

# 1. this machine's key
if [ ! -s "$BR/identity.key" ]; then age-keygen -o "$BR/identity.key" 2>/dev/null; say "made this machine's key"; fi
chown "${HOST_UID:-0}:${HOST_GID:-0}" "$BR/identity.key" 2>/dev/null || true
RECIPIENT="$(age-keygen -y "$BR/identity.key")"

# 2. local certificates, logins, secrets
if [ ! -f certs/mosquitto/broker/broker.crt ]; then
  say "generating local certificates and logins"
  # shellcheck disable=SC2086
  ./scripts/gen-certs.sh ${BRIDGE_HOSTS:-127.0.0.1} | grep -v -e 'password:' -e '^Next:' -e '^Control4' || true
fi

# 3. config
if [ ! -f config/config.yaml ]; then
  [ -n "${PANEL_IP:-}" ] || { say "set PANEL_IP (the IQ5 panel's address) in deploy/.env"; exit 1; }
  sed "s/^  panel_ip: [^ ]*/  panel_ip: $PANEL_IP/" config/config.example.yaml >config/config.yaml
  chmod 644 config/config.yaml
  say "wrote config/config.yaml (panel $PANEL_IP)"
fi

# 5. central broker login
if [ -n "${FUSION_PASSWORD:-}" ]; then
  mkdir -p secrets/fusion; printf '%s\n' "$FUSION_PASSWORD" >secrets/fusion/fusion_password
  chown 10003 secrets/fusion secrets/fusion/fusion_password; chmod 700 secrets/fusion; chmod 600 secrets/fusion/fusion_password
fi

# 4. pairing
has_pairing() { find data/pki -name panel_pin.json 2>/dev/null | grep -q .; }
if ! has_pairing; then
  DEVICE="${DEVICE:-}"
  [ -n "$DEVICE" ] || { say "no panel pairing in data/pki: set DEVICE (this machine, as Jayden seals it) in deploy/.env"; exit 1; }
  [[ "$DEVICE" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { say "DEVICE must be lowercase letters, digits and dashes"; exit 1; }
  ref="$SECRETS_BASE/$DEVICE${PAIRING_BUNDLE:+/$PAIRING_BUNDLE}:latest"
  user="${REGISTRY_USER:-}"; pw="${REGISTRY_TOKEN:-}"
  [ -n "$pw" ] || [ ! -s "$BR/password" ] || pw="$(tr -d '\n' <"$BR/password")"
  [ -n "$user" ] || [ ! -s "$BR/user" ] || user="$(tr -d '\n' <"$BR/user")"
  auth=(); [ -z "$pw" ] || auth=(-u "${user:-token}" --password-stdin)
  ca=(); [ -z "${SECRETS_CA:-}" ] || ca=(--ca-file "$SECRETS_CA")
  work="$(mktemp -d)"; start=$(date +%s); told=0
  until printf '%s' "$pw" | oras pull "${ca[@]}" "${auth[@]}" -o "$work" "$ref" >/dev/null 2>"$work/err"; do
    if [ "$told" = 0 ]; then
      say "no panel pairing yet for '$DEVICE' ($(tail -n1 "$work/err"))."
      say "Send Jayden this line so he can seal it for this machine, then leave this running:"
      say "    $DEVICE $RECIPIENT"
      told=1
    fi
    if [ "$WAIT_S" != 0 ] && [ $(( $(date +%s) - start )) -ge "$WAIT_S" ]; then say "gave up waiting"; exit 1; fi
    sleep 30
  done
  stage="$(mktemp -d)"
  if ! age -d -i "$BR/identity.key" "$work/bundle.age" | tar -C "$stage" -xpf -; then
    say "the bundle for '$DEVICE' was not sealed for this machine's key. Send Jayden: $DEVICE $RECIPIENT"; exit 1
  fi
  while IFS= read -r p; do
    case "$p" in ""|/*|*..*) say "bad path in bundle: $p"; exit 1 ;; esac
    # Bundles keep the sealing machine's layout; the pairing always lands in data/pki here.
    dst="$p"; case "$p" in data/pki-*) dst="data/pki" ;; esac
    rm -rf "$dst"; mkdir -p "$(dirname "$dst")"; mv "$stage/$p" "$dst"
  done <"$work/paths.txt"
  chown -R 10001:10000 data/pki; chmod 700 data/pki
  rm -rf "$work" "$stage"
  say "panel pairing installed from $ref"
fi
say "ready"
