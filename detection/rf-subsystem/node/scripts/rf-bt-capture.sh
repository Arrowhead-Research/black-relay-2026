#!/usr/bin/env bash
# rf-bt-capture: start Kismet Bluetooth remote capture on the configured adapter.
# Used by kismet-cap-bt.service. Settings come from /etc/rf-node/rf-node.env:
# CENTRAL_HOST, CENTRAL_PORT, NODE_NAME, BT_ADDR / BT_PREFER / BT_IFACE, BT_SOURCE_OPTS.
set -u
ENV_FILE=/etc/rf-node/rf-node.env
# shellcheck disable=SC1090
[[ -f $ENV_FILE ]] && . "$ENV_FILE"
# shellcheck source=rf-bt-lib.sh
. "$(dirname "$(readlink -f "$0")")/rf-bt-lib.sh"

# USB dongles can take a few seconds to enumerate at boot.
iface=""
for _ in $(seq 1 30); do
  iface=$(capture_iface) && [[ -e /sys/class/bluetooth/$iface ]] && break
  iface=""; sleep 1
done
if [[ -z $iface ]]; then
  echo "rf-bt-capture: Bluetooth adapter not found (BT_ADDR=${BT_ADDR:-} BT_PREFER=${BT_PREFER:-} BT_IFACE=${BT_IFACE:-hci0})" >&2
  exit 1   # systemd restarts the unit
fi

# Keep USB adapters awake and powered, and clear a scan a previous helper left
# running (the cause of "running but no packets" after Kismet restarts).
u=$(usb_dir "$iface")
[[ -n $u && -w $u/power/control ]] && echo on > "$u/power/control"
bt_unblock "$iface"
if command -v btmgmt >/dev/null; then
  bt_mgmt "${iface#hci}" power on >/dev/null
  bt_clear_discovery "$iface"
fi

echo "rf-bt-capture: capturing on $iface ($(bt_addr "$iface"))"
# --disable-retry: on a lost connection the helper exits instead of reconnecting
# by itself, so systemd restarts this wrapper and the adapter is cleared again.
exec /usr/bin/kismet_cap_linux_bluetooth --tcp --connect "${CENTRAL_HOST:-rf-central}:${CENTRAL_PORT:-3501}" \
  --source "${iface}:name=${NODE_NAME:-rf-node-01}:bt${BT_SOURCE_OPTS:-}" --disable-retry
