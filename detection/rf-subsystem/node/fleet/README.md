# Node fleet: flash and provision the Raspberry Pis (RF and camera)

Two commands take a blank SD card to a WBT node that is logged in to the central MQTT broker and streams capture to the central server. Camera kits (`cam-N`) use the same commands and the same setup network; see [Camera nodes](#camera-nodes).

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
| `fleet.sh provision [target]` | Runs `ansible/site.yml`: installs and starts the node stack, points it at the central server and delivers the heartbeat token. Safe to re-run. Extra args go to Ansible: `fleet.sh provision all --check --diff`. |
| `fleet.sh ping [target]` | Checks Ansible can log in. |
| `fleet.sh wifi <target> "<SSID>"` | Ansible (`ansible/wifi.yml`): moves running nodes onto a Wi-Fi network (asks for the passphrase, or `WIFI_PASSWORD`). With a setup hub it hands over in order, hub last; see below. |

A target is a node, `rf`, `camera` or `all` (every node, RF and camera). Nodes are `wbt-1` … `wbt-4` and `cam-1` … `cam-4` (`ansible/inventory.yml`; `fleet_nodes` holds both groups). The name is the hostname, the Kismet source prefix (`wbt-4:wifi`, `:bt`, `:tpms`) and the analytics `node_id`.

## What goes on the card

`fleet.sh` writes cloud-init NoCloud files to the boot partition (Raspberry Pi OS reads them on first boot):

- **hostname** = the node name;
- **user** `ADMIN_USER` (default `pi`) with **no password** (locked), passwordless sudo and the **fleet SSH public key**; password SSH logins off;
- **SSH** enabled; **Ethernet** DHCP, and **Wi-Fi** if `WIFI_SSID` is set.

The fleet key pair (`SSH_KEY`, default `~/.ssh/blackrelay_fleet`) is created on first use. Only the `.pub` half is ever written to a card; keep the private half on the admin machine. The Wi-Fi passphrase is asked for at flash time (or read from `WIFI_PASSWORD`) and the card stores only the derived WPA key, not the passphrase. That key still joins that network, so treat cards as sensitive until they are in a Pi.

## What provisioning does

`ansible/site.yml` (role `rf_node`) on each RF node:

1. Installs the Kismet capture helpers from the Kismet apt repo **pinned to the central server's release** (`kismet_version`, same as `central/kismet/Dockerfile`) and holds them there, plus `rtl-433`, `bluez` and tools.
2. Installs the files in `../` exactly as the node README describes: Bluetooth helper scripts, `rf-node-agent`, the `kismet-cap-*` and `rf-node-agent` units, the RTL-SDR driver blacklist.
3. Writes `/etc/rf-node/rf-node.env` (0600): `CENTRAL_HOST`, `CENTRAL_PORT`, `NODE_NAME`, `WIFI_IFACE`, `BT_PREFER`/`BT_ADDR`, `NODE_TOKEN` and the node's broker login `MQTT_HOST`/`MQTT_PORT`/`MQTT_USER`/`MQTT_PASSWORD`. Other keys and hand edits are kept. Pins `reg.bulexa.com` to `172.20.100.150` in `/etc/hosts` so the broker's Let's Encrypt name works on the LAN.
4. Enables and starts capture and the agent.

**Central connection.** Nodes are **not on the tailnet**. Everything goes to the central server (`172.20.100.150`) on the LAN:

- **Central MQTT broker** (the root compose's `broker`, the same one the physical subsystem publishes to): each node logs in as its own `rf-node-<node>` on 8883 with TLS (the `reg.bulexa.com` Let's Encrypt certificate; `broker_tls` and `broker_port` in `ansible/group_vars/fleet_nodes.yml` cover the plain 1883 listener, `broker_ca_file` in `rf_nodes.yml` a private CA) and publishes its health, retained, to `detection/rf/nodes/<node>/health`, plus `.../online` (`1`, or `0` as its last will when it drops off). The broker only lets it write under its own `detection/rf/nodes/<node>/`.
- **Kismet remote capture** on TCP 3501 (raw packets; the RF stack's analytics turn them into `detection/rf/...` messages under its own login) and **console heartbeats** on 8080. The RF stack must publish these on that address (`BIND_ADDR=172.20.100.150` in `rf-subsystem/central/.env`).

The nodes' network must be allowed to reach 8883, 3501 and 8080 there. A node that was joined earlier stays joined until you run `fleet.sh provision <node> -e leave_tailnet=true` (logs out, removes the package).

**Secrets.** Nothing secret is in the repo, the inventory or on the card. **Broker logins:** the playbook runs `detection/central/rf-node-logins.sh <nodes>` on the central server, which keeps one random password per node in the root `.env` (`BROKER_RF_NODE_LOGINS`, 0600) and re-creates the broker when it adds one; it then reads them back over SSH (`no_log`) and writes each node only its own. Re-running keeps existing passwords; to rotate one, delete that `node:password` entry from `BROKER_RF_NODE_LOGINS` and provision the node again. The console's heartbeat token is read over SSH from `CONSOLE_NODE_TOKEN` in the central RF stack's `.env` and written to each node (`no_log`). If the console has none yet, the first `fleet.sh provision all` creates one, writes the nodes first and then the console, and restarts `rf-console`, the same order as `../set-node-token.sh` (which still works to rotate it). The central login and this repo's checkout there are `ansible_user` and `central_repo_dir` in `ansible/group_vars/central.yml`.

**Without SSH to the central server.** Run this by hand on the central server, from the repo root:

```sh
detection/central/node-secrets.sh rf wbt-1 wbt-2 wbt-3 wbt-4 > node-secrets
detection/central/node-secrets.sh camera cam-1 cam-2 >> node-secrets    # if camera nodes are in the run
```

It creates the broker logins and, for `rf`, the heartbeat token if the console has none (restarting `broker` / `rf-console` as needed), and prints one line per node plus a `token` line (`rf`) or the shared kit login's `relay_user` and `relay_password` lines (`camera`). `rf-node-secrets.sh <nodes>` is the `rf` form under its old name. Bring that file to the admin machine (it is secret: keep it 0600, delete it afterwards; `node-secrets*` and `rf-node-secrets*` are gitignored here) and provision with it:

```sh
CENTRAL_SECRETS=node-secrets ./fleet.sh provision wbt-2
```

(or set `CENTRAL_SECRETS=` in `fleet.env`). The central host is then left out of the run entirely. Each node in the run needs its line in the file, and RF nodes need the `token` line, camera nodes the `relay_password` line; the play stops with the command to run otherwise.

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

Shared connection and broker settings are in `ansible/group_vars/fleet_nodes.yml`; RF defaults in `rf_nodes.yml`, camera defaults in `camera_nodes.yml`. Put anything node-specific in `ansible/host_vars/<node>.yml` (gitignored; see `wbt-1.yml.example`): its address if `<node>.local` does not resolve, `wifi_iface`, `bt_addr` (from `sudo btmgmt info`), or a shorter `capture_units` for a node without an RTL-SDR.

**Existing nodes (wbt-1 … wbt-4).** Their key is not the fleet key yet: add it once (`ssh-copy-id -i ~/.ssh/blackrelay_fleet.pub pi@<node>`), give their addresses and adapter settings in `host_vars`, and review `fleet.sh provision wbt-1 --check --diff` before the real run. Do one node at a time and confirm its sources in Kismet.

## Tests

`bash tests/test_fleet.sh` checks the seed files (valid cloud-init, key and PSK present, no passphrase or private key on the card, refuses bad names and non-boot directories) and, with Ansible installed, the playbook syntax and inventory, including the camera-node cases (a `cam-` card needs Wi-Fi and gets no `eth0`).

## Camera nodes

Camera kits are a Raspberry Pi 5 with a Hailo-8 and one Reolink RLC-520A on `eth0` (code in `../../camera-subsystem`). They are flashed, bootstrapped and provisioned like the RF nodes: same `fleet.sh`, same inventory (`camera_nodes`: `cam-1` … `cam-4`), same `BlackRelay-Setup` network (hosted by the hub, `SETUP_HUB`), same `<node>-setup` rescue hotspot and watchdog.

```sh
./fleet.sh flash cam-2 /dev/disk4         # needs SETUP_SSID or WIFI_SSID in fleet.env
# card in the Pi, power on, wait ~2 minutes; join the setup network from this machine
CAMERA_HEF=~/models/yolov6n.hef ./fleet.sh provision cam-2    # or CAMERA_HEF in fleet.env
# once it is provisioned, plug in the factory-reset camera
./fleet.sh provision camera              # all camera nodes; `all` is RF and camera
```

Put each node's covered zones in `ansible/host_vars/<node>.yml` first (`camera_zones`, required; see `cam-1.yml.example`), and `camera_upside_down: true` if the camera is mounted inverted.

**What provisioning does** (role `camera_node`, safe to re-run):

1. Installs NetworkManager, dnsmasq-base, nftables, avahi, Docker and Compose and the other host packages; stops and disables a host `mosquitto` (the broker runs in a container).
2. Makes the `camerabridge` user and the `/etc/camerabridge`, `/var/lib/...` and `/etc/black-relay` directories.
3. Installs the Hailo driver 4.21.0 with Frigate's own `user_installation.sh` (pinned tag, checksum checked) if it is not there already. It fails if `hailo-all` is installed.
4. Copies `yolov6n.hef` into Frigate's model cache, checking its SHA-256 (`camera_hef_sha256`).
5. Installs Frigate's host units (`frigate.service`, the camera-link DHCP service, the cache mount), `daemon.json`, the Hailo module option and the camera-link NetworkManager profile for `eth0`; removes `Wired connection 1` and any netplan file for `eth0`. Nothing brings `eth0` up or down live.
6. Installs the firewall (`/etc/nftables.conf`, checked with `nft -c` first): the same `black_relay` table as the camera image, minus the phone portal. Only the central server reaches SSH, Frigate and RTSP; the setup network and the machine running Ansible reach SSH only (`fleet_admin_nets`). The setup network is not blocked wholesale, because a fleet node is a client of it, and a rescue hotspot gets DHCP and DNS. Changes apply with a reload, so open sessions survive.
7. Key-only SSH, persistent journal, time from the central server (`camera_ntp_server`), `reg.bulexa.com` pinned to the central address in `/etc/hosts`.
8. Writes `/etc/camerabridge/bridge.yaml` (0600) and `/var/lib/camera-provision/node.json`, copies `camera-subsystem` to `/opt/black-relay/src` and runs `docker compose up -d --build` there (MQTT broker, bridge and camera-link containers). `camera_node_image` pulls a prebuilt image instead of building.
9. Reboots, only when the Hailo driver, the first firewall, the `eth0` profile, `daemon.json` or the module option changed, then checks the stack is up.
10. Nothing to do on the relay: `detection/camera-relay-service`, which the root compose runs on the central server, adds each node's video path from its heartbeats, once the node's Wi-Fi address is inside `RELAY_SITE_SUBNET` (root `.env`; default the `/24` of `BIND_ADDRESS`). A node still on the setup network gets no path.

**What is different from RF nodes**

- `eth0` is the camera link (10.77.0.1/24, one DHCP lease for the camera), never the node's uplink. Cards for camera nodes get no `eth0` in cloud-init, so they need Wi-Fi at flash time, and Ansible must reach the node over Wi-Fi. If it reached it over `eth0` earlier, it will not come back after the reboot.
- There is no phone setup portal: Ansible writes what the portal would. The camera-subsystem `provision` package and hand-built images are unchanged and still work for existing kits.
- The model file is needed the first time (`CAMERA_HEF`). The Hailo driver is built on the node, so the first provision needs internet through the setup network and takes a while, and ends with a reboot.
- Plug the factory-reset camera in after provisioning; camera-link adopts it.

**Broker logins.** `setup-central.sh` (`detection/central/make-env.sh`) already makes `cam-node-<node>` for every camera node in the inventory, and the kit login; the playbook still makes any that are missing (`detection/central/node-logins.sh camera`, password in `BROKER_CAMERA_NODE_LOGINS` in the root `.env`, read back over SSH and written only to that node's `bridge.yaml`; without SSH, from `node-secrets.sh camera`'s output, see *Without SSH to the central server* under [What provisioning does](#what-provisioning-does)). It may publish under `detection/camera/` and read RF cues. The node also gets the shared kit login (`BROKER_CAMERA_USER` / `BROKER_CAMERA_PASSWORD`, read from the root `.env`) as the `relay` section of `bridge.yaml`: that is the secret MediaMTX pulls RTSP with and fusion derives the Frigate proxy secret from, so the node must use it for go2rtc and the proxy even though it is not its broker login. Rotating a node's login: delete its entry from `BROKER_CAMERA_NODE_LOGINS` and provision it again. Provisioning camera nodes never touches the console heartbeat token. The bridge checks the broker's TLS certificate against the system CAs only, so camera nodes cannot use `broker_ca_file`; with a private CA, give `camera_nodes` `broker_tls: false` and `broker_port: 1883`.

**Open caveats**

- Not yet run on real hardware: the Ansible role is checked for syntax and its templates and firewall for validity, not against a node. `docker-compose` as Debian's Compose v2 package is unverified; so is cloud-init accepting a network config with no `eth0` and no Wi-Fi (a `cam-` card seeded with only `SETUP_SSID`).
- The Hailo installer checksum was taken from the `v0.18.2` tag on GitHub; update `hailo_install_sha256` with the tag.
- The cache mount has no `[Install]` section, so the playbook starts it and Frigate's unit pulls it in at boot.
- The controller's address is added to the SSH allow list each run, so SSH from another machine later needs `fleet_admin_nets` or a re-run from that machine.
- A node reached over a hotspot at `10.42.0.1` and Wi-Fi hand-over (`fleet.sh wifi`) work as for RF nodes but have not been exercised on camera nodes.
