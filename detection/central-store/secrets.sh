#!/usr/bin/env bash
# Per-device secrets on the central store: sealed (age-encrypted) where they are made, opened only on
# the device they belong to. Runs on any machine with Docker (macOS or Linux).
#
#   ./secrets.sh setup <server> <name> <ca.crt|->   once per machine: login, CA and this machine's own key
#   ./secrets.sh recipient                          print this machine's public key (age1...)
#   ./secrets.sh admin-keygen <file>                make the admin recovery key (keep <file> OFF the server)
#   ./secrets.sh seal <device> <dir> <path>... [--to age1...]
#                                                   encrypt <path>s (relative to <dir>) for <device> and
#                                                   upload them; asks for the keeper password
#   ./secrets.sh fetch <dir> [device] [--chown uid:gid]
#                                                   download and decrypt a bundle into <dir>, replacing
#                                                   exactly the sealed paths (default device: this machine)
#   ./secrets.sh list [device]                      bundles stored for a device (latest + dated versions)
#
# seal, fetch and list take --bundle NAME for a second, independent bundle per device (e.g. `broker`,
# the central broker login that detection/central/share-broker-login.sh seals). Without it: the main
# bundle (pairing keys). BR_RESOLVE=host:port:address pins a name to an address (like curl --resolve).
#
# Settings live in $BR_HOME (default ~/.black-relay, 0700): server, name, password, ca.crt, identity.key
# and, optionally, admin-recipients (one age1... per line; every bundle sealed here is also readable by them).
# The keeper password comes from $BR_KEEPER_PASSWORD or a prompt; it is never written to disk.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BR_HOME="${BR_HOME:-$HOME/.black-relay}"
TOOLS_IMAGE="${TOOLS_IMAGE:-black-relay/central-tools:0.1.0}"
ARTIFACT_TYPE=application/vnd.blackrelay.secrets.v1
umask 077

die() { echo "error: $*" >&2; exit 1; }
conf() { [ -f "$BR_HOME/$1" ] || die "missing $BR_HOME/$1: run ./secrets.sh setup first"; tr -d '\n' <"$BR_HOME/$1"; }
valid_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "device names are lowercase letters, digits and dashes: $1"; }
ensure_tools() {
  docker image inspect "$TOOLS_IMAGE" >/dev/null 2>&1 && return
  if [ -f "$here/tools/Dockerfile" ]; then docker build -q -t "$TOOLS_IMAGE" "$here/tools" >/dev/null
  else docker pull "$(conf server):5000/$TOOLS_IMAGE" && docker tag "$(conf server):5000/$TOOLS_IMAGE" "$TOOLS_IMAGE"; fi
}
# The CA flag for oras, if this store uses its own CA.
ca_flag() {
  [ -s "$BR_HOME/ca.crt" ] && printf '%s ' "--ca-file /br/ca.crt"
  [ -z "${BR_RESOLVE:-}" ] || printf '%s ' "--resolve $BR_RESOLVE"
  return 0
}
repo() { if [ -n "$2" ]; then valid_name "$2"; echo "secrets/$1/$2"; else echo "secrets/$1"; fi; }
read_secret() { # read_secret <prompt> -> stdout
  local v
  if [ -t 0 ]; then read -r -s -p "$1" v </dev/tty; echo >&2; else read -r v; fi
  printf '%s' "$v"
}

cmd_setup() {
  local server="${1:?usage: setup <server> <name> <ca.crt|->}" name="${2:?name}" ca="${3:?ca.crt or -}" pw
  valid_name "$name"
  mkdir -p "$BR_HOME"; chmod 700 "$BR_HOME"
  printf '%s\n' "$server" >"$BR_HOME/server"; printf '%s\n' "$name" >"$BR_HOME/name"
  if [ "$ca" = "-" ]; then rm -f "$BR_HOME/ca.crt"; else cp "$ca" "$BR_HOME/ca.crt"; fi
  pw="$(read_secret "Password for $name (from ./server.sh add-client): ")"
  [ -n "$pw" ] || die "empty password"
  printf '%s\n' "$pw" >"$BR_HOME/password"; chmod 600 "$BR_HOME/password"
  ensure_tools
  if [ ! -f "$BR_HOME/identity.key" ]; then
    docker run --rm --network none --user "$(id -u):$(id -g)" -v "$BR_HOME:/br" "$TOOLS_IMAGE" \
      sh -c 'umask 077; age-keygen -o /br/identity.key 2>/dev/null'
  fi
  echo "This machine ($name) is set up. Its public key, for sealing its bundle:"
  cmd_recipient
}

cmd_recipient() {
  ensure_tools
  docker run --rm --network none --user "$(id -u):$(id -g)" -v "$BR_HOME:/br:ro" "$TOOLS_IMAGE" age-keygen -y /br/identity.key
}

cmd_admin_keygen() {
  local out="${1:?usage: admin-keygen <file>}" dir
  [ ! -e "$out" ] || die "$out already exists"
  dir="$(cd "$(dirname "$out")" && pwd)"
  ensure_tools
  docker run --rm --network none --user "$(id -u):$(id -g)" -v "$dir:/out" "$TOOLS_IMAGE" \
    sh -c "umask 077; age-keygen -o /out/$(basename "$out") 2>/dev/null && age-keygen -y /out/$(basename "$out")" \
    | tee -a "$BR_HOME/admin-recipients"
  echo "Admin recovery key written to $out and its public half added to $BR_HOME/admin-recipients."
  echo "Move $out to offline storage. With it you can open any bundle sealed from here on."
}

cmd_seal() {
  local device="" src="" to="" bundle="" paths=() p
  while [ $# -gt 0 ]; do
    case "$1" in
      --to) to="${2:?--to needs an age1... key}"; shift 2 ;;
      --bundle) bundle="${2:?--bundle needs a name}"; shift 2 ;;
      *) if [ -z "$device" ]; then device="$1"; elif [ -z "$src" ]; then src="$1"; else paths+=("$1"); fi; shift ;;
    esac
  done
  [ -n "$device" ] && [ -n "$src" ] && [ ${#paths[@]} -gt 0 ] || die "usage: seal <device> <dir> <path>... [--to age1...]"
  valid_name "$device"; [ -z "$bundle" ] || valid_name "$bundle"
  [ -z "$to" ] || [[ "$to" =~ ^age1[0-9a-z]{58}$ ]] || die "--to is not an age public key: $to"
  src="$(cd "$src" && pwd)"
  for p in "${paths[@]}"; do
    [[ "$p" != /* && "$p" != *..* ]] || die "paths are relative to <dir> and may not contain '..': $p"
    [ -e "$src/$p" ] || die "not found: $src/$p"
  done
  local server pw admin=""
  server="$(conf server)"
  [ -f "$BR_HOME/admin-recipients" ] && admin="$(cat "$BR_HOME/admin-recipients")"
  [ -n "$admin" ] || [ -n "${BR_NO_ADMIN_WARNING:-}" ] || echo "warning: no admin recovery key in $BR_HOME/admin-recipients; only $device will be able to open this bundle" >&2
  pw="${BR_KEEPER_PASSWORD:-}"
  [ -n "$pw" ] || pw="$(read_secret "Keeper password for $server:5003: ")"
  ensure_tools
  # The plaintext only ever exists as a pipe inside this container: tar | age. /work is memory-backed.
  printf '%s\n' "$pw" | docker run --rm -i --network host --tmpfs /work:mode=0700 -v "$BR_HOME:/br:ro" -v "$src:/src:ro" \
    -e SERVER="$server" -e DEVICE="$device" -e REPO="$(repo "$device" "$bundle")" -e TO="$to" -e ADMIN="$admin" -e CA="$(ca_flag)" -e TYPE="$ARTIFACT_TYPE" \
    "$TOOLS_IMAGE" sh -euc '
      read -r pw; cd /work
      ref="$SERVER:5003/$REPO"
      if [ -z "$TO" ]; then
        # The device key is recorded with its bundles: this bundle first, then its main bundle.
        { printf "%s" "$pw" | oras pull $CA -u keeper --password-stdin -o prev "$ref:latest" >/dev/null 2>&1 ||
          printf "%s" "$pw" | oras pull $CA -u keeper --password-stdin -o prev "$SERVER:5003/secrets/$DEVICE:latest" >/dev/null 2>&1; } \
          || { echo "error: no earlier bundle for $DEVICE, so its key is unknown: pass --to <its ./secrets.sh recipient>" >&2; exit 1; }
        TO="$(head -n1 prev/recipients.txt)"
      fi
      printf "%s\n" "$TO" >recipients.txt
      printf "%s\n" "$@" >paths.txt
      { printf "%s\n" "$TO"; [ -z "$ADMIN" ] || printf "%s\n" "$ADMIN"; } >all-recipients
      tar -C /src --numeric-owner -cpf - "$@" | age -R all-recipients -o bundle.age
      ts="$(date -u +%Y%m%dT%H%M%SZ)"
      printf "%s" "$pw" | oras push $CA -u keeper --password-stdin --artifact-type "$TYPE" \
        -a "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" -a "blackrelay.device=$DEVICE" \
        "$ref:latest,$ts" bundle.age:application/vnd.blackrelay.secrets.bundle.v1+age recipients.txt:text/plain paths.txt:text/plain >/dev/null
      echo "sealed $# path(s) for $DEVICE -> $ref:latest (also :$ts)"
    ' seal "${paths[@]}"
}

cmd_fetch() {
  local dest="" device="" owner="" bundle=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --chown) owner="${2:?--chown needs uid:gid}"; shift 2 ;;
      --bundle) bundle="${2:?--bundle needs a name}"; shift 2 ;;
      *) if [ -z "$dest" ]; then dest="$1"; else device="$1"; fi; shift ;;
    esac
  done
  [ -n "$dest" ] || die "usage: fetch <dir> [device] [--chown uid:gid]"
  [ -n "$device" ] || device="$(conf name)"
  valid_name "$device"; [ -z "$bundle" ] || valid_name "$bundle"
  [ -z "$owner" ] || [[ "$owner" =~ ^[0-9]+:[0-9]+$ ]] || die "--chown takes numeric uid:gid"
  mkdir -p "$dest"; dest="$(cd "$dest" && pwd)"
  conf identity.key >/dev/null
  ensure_tools
  # Runs as root in the container so file owners recorded at seal time (e.g. uid 10001 for the
  # iq-remote key) are kept; --chown sets one owner instead.
  docker run --rm --network host --tmpfs /work:mode=0700 -v "$BR_HOME:/br:ro" -v "$dest:/dest" \
    -e SERVER="$(conf server)" -e NAME="$(conf name)" -e DEVICE="$device" -e REPO="$(repo "$device" "$bundle")" -e CA="$(ca_flag)" -e OWNER="$owner" \
    "$TOOLS_IMAGE" sh -euc '
      cd /work
      oras pull $CA -u "$NAME" --password-stdin -o . "$SERVER:5002/$REPO:latest" </br/password >/dev/null
      stage="/dest/.br-fetch.$$"; mkdir -m 700 "$stage"; trap "rm -rf \"$stage\"" EXIT
      age -d -i /br/identity.key bundle.age | tar -C "$stage" --numeric-owner -xpf -
      [ -z "$OWNER" ] || chown -R "$OWNER" "$stage"
      n=0
      while IFS= read -r p; do
        case "$p" in ""|/*|*..*) echo "error: bad path in bundle: $p" >&2; exit 1 ;; esac
        [ -e "$stage/$p" ] || { echo "error: bundle lists $p but does not contain it" >&2; exit 1; }
        mkdir -p "$(dirname "/dest/$p")"; rm -rf "/dest/$p"; mv "$stage/$p" "/dest/$p"; n=$((n+1)); echo "  $p"
      done <paths.txt
      echo "fetched $REPO: $n path(s) replaced, nothing else touched"
    '
}

cmd_list() {
  local device="" bundle=""
  while [ $# -gt 0 ]; do
    case "$1" in --bundle) bundle="${2:?--bundle needs a name}"; shift 2 ;; *) device="$1"; shift ;; esac
  done
  [ -n "$device" ] || device="$(conf name)"
  valid_name "$device"; [ -z "$bundle" ] || valid_name "$bundle"
  ensure_tools
  docker run --rm -i --network host -v "$BR_HOME:/br:ro" "$TOOLS_IMAGE" \
    sh -c "oras repo tags $(ca_flag) -u '$(conf name)' --password-stdin '$(conf server):5002/$(repo "$device" "$bundle")' </br/password"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  recipient) cmd_recipient ;;
  admin-keygen) shift; cmd_admin_keygen "$@" ;;
  seal) shift; cmd_seal "$@" ;;
  fetch) shift; cmd_fetch "$@" ;;
  list) shift; cmd_list "$@" ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
