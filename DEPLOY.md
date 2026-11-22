# Deploying and updating the central server

How to deploy, update or rebuild the BlackRelay central server and point the Raspberry Pi nodes at
it: the RF nodes (wbt-1 to wbt-4) and the camera kits (cam-1 to cam-4).

The central server is 172.22.41.10, an AMD (x86_64) machine running Ubuntu 22.04, with this repo
checked out at `/srv/sensor-fusion`. It replaced the old Mac central at 172.20.100.150, which is
retired. To update the running server go straight to step 9; steps 1 to 8 are the full deploy, for a
rebuild or a second server. Replace these placeholders throughout:

| Placeholder | Meaning |
|---|---|
| `<SERVER_IP>` | the server's LAN address (172.22.41.10) |
| `<USER>` | your login on the server |
| `<REPO>` | the checkout on the server (`/srv/sensor-fusion`) |
| `<EMAIL>` | the Let's Encrypt account email |

Secrets (the certificate folder, `node-secrets`, console passwords) only travel over `scp` or AirDrop.
Never paste them in chat or commit them.

## 0. Before you start: which branch

Deploy `detection/develop`. It holds the RF node fleet (PR #14), the camera kits and the camera relay
in the central project (PR #15), the physical bridge on the same host (PRs #16 and #17, site id
`sh1`) and the camera node fixes (PR #18).

## 1. Server prep (on the server)

1. Install Docker Engine with the Compose plugin (Ubuntu or Debian):
   ```
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker <USER>    # then log out and in again
   docker compose version            # must print v2.x
   ```
2. Give the server a fixed address (`<SERVER_IP>`): a static lease or a static config. The nodes get
   this address written into their configuration, so it must not change.
3. Clone and check out:
   ```
   git clone git@github.com:Arrowhead-Research/black-relay-2026.git <REPO>
   cd <REPO> && git checkout detection/develop
   ```
4. Optional, saves build time: the images are private on ghcr.io, already built for amd64.
   `docker login ghcr.io -u <github user>` with a token that has `read:packages`. Without it the setup
   builds the images locally, which works and takes longer.

## 2. The certificate

The reg.bulexa.com certificate, the Let's Encrypt account and the Cloudflare token live in a
git-ignored folder, `<REPO>/detection/central-store/state`. A server that already has the folder
keeps its certificate. To rebuild on another machine, copy the folder from a backup or the old
server so nothing is issued again (over `scp`, from the machine that has it):

```
scp -r detection/central-store/state <USER>@<SERVER_IP>:<REPO>/detection/central-store/
```

Then on the server: `chmod -R go-rwx <REPO>/detection/central-store/state`.

Do not copy another machine's `.env` files. A new server generates its own logins.

If the folder is lost: create a Cloudflare API token (Zone:Read and DNS:Edit on bulexa.com only) and
the setup asks for it once.

## 3. DNS

In Cloudflare, the `reg.bulexa.com` A record points at `<SERVER_IP>` (172.22.41.10), DNS only (grey
cloud). Change it if the server's address changes. The Pis pin the name locally and do not depend on
it. Every other client does.

## 4. Start the central stack (on the server)

From `<REPO>`:

```
./setup-central.sh <SERVER_IP> --domain reg.bulexa.com --email <EMAIL>
```

This creates the logins, reuses the certificate, adds the daily renewal to cron, and starts the
registry and secrets store, the broker (1883 and 8883 with TLS), fusion, the RF services (Kismet, WBT
Console, rf-analytics) and the camera relay (API, MediaMTX video, NTP). It is safe to run again. After the first run,
`docker compose up -d` from `<REPO>` is enough; to update, see step 9.

### Physical bridge, if the IQ panel is cabled to this server

Full notes: `detection/physical-subsystem/README.md`.

1. The server needs an address on the panel link, for example `192.168.2.1/24` on the interface cabled
   to the panel. The panel does not answer ping; check with `nc -z <PANEL_IP> 8883`.
2. Put the panel pairing in place, owned by the bridge's user. From the pairing backup
   (`pki-eth` is the Ethernet pairing):
   ```
   cd <REPO>/detection/physical-subsystem/deploy
   sudo mkdir -p data/pki
   sudo cp -Rp <backup>/pki-eth/. data/pki/
   sudo chown -R 10001:10000 data/pki && sudo chmod -R go-rwx data/pki
   ```
   Moving a pairing to another machine has not been tried on the real panel. If the panel refuses it,
   pair again (`detection/physical-subsystem/README.md`, "Pairing").
3. From `<REPO>`, the same setup command with the bridge added. It asks for sudo once:
   ```
   ./setup-central.sh <SERVER_IP> --domain reg.bulexa.com --email <EMAIL> --with-bridge --panel-ip <PANEL_IP>
   ```
   It makes the broker answer as reg.bulexa.com inside Docker, points the bridge at it on 8883 with TLS,
   sets the bridge's site id to fusion's (`sh1`), writes the bridge's broker password and starts the
   bridge. No `CENTRAL_BROKER_NAME`/`CENTRAL_BROKER_IP` settings and no compose override are needed.
   Re-run this command whenever something drifts.
4. Start or stop the bridge with `--profile fusion`, or its sender to fusion does not run. The panel
   tools run from the bridge's folder, not the repo root:
   ```
   cd <REPO>/detection/physical-subsystem/deploy
   docker compose --profile fusion ps
   docker compose run --rm iq-tools watch --minutes 10 --config /config/config.yaml
   ```

*(camera)* Check `RELAY_SITE_SUBNET` in `<REPO>/.env`. It defaults to the /24 of the server's address.
The relay only registers kits whose heartbeats come from inside it.

## 5. Firewall (on the server)

Open these to the node network. With ufw: `sudo ufw allow <port>/<proto>`.

| Port | For |
|---|---|
| 22/tcp | SSH for you |
| 8883/tcp | broker, TLS (all nodes, the bridge) |
| 1883/tcp | broker, plain login listener |
| 3501/tcp | Kismet remote capture from the RF nodes |
| 8080/tcp | WBT Console, and the RF nodes' heartbeat |
| 8090/tcp | fusion API |
| 5000-5003/tcp | registry and secrets store, only if you use them |
| 8091/tcp | camera relay API *(camera)* |
| 8554/tcp, 8889/tcp, 8189/udp | camera video: RTSP (`rtsp://viewer:<password>@<SERVER_IP>:8554/<node_id>`) and WebRTC (`http://<SERVER_IP>:8889/<node_id>`, media on UDP 8189) *(camera)* |
| 123/udp | NTP for the camera kits *(camera)* |

Kismet's own web UI (2501) stays on loopback. Reach it with an SSH tunnel.

## 6. Node logins (on the server)

From `<REPO>`:

```
detection/central/node-secrets.sh rf wbt-1 wbt-2 wbt-3 wbt-4 > node-secrets
detection/central/node-secrets.sh camera cam-1 cam-2 cam-3 cam-4 >> node-secrets
chmod 600 node-secrets
```

Copy it to your admin machine (the one that runs `fleet.sh`), then delete it on the server:

```
scp <USER>@<SERVER_IP>:<REPO>/node-secrets ~/node-secrets     # run on the admin machine
rm <REPO>/node-secrets                                        # run on the server
```

## 7. Provision the nodes (on your admin machine)

In the fleet folder (`detection/rf-subsystem/node/fleet`) of a checkout on `detection/develop`:

1. The fleet already points at the central server, 172.22.41.10. If the server's address ever
   changes, change it in `ansible/group_vars/fleet_nodes.yml` (`central_host` and `broker_address`)
   and in `ansible/inventory.yml` (`ansible_host` of the `central` host), check with
   `grep -rn '<new address>' ansible`, commit it, and run the provision steps below again so every
   node gets the new address.
2. The nodes must be on a network that reaches `<SERVER_IP>`. To move them to another Wi-Fi first:
   ```
   WIFI_PASSWORD='<passphrase>' ./fleet.sh wifi all "<SSID>"
   ```
   The SSID is case-sensitive.
3. Check they answer: `./fleet.sh ping all`
4. Provision the RF nodes, one first:
   ```
   chmod 600 ~/node-secrets
   CENTRAL_SECRETS=~/node-secrets ./fleet.sh provision wbt-1
   CENTRAL_SECRETS=~/node-secrets ./fleet.sh provision rf
   ```
5. *(camera)* Set `CAMERA_HEF=/path/to/yolov6n.hef` in `fleet.env` (sha256 starts `6a20a395`), check
   `camera_zones` in `ansible/host_vars/cam-N.yml` (copy `cam-1.yml.example`) against the RF zone names, then:
   ```
   ./fleet.sh ping camera
   CENTRAL_SECRETS=~/node-secrets ./fleet.sh provision camera
   ```
   This is proven on cam-3 and cam-4: Reolink adopted, Frigate healthy on the Hailo-8, bridge clock
   synced and connected to the broker over TLS. The camera Pi fixes of PR #18 are in the playbook:
   the bridge's NTP check works and time syncs once Wi-Fi is up, camera-link can drive the host
   units, the `eth0` profile is always loaded, Docker starts after Frigate's segment cache mount, the
   image builds on the host network, and the controller connects over IPv4. Each Reolink goes on its Pi's `eth0` through a powered
   PoE injector; a kit with no camera link stays at `no_camera` and needs no re-run once one is plugged
   in. The first start downloads Frigate's image (several GB), so allow a few minutes per kit.
6. Delete the file: `rm ~/node-secrets`

Run one provision at a time. Two runs against the same node at once collide on the package lock.
Re-running is safe.

## 8. Verify

On the server, from `<REPO>`:

```
docker compose ps
docker compose exec broker sh -c 'mosquitto_sub -h 127.0.0.1 -u "$BROKER_FUSION_USER" -P "$BROKER_FUSION_PASSWORD" -t "detection/#" -v'
```

Within 30 seconds expect `detection/rf/nodes/wbt-N/health` and `/online` for each RF node, then
`detection/rf/status/nodes`. *(camera)* `detection/camera/status/nodes` lists cam-1 to cam-4.

```
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" http://<SERVER_IP>:8090/api/v1/nodes
```

From your admin machine:

```
openssl s_client -connect <SERVER_IP>:8883 -servername reg.bulexa.com </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -enddate
```

Expect `CN=reg.bulexa.com` and a Let's Encrypt issuer.

Physical bridge, on the server:

```
cd <REPO>/detection/physical-subsystem/deploy
docker compose --profile fusion logs fusion | grep -E 'publisher up|connected|refused|cannot' | tail
```

Expect `fusion publisher up: reg.bulexa.com:8883 prefix=detection/physical site=sh1` and
`connected`. The `/api/v1/nodes` call above should list `physical-bridge-01` with site `sh1` within
about a minute. Open and close a door, then:

```
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" 'http://<SERVER_IP>:8090/api/v1/events?subsystem=physical'
```

WBT Console: `http://<SERVER_IP>:8080`, Nodes should show four online. The password is generated on the
server: `docker compose exec rf-console cat /run/rf-secrets/console-admin-password`. Device positions
on `detection/rf/positions` start once a site is set up in the console with the nodes placed. The site
and floor plan live in the console's Docker volume, so a rebuilt server starts without them.

Quick event test: unplug one RF node. Its `online` topic goes to `0` within about a minute, and an
offline alert follows after five minutes of silence.

## 9. Updating a machine that already runs the stack

On the server, from `<REPO>`:

```
git pull
detection/central/make-env.sh
docker compose up -d --build
```

`make-env.sh` only adds `.env` keys that are missing (new services' settings, logins for nodes newly in
the fleet inventory). Existing logins, tokens and the certificate are kept, so nodes and clients keep
working without re-provisioning.

## 10. Afterwards

- The old Mac central (172.20.100.150) is retired. If its stack is still up, stop it
  (`docker compose down` there) so no node or client keeps talking to it.
- Delete every copy of `node-secrets` and of the old `rf-node-secrets`.
- The certificate renews itself from cron on the server. Check `detection/central-store/state/renew.log`
  after the first night.
