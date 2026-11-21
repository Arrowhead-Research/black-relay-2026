#!/bin/bash
# Owns the stack's PKI. Runs as its own compose service ("pki") that mounts two volumes:
#   tak-pki        -> /opt/tak/certs/files    everything, including the CA private keys. TAK Server mounts it too.
#   gateway-certs  -> /opt/tak/certs/gateway  public + leaf material only. This is all the gateway ever sees.
#
# Commands (default: run):
#   run              create anything missing (idempotent), publish to the gateway, then keep it in sync
#   publish          one sync pass: copy changed files from tak-pki to gateway-certs
#   rotate-gateway   re-issue the gateway's server cert (the CA is untouched); the sync loop publishes it
#   revoke <name>    revoke files/<name>.pem with the intermediate CA and rebuild intermediate.crl
#   status           show expiry dates and the CRL's next update
#
# Run it inside the running container, from the repo root on the host:
#   docker compose exec pki /opt/configurator/pki-init.sh <command> [argument]
# Only what makeRootCa.sh / makeCert.sh / revokeCert.sh already do is used here.
set -euo pipefail

CERTS=/opt/tak/certs
FILES=$CERTS/files
OUT=${GATEWAY_CERTS_DIR:-$CERTS/gateway}
GATEWAY_NAME=${GATEWAY_TLS_NAME:-127.0.0.1} # CN and SAN of the gateway's server cert (makeCert.sh allows exactly one)
SYNC_INTERVAL=${PKI_SYNC_INTERVAL:-30}
TAK_UID_GID=1001:0 # the "tak" user in tak-server/Dockerfile
GATEWAY_UID=10001 # the "gateway" user in gateway/Dockerfile: it alone reads its keys and .p12 (mode 600)

cd "$CERTS"

die() { echo "pki: $*" >&2; exit 1; }
have() { [ -s "$FILES/$1" ]; }

init_ca() {
  mkdir -p "$FILES"
  # revokeCert.sh and makeCert.sh's CRL step expect these to exist
  touch "$FILES/crl_index.txt"
  grep -qs unique_subject "$FILES/crl_index.txt.attr" || echo "unique_subject = no" >> "$FILES/crl_index.txt.attr"

  if ! have root-ca.pem; then
    [ ! -e "$FILES/ca.pem" ] || die "half-built CA in $FILES (ca.pem without root-ca.pem): empty the tak-pki volume and start again"
    ./makeRootCa.sh --ca-name root-ca
  fi
  if ! have intermediate.pem; then
    printf 'y\n' | ./makeCert.sh ca intermediate # "y" = sign future certs with the intermediate
  fi
}

# issue <server|client> <name> [force]: skipped when the cert and key already exist
issue() {
  if [ "${3:-}" != force ] && have "$2.pem" && have "$2.key"; then
    return 0
  fi
  ./makeCert.sh "$1" "$2"
}

fix_perms() { chown -R "$TAK_UID_GID" "$FILES"; }

# put <src in tak-pki> <dst in gateway-certs> <mode>: copy only if changed, and swap atomically so the gateway never reads half a file
put() {
  local src=$FILES/$1 dst=$OUT/$2 mode=$3
  [ -f "$src" ] || { echo "pki: $1 not found, not publishing $2" >&2; return 0; }
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    chown "$GATEWAY_UID" "$dst" # also fixes a copy published before the gateway ran as a non-root user
    return 0
  fi
  install -o "$GATEWAY_UID" -m "$mode" "$src" "$OUT/.$2.tmp"
  mv -f "$OUT/.$2.tmp" "$dst"
  echo "pki: published $2"
}

publish() {
  mkdir -p "$OUT"

  # root + intermediate (the intermediate's own PEM also carries the root, so take its first cert only)
  local bundle=$OUT/.ca-bundle.pem.tmp
  { cat "$FILES/root-ca.pem"; openssl x509 -in "$FILES/intermediate.pem"; } > "$bundle"
  if [ -f "$OUT/ca-bundle.pem" ] && cmp -s "$bundle" "$OUT/ca-bundle.pem"; then
    rm -f "$bundle"
  else
    chmod 644 "$bundle" && mv -f "$bundle" "$OUT/ca-bundle.pem" && echo "pki: published ca-bundle.pem"
  fi

  put intermediate.crl intermediate.crl 644
  put "$GATEWAY_NAME.key" gateway-server.key 600
  put "$GATEWAY_NAME.pem" gateway-server.pem 644
  # the gateway's own client cert towards TAK: .p12 for pytak, PEM + key for the health monitor
  put pytak-gateway.p12 pytak-gateway.p12 600
  put pytak-gateway.key pytak-gateway.key 600
  put pytak-gateway.pem pytak-gateway.pem 644
}

init() {
  init_ca
  issue server takserver # CN = the takserver container name
  issue server "$GATEWAY_NAME"
  issue client pytak-gateway
  fix_perms
}

rotate_gateway() {
  have root-ca.pem || die "no PKI yet: run 'run' first"
  issue server "$GATEWAY_NAME" force
  fix_perms
  publish
}

revoke() {
  [ -n "${1:-}" ] || die "usage: revoke <name>   (files/<name>.pem)"
  have "$1.pem" || die "$FILES/$1.pem not found"
  ./revokeCert.sh "$1" intermediate intermediate
  fix_perms
  publish
}

show() { # <label> <pem>
  printf '%-22s %s\n' "$1" "$(openssl x509 -noout -enddate -in "$2" | sed 's/notAfter=/expires /')"
}

status() {
  have root-ca.pem || die "no PKI yet"
  show root-ca "$FILES/root-ca.pem"
  show intermediate "$FILES/intermediate.pem"
  show takserver "$FILES/takserver.pem"
  show "$GATEWAY_NAME" "$FILES/$GATEWAY_NAME.pem"
  show pytak-gateway "$FILES/pytak-gateway.pem"
  printf '%-22s %s\n' "intermediate.crl" "$(openssl crl -noout -nextupdate -in "$FILES/intermediate.crl" | sed 's/nextUpdate=/next update /')"
  echo "(an expired CRL makes the gateway refuse every client; the intermediate CA expires 730 days after creation)"
}

run() {
  init
  publish
  touch /tmp/pki-ready # the compose healthcheck: TAK and the gateway wait for this
  echo "pki: ready, syncing $FILES -> $OUT every ${SYNC_INTERVAL}s"
  while sleep "$SYNC_INTERVAL"; do
    publish || echo "pki: sync failed, retrying" >&2
  done
}

case "${1:-run}" in
  run) run ;;
  publish) publish ;;
  rotate-gateway) rotate_gateway ;;
  revoke) revoke "${2:-}" ;;
  status) status ;;
  *) die "unknown command '$1' (run | publish | rotate-gateway | revoke <name> | status)" ;;
esac
