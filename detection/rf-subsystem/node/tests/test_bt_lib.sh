#!/usr/bin/env bash
# Tests for rf-bt-lib.sh adapter selection, on a fake sysfs tree:
# hci0 = USB dongle, hci1 = onboard (the order seen on some nodes).
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=$(mktemp -d); trap 'rm -rf "$root"' EXIT
mkdir -p "$root/devices/usb1/1-1/1-1:1.0/bluetooth/hci0" "$root/devices/platform/soc/serial/bluetooth/hci1" "$root/class/bluetooth"
echo 2357 > "$root/devices/usb1/1-1/idVendor"; echo 0604 > "$root/devices/usb1/1-1/idProduct"
echo 18:69:45:00:00:01 > "$root/devices/usb1/1-1/1-1:1.0/bluetooth/hci0/address"
echo 2c:cf:67:00:00:02 > "$root/devices/platform/soc/serial/bluetooth/hci1/address"
ln -s "$root/devices/usb1/1-1/1-1:1.0/bluetooth/hci0" "$root/class/bluetooth/hci0"
ln -s "$root/devices/platform/soc/serial/bluetooth/hci1" "$root/class/bluetooth/hci1"
ln -s "$root/devices/usb1/1-1/1-1:1.0" "$root/devices/usb1/1-1/1-1:1.0/bluetooth/hci0/device"
ln -s "$root/devices/platform/soc/serial" "$root/devices/platform/soc/serial/bluetooth/hci1/device"
export RF_SYS=$root
# shellcheck source=../scripts/rf-bt-lib.sh
. "$here/../scripts/rf-bt-lib.sh"
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "ok   $1"; else echo "FAIL $1: got '$2', want '$3'"; fail=1; fi; }
check "usb adapter detected"       "$(basename "$(usb_dir hci0)")" "1-1"
check "onboard adapter not usb"    "$(usb_dir hci1)" ""
check "default is hci0"            "$(BT_IFACE='' capture_iface)" "hci0"
check "prefer usb"                 "$(BT_PREFER=usb capture_iface)" "hci0"
check "prefer onboard"             "$(BT_PREFER=onboard capture_iface)" "hci1"
check "by address (case-insens.)"  "$(BT_ADDR=2C:CF:67:00:00:02 capture_iface)" "hci1"
check "address wins over prefer"   "$(BT_ADDR=18:69:45:00:00:01 BT_PREFER=onboard capture_iface)" "hci0"
BT_ADDR=aa:aa:aa:aa:aa:aa capture_iface >/dev/null; check "missing address fails" "$?" "1"
# btmgmt info parsing, from a real node's output (adapters listed out of order)
sample=$'hci1:\tPrimary controller\n\taddr 00:C0:CA:00:00:03 version 11 manufacturer 70 class 0x000000\n\tname raspberrypi #2\nhci2:\tPrimary controller\n\taddr 2C:CF:67:00:00:04 version 9 manufacturer 305 class 0x6c0000\nhci0:\tPrimary controller\n\taddr 18:69:45:00:00:01 version 10 manufacturer 93 class 0x6c0000\nhci1:\tConfiguration options\n\tsupported options: public-address'
check "btmgmt info parsed" "$(parse_btmgmt_info <<< "$sample" | tr '\n' ' ')" "hci1 00:c0:ca:00:00:03 hci2 2c:cf:67:00:00:04 hci0 18:69:45:00:00:01 "
exit $fail
