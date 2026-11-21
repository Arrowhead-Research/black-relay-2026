#!/usr/bin/env bash
# The rf form of detection/central/node-secrets.sh, under its old name. Run on the central server:
#   detection/central/rf-node-secrets.sh wbt-1 wbt-2 ... > node-secrets   same as node-secrets.sh rf ...
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/node-secrets.sh" rf "$@"
