#!/usr/bin/env bash
# Fetches this bridge's central broker login (sealed on the central server by
# detection/central/share-broker-login.sh) into deploy/secrets/fusion_password, owned by the fusion
# publisher's uid (10003), mode 0600. Needs `secrets.sh setup` done once on this machine.
#   deploy/scripts/fetch-broker-login.sh [device]      (default: this machine's name from secrets.sh setup)
set -euo pipefail
deploy="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$deploy/../../central-store/secrets.sh" fetch "$deploy" ${1:+"$1"} --bundle broker --chown 10003:10000
echo "Restart the publisher to use it: docker compose --profile fusion up -d fusion"
