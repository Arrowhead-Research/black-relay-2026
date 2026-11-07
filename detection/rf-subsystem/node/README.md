# RF Node (WBT) Capture Helpers

This directory contains **example** artifacts for running Kismet remote-capture helpers on WBT nodes (Raspberry Pi). The production node agent and provisioning flow are not built yet (see [`../README.md`](../README.md) and [`../DESIGN.md`](../DESIGN.md)). These files are templates: review and adapt them before use.

## Prerequisites

- Raspberry Pi OS / Debian-based node (arm64) with network access to the central Kismet server.
- Kismet capture helpers installed from the Kismet apt repo **matching the node OS release** and **pinned to the same Kismet release as the central server** (currently `2026-10-R1`): `kismet-capture-linux-wifi`, `kismet-capture-linux-bluetooth`, `kismet-capture-rtl433-v2`.
- **`rtl-433`** package installed for TPMS decoding (`kismet-capture-rtl433-v2` needs the `rtl_433` binary).
- Radios attached and visible (verify per node): external Wi‑Fi adapter, Bluetooth adapter, RTL‑SDR.

## Install the example systemd units

These units use legacy TCP remote capture (`--tcp`, port 3501), validated with Kismet 2026-10-R1 (see [`../central/README.md`](../central/README.md), DESIGN.md open question 5).

```sh
sudo mkdir -p /etc/rf-node
sudo cp detection/rf-subsystem/node/systemd/rf-node.env.example /etc/rf-node/rf-node.env
sudoedit /etc/rf-node/rf-node.env   # set CENTRAL_HOST, NODE_NAME, interfaces; verify devices

sudo cp detection/rf-subsystem/node/systemd/kismet-cap-*.service /etc/systemd/system/
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
