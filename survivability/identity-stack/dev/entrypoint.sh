#!/usr/bin/env bash
set -euo pipefail

mkdir -p \
  /home/pi/.pi/agent \
  /home/pi/.pi-web

pi-web-sessiond &

exec pi-web-server
