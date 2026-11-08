# shellcheck shell=bash
# Shared helpers for rf-bt-capture and rf-bt-doctor (sourced, not run).
#
# Bluetooth adapter numbering (hci0, hci1) depends on enumeration order: on one
# node hci0 was the Pi's onboard radio, on others the USB dongle. Choose the
# capture adapter by what it is instead:
#   BT_ADDR=aa:bb:cc:dd:ee:ff   this exact adapter (most specific)
#   BT_PREFER=usb|onboard       the first adapter of that kind
#   BT_IFACE=hci0               fallback: by index (old behaviour)
# RF_SYS overrides /sys (tests only).

# btmgmt reads commands from stdin and quits at end-of-input, before the
# controller has answered, so give it an input that stays open. Process
# substitution (not a pipe) so the call returns as soon as btmgmt is done.
bt_mgmt() {  # bt_mgmt <index> <command...>
  local idx=$1; shift
  timeout "${BT_MGMT_TIMEOUT:-4}" btmgmt --index "$idx" "$@" 2>&1 < <(sleep "${BT_MGMT_WAIT:-5}")
}

# "hciN addr" lines from `btmgmt info` output (stdin); one query covers every adapter.
parse_btmgmt_info() {
  awk '/^hci[0-9]+:[[:space:]]+Primary controller/ {h=$1; sub(":", "", h)}
       /^[[:space:]]+addr / && h != "" {print h, tolower($2); h=""}'
}

BT_ADDR_CACHE=""
bt_addr_cache_fill() {  # call in the current shell (not in $(...)) so the cache survives
  [[ -n $BT_ADDR_CACHE ]] && return 0
  BT_ADDR_CACHE=$(timeout "${BT_MGMT_TIMEOUT:-4}" btmgmt info 2>/dev/null < <(sleep "${BT_MGMT_WAIT:-5}") | parse_btmgmt_info)
  return 0
}

bt_addr() {  # adapter address: sysfs where the kernel provides it, else btmgmt (cached)
  local h=$1 f="${RF_SYS:-/sys}/class/bluetooth/$1/address"
  if [[ -r $f ]]; then tr 'A-Z' 'a-z' < "$f"; return 0; fi
  bt_addr_cache_fill
  awk -v h="$h" '$1 == h {print $2; exit}' <<< "$BT_ADDR_CACHE"
}

bt_unblock() {  # clear the rfkill soft block of this adapter only
  local r
  for r in "${RF_SYS:-/sys}"/class/bluetooth/"$1"/rfkill*; do
    [[ -w $r/soft ]] && echo 0 > "$r/soft"
  done
  return 0
}

# Clear a discovery left running by a capture helper that exited (e.g. when
# Kismet restarted): the adapter then answers "Busy" to every new scan and the
# source runs without delivering packets. Harmless when nothing is running.
bt_clear_discovery() { bt_mgmt "${1#hci}" stop-find >/dev/null; return 0; }

usb_dir() {  # sysfs USB device directory of an hci adapter, or nothing if not USB
  local p
  p=$(readlink -f "${RF_SYS:-/sys}/class/bluetooth/$1/device" 2>/dev/null) || return 0
  while [[ -n $p && $p != / && ! -f $p/idVendor ]]; do p=$(dirname "$p"); done
  [[ -f $p/idVendor ]] && echo "$p"
  return 0
}

capture_iface() {
  local d h u want
  if [[ -n ${BT_ADDR:-} ]]; then
    want=$(echo "$BT_ADDR" | tr 'A-Z' 'a-z')
    compgen -G "${RF_SYS:-/sys}/class/bluetooth/hci*/address" >/dev/null || bt_addr_cache_fill
    for d in ${RF_SYS:-/sys}/class/bluetooth/hci*; do
      [[ -e $d ]] || continue
      [[ $(bt_addr "$(basename "$d")") == "$want" ]] && { basename "$d"; return 0; }
    done
    return 1   # the configured adapter is not present
  fi
  if [[ ${BT_PREFER:-} == usb || ${BT_PREFER:-} == onboard ]]; then
    for d in ${RF_SYS:-/sys}/class/bluetooth/hci*; do
      [[ -e $d ]] || continue
      h=$(basename "$d"); u=$(usb_dir "$h")
      if [[ $BT_PREFER == usb && -n $u ]] || [[ $BT_PREFER == onboard && -z $u ]]; then echo "$h"; return 0; fi
    done
    return 1
  fi
  echo "${BT_IFACE:-hci0}"
}
