# RF Node (WBT) Capture Helpers

This directory contains **example** artifacts for running Kismet remote-capture helpers on WBT nodes (Raspberry Pi). The production node agent and provisioning flow are not built yet (see [`../README.md`](../README.md) and [`../DESIGN.md`](../DESIGN.md)). These files are templates: review and adapt them before use.

**To flash and set up nodes, use [`fleet/`](fleet/README.md)**: `fleet.sh flash <node> <disk>` writes a card with the hostname, the fleet SSH key and Wi-Fi, and `fleet.sh provision` installs everything below with Ansible. The manual steps below are what it automates.

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

## Network: LAN, not the tailnet

Nodes are **not** on the Headscale tailnet. They connect straight to the central server on the LAN (`CENTRAL_HOST`, default `172.20.100.150`): Kismet remote capture on TCP 3501, console heartbeats on 8080 and the central MQTT broker on 8883 (TLS, as `reg.bulexa.com`), so the RF stack's `BIND_ADDR` must be that address and the nodes' network must reach it. wbt-1 … wbt-4 were joined to the tailnet on 2026-11-08 (`node-wbt-1` …); `fleet/fleet.sh provision <node> -e leave_tailnet=true` takes one off it and points it at the LAN address.

## Node agent (rf-node-agent)

`agent/rf-node-agent` (Python standard library) runs as `rf-node-agent.service` and POSTs the node's health to the WBT Console every 30 s (`http://CENTRAL_HOST:8080/api/nodes/heartbeat`): temperature, throttling and undervoltage (`vcgencmd get_throttled`), uptime, load, memory, disk, the capture units (`kismet-cap-*`, and `tailscaled` where installed: state and restart count), whether the Wi-Fi, Bluetooth (`bluetoothctl list`: sysfs has no address on these kernels) and RTL-SDR adapters are present, and the node's tailnet address if it has one. The console adds it to `rf.node-status.v0` and raises `node_health` alerts for new problems. With `MQTT_HOST` set the agent also publishes the same heartbeat to the central MQTT broker (the root compose's `broker`, the one the physical subsystem uses), logged in as the node's own `rf-node-<node>` over TLS: retained on `detection/rf/nodes/<node>/health`, with `detection/rf/nodes/<node>/online` `1`/`0` (last will). It needs `python3-paho-mqtt`; the fleet playbook installs it and the login. An admin can queue a restart of a capture unit (`POST /api/nodes/<node>/restart`); the agent runs it on its next heartbeat (only `kismet-cap-wifi`, `-bt`, `-tpms`).

**Watchdog.** A capture helper can stay connected yet deliver nothing (a Bluetooth adapter that stopped scanning, a link that accepts nothing), which systemd cannot see. Every 30 s the agent reads, for the Wi-Fi and Bluetooth helpers, how much capture data Kismet has acknowledged on the helper's connection to central (`ss -ti`: `bytes_acked` minus `bytes_received`, so the helper's keepalives cancel out). If that grows by less than 2 KB in 10 minutes, or the helper has had no connection for 10 minutes, the agent restarts the unit itself, at most once per 10 minutes per unit, logs it (`journalctl -u rf-node-agent`) and reports it; the console lists *kismet-cap-… restarted by watchdog* in the node's problems for 30 minutes. TPMS is not watched: tire sensors can be silent for hours. A restart cannot fix a broken network link; it gets capture going again once the link is back.

```sh
sudo install -m 755 detection/rf-subsystem/node/agent/rf-node-agent /usr/local/sbin/
sudo install -m 644 detection/rf-subsystem/node/systemd/rf-node-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rf-node-agent
```

It reads `/etc/rf-node/rf-node.env` (`CENTRAL_HOST`, `NODE_NAME`, `WIFI_IFACE`, `BT_ADDR`; optional `CONSOLE_PORT`, `AGENT_INTERVAL_S`, `NODE_TOKEN`, `MQTT_HOST`, `MQTT_PORT` (8883), `MQTT_USER`, `MQTT_PASSWORD`, `MQTT_TLS` (1), `MQTT_PREFIX`). Until a token is set the console accepts heartbeats without one (its log says so at start). To turn authentication on (or rotate the token), run from a machine with SSH to the nodes and the central host:

```sh
detection/rf-subsystem/node/set-node-token.sh -k ~/.ssh/<node key> -c dev@<central host> <node> [<node> ...]
```

## Install the example systemd units

These units use legacy TCP remote capture (`--tcp`, port 3501), validated with Kismet 2026-10-R1 (see [`../central/README.md`](../central/README.md), DESIGN.md open question 5).

```sh
sudo mkdir -p /etc/rf-node
sudo cp detection/rf-subsystem/node/systemd/rf-node.env.example /etc/rf-node/rf-node.env
sudoedit /etc/rf-node/rf-node.env   # set CENTRAL_HOST, NODE_NAME, interfaces; verify devices

# keep the kernel's TV-tuner drivers off the RTL-SDR (rtl_433 needs the device)
sudo install -m 644 detection/rf-subsystem/node/modprobe/rf-rtlsdr-blacklist.conf /etc/modprobe.d/
sudo modprobe -r rtl2832_sdr dvb_usb_rtl28xxu rtl2832 dvb_usb_v2 2>/dev/null || true   # unbound: safe while rtl_433 runs

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
- **No secrets here.** Do not commit `/etc/rf-node/rf-node.env` values (node token, broker password) or other credentials.
- **TPMS conflicts:** stop any standalone `rtl_433` process/service that claims the RTL‑SDR before running the Kismet TPMS helper. Without `modprobe/rf-rtlsdr-blacklist.conf`, the kernel's DVB-T driver (`dvb_usb_rtl28xxu`) binds the dongle at boot and `dmesg` shows `RTL2832U ... deinitialized and disconnected` when `rtl_433` takes it over. That message is the driver being detached, not a power or USB fault.
- **WebSocket remote capture** (port 2501, API key/user auth) is not validated by these templates; adjust `ExecStart` only after confirming the flags for your Kismet release.
