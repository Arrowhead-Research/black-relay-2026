#!/usr/bin/env bash
# Checks fleet.sh seed output (valid YAML, no passphrase on the card, PSK matches wpa_passphrase's
# algorithm) and the playbook's syntax. Runs anywhere with bash and python3 (PyYAML); the playbook
# check also needs ansible-playbook.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

export SSH_KEY="$work/keys/fleet" FLEET_SEED_ANY_DIR=1 FLEET_ENV=/dev/null
mkdir -p "$work/boot"
WIFI_SSID='Lab "5G"' WIFI_PASSWORD='correct horse battery' "$here/fleet.sh" seed wbt-5 "$work/boot" >/dev/null
[ -f "$SSH_KEY" ] && [ -f "$SSH_KEY.pub" ] || fail "fleet key not created"
for f in user-data meta-data network-config ssh; do [ -e "$work/boot/$f" ] || fail "missing $f"; done
! grep -rq 'correct horse' "$work/boot" || fail "passphrase written to the card"
! grep -rq 'PRIVATE KEY' "$work/boot" || fail "private key written to the card"

python3 - "$work/boot" "$SSH_KEY.pub" <<'PY'
import hashlib, sys, yaml
boot, pub = sys.argv[1], open(sys.argv[2]).read().strip()
ud_text = open(f"{boot}/user-data").read()
assert ud_text.startswith("#cloud-config\n")
ud = yaml.safe_load(ud_text)
assert ud["hostname"] == "wbt-5" and ud["ssh_pwauth"] is False
u = ud["users"][0]
assert u["name"] == "pi" and u["lock_passwd"] is True and u["ssh_authorized_keys"] == [pub]
nc = yaml.safe_load(open(f"{boot}/network-config"))
ap = nc["network"]["wifis"]["wlan0"]["access-points"]
psk = hashlib.pbkdf2_hmac("sha1", b"correct horse battery", b'Lab "5G"', 4096, 32).hex()
assert ap == {'Lab "5G"': {"password": psk}}, ap
md = yaml.safe_load(open(f"{boot}/meta-data"))
assert md == {"instance-id": "wbt-5", "local-hostname": "wbt-5"}
PY

rm -f "$work/boot"/*
WIFI_SSID='' "$here/fleet.sh" seed wbt-6 "$work/boot" >/dev/null
python3 -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["network"]; assert "wifis" not in n and n["ethernets"]["eth0"]["dhcp4"]' "$work/boot/network-config"

if "$here/fleet.sh" seed 'Bad_Name' "$work/boot" 2>/dev/null; then fail "accepted a bad node name"; fi
mkdir "$work/notboot"
if FLEET_SEED_ANY_DIR='' "$here/fleet.sh" seed wbt-1 "$work/notboot" 2>/dev/null; then fail "seeded a non-boot dir"; fi

if command -v ansible-playbook >/dev/null; then
  (cd "$here/ansible" && ansible-playbook --syntax-check site.yml >/dev/null) || fail "playbook syntax"
  (cd "$here/ansible" && ansible-inventory --graph | grep -q wbt-8) || fail "inventory"
fi
echo "fleet tests passed"
