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
assert md["local-hostname"] == "wbt-5" and md["instance-id"].startswith("wbt-5-"), md
PY

rm -f "$work/boot"/*
WIFI_SSID='' "$here/fleet.sh" seed wbt-6 "$work/boot" >/dev/null
python3 -c 'import sys,yaml; n=yaml.safe_load(open(sys.argv[1]))["network"]; assert "wifis" not in n and n["ethernets"]["eth0"]["dhcp4"]' "$work/boot/network-config"

# Setup hub and rescue hotspot
rm -rf "$work/hub" "$work/spoke"; mkdir "$work/hub" "$work/spoke"
SETUP_SSID=BlackRelay-Setup SETUP_HUB=wbt-1 SETUP_PASSWORD='setup pass 99' "$here/fleet.sh" seed wbt-1 "$work/hub" >/dev/null
SETUP_SSID=BlackRelay-Setup SETUP_HUB=wbt-1 SETUP_PASSWORD='setup pass 99' "$here/fleet.sh" seed wbt-4 "$work/spoke" >/dev/null
! grep -rq 'setup pass 99' "$work/hub" "$work/spoke" || fail "setup passphrase written to the card"
python3 - "$work" <<'PY'
import base64, hashlib, sys, yaml
def files(d):
    ud = yaml.safe_load(open(f"{sys.argv[1]}/{d}/user-data"))
    return ud, {f["path"].rsplit("/", 1)[1]: base64.b64decode(f["content"]).decode() for f in ud["write_files"]}
def kv(text):
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
def psk(ssid):
    return hashlib.pbkdf2_hmac("sha1", b"setup pass 99", ssid.encode(), 4096, 32).hex()
ud, f = files("hub")
ap = kv(f["setup-ap.nmconnection"])
assert ap["mode"] == "ap" and ap["ssid"] == "BlackRelay-Setup" and ap["autoconnect"] == "true" and ap["method"] in ("shared", "disabled")
assert ap["pmf"] == "1" and ap["key-mgmt"] == "wpa-psk" and ap["pairwise"] == ap["group"] == "ccmp"
assert ap["psk"] == psk("BlackRelay-Setup") and "setup-wifi.nmconnection" not in f
assert ["nmcli", "connection", "up", "setup-ap"] in ud["runcmd"]
ud, f = files("spoke")
cl, ap = kv(f["setup-wifi.nmconnection"]), kv(f["setup-ap.nmconnection"])
assert cl["mode"] == "infrastructure" and cl["ssid"] == "BlackRelay-Setup" and cl["psk"] == psk("BlackRelay-Setup")
assert ap["ssid"] == "wbt-4-setup" and ap["autoconnect"] == "false" and ap["pmf"] == "1" and ap["psk"] == psk("wbt-4-setup")
assert cl["autoconnect"] == "true" and "pmf" not in cl
assert not any(r and r[0] == "systemd-run" for r in ud["runcmd"] if r) and "rf-diag" not in f
assert ["nmcli", "connection", "up", "setup-ap"] not in ud["runcmd"]
assert ["systemctl", "enable", "--now", "rf-wifi-fallback.timer"] in ud["runcmd"] and "rf-wifi-fallback" in f
PY

# Re-seeding gives a new instance-id (first boot runs again) and forgets the node's old host key;
# FLEET_DIAG adds the Wi-Fi diagnostics, started first.
before="$(grep instance-id "$work/hub/meta-data")"; sleep 1
printf 'wbt-1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld1111111111111111111111111111111111111111\nwbt-2 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKeep222222222222222222222222222222222222222\n' >"$work/known_hosts"
KNOWN_HOSTS="$work/known_hosts" FLEET_DIAG=1 SETUP_SSID=BlackRelay-Setup SETUP_HUB=wbt-1 SETUP_PASSWORD='setup pass 99' "$here/fleet.sh" seed wbt-1 "$work/hub" >/dev/null
[ "$(grep instance-id "$work/hub/meta-data")" != "$before" ] || fail "re-seed kept the instance-id"
{ ! grep -q '^wbt-1 ' "$work/known_hosts" && grep -q '^wbt-2 ' "$work/known_hosts"; } || fail "known_hosts not updated"
python3 - "$work/hub/user-data" <<'PY'
import sys, yaml
ud = yaml.safe_load(open(sys.argv[1]))
assert [r for r in ud["runcmd"] if r][0] == ["systemd-run", "--collect", "--unit=rf-diag", "/usr/local/sbin/rf-diag"], ud["runcmd"]
paths = [f["path"] for f in ud["write_files"]]
assert "/usr/local/sbin/rf-diag" in paths and any(p.endswith("setup-ap.nmconnection") for p in paths), paths
PY
sh -n "$here/files/rf-diag" || fail "rf-diag syntax"

if "$here/fleet.sh" seed 'Bad_Name' "$work/boot" 2>/dev/null; then fail "accepted a bad node name"; fi
mkdir "$work/notboot"
if FLEET_SEED_ANY_DIR='' "$here/fleet.sh" seed wbt-1 "$work/notboot" 2>/dev/null; then fail "seeded a non-boot dir"; fi

if command -v ansible-playbook >/dev/null; then
  (cd "$here/ansible" && ansible-playbook --syntax-check site.yml >/dev/null && ansible-playbook --syntax-check wifi.yml >/dev/null) || fail "playbook syntax"
  (cd "$here/ansible" && ansible-inventory --graph | grep -q wbt-4) || fail "inventory"
fi
echo "fleet tests passed"
