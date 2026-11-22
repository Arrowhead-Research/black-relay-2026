#!/usr/bin/env bash
# The RF form of detection/central/node-logins.sh, under its old name (the fleet playbook calls it):
#   rf-node-logins.sh [--get] [--no-restart] wbt-1 ...   same as node-logins.sh rf ...
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/node-logins.sh" rf "$@"
