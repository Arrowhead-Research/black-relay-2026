# RF Central Stack

The Docker Compose stack for the WBT central service (see [`../DESIGN.md`](../DESIGN.md), section 7). It currently contains:

| Service | Purpose | Ports |
|---|---|---|
| `kismet-server` | Kismet `2026-10-R1` (server and log tools only). Receives remote captures from WBT nodes and provides the REST API. | 2501 (web UI, REST, WebSocket remote capture), 3501 (legacy TCP remote capture) |
| `rf-console` | **Opt-in (profile `console`).** WBT Console: live RF map, sites, node placement, walls and calibration, with sign-in: `http://<BIND_ADDR>:8080`. Site data is kept in the `rf-console-data` volume. It publishes to the central broker. See [`../console/README.md`](../console/README.md). | 8080 |

Live analytics (`rf-analytics`) is not part of this stack. It runs as a service of the repo-root [`compose.yaml`](../../../compose.yaml), next to the central broker that the console and analytics publish to; see [Live analytics](#live-analytics-kismet--alerts--mqtt).

The analytics database is not part of the stack yet.

`kismet/kismet_site.conf` sets `keep_per_datasource_stats=true`. Without it, Kismet leaves every node's per-device signal (`seenby` signal) empty and analytics cannot tell which node heard a device loudest. It costs some RAM per device per node.

## Start

```sh
cp .env.example .env        # set BIND_ADDR and the Kismet admin credentials
docker compose up -d --build --wait
```

Open `http://<BIND_ADDR>:2501` and log in with the credentials from `.env`. The credentials are written into the `kismet-home` volume **on first start only**. To change them later, use the Kismet UI, or run `docker compose down -v` (this also deletes the logs) and start again.

## Shared fusion host

On the team's shared Docker host, the stack lives under the host's per-team directory (copy the whole `rf-subsystem` tree: the console builds from `../console`). Add to `.env`:

```sh
BIND_ADDR=0.0.0.0
COMPOSE_FILE=docker-compose.yml:docker-compose.fusion.yml
COMPOSE_PROFILES=console
```

`docker-compose.fusion.yml` publishes only 3501 (node capture) and the console port, keeps Kismet's 2501 on the host's loopback (use an SSH tunnel), and puts `kismet-server` on the external `fusion` network as `rf-kismet`, where the repo-root `rf-analytics` polls it. Claim published ports in the host's `PORTS.md` first. Nodes point `CENTRAL_HOST` in `/etc/rf-node/rf-node.env` at the host's tailnet address.

**Host behind a site's subnet router** (not on the tailnet itself): use the same `.env`, with `BIND_ADDR=0.0.0.0`, and create the external network once with `docker network create fusion` if no other team has yet. Nodes reach the host through the router: they need `tailscale set --accept-routes` and a Headscale rule for ports 3501 and 8080 on the host's network, and `CENTRAL_HOST` is the host's LAN address (see [`../node/README.md`](../node/README.md#tailscale-headscale-tailnet)). When copying the tree, carry `central/.env` over separately (it holds the Kismet and console credentials). For a new building, leave out the git-ignored `console/site/` and start the site fresh. To move the same deployment to another host, also carry over `console/site/layout.yaml` and the console's data (sites, calibration, areas, marks, history, sign-in sessions). Stop `rf-console` on both hosts, then:

```sh
# old host
docker run --rm -v rf-central_rf-console-data:/d:ro alpine tar -C /d -czf - . > rf-console-data.tgz
# new host, after copying the file across
docker run --rm -i -v rf-central_rf-console-data:/d alpine sh -c 'find /d -mindepth 1 -delete && tar -C /d -xzf -' < rf-console-data.tgz
```

Bring the old host's stack down with `docker compose down` (no `-v`: its volumes stay until you decide to delete them).

## Live analytics (Kismet → alerts → MQTT)

`rf-analytics` runs in the repo-root [`compose.yaml`](../../../compose.yaml) (service `rf-analytics`, profile `rf`). It polls `kismet-server` at `http://rf-kismet:2501` over the shared `fusion` network and publishes alerts, camera cues and node status to the central broker (`broker:1883`) with its own login, `rf-analytics`. To turn it on, run this on the central server ([`setup-central.sh`](../../../setup-central.sh); add `--kismet-url URL` if Kismet is elsewhere):

```sh
./setup-central.sh <address> --with-rf
```

That sets `COMPOSE_PROFILES=rf` and `RF_ENABLED=1` in the root `.env`, copies the Kismet admin login and `RF_SITE`, `RF_RULES` and `RF_FRIENDLY` from `central/.env` (only where the root `.env` does not have them yet), points the console at the central broker, and creates the `fusion` network if it is missing. Then, from the repo root:

```sh
docker compose logs -f rf-analytics
docker compose exec broker sh -c 'mosquitto_sub -u "$BROKER_FUSION_USER" -P "$BROKER_FUSION_PASSWORD" -t "detection/rf/#" -v'   # watch what fusion would receive
docker compose stop rf-analytics   # stop only the analytics service
```

Site, rules and friendly files come from `../analytics/config` (mounted read-only). Choose them with `RF_SITE`, `RF_RULES` and `RF_FRIENDLY` in the root `.env`; the defaults are the `*.example.yaml` files for `wbt-1`–`wbt-4`, and the example site geometry is a placeholder. Analytics events (`/data/events.jsonl`) and the durable outbox (`/data/rf-outbox.jsonl`) live in the root project's `rf-analytics-data` volume. It logs a stats line every minute: `observations`, `stale` (old per-node entries dropped), `alerts`, `node_status` and so on.

## Verify

```sh
docker compose ps                                           # kismet-server should be "healthy"
curl -u "$USER:$PASS" http://127.0.0.1:2501/system/status.json | head -c 300
docker compose logs kismet-server | grep -E "remote capture|HTTP server"
```

On startup the log shows `ERROR: Could not open ICAO database`. You can ignore it: the ADS-B data package is not installed because this stack doesn't use it.

## Connect a stand-in node

You don't need a Pi 5 to act as a node. Any Linux machine can stream to the central server using Kismet capture helpers from the **same release**.

**Install (use the Kismet apt repo that matches the node's OS release):**

```sh
# Debian/Ubuntu node with the Kismet apt repo configured for its release.
# kismet-capture-rtl433-v2 needs the rtl_433 decoder binary installed separately.
sudo apt install \
  kismet-capture-linux-wifi=2026-10-R1 \
  kismet-capture-linux-bluetooth=2026-10-R1 \
  kismet-capture-rtl433-v2=2026-10-R1 \
  rtl-433
```

**Remote capture (validated on Kismet 2026-10-R1, legacy TCP on port 3501):**

Replace `<central-ip>` with the central host (for example `rf-central` over MagicDNS/Tailscale, or a LAN IP that the node can reach). Replace interfaces and `name=` values with the node's actual devices/ID. **Do not capture on the interface used for backhaul/SSH** (for example, do not put an onboard Wi-Fi interface into monitor mode if it carries your connection).

```sh
# Wi-Fi (external adapter; monitor mode is handled by the helper)
sudo kismet_cap_linux_wifi --tcp --connect <central-ip>:3501 \
  --source 'wlan1:name=rf-node-01:wifi'

# Bluetooth / BLE (verify which hciN is the external adapter on each node)
sudo kismet_cap_linux_bluetooth --tcp --connect <central-ip>:3501 \
  --source 'hci0:name=rf-node-01:bt'

# TPMS via RTL-SDR + rtl_433
sudo kismet_cap_sdr_rtl433 --tcp --connect <central-ip>:3501 \
  --source 'rtl433-0:name=rf-node-01:tpms'
```

WebSocket remote capture on port 2501 (with `--user`/`--password` or `--apikey`) is supported by the helpers but has not been validated in this environment yet (DESIGN.md, open question 5). For ad-hoc testing, run helpers with `nohup`/`tmux` or as `systemd` units; ad-hoc starts are not persistent across reboot.

## Security notes

- Leave `BIND_ADDR` set to `127.0.0.1` or the host's Tailscale IP. Use `0.0.0.0` only on a host behind a subnet router with `docker-compose.fusion.yml`, which publishes just 3501 and the console. Never expose it on a public interface.
- `.env` is git-ignored. Never commit real credentials.

## Kismet version upgrades

The Kismet apt repository only carries the current release. When it moves on, the pinned version stops installing. To upgrade, change `KISMET_VERSION` in `.env.example`, `docker-compose.yml`, and `kismet/Dockerfile`, and update the nodes in the same change.
