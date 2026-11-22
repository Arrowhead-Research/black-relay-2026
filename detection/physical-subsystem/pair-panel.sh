#!/usr/bin/env bash
# Pair this computer with a Qolsys IQ5 panel as a read-only IQ Remote, then print the panel's zones.
# Usage:  ./pair-panel.sh [PANEL_IP]        (default 192.168.50.1, the panel's own Wi-Fi access point)
# macOS or Linux, Python 3.11+. Nothing here can arm, disarm or bypass: the bridge's one-way guard blocks it.
set -euo pipefail
cd "$(dirname "$0")"

PANEL_IP="${1:-192.168.50.1}"
PKI_DIR="data/pki"
CONFIG="data/config.yaml"

say() { printf '\n== %s\n' "$*"; }

say "Checking Python"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "Need Python 3.11 or newer (macOS: brew install python@3.12)."; exit 1; }
echo "Using $($PY --version)"

say "Installing the bridge into .venv (first run only takes a minute)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e '.[pair]'

say "Checking this computer can reach the panel at $PANEL_IP"
if [ -z "${SKIP_PING:-}" ] && command -v ping >/dev/null && ! ping -c 2 -W 2 "$PANEL_IP" >/dev/null 2>&1 && ! ping -c 2 -t 2 "$PANEL_IP" >/dev/null 2>&1; then
  echo "Cannot reach $PANEL_IP. Join the panel's Wi-Fi (or the same network as the panel), turn off any VPN, and run this again."
  exit 1
fi
echo "Panel answers."

mkdir -p "$PKI_DIR"; chmod 700 data "$PKI_DIR"
[ -f "$CONFIG" ] || cp deploy/config/config.example.yaml "$CONFIG"

say "Pairing. When it says it is listening, tap Pair on the panel:"
echo "   Settings > Advanced Settings (installer code 1111) > Installation > Devices > Wi-Fi Devices > IQ Remote Devices > Pair"
echo "   If macOS asks whether Python may accept incoming connections, click Allow."
.venv/bin/python -m br2 pair --config "$CONFIG" --panel-ip "$PANEL_IP" --pki-dir "$PKI_DIR" --timeout 600

say "Reading the panel's zones"
.venv/bin/python -m br2 snapshot --config "$CONFIG" --panel-ip "$PANEL_IP" --pki-dir "$PKI_DIR"

chmod -R go-rwx data
say "Done. Paired files are in $(pwd)/$PKI_DIR. Keep them private and never commit them."
echo "Watch live changes:  .venv/bin/python -m br2 watch --config $CONFIG --panel-ip $PANEL_IP --pki-dir $PKI_DIR"
