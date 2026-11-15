#!/usr/bin/env bash
# usage: harness/run.sh <impl_dir> [pytest args...]     e.g.  run.sh ../f1-py-mono -k failover -x
# env:   HARNESS_TIMEOUT       wall-clock limit for the whole run, seconds (default 2400); exit 124 when it fires
#        HARNESS_TEST_TIMEOUT  per-test limit, seconds (default 240), enforced inside conformance/conftest.py
# Cleanup: this run has its own work dir, harness/work/<impl>-<timestamp>-<pid>. However the run ends (normal end, test
# error, Ctrl-C, SIGTERM/HUP, pytest killed with -9), every process whose argv contains THAT work dir gets SIGTERM, then
# 3 s later SIGKILL (together with its process group). Processes of other runs are never matched.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPL="${1:?usage: run.sh <impl_dir> [pytest args]}"; shift || true
IMPL="$(cd "$IMPL" && pwd)"
[ -x "$IMPL/harness_start.sh" ] || { echo "FAIL: $IMPL/harness_start.sh missing or not executable (see $HERE/CONVENTION.md)"; exit 2; }
TMO="${HARNESS_TIMEOUT:-2400}"
export BR_IMPL="$IMPL"
export BR_WORK="$HERE/work/$(basename "$IMPL")-$(date +%Y%m%d-%H%M%S)-$$"
CHILD=""

# pids whose argv contains this run's work dir (never this shell)
run_pids() {
  local d pid arg
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [ "$pid" = "$$" ] && continue
    { while IFS= read -r -d '' arg; do
        case "$arg" in "$BR_WORK"|*"$BR_WORK/"*) echo "$pid"; break ;; esac
      done < "$d/cmdline"; } 2>/dev/null || true     # a process may exit mid-scan: its cmdline vanishes
  done
}

# send a signal to each matching process and to its process group (every harness child is a session leader)
signal_run() {
  local pid
  for pid in $(run_pids); do
    kill -s "$1" -- "-$pid" 2>/dev/null || true
    kill -s "$1" "$pid" 2>/dev/null || true
  done
}

reap_run() {
  [ -n "$(run_pids)" ] || return 0
  signal_run TERM
  for _ in $(seq 15); do                      # 3 s grace
    [ -n "$(run_pids)" ] || return 0
    sleep 0.2
  done
  signal_run KILL
  sleep 0.2
  local left; left="$(run_pids | tr '\n' ' ')"
  [ -z "$left" ] || echo "WARNING: processes still alive for $BR_WORK after SIGKILL: $left" >&2
  return 0
}

stop_child() {
  [ -n "$CHILD" ] || return 0
  kill -TERM "$CHILD" 2>/dev/null || return 0   # already reaped: nothing to stop
  local i st
  for i in $(seq 50); do                        # up to 10 s for timeout -> pytest to reap its groups and exit
    st=$(sed -n 's/.*) \(.\).*/\1/p' "/proc/$CHILD/stat" 2>/dev/null) || break
    [ "$st" = Z ] && break
    sleep 0.2
  done
  kill -KILL "$CHILD" 2>/dev/null || true
  wait "$CHILD" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  stop_child
  reap_run
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

mkdir -p "$BR_WORK"
echo "run.sh: work dir $BR_WORK (global timeout ${TMO}s)"
command -v mosquitto >/dev/null || { echo "installing mosquitto (apt)"; apt-get install -y mosquitto mosquitto-clients >/dev/null; }
if [ ! -x "$HERE/.venv/bin/python" ]; then python3 -m venv "$HERE/.venv"; "$HERE/.venv/bin/pip" -q install -r "$HERE/requirements.txt"; fi
cd "$HERE"
# default: whole conformance suite; extra args go to pytest. --basetemp puts this run's work dir into pytest's own
# argv, so cleanup also finds the pytest process itself.
rc=0
timeout --kill-after=30 "$TMO" "$HERE/.venv/bin/python" -m pytest -v -rfE --tb=short -p no:cacheprovider --color=no \
  --basetemp="$BR_WORK/pytest-basetemp" conformance "$@" &
CHILD=$!
wait "$CHILD" || rc=$?
[ "$rc" != 124 ] || echo "FAIL: run exceeded HARNESS_TIMEOUT=${TMO}s (rc 124)"
exit "$rc"
