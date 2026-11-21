#!/usr/bin/env bash
# wifi-diag.sh [ssid] [node|all]: what the nodes' onboard radios see and why joining <ssid> failed.
# Run from a machine that can reach the nodes (e.g. on the setup network). Writes ~/fleet-wifi-diag.txt
# so it can be read after you switch networks. Prints no keys or passphrases.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ssid="${1:-}" limit="${2:-rf_nodes}" out="$HOME/fleet-wifi-diag.txt"
[ "$limit" != all ] || limit=rf_nodes
[ ! -f "$here/fleet.env" ] || . "$here/fleet.env"
export FLEET_SSH_KEY="${SSH_KEY:-$HOME/.ssh/blackrelay_fleet}" FLEET_ADMIN_USER="${ADMIN_USER:-pi}"
export FLEET_KNOWN_HOSTS="${KNOWN_HOSTS:-$HOME/.ssh/blackrelay_fleet_known_hosts}"
cmd='echo "--- $(hostname) $(date -Is)"
echo "--- radio"; iw dev wlan0 info 2>&1 | grep -E "type|ssid|channel"; iw reg get 2>&1 | head -3
echo "--- connections"; nmcli -f NAME,DEVICE,STATE connection show --active
echo "--- fleet-wifi profile (no secrets)"; nmcli -f connection.autoconnect,802-11-wireless.ssid,802-11-wireless-security.key-mgmt connection show fleet-wifi 2>&1
echo "--- scan (matching networks; all if no ssid given)"; nmcli -f SSID,SECURITY,CHAN,FREQ,SIGNAL,BSSID device wifi list --rescan yes 2>&1 | grep -iE "^SSID|'"${ssid:-.}"'"
echo "--- NetworkManager log (fleet-wifi attempts)"
journalctl -b --no-pager -u NetworkManager -u wpa_supplicant 2>&1 | grep -iE "WRONG_KEY|TEMP-DISABLED|ASSOC-REJECT|status_code|EAPOL|PMKSA|fleet-wifi|SSID_FILTER|'"${ssid:-fleet-wifi}"'|4-way|handshake|reason|auth|assoc|sae|psk|no secrets|timeout|failed" | grep -vE "psk=|p2p-dev" | tail -90'
cd "$here/ansible" && ansible "$limit" -b -m ansible.builtin.shell -a "$cmd" >"$out" 2>&1
echo "wrote $out ($(wc -l <"$out") lines)"
