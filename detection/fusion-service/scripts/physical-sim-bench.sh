#!/usr/bin/env bash
# Bring up the physical subsystem's simulated bench for fusion: the panel simulator, then the
# bridge and its brokers (scripts/bench.sh --simulator) publishing to the plaintext team broker on
# 127.0.0.1:11883, which config.physical.example.yaml expects. Fusion is started separately.
#
#   scripts/physical-sim-bench.sh            Ctrl-C stops everything it started
#
# Drive the simulated panel by writing commands to bench/sim.in, one per line:
#   echo "zone 12 Open" > bench/sim.in        zone 12 is "Front door" (zones 12-16, 40 exist)
#   echo "zone 12 Closed" > bench/sim.in
#   echo "iq outage" > bench/sim.in           drop the panel link (zone_stale events); "iq restore"
# The physical tree must be checked out beside this one. Needs python3, openssl, curl and
# mosquitto (bench.sh's own requirements). The first run creates physical-subsystem/.venv.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
PHYSICAL=$(cd "$HERE/../physical-subsystem" && pwd)
BENCH="$HERE/bench"
PY="$PHYSICAL/.venv/bin/python"

[ -f "$PHYSICAL/scripts/bench.sh" ] || { echo "ERROR: $PHYSICAL/scripts/bench.sh not found" >&2; exit 2; }
if [ ! -x "$PY" ] || ! "$PY" -c "import br2, aiomqtt, argon2, yaml, paho.mqtt, cryptography" 2>/dev/null; then
  echo "Creating $PHYSICAL/.venv with the bridge and harness dependencies (needs the network, once) ..." >&2
  [ -x "$PY" ] || python3 -m venv "$PHYSICAL/.venv"
  "$PHYSICAL/.venv/bin/pip" install -q -e "$PHYSICAL" -r "$PHYSICAL/harness/requirements.txt" >&2
fi

rm -rf "$BENCH"
mkdir -p "$BENCH"
mkfifo "$BENCH/sim.in"

SIM_PID=""; BENCH_PID=""; TAIL_PID=""
cleanup() {
  trap - EXIT INT TERM
  echo >&2; echo "Stopping ..." >&2
  # bench.sh has its own trap: TERM makes it stop the bridge and both brokers.
  [ -n "$BENCH_PID" ] && kill -TERM "$BENCH_PID" 2>/dev/null || true
  [ -n "$BENCH_PID" ] && wait "$BENCH_PID" 2>/dev/null || true
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null || true
  [ -n "$SIM_PID" ] && { echo quit > "$BENCH/sim.in" 2>/dev/null || true; }
  exec 4>&- 2>/dev/null || true
  sleep 1
  # Whatever is left of the simulator and its broker is addressed by our bench directory.
  pkill -TERM -f "$BENCH/sim" 2>/dev/null || true
  sleep 1
  pkill -KILL -f "$BENCH/sim" 2>/dev/null || true
  rm -f "$BENCH/sim.in"
  echo "Stopped. bench/ keeps the simulator's working directory and logs." >&2
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The simulator reads commands from stdin; a pipe held open on fd 4 keeps it from seeing EOF.
( cd "$PHYSICAL/harness" && exec "$PY" -m sim.run_standalone "$BENCH/sim" < "$BENCH/sim.in" ) > "$BENCH/sim.log" 2>&1 &
SIM_PID=$!
exec 4>"$BENCH/sim.in"

n=0
while ! grep -q 'panel_port=' "$BENCH/sim.log" 2>/dev/null; do
  kill -0 "$SIM_PID" 2>/dev/null || { echo "ERROR: the simulator exited (see $BENCH/sim.log)" >&2; exit 1; }
  n=$((n + 1)); [ $n -lt 60 ] || { echo "ERROR: the simulator did not start within 30 s" >&2; exit 1; }
  sleep 0.5
done
PKI_DIR=$(sed -n 's/^pki_dir=\([^ ]*\).*/\1/p' "$BENCH/sim.log")
CA_FILE=$(sed -n 's/.*ca_file=\([^ ]*\).*/\1/p' "$BENCH/sim.log")
PANEL_PORT=$(sed -n 's/.*panel_port=\([0-9]*\).*/\1/p' "$BENCH/sim.log")
echo "Simulated panel on 127.0.0.1:$PANEL_PORT" >&2

( cd "$PHYSICAL" && exec scripts/bench.sh --simulator --panel-ip 127.0.0.1 --panel-port "$PANEL_PORT" \
    --pki-dir "$PKI_DIR" --ca-file "$CA_FILE" ) > "$BENCH/bench.log" 2>&1 &
BENCH_PID=$!
tail -n +1 -f "$BENCH/bench.log" >&2 &
TAIL_PID=$!
n=0
while ! grep -q 'Bench is up' "$BENCH/bench.log" 2>/dev/null; do
  grep -q '^ERROR' "$BENCH/bench.log" 2>/dev/null && exit 1
  n=$((n + 1)); [ $n -lt 120 ] || { echo "ERROR: the bench did not come up within 120 s" >&2; exit 1; }
  sleep 1
done

cat <<EOT >&2

================================================================================================
Physical simulator bench is up. The bridge publishes detection/physical/# to 127.0.0.1:11883.

Start fusion in another terminal (from $HERE):
  FUSION_API_TOKEN=benchtoken uv run fusion-service serve --config config.physical.example.yaml --port 8090

Trip the simulated door, then read it back:
  echo "zone 12 Open" > bench/sim.in
  echo "zone 12 Closed" > bench/sim.in
  curl -s -H "Authorization: Bearer benchtoken" 'http://127.0.0.1:8090/api/v1/events?type=physical.event' | python3 -m json.tool

Ctrl-C here stops the simulator and the bench.
================================================================================================
EOT
wait "$BENCH_PID" || true
BENCH_PID=""
