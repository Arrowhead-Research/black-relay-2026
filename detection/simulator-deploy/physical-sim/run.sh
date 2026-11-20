#!/usr/bin/env bash
# The bridge reads the broker password from a file; hand it over from the environment and start
# the bench (simulator + bridge + its local brokers). Ctrl-C / compose stop ends everything.
set -eu
umask 077
printf '%s\n' "${BENCH_FUSION_PASSWORD:?BENCH_FUSION_PASSWORD is required}" > /run/fusion.pw
export BENCH_FUSION_PASSWORD_FILE=/run/fusion.pw
unset BENCH_FUSION_PASSWORD
exec /app/fusion-service/scripts/physical-sim-bench.sh
