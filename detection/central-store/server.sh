#!/usr/bin/env bash
# Central store admin. Run ON the central server, from this folder. Needs only Docker.
#
#   ./server.sh init <address> [extra DNS names or IPs...]   TLS, publisher/keeper logins, .env
#   ./server.sh add-client <name> [--secrets]   a pull login for a host (+ its own secrets with --secrets)
#   ./server.sh remove-client <name>            revoke a host's logins
#   ./server.sh users                           who can log in where
#   ./server.sh gc images|secrets               delete unreferenced blobs (stops that store briefly)
#   ./server.sh letsencrypt <name> <email>      publicly trusted cert via Let's Encrypt (Cloudflare DNS);
#   ./server.sh letsencrypt                     later runs renew it (put that in cron), see README
#
# Everything it writes lives in state/ (0700, never committed). Passwords are printed once and also
# kept in state/credentials/ (0600) so you can hand them over; delete those files once you have.
# To use a certificate you already have (for example from your own CA), set TLS_CERT and TLS_KEY
# for `init` and it is copied in instead of the store's own CA issuing one.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"
TOOLS_IMAGE="${TOOLS_IMAGE:-black-relay/central-tools:0.1.0}"
LEGO_IMAGE=goacme/lego:v4.28.1@sha256:bc674054d11643fa8c248915574e59fb8856e4d1fd3d5026933521df1782cc47
umask 077

die() { echo "error: $*" >&2; exit 1; }
random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}" || true; }
ensure_tools() { docker image inspect "$TOOLS_IMAGE" >/dev/null 2>&1 || docker build -q -t "$TOOLS_IMAGE" "$here/tools" >/dev/null; }
# Runs a tool with state/ mounted, as the invoking user, with no network.
tool() { ensure_tools; docker run --rm -i --network none --user "$(id -u):$(id -g)" -v "$here/state:/state" -w /state "$TOOLS_IMAGE" "$@"; }
valid_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "name must be lowercase letters, digits and dashes: $1"; }
envadd() { touch .env; grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >>.env; }
running() { docker compose ps --status running --services 2>/dev/null | grep -qx "$1"; }
restart_if_running() { for s in "$@"; do running "$s" && docker compose restart "$s" >/dev/null 2>&1 && echo "restarted $s"; done; return 0; }

# set_user <auth dir> <user> -> prints the new password; the password goes to htpasswd on stdin
set_user() {
  local dir="$1" user="$2" pw create=""
  pw="$(random 32)"
  [ -f "state/auth/$dir/htpasswd" ] || create="-c"
  printf '%s' "$pw" | tool htpasswd -B -C 10 -i $create "auth/$dir/htpasswd" "$user" 2>/dev/null
  printf '%s' "$pw"
}
save_cred() { # save_cred <file> <lines...>
  local f="state/credentials/$1"; shift
  printf '%s\n' "$@" >"$f"; chmod 600 "$f"
}

cmd_init() {
  local bind="${1:?usage: $0 init <address> [extra names...]}"; shift
  mkdir -p state/ca state/tls state/credentials state/auth/{images-push,images-pull,secrets-push,secrets-pull}
  chmod 700 state

  if [ -n "${TLS_CERT:-}" ]; then
    [ -n "${TLS_KEY:-}" ] || die "TLS_CERT needs TLS_KEY"
    cp "$TLS_CERT" state/tls/server.crt; cp "$TLS_KEY" state/tls/server.key
    echo "TLS: using the certificate you gave (clients need no extra CA if it is publicly trusted)"
  elif [ ! -f state/tls/server.crt ]; then
    if [ ! -f state/ca/ca.key ]; then
      tool sh -c 'openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ca/ca.key 2>/dev/null &&
        openssl req -x509 -new -key ca/ca.key -sha256 -days 3650 -subj "/CN=BlackRelay central store CA" \
          -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca/ca.crt'
    fi
    local san="" n
    for n in "$bind" "$@"; do
      if [[ "$n" =~ ^[0-9.]+$ || "$n" == *:* ]]; then san+="IP:$n,"; else san+="DNS:$n,"; fi
    done
    tool sh -c "openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out tls/server.key 2>/dev/null &&
      openssl req -new -key tls/server.key -subj '/CN=$bind' -out tls/server.csr &&
      printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\nbasicConstraints=critical,CA:FALSE\n' '${san%,}' > tls/ext.cnf &&
      openssl x509 -req -in tls/server.csr -CA ca/ca.crt -CAkey ca/ca.key -CAcreateserial -days 825 -sha256 \
        -extfile tls/ext.cnf -out tls/server.crt 2>/dev/null && rm tls/server.csr tls/ext.cnf"
    cp state/ca/ca.crt state/ca-public.crt
    chmod 644 state/ca-public.crt
    echo "TLS: issued a certificate for ${san%,} from the store's own CA (state/ca-public.crt)"
  fi

  envadd BIND_ADDRESS "$bind"
  envadd RUN_UID "$(id -u)"
  envadd RUN_GID "$(id -g)"
  envadd REGISTRY_HTTP_SECRET "$(random 48)"

  local pw
  if [ ! -f state/auth/images-push/htpasswd ]; then
    pw="$(set_user images-push publisher)"
    save_cred publisher.txt "server: $bind:5001" "user: publisher" "password: $pw"
    echo "Image publisher login (pushes images to $bind:5001):  publisher / $pw"
  fi
  if [ ! -f state/auth/secrets-push/htpasswd ]; then
    pw="$(set_user secrets-push keeper)"
    save_cred keeper.txt "server: $bind:5003" "user: keeper" "password: $pw"
    echo "Secrets keeper login (seals bundles into $bind:5003):  keeper / $pw"
  fi
  # The pull stores start with no users; ./server.sh add-client creates them.
  touch state/auth/images-pull/htpasswd state/auth/secrets-pull/htpasswd
  # The registries run as this uid, so everything stays private to it; only the public CA is readable.
  chmod -R go-rwx state; chmod 755 state
  if [ -f state/ca-public.crt ]; then chmod 644 state/ca-public.crt; fi
  echo
  echo "Next: docker compose up -d --wait, then ./server.sh add-client <name> [--secrets] for each host."
}

cmd_add_client() {
  local name="${1:?usage: $0 add-client <name> [--secrets]}" secrets="${2:-}" bind pw
  valid_name "$name"
  [ -f .env ] || die "run ./server.sh init first"
  bind="$(grep '^BIND_ADDRESS=' .env | cut -d= -f2)"
  pw="$(set_user images-pull "$name")"
  local lines=("server: $bind" "user: $name" "password: $pw" "images: $bind:5000")
  if [ "$secrets" = "--secrets" ]; then
    printf '%s' "$pw" | tool htpasswd -B -C 10 -i auth/secrets-pull/htpasswd "$name" 2>/dev/null
    lines+=("secrets: $bind:5002 (bundle secrets/$name)")
  fi
  save_cred "client-$name.txt" "${lines[@]}"
  restart_if_running images-pull secrets-pull
  printf '%s\n' "${lines[@]}"
  echo "(saved in state/credentials/client-$name.txt; give it and state/ca-public.crt to that host, then delete the file)"
}

cmd_remove_client() {
  local name="${1:?usage: $0 remove-client <name>}"
  valid_name "$name"
  tool htpasswd -D auth/images-pull/htpasswd "$name" 2>/dev/null || true
  tool htpasswd -D auth/secrets-pull/htpasswd "$name" 2>/dev/null || true
  rm -f "state/credentials/client-$name.txt"
  restart_if_running images-pull secrets-pull
  echo "$name can no longer pull. Its sealed bundle is still stored; re-pair the device to rotate its keys."
}

cmd_users() {
  local d
  for d in images-push images-pull secrets-push secrets-pull; do
    printf '%-13s %s\n' "$d:" "$(cut -d: -f1 "state/auth/$d/htpasswd" 2>/dev/null | tr '\n' ' ')"
  done
}

cmd_gc() {
  local store="${1:?usage: $0 gc images|secrets}"
  [[ "$store" == images || "$store" == secrets ]] || die "gc images|secrets"
  docker compose stop "$store" "$store-pull"
  docker compose run --rm --no-deps "$store" garbage-collect --delete-untagged /etc/distribution/config.yml
  docker compose up -d --wait "$store" "$store-pull"
}

# Let's Encrypt over DNS-01 (Cloudflare): the server never has to be reachable from the internet.
# The name only needs a DNS record (A <server LAN IP>, DNS only / grey cloud) in Cloudflare.
cmd_letsencrypt() {
  local name="${1:-}" email="${2:-}"
  [ -f .env ] || die "run ./server.sh init first"
  mkdir -p state/acme
  [ -n "$name" ] || name="$(cat state/acme/name 2>/dev/null)" || true
  [ -n "$email" ] || email="$(cat state/acme/email 2>/dev/null)" || true
  [ -n "$name" ] && [ -n "$email" ] || die "usage: $0 letsencrypt <name> <email> (first time)"
  [ -s state/cloudflare.token ] || die "put a Cloudflare API token (Zone > DNS > Edit AND Zone > Zone > Read, this zone only) in state/cloudflare.token (chmod 600)"
  chmod 600 state/cloudflare.token
  if [ ! -f state/acme/accepted ]; then
    [ "${LE_ACCEPT_TOS:-}" = yes ] || { read -r -p "Accept the Let's Encrypt Subscriber Agreement (https://letsencrypt.org/repository/)? [yes/no] " a </dev/tty; [ "$a" = yes ] || die "not accepted"; }
    touch state/acme/accepted
  fi
  printf '%s\n' "$name" >state/acme/name; printf '%s\n' "$email" >state/acme/email
  local action=(run)
  [ -f "state/acme/certificates/$name.crt" ] && action=(renew --dynamic)
  docker run --rm --user "$(id -u):$(id -g)" -v "$here/state:/state" \
    -e CLOUDFLARE_DNS_API_TOKEN_FILE=/state/cloudflare.token ${LEGO_SERVER:+-e LEGO_SERVER="$LEGO_SERVER"} \
    "$LEGO_IMAGE" --path /state/acme --email "$email" --accept-tos --dns cloudflare \
    --dns.resolvers 1.1.1.1:53 --domains "$name" "${action[@]}"
  local crt="state/acme/certificates/$name.crt" key="state/acme/certificates/$name.key"
  [ -f "$crt" ] || die "no certificate was issued; see the output above"
  if [[ "${LEGO_SERVER:-}" == *staging* ]]; then
    echo "staging test passed: a (untrusted) test certificate was issued; not installing it"
    rm -rf state/acme/certificates; return 0
  fi
  if ! cmp -s "$crt" state/tls/server.crt; then
    cp "$crt" state/tls/server.crt; cp "$key" state/tls/server.key; chmod 600 state/tls/server.key
    echo "installed a Let's Encrypt certificate for $name"
    restart_if_running images images-pull secrets secrets-pull
  else
    echo "certificate for $name is current"
  fi
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add-client) shift; cmd_add_client "$@" ;;
  remove-client) shift; cmd_remove_client "$@" ;;
  users) cmd_users ;;
  gc) shift; cmd_gc "$@" ;;
  letsencrypt) shift; cmd_letsencrypt "$@" ;;
  *) sed -n '2,17p' "$0"; exit 1 ;;
esac
