#!/usr/bin/env bash
# rf-bt-doctor: diagnose (and with --fix, repair) Bluetooth capture on a WBT node.
#
#   sudo ./rf-bt-doctor.sh          # report only, changes nothing
#   sudo ./rf-bt-doctor.sh --fix    # apply the standard fixes, then re-test
#
# Symptom this targets: Kismet shows the node's ":bt" source as running, with
# no errors, but it delivers no packets. Cause found on TP-Link UB500 dongles
# (Realtek RTL8761BU): after a capture helper exited (e.g. Kismet restarted),
# the adapter stayed in "discovering" and answered every new scan with "Busy".
# The Pi's onboard Bluetooth did not get stuck.
#
# --fix: clears the stuck discovery, disables USB autosuspend for Bluetooth
# (now and on future boots), unblocks rfkill, power-cycles (or re-enumerates)
# the capture adapter, and restarts the capture unit (kismet-cap-bt). Nothing
# is removed. Reads /etc/rf-node/rf-node.env.

set -u
FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

ENV_FILE=/etc/rf-node/rf-node.env
# shellcheck disable=SC1090
[[ -f $ENV_FILE ]] && . "$ENV_FILE"
UNIT=kismet-cap-bt
SCAN_S=10
problems=0
note() { echo "  - $*"; }
bad() { echo "  ! $*"; problems=$((problems + 1)); }

# shellcheck source=rf-bt-lib.sh
. "$(dirname "$(readlink -f "$0")")/rf-bt-lib.sh"

bt_addr_cache_fill   # one btmgmt query for all adapter addresses
echo "== Bluetooth adapters"
shopt -s nullglob
adapters=(/sys/class/bluetooth/hci*)
[[ ${#adapters[@]} -gt 0 ]] || bad "no Bluetooth adapters found (is the dongle plugged in? lsusb)"
for d in "${adapters[@]}"; do
  h=$(basename "$d"); addr=$(cat "$d/address" 2>/dev/null)
  u=$(usb_dir "$h")
  if [[ -n $u ]]; then
    kind="USB $(cat "$u/idVendor"):$(cat "$u/idProduct") $(cat "$u/product" 2>/dev/null)"
    pc=$(cat "$u/power/control" 2>/dev/null)
    kind+=" (power/control=$pc)"
    [[ $pc == auto ]] && bad "$h: USB autosuspend is on; Realtek dongles often stop delivering events when suspended"
  else
    kind="onboard"
  fi
  echo "  $h  ${addr:-$(bt_addr "$h")}  $kind"
done
CAP=$(capture_iface) || CAP="(configured adapter missing)"
echo "  capture adapter: $CAP (BT_ADDR=${BT_ADDR:-} BT_PREFER=${BT_PREFER:-} BT_IFACE=${BT_IFACE:-hci0})"
[[ -e /sys/class/bluetooth/$CAP ]] || bad "capture adapter $CAP does not exist"

echo "== Driver and kernel log"
ap=$(cat /sys/module/btusb/parameters/enable_autosuspend 2>/dev/null)
[[ -n $ap ]] && note "btusb enable_autosuspend=$ap"
[[ $ap == Y ]] && bad "btusb autosuspend enabled for all Bluetooth dongles"
# Only the capture adapter matters (other adapters, e.g. a Wi-Fi combo chip's
# Bluetooth, often log harmless timeouts at boot).
dmesg 2>/dev/null | grep -E "${CAP}:|btusb|rtl_bt" | tail -n 8 | sed 's/^/    /'
if dmesg 2>/dev/null | grep -E "${CAP}:" | grep -iqE "tx timeout|command .* timeout|failed|error"; then
  bad "kernel reports command timeouts/errors on $CAP (see above)"
fi
if dmesg 2>/dev/null | grep -iqE "rtl_bt.*(failed|not found)|firmware.*rtl8761.*(failed|not found)"; then
  bad "Realtek Bluetooth firmware missing: install firmware-realtek (Debian) / linux-firmware"
fi

echo "== Blocking and services"
for r in /sys/class/bluetooth/"$CAP"/rfkill*; do   # the capture adapter's own rfkill switch
  [[ -r $r/soft ]] || continue
  [[ $(cat "$r/soft") == 1 || $(cat "$r/hard" 2>/dev/null) == 1 ]] && bad "$CAP is rfkill-blocked"
done
note "bluetooth.service: $(systemctl is-active bluetooth 2>/dev/null)"
note "$UNIT: $(systemctl is-active $UNIT 2>/dev/null)"
helpers=$(pgrep -af "kismet_cap_linux_bluetooth" | grep -v pgrep)   # -f: name is longer than 15 chars
if [[ -z $helpers ]]; then
  bad "no kismet_cap_linux_bluetooth helper running"
else
  echo "$helpers" | sed 's/^/    helper: /'
  echo "$helpers" | grep -q -- "--source ${CAP}:" || bad "helper is not capturing on $CAP"
fi

scan_test() {  # sets SCAN_FOUND (devices seen in SCAN_S s) and SCAN_BUSY (1 if it answered "Busy")
  local out
  out=$(BT_MGMT_WAIT=$((SCAN_S + 2)) BT_MGMT_TIMEOUT=$SCAN_S bt_mgmt "${CAP#hci}" find)
  SCAN_BUSY=0; grep -q "Busy" <<< "$out" && SCAN_BUSY=1
  SCAN_FOUND=$(grep -c "dev_found" <<< "$out")
}

echo "== Scan test ($SCAN_S s on $CAP; the capture helper is paused meanwhile)"
was_active=$(systemctl is-active $UNIT 2>/dev/null)
[[ $was_active == active ]] && systemctl stop $UNIT
# A helper started by hand (not by the unit): remember it, stop it for the test, restart it after.
manual=$(pgrep -af "[k]ismet_cap_linux_bluetooth.*--source ${CAP}:" | head -1 | cut -d' ' -f2-)
restart_manual() {
  [[ -n $manual ]] || return 0
  bt_clear_discovery "$CAP"
  nohup sh -c "exec $manual" >> /tmp/kcap-bt.log 2>&1 < /dev/null &
  note "restarted the hand-started helper: $manual"
}
[[ -n $manual ]] && { pkill -f "[k]ismet_cap_linux_bluetooth.*--source ${CAP}:"; sleep 2; }
# Some adapters (TP-Link UB500) keep "discovering" after the helper that started
# it exits, so we may have caused this by stopping it. rf-bt-capture clears it
# at every start; a helper started without it stays stuck after a reconnect.
busy=$(BT_MGMT_WAIT=4 BT_MGMT_TIMEOUT=3 bt_mgmt "${CAP#hci}" find | grep -c "Busy")
if [[ $busy -gt 0 ]]; then
  note "$CAP still \"discovering\" after its helper stopped (adapter quirk); clearing it for the test"
  [[ -n $manual ]] && bad "helper on $CAP was started by hand, without rf-bt-capture: after a Kismet restart it will deliver nothing"
fi
bt_clear_discovery "$CAP"
scan_test; found=$SCAN_FOUND
note "devices found: $found"
if [[ $SCAN_BUSY -eq 1 ]]; then
  bad "$CAP stays \"Busy\" even after clearing discovery (--fix power-cycles it)"
elif [[ $found -eq 0 ]]; then
  bad "$CAP found nothing in $SCAN_S s (busy areas normally show several devices)"
fi
if [[ $FIX -eq 0 ]]; then
  [[ $was_active == active ]] && systemctl start $UNIT
  restart_manual
fi

if [[ $FIX -eq 1 ]]; then
  echo "== Fixing"
  echo "options btusb enable_autosuspend=n" > /etc/modprobe.d/rf-btusb.conf
  note "wrote /etc/modprobe.d/rf-btusb.conf (no btusb autosuspend after reboot)"
  cat > /etc/udev/rules.d/50-rf-bt-no-autosuspend.rules <<'EOF'
# rf-node: keep USB Bluetooth adapters awake (Realtek dongles stop reporting when suspended)
# Wireless-controller class devices (most Bluetooth dongles):
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{bDeviceClass}=="e0", TEST=="power/control", ATTR{power/control}="on"
# TP-Link UB500 and Realtek RTL8761B-based dongles that report a vendor class:
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="2357", ATTR{idProduct}=="0604", TEST=="power/control", ATTR{power/control}="on"
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0bda", ATTR{idProduct}=="8771", TEST=="power/control", ATTR{power/control}="on"
EOF
  note "wrote /etc/udev/rules.d/50-rf-bt-no-autosuspend.rules"
  [[ -w /sys/module/btusb/parameters/enable_autosuspend ]] && echo N > /sys/module/btusb/parameters/enable_autosuspend
  for d in /sys/class/bluetooth/hci*; do
    u=$(usb_dir "$(basename "$d")"); [[ -n $u && -w $u/power/control ]] && echo on > "$u/power/control"
  done
  bt_unblock "$CAP"
  bt_clear_discovery "$CAP"; note "cleared any stuck discovery on $CAP"
  scan_test
  if [[ $SCAN_FOUND -eq 0 ]]; then
    bt_mgmt "${CAP#hci}" power off >/dev/null; sleep 1
    bt_mgmt "${CAP#hci}" power on >/dev/null
    note "power-cycled $CAP"
  fi
  u=$(usb_dir "$CAP")
  scan_test
  if [[ -n $u && $SCAN_FOUND -eq 0 ]]; then  # still deaf: re-enumerate the USB device
    echo 0 > "$u/authorized"; sleep 2; echo 1 > "$u/authorized"; sleep 4
    note "re-enumerated USB adapter $CAP"
    CAP=$(capture_iface) || CAP=hci0
  fi
  scan_test; found=$SCAN_FOUND
  note "devices found after fix: $found"
  if systemctl cat $UNIT >/dev/null 2>&1; then systemctl restart $UNIT && note "restarted $UNIT"
  else restart_manual; [[ -n $manual ]] || note "no $UNIT unit and no hand-started helper: start capture for $CAP (see node/README.md)"; fi
  [[ $found -gt 0 ]] && problems=0
fi

echo "== Result"
if [[ $problems -eq 0 ]]; then
  echo "  OK: $CAP is scanning. Check the node's :bt source packet count in Kismet (Data Sources)."
else
  echo "  $problems problem(s) found.$([[ $FIX -eq 0 ]] && echo ' Run again with --fix.')"
  [[ $FIX -eq 1 ]] && echo "  Still failing: try the onboard adapter (BT_PREFER=onboard in $ENV_FILE), another USB port, or replace the dongle."
fi
exit $(( problems > 0 ))
