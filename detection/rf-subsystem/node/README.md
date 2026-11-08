# RF Node (WBT) Capture Helpers

This directory contains **example** artifacts for running Kismet remote-capture helpers on WBT nodes (Raspberry Pi). The production node agent and provisioning flow are not built yet (see [`../README.md`](../README.md) and [`../DESIGN.md`](../DESIGN.md)). These files are templates: review and adapt them before use.

## Prerequisites

- Raspberry Pi OS / Debian-based node (arm64) with network access to the central Kismet server.
- Kismet capture helpers installed from the Kismet apt repo **matching the node OS release** and **pinned to the same Kismet release as the central server** (currently `2026-10-R1`): `kismet-capture-linux-wifi`, `kismet-capture-linux-bluetooth`, `kismet-capture-rtl433-v2`.
- **`rtl-433`** package installed for TPMS decoding (`kismet-capture-rtl433-v2` needs the `rtl_433` binary).
- Radios attached and visible (verify per node): external Wi‑Fi adapter, Bluetooth adapter, RTL‑SDR.

## Bluetooth helper scripts

**Why they exist.** On TP-Link UB500 dongles (Realtek RTL8761BU), a scan started by `kismet_cap_linux_bluetooth` keeps running after the helper exits, for example when Kismet restarts and the helper reconnects. Every later scan then gets "Busy", and Kismet shows the source as running, with no error, while it delivers nothing. The Pi's onboard Bluetooth does not get stuck this way. The fix is to clear the adapter's discovery (`btmgmt stop-find`) before every start, and to let systemd (not the helper's own retry loop) handle reconnects, so every reconnect goes through that step.

Also note: `hci` numbers follow enumeration order and differ between nodes (one node's `hci0` was the onboard radio, the others' the UB500), and a node may have three adapters (onboard, UB500, and the Bluetooth half of the Alfa's MT7921 combo chip). These kernels do not expose adapter addresses in sysfs, so addresses are read with `btmgmt info`.

`scripts/` holds three files to install with the Bluetooth unit:

| File | Purpose |
|---|---|
| `rf-bt-capture.sh` | Started by `kismet-cap-bt.service`. Chooses the adapter by `BT_ADDR` or `BT_PREFER` (not by `hci` number), waits for it at boot, keeps it awake, powered and unblocked, **clears any stuck discovery**, then runs `kismet_cap_linux_bluetooth --disable-retry`: on a lost connection the helper exits and systemd restarts the wrapper (5 s), which clears the adapter again. Start to capture takes under 0.1 s. |
| `rf-bt-doctor.sh` | Diagnoses a Bluetooth source that Kismet shows as running but that delivers no packets: adapters (USB or onboard, address, autosuspend), kernel errors on the capture adapter, its rfkill state, the helper, whether the adapter stays "discovering" after its helper stops, and a 10 s scan test. It restarts whatever capture it paused. `--fix` also clears discovery, turns off USB autosuspend (now and after reboot), power-cycles or re-enumerates the adapter, and restarts the capture unit. |
| `rf-bt-lib.sh` | Shared adapter selection (sourced by both). |

```sh
sudo install -m 0755 detection/rf-subsystem/node/scripts/rf-bt-capture.sh /usr/local/sbin/rf-bt-capture
sudo install -m 0755 detection/rf-subsystem/node/scripts/rf-bt-doctor.sh /usr/local/sbin/rf-bt-doctor
sudo install -m 0644 detection/rf-subsystem/node/scripts/rf-bt-lib.sh /usr/local/sbin/rf-bt-lib.sh
sudo rf-bt-doctor          # report
sudo rf-bt-doctor --fix    # repair, then re-test
```

Pin each node's capture adapter in `/etc/rf-node/rf-node.env` with `BT_ADDR` (list addresses with `sudo btmgmt info`), or use `BT_PREFER=usb`/`onboard`.

Tests for the adapter selection and `btmgmt info` parsing run anywhere with bash: `bash detection/rf-subsystem/node/tests/test_bt_lib.sh`. Verified on four nodes: after a Kismet restart, all Bluetooth sources delivered packets again within 10 s; before the fix the UB500 sources stayed at zero.

## Install the example systemd units

These units use legacy TCP remote capture (`--tcp`, port 3501), validated with Kismet 2026-10-R1 (see [`../central/README.md`](../central/README.md), DESIGN.md open question 5).

```sh
sudo mkdir -p /etc/rf-node
sudo cp detection/rf-subsystem/node/systemd/rf-node.env.example /etc/rf-node/rf-node.env
sudoedit /etc/rf-node/rf-node.env   # set CENTRAL_HOST, NODE_NAME, interfaces; verify devices

sudo cp detection/rf-subsystem/node/systemd/kismet-cap-*.service /etc/systemd/system/
# kismet-cap-bt.service runs /usr/local/sbin/rf-bt-capture: install the Bluetooth helper scripts first (above)
sudo systemctl daemon-reload
sudo systemctl enable --now kismet-cap-wifi.service kismet-cap-bt.service kismet-cap-tpms.service
```

## Verify

```sh
systemctl status kismet-cap-wifi.service kismet-cap-bt.service kismet-cap-tpms.service --no-pager
journalctl -u 'kismet-cap-*' -f
```

On the central server, confirm data sources appear (named `<NODE_NAME>:wifi`, `:bt`, `:tpms`) in the Kismet UI or API.

## Safety / cautions

- **Do not capture on the backhaul/SSH interface.** If the node uses onboard Wi‑Fi for connectivity, keep it off monitor mode and capture only on the external Wi‑Fi adapter.
- **Verify interfaces per node** (`kismet_cap_linux_wifi --list`, `kismet_cap_linux_bluetooth --list`, `kismet_cap_sdr_rtl433 --list`). USB enumeration and `hci*` ordering can vary.
- **No secrets here.** Do not commit `/etc/rf-node/rf-node.env` values, credentials, or Tailscale keys.
- **TPMS conflicts:** stop any standalone `rtl_433` process/service that claims the RTL‑SDR before running the Kismet TPMS helper.
- **WebSocket remote capture** (port 2501, API key/user auth) is not validated by these templates; adjust `ExecStart` only after confirming the flags for your Kismet release.
