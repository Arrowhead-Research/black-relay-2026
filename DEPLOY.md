# Deploying the central server

How to bring up the BlackRelay central server on a new Linux machine and point the Raspberry Pi nodes
at it: the RF nodes (wbt-1 to wbt-4) and the camera kits (cam-1 to cam-4). Written for the new AMD
(x86_64) server. Replace these placeholders throughout:

| Placeholder | Meaning |
|---|---|
| `<SERVER_IP>` | the new server's LAN address |
| `<USER>` | your login on the server |
| `<REPO>` | the checkout on the server, for example `/srv/black-relay-2026` |
| `<EMAIL>` | the Let's Encrypt account email used before |

Secrets (the certificate folder, `node-secrets`, console passwords) only travel over `scp` or AirDrop.
Never paste them in chat or commit them.

## 0. Before you start: which branch

Deploy `detection/develop`. It holds the RF node fleet (PR #14), the camera kits and the camera relay
in the central project (PR #15) and the physical bridge on the same host (PRs #16 and #17, site id
`sh1`).

## 1. Server prep (on the new server)

1. Install Docker Engine with the Compose plugin (Ubuntu or Debian):
   ```
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker <USER>    # then log out and in again
   docker compose version            # must print v2.x
   ```
2. Give the server a fixed address (`<SERVER_IP>`): a static lease or a static config. The nodes get
   this address written into their configuration.
3. Clone and check out:
   ```
   git clone git@github.com:Arrowhead-Research/black-relay-2026.git <REPO>
   cd <REPO> && git checkout detection/develop
   ```
4. Optional, saves build time: the images are private on ghcr.io, already built for amd64.
   `docker login ghcr.io -u <github user>` with a token that has `read:packages`. Without it the setup
   builds the images locally, which works and takes longer.

## 2. Move the certificate over (from the old central machine)

The reg.bulexa.com certificate, the Let's Encrypt account and the Cloudflare token live in a
git-ignored folder. Copy it so nothing is issued again. On the old central machine, from its checkout:

```
scp -r detection/central-store/state <USER>@<SERVER_IP>:<REPO>/detection/central-store/
```

Then on the new server: `chmod -R go-rwx <REPO>/detection/central-store/state`.

Do not copy the old machine's `.env` files. The new server generates its own logins, which also
replaces the node logins that were exposed during testing.

If the folder is lost: create a Cloudflare API token (Zone:Read and DNS:Edit on bulexa.com only) and
the setup asks for it once.

## 3. DNS

In Cloudflare, point the `reg.bulexa.com` A record at `<SERVER_IP>`, DNS only (grey cloud). The Pis pin
the name locally and do not depend on it. Every other client does.

## 4. Start the central stack (on the new server)

From `<REPO>`:

```
./setup-central.sh <SERVER_IP> --domain reg.bulexa.com --email <EMAIL>
```

This creates the logins, reuses the certificate, adds the daily renewal to cron, and starts the
registry and secrets store, the broker (1883 and 8883 with TLS), fusion, the RF services (Kismet, WBT
Console, rf-analytics) and the camera relay. It is safe to run again. After the first run,
`docker compose up -d` from `<REPO>` is enough; to update, see step 9.

### Physical bridge, if the IQ panel is cabled to this server

Full notes: the physical thread's `central-server-physical-bridge.md`.

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
   sets the bridge's site id to fusion's (`central`), writes the bridge's broker password and starts the
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

## 5. Firewall (on the new server)

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
| 8554/tcp, 8889/tcp, 8189/udp | camera video: RTSP and WebRTC *(camera)* |
| 123/udp | NTP for the camera kits *(camera)* |

Kismet's own web UI (2501) stays on loopback. Reach it with an SSH tunnel.

## 6. Node logins (on the new server)

From `<REPO>`:

```
detection/central/node-secrets.sh rf wbt-1 wbt-2 wbt-3 wbt-4 > node-secrets
detection/central/node-secrets.sh camera cam-1 cam-2 cam-3 cam-4 >> node-secrets
chmod 600 node-secrets
```

Copy it to your Mac, then delete it on the server:

```
scp <USER>@<SERVER_IP>:<REPO>/node-secrets ~/node-secrets     # run on the Mac
rm <REPO>/node-secrets                                        # run on the server
```

## 7. Repoint and provision the nodes (on your Mac)

In the fleet folder (`detection/rf-subsystem/node/fleet`) of a checkout on `detection/develop`:

1. Point the fleet at the new server. The old address is written in the Ansible settings; this replaces
   every occurrence and shows the result:
   ```
   grep -rl '172\.20\.100\.150' ansible | xargs sed -i '' 's/172\.20\.100\.150/<SERVER_IP>/g'
   grep -rn '<SERVER_IP>' ansible
   ```
   Expect `central_host`, `broker_address` and the central `ansible_host`. Commit the change.
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
   `camera_zones` in `ansible/host_vars/cam-N.yml` against the RF zone names, then:
   ```
   ./fleet.sh ping camera
   CENTRAL_SECRETS=~/node-secrets ./fleet.sh provision camera
   ```
   This is proven on cam-3 and cam-4: Reolink adopted, Frigate healthy on the Hailo-8, bridge clock
   synced and connected to the broker over TLS. Each Reolink goes on its Pi's `eth0` through a powered
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

From your Mac:

```
openssl s_client -connect <SERVER_IP>:8883 -servername reg.bulexa.com </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -enddate
```

Expect `CN=reg.bulexa.com`, a Let's Encrypt issuer and the same expiry as before.

Physical bridge, on the server:

```
cd <REPO>/detection/physical-subsystem/deploy
docker compose --profile fusion logs fusion | grep -E 'publisher up|connected|refused|cannot' | tail
```

Expect `fusion publisher up: reg.bulexa.com:8883 prefix=detection/physical site=central` and
`connected`. The `/api/v1/nodes` call above should list `physical-bridge-01` with site `central` within
about a minute. Open and close a door, then:

```
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" 'http://<SERVER_IP>:8090/api/v1/events?subsystem=physical'
```

WBT Console: `http://<SERVER_IP>:8080`, Nodes should show four online. The password is generated on the
server: `docker compose exec rf-console cat /run/rf-secrets/console-admin-password`. Device positions
on `detection/rf/positions` start once a site is set up in the console with the nodes placed. The site
and floor plan from the old machine do not carry over.

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

- On the old central machine, remove the test workarounds if the bridge ran there: delete
  `detection/physical-subsystem/deploy/docker-compose.override.yml` and the `brlink` Docker network.
- Stop the stack on the old central machine (`docker compose down` there) so no node or client keeps
  talking to it.
- Delete every copy of `node-secrets` and of the old `rf-node-secrets`.
- The certificate renews itself from cron on the server. Check `detection/central-store/state/renew.log`
  after the first night.
