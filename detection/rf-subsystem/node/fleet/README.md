# RF node fleet: flash and provision the Raspberry Pis

Two commands take a blank SD card to a WBT node that is logged in to the central MQTT broker and streams capture to the central server:

```sh
cd detection/rf-subsystem/node/fleet
cp fleet.env.example fleet.env            # once: user, SSH key path, timezone, optional Wi-Fi
./fleet.sh flash wbt-4 /dev/disk4         # macOS (diskutil list external) or /dev/sdX on Linux
# put the card in the Pi, power it on, wait ~2 minutes
./fleet.sh provision wbt-4
```

| Command | What it does |
|---|---|
| `fleet.sh flash <node> <disk>` | Downloads Raspberry Pi OS Lite (64-bit), checks its SHA-256, writes it to the card (refuses internal disks and asks you to type the node name), then seeds first boot. |
| `fleet.sh seed <node> <bootfs>` | Seeds a card you already wrote (e.g. with Raspberry Pi Imager, OS customisation off), or re-seeds one: each seed has a new cloud-init instance-id, so first boot runs again even on a card that has booted (new SSH host keys; the node's old key is dropped from `~/.ssh/blackrelay_fleet_known_hosts`). `FLEET_DIAG=1` adds Wi-Fi diagnostics: for 6 minutes after boot the node writes `rf-diag.txt` to its boot partition. |
| `fleet.sh provision [node\|all]` | Runs `ansible/site.yml`: installs and starts the node stack, points it at the central server and delivers the heartbeat token. Safe to re-run. Extra args go to Ansible: `fleet.sh provision all --check --diff`. |
| `fleet.sh ping [node\|all]` | Checks Ansible can log in. |
| `fleet.sh wifi <node\|all> "<SSID>"` | Ansible (`ansible/wifi.yml`): moves running nodes onto a Wi-Fi network (asks for the passphrase, or `WIFI_PASSWORD`). With a setup hub it hands over in order, hub last; see below. |

Nodes are `wbt-1` … `wbt-4` (`ansible/inventory.yml`). The name is the hostname, the Kismet source prefix (`wbt-4:wifi`, `:bt`, `:tpms`) and the analytics `node_id`.

## What goes on the card

`fleet.sh` writes cloud-init NoCloud files to the boot partition (Raspberry Pi OS reads them on first boot):

- **hostname** = the node name;
- **user** `ADMIN_USER` (default `pi`) with **no password** (locked), passwordless sudo and the **fleet SSH public key**; password SSH logins off;
- **SSH** enabled; **Ethernet** DHCP, and **Wi-Fi** if `WIFI_SSID` is set.

The fleet key pair (`SSH_KEY`, default `~/.ssh/blackrelay_fleet`) is created on first use. Only the `.pub` half is ever written to a card; keep the private half on the admin machine. The Wi-Fi passphrase is asked for at flash time (or read from `WIFI_PASSWORD`) and the card stores only the derived WPA key, not the passphrase. That key still joins that network, so treat cards as sensitive until they are in a Pi.

## What provisioning does

`ansible/site.yml` (role `rf_node`) on each node:

1. Installs the Kismet capture helpers from the Kismet apt repo **pinned to the central server's release** (`kismet_version`, same as `central/kismet/Dockerfile`) and holds them there, plus `rtl-433`, `bluez` and tools.
2. Installs the files in `../` exactly as the node README describes: Bluetooth helper scripts, `rf-node-agent`, the `kismet-cap-*` and `rf-node-agent` units, the RTL-SDR driver blacklist.
3. Writes `/etc/rf-node/rf-node.env` (0600): `CENTRAL_HOST`, `CENTRAL_PORT`, `NODE_NAME`, `WIFI_IFACE`, `BT_PREFER`/`BT_ADDR`, `NODE_TOKEN` and the node's broker login `MQTT_HOST`/`MQTT_PORT`/`MQTT_USER`/`MQTT_PASSWORD`. Other keys and hand edits are kept. Pins `reg.bulexa.com` to `172.20.100.150` in `/etc/hosts` so the broker's Let's Encrypt name works on the LAN.
4. Enables and starts capture and the agent.

**Central connection.** Nodes are **not on the tailnet**. Everything goes to the central server (`172.20.100.150`) on the LAN:

- **Central MQTT broker** (the root compose's `broker`, the same one the physical subsystem publishes to): each node logs in as its own `rf-node-<node>` on 8883 with TLS and publishes its health, retained, to `detection/rf/nodes/<node>/health`, plus `.../online` (`1`, or `0` as its last will when it drops off). The broker only lets it write under its own `detection/rf/nodes/<node>/`.
- **Kismet remote capture** on TCP 3501 (raw packets; the RF stack's analytics turn them into `detection/rf/...` messages under its own login) and **console heartbeats** on 8080. The RF stack must publish these on that address (`BIND_ADDR=172.20.100.150` in `rf-subsystem/central/.env`).

The nodes' network must be allowed to reach 8883, 3501 and 8080 there. A node that was joined earlier stays joined until you run `fleet.sh provision <node> -e leave_tailnet=true` (logs out, removes the package).

**Secrets.** Nothing secret is in the repo, the inventory or on the card. **Broker logins:** the playbook runs `detection/central/rf-node-logins.sh <nodes>` on the central server, which keeps one random password per node in the root `.env` (`BROKER_RF_NODE_LOGINS`, 0600) and re-creates the broker when it adds one; it then reads them back over SSH (`no_log`) and writes each node only its own. Re-running keeps existing passwords; to rotate one, delete that `node:password` entry from `BROKER_RF_NODE_LOGINS` and provision the node again. The console's heartbeat token is read over SSH from `CONSOLE_NODE_TOKEN` in the central RF stack's `.env` and written to each node (`no_log`). If the console has none yet, the first `fleet.sh provision all` creates one, writes the nodes first and then the console, and restarts `rf-console`, the same order as `../set-node-token.sh` (which still works to rotate it). The central login and this repo's checkout there are `ansible_user` and `central_repo_dir` in `ansible/group_vars/central.yml`.

## Wi-Fi: setup hub, hand-over, rescue hotspot

For when the site's Wi-Fi isn't known at flash time. With `SETUP_SSID=BlackRelay-Setup` and `SETUP_HUB=wbt-1` in `fleet.env` (you choose its passphrase when flashing):

- **Staging.** `wbt-1` hosts `BlackRelay-Setup` as a hotspot (it is `10.42.0.1`; with Ethernet plugged in it shares that connection, so the others can install packages). Every other card joins it on first boot. Join it from your laptop too, then `./fleet.sh ping` and `./fleet.sh provision all`.
- **On site.** Once the nodes are placed and you know the network:

  ```sh
  ./fleet.sh wifi all "Safehouse WiFi"     # asks for its passphrase
  ```

  Every node saves it (ranked above the setup network), the other nodes switch first, and after 90 s the playbook checks none of them is still answering on the setup network. If any is (wrong passphrase, out of range) or was never reached, it stops and the hub keeps the hotspot up, so you can fix it and run again. Otherwise the hub switches last and turns its hotspot off; if it can't join after 60 s it turns the hotspot back on. Then join the site network yourself and `./fleet.sh ping`.
- **Rescue hotspot.** Every node runs `rf-wifi-fallback` (a 1-minute timer). If it has had no network at all (no default route) for 3 minutes, it opens its own hotspot, `wbt-4-setup` for wbt-4 (the hub reuses `BlackRelay-Setup`), with the setup passphrase. Join it and run `./fleet.sh wifi wbt-4 "<SSID>" -e ansible_host=10.42.0.1` (or SSH to `pi@10.42.0.1` with the fleet key). While nobody is connected, it drops the hotspot every 10 minutes to retry the known networks.

**Hotspot can't be joined?** The hotspots are WPA2-AES with PMF off (the Pi's radio advertises PMF in hotspot mode but doesn't complete it, which stops iPhones and Macs joining). Give a node 3–4 minutes after first power-on. With the card in your machine, `./check-setup-pass.sh [/Volumes/bootfs]` tells you whether a passphrase matches the card's setup keys (it prints neither). For the node's own view, re-seed with `FLEET_DIAG=1`, boot it, try to join, then read `rf-diag.txt` from the card.

Wi-Fi is only the node's link back to central, on the onboard radio (`wlan0`); capture stays on the external adapter. Only the derived WPA keys are stored on cards and nodes. Anyone with a card can join the setup network while it's up, so keep it off when you aren't staging; SSH still only accepts the fleet key and the broker link is TLS. The site network must reach the central server (172.20.100.150) for capture and MQTT.

## Per-node settings

Defaults are in `ansible/group_vars/rf_nodes.yml`. Put anything node-specific in `ansible/host_vars/<node>.yml` (gitignored; see `wbt-1.yml.example`): its address if `<node>.local` does not resolve, `wifi_iface`, `bt_addr` (from `sudo btmgmt info`), or a shorter `capture_units` for a node without an RTL-SDR.

**Existing nodes (wbt-1 … wbt-4).** Their key is not the fleet key yet: add it once (`ssh-copy-id -i ~/.ssh/blackrelay_fleet.pub pi@<node>`), give their addresses and adapter settings in `host_vars`, and review `fleet.sh provision wbt-1 --check --diff` before the real run. Do one node at a time and confirm its sources in Kismet.

## Tests

`bash tests/test_fleet.sh` checks the seed files (valid cloud-init, key and PSK present, no passphrase or private key on the card, refuses bad names and non-boot directories) and, with Ansible installed, the playbook syntax and inventory.
