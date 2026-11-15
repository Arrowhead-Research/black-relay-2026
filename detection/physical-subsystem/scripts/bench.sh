#!/usr/bin/env bash
# One-command bench run of the whole bridge on a developer machine, no Docker, against a real panel.
#
#   scripts/bench.sh --panel-ip <ip> --pki-dir <dir>
#
# Needs: python3, openssl, mosquitto (brew install mosquitto | apt install mosquitto). Needs the network only to install the
# Python dependencies the first time. Everything it creates lives in the git-ignored bench/ folder (certificates, a fresh API
# key's hash, broker configs, generated config, event log); Ctrl-C stops everything it started.
#
# It never changes the panel guard: the bridge stays strictly read-only toward the panel, and iq_remote.pin_mode stays
# `required` (a recorded pin is mandatory; run `br2 repin --yes` first).
#
# --simulator (harness panel simulator ONLY): needs --ca-file <sim CA>; it switches pin_mode to `tofu` and trusts that CA.
#   Never use it against a real panel.
# Bash 3.2 compatible (macOS); no GNU-only flags.
set -eu
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BENCH="$ROOT/bench"
API_PORT=${BENCH_API_PORT:-18443}
CONSUMER_PORT=${BENCH_CONSUMER_PORT:-18883}     # TLS, authenticated: the "consumer" broker
TEAM_PORT=${BENCH_TEAM_PORT:-11883}             # plaintext on localhost: stands in for the team (fusion) broker

PANEL_IP=""; PKI_DIR=""; SIM=0; CA_FILE=""; PANEL_PORT=8883
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --panel-ip) PANEL_IP=${2:-}; shift 2 ;;
    --pki-dir) PKI_DIR=${2:-}; shift 2 ;;
    --simulator) SIM=1; shift ;;
    --ca-file) CA_FILE=${2:-}; shift 2 ;;
    --panel-port) PANEL_PORT=${2:-}; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$PANEL_IP" ] && [ -n "$PKI_DIR" ] || { echo "usage: scripts/bench.sh --panel-ip <ip> --pki-dir <dir>" >&2; exit 2; }
[ -d "$PKI_DIR" ] || { echo "ERROR: --pki-dir $PKI_DIR is not a directory" >&2; exit 2; }
PKI_DIR=$(cd "$PKI_DIR" && pwd)
if [ "$SIM" = 1 ]; then
  [ -n "$CA_FILE" ] || { echo "ERROR: --simulator needs --ca-file <the simulator's CA certificate>" >&2; exit 2; }
  echo "*** SIMULATOR MODE: pin_mode=tofu and a custom panel CA. For the harness simulator only, never a real panel. ***" >&2
elif [ -n "$CA_FILE" ]; then
  echo "ERROR: --ca-file is only accepted together with --simulator" >&2; exit 2
fi

# ---- tools
MOSQ=""
for d in $(echo "$PATH" | tr ':' ' ') /usr/sbin /opt/homebrew/sbin /usr/local/sbin /opt/homebrew/opt/mosquitto/sbin /usr/local/opt/mosquitto/sbin; do
  if [ -x "$d/mosquitto" ]; then MOSQ="$d/mosquitto"; break; fi
done
[ -n "$MOSQ" ] || { echo "ERROR: mosquitto not found (looked in PATH, /usr/sbin, /opt/homebrew/sbin, /usr/local/sbin). Install it: brew install mosquitto   or   apt install mosquitto" >&2; exit 1; }
MOSQ_DIR=$(dirname "$MOSQ")
MOSQ_PASSWD=$(command -v mosquitto_passwd 2>/dev/null || true)
# mosquitto_passwd / mosquitto_sub live in a bin folder next to the sbin one (Homebrew: .../opt/mosquitto/bin)
for d in "$MOSQ_DIR" "$MOSQ_DIR/../bin" /opt/homebrew/opt/mosquitto/bin /usr/local/opt/mosquitto/bin /opt/homebrew/bin /usr/local/bin; do
  [ -n "$MOSQ_PASSWD" ] && break
  [ -x "$d/mosquitto_passwd" ] && MOSQ_PASSWD="$d/mosquitto_passwd"
done
[ -n "$MOSQ_PASSWD" ] || { echo "ERROR: mosquitto_passwd not found next to mosquitto (brew install mosquitto | apt install mosquitto)" >&2; exit 1; }
MOSQ_SUB=$(command -v mosquitto_sub 2>/dev/null || true)
for d in "$MOSQ_DIR/../bin" /opt/homebrew/opt/mosquitto/bin /usr/local/opt/mosquitto/bin; do
  [ -n "$MOSQ_SUB" ] && break
  [ -x "$d/mosquitto_sub" ] && MOSQ_SUB="$d/mosquitto_sub"
done
[ -n "$MOSQ_SUB" ] || MOSQ_SUB=mosquitto_sub
command -v openssl >/dev/null || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 1; }
command -v curl >/dev/null || { echo "ERROR: curl not found" >&2; exit 1; }

# ---- the tree's own venv
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -c "import br2, aiohttp, aiomqtt, argon2, yaml" 2>/dev/null; then
  echo "Creating $ROOT/.venv and installing the bridge (needs the network, once) ..." >&2
  [ -x "$PY" ] || python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -e "$ROOT" >&2
fi

# ---- the pin must already be recorded (pin_mode stays `required`)
if [ "$SIM" = 0 ]; then
  set +e
  "$PY" - "$PKI_DIR" <<'PY'
import sys
from br2.pin import PanelPin, PinUnreadable
try:
    sys.exit(0 if PanelPin(sys.argv[1]).stored() else 3)
except PinUnreadable as e:
    print(e, file=sys.stderr); sys.exit(4)
PY
  rc=$?
  set -e
  if [ $rc -eq 3 ]; then
    echo "ERROR: no panel pin is recorded in $PKI_DIR. The bench keeps iq_remote.pin_mode: required and will not trust whatever answers at the panel address." >&2
    echo "       Verify you are on the panel's network, then run:  $ROOT/.venv/bin/python -m br2 repin --config <config> --panel-ip $PANEL_IP --pki-dir $PKI_DIR --yes" >&2
    exit 1
  elif [ $rc -ne 0 ]; then
    echo "ERROR: the pin file in $PKI_DIR is unreadable (see above). Fix it, or re-record it with: br2 repin --yes" >&2
    exit 1
  fi
fi

# ---- bench/ : certificates (kept between runs), then fresh secrets, configs, data
umask 077
mkdir -p "$BENCH/certs" "$BENCH/secrets" "$BENCH/data" "$BENCH/mosquitto"
if [ ! -f "$BENCH/certs/ca.crt" ] || [ ! -f "$BENCH/certs/server.crt" ]; then
  echo "Generating the bench test CA and server certificate ..." >&2
  cat > "$BENCH/certs/ca.cnf" <<CNF
[req]
distinguished_name=dn
prompt=no
x509_extensions=v3
[dn]
CN=BlackRelay Bench CA
[v3]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
CNF
  cat > "$BENCH/certs/server.ext" <<CNF
subjectAltName=IP:127.0.0.1,DNS:localhost
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
CNF
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$BENCH/certs/ca.key" -out "$BENCH/certs/ca.crt" -days 825 -config "$BENCH/certs/ca.cnf" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout "$BENCH/certs/server.key" -out "$BENCH/certs/server.csr" -subj "/CN=localhost" 2>/dev/null
  openssl x509 -req -in "$BENCH/certs/server.csr" -CA "$BENCH/certs/ca.crt" -CAkey "$BENCH/certs/ca.key" -CAcreateserial \
    -out "$BENCH/certs/server.crt" -days 825 -extfile "$BENCH/certs/server.ext" 2>/dev/null
  rm -f "$BENCH/certs/server.csr" "$BENCH/certs/ca.srl"
  chmod 600 "$BENCH"/certs/*.key
  chmod 644 "$BENCH/certs/ca.crt" "$BENCH/certs/server.crt"
fi

# fresh API key every run: printed once, only its key_id and argon2 hash are written
HK=$("$PY" -m br2 hash-key)
API_KEY=$(printf '%s\n' "$HK" | sed -n 's/^API key (shown once): //p')
KEY_ID=$(printf '%s\n' "$HK" | sed -n 's/^key_id: //p')
KEY_HASH=$(printf '%s\n' "$HK" | sed -n 's/^argon2_hash: //p')
[ -n "$API_KEY" ] && [ -n "$KEY_ID" ] && [ -n "$KEY_HASH" ] || { echo "ERROR: could not generate an API key" >&2; exit 1; }

MQ_PW=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(18))')
printf '%s\n' "$MQ_PW" > "$BENCH/secrets/mqtt_password"
rm -f "$BENCH/mosquitto/passwd"
"$MOSQ_PASSWD" -c -b "$BENCH/mosquitto/passwd" blackrelay "$MQ_PW" >/dev/null 2>&1

# a mosquitto started as root drops to the `mosquitto` user, which cannot read this folder: stay root (bench only)
RUN_AS=""; [ "$(id -u)" = 0 ] && RUN_AS="user root"
cat > "$BENCH/mosquitto/consumer.conf" <<CNF
$RUN_AS
per_listener_settings true
persistence false
log_dest file $BENCH/mosquitto/consumer.log
listener $CONSUMER_PORT 127.0.0.1
protocol mqtt
allow_anonymous false
password_file $BENCH/mosquitto/passwd
cafile $BENCH/certs/ca.crt
certfile $BENCH/certs/server.crt
keyfile $BENCH/certs/server.key
tls_version tlsv1.2
CNF
cat > "$BENCH/mosquitto/team.conf" <<CNF
$RUN_AS
persistence false
log_dest file $BENCH/mosquitto/team.log
listener $TEAM_PORT 127.0.0.1
protocol mqtt
allow_anonymous true
CNF

PIN_MODE=required; CA_LINE=""
if [ "$SIM" = 1 ]; then PIN_MODE=tofu; CA_LINE="  ca_file: '$CA_FILE'"; fi
cat > "$BENCH/config.yaml" <<CNF
# generated by scripts/bench.sh; contains no secret (a key_id and an argon2 hash only)
iq_remote:
  enabled: true
  panel_ip: '$PANEL_IP'
  panel_port: $PANEL_PORT
  pki_dir: '$PKI_DIR'
  pin_mode: $PIN_MODE
  owner: dir
$CA_LINE
control4:
  enabled: false
mqtt:
  host: 127.0.0.1
  port: $CONSUMER_PORT
  tls: true
  ca_file: '$BENCH/certs/ca.crt'
  username: blackrelay
  password_file: '$BENCH/secrets/mqtt_password'
api:
  host: 127.0.0.1
  port: $API_PORT
  tls_cert: '$BENCH/certs/server.crt'
  tls_key: '$BENCH/certs/server.key'
  metrics_port: 0
  keys:
    - name: bench
      key_id: '$KEY_ID'
      argon2_hash: '$KEY_HASH'
site:
  id: bench
  name: bench
fusion:
  enabled: true
  host: 127.0.0.1
  port: $TEAM_PORT
  tls: null
  client_id: physical-subsystem-bench
  metrics_port: 0
storage:
  data_dir: '$BENCH/data'
  retention_days: 30
log_level: INFO
CNF

# ---- start everything; Ctrl-C (or any exit) stops exactly what we started
IPC=$(mktemp -d "${TMPDIR:-/tmp}/br2bench.XXXXXX")        # short path: unix socket paths are limited to ~104 bytes
PIDS=""; BR2_PID=""
cleanup() {
  trap - EXIT INT TERM
  echo >&2; echo "Stopping ..." >&2
  [ -n "$BR2_PID" ] && kill -TERM "$BR2_PID" 2>/dev/null || true
  [ -n "$BR2_PID" ] && wait "$BR2_PID" 2>/dev/null || true
  for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
  for p in $PIDS; do wait "$p" 2>/dev/null || true; done
  rm -rf "$IPC"
  echo "Stopped. Nothing is left running; bench/ keeps the certificates, config and event log." >&2
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$MOSQ" -c "$BENCH/mosquitto/consumer.conf" >/dev/null 2>&1 & PIDS="$PIDS $!"
"$MOSQ" -c "$BENCH/mosquitto/team.conf" >/dev/null 2>&1 & PIDS="$PIDS $!"
sleep 1
for p in $PIDS; do
  kill -0 "$p" 2>/dev/null || { echo "ERROR: a mosquitto instance exited at start (see $BENCH/mosquitto/*.log; are ports $CONSUMER_PORT / $TEAM_PORT free?)" >&2; exit 1; }
done

BR2_IPC_DIR="$IPC" PYTHONUNBUFFERED=1 "$PY" -m br2 run --config "$BENCH/config.yaml" >&2 & BR2_PID=$!

URL="https://localhost:$API_PORT"
n=0; code=000
while [ $n -lt 60 ]; do
  kill -0 "$BR2_PID" 2>/dev/null || { echo "ERROR: br2 run exited during start (see the log above)" >&2; BR2_PID=""; exit 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$BENCH/certs/ca.crt" "$URL/v1/health" 2>/dev/null || true)
  [ "$code" != 000 ] && [ -n "$code" ] && break
  n=$((n+1)); sleep 1
done
[ "$code" != 000 ] || { echo "ERROR: the API did not answer within 60 s" >&2; exit 1; }

cat <<EOT

================================================================================================
Bench is up (panel $PANEL_IP). Ctrl-C here stops everything.

API key (shown once, not stored; only its hash is in bench/config.yaml):
  export BR2_KEY='$API_KEY'
  cd '$ROOT'

Health, zones:
  curl --cacert bench/certs/ca.crt $URL/v1/health
  curl --cacert bench/certs/ca.crt -H "Authorization: Bearer \$BR2_KEY" $URL/v1/zones

Live stream (SSE):
  curl -N --cacert bench/certs/ca.crt -H "Authorization: Bearer \$BR2_KEY" $URL/v1/stream

Team broker (plaintext, localhost):
  $MOSQ_SUB -h 127.0.0.1 -p $TEAM_PORT -t 'detection/physical/#' -v

Last 10 events in the SQLite log:
  sqlite3 -readonly bench/data/blackrelay.db "select seq, ts, type, subject, from_json, to_json, source from events order by seq desc limit 10;"
================================================================================================
EOT
wait "$BR2_PID" || true
BR2_PID=""
