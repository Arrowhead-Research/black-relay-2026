# RF Central Stack

The Docker Compose stack for the WBT central service (see [`../DESIGN.md`](../DESIGN.md), section 7). It currently contains:

| Service | Purpose | Ports |
|---|---|---|
| `kismet-server` | Kismet `2026-10-R1` (server and log tools only). Receives remote captures from WBT nodes and provides the REST API. | 2501 (web UI, REST, WebSocket remote capture), 3501 (legacy TCP remote capture) |
| `mqtt` | Mosquitto 2.0.22. A development stand-in for the fusion broker. | 1883 |
| `rf-console` | **Opt-in (profile `console`).** WBT Console: live RF map, sites, node placement, walls and calibration, with sign-in: `http://<BIND_ADDR>:8080`. Site data is kept in the `rf-console-data` volume. See [`../console/README.md`](../console/README.md). | 8080 |
| `rf-analytics` | **Opt-in (profile `analytics`).** Runs `rfanalytics live`: polls `kismet-server`, runs alert rules, publishes alerts, camera cues and node status to `mqtt`. Image built from [`../analytics`](../analytics). | none |

The analytics database is not part of the stack yet.

`kismet/kismet_site.conf` sets `keep_per_datasource_stats=true`. Without it, Kismet leaves every node's per-device signal (`seenby` signal) empty and analytics cannot tell which node heard a device loudest. It costs some RAM per device per node.

## Start

```sh
cp .env.example .env        # set BIND_ADDR and the Kismet admin credentials
docker compose up -d --build --wait
```

Open `http://<BIND_ADDR>:2501` and log in with the credentials from `.env`. The credentials are written into the `kismet-home` volume **on first start only**. To change them later, use the Kismet UI, or run `docker compose down -v` (this also deletes the logs) and start again.

## Shared fusion host

On the team's shared Docker host, the stack lives under the host's per-team directory (copy the whole `rf-subsystem` tree: the builds use `../analytics` and `../console`). Add to `.env`:

```sh
BIND_ADDR=0.0.0.0
COMPOSE_FILE=docker-compose.yml:docker-compose.fusion.yml
COMPOSE_PROFILES=console
```

`docker-compose.fusion.yml` publishes only 3501 (node capture) and the console port, keeps Kismet's 2501 on the host's loopback (use an SSH tunnel), and puts `mqtt` on the external `fusion` network as `rf-mqtt`. Claim published ports in the host's `PORTS.md` first. Nodes point `CENTRAL_HOST` in `/etc/rf-node/rf-node.env` at the host's tailnet address.

## Live analytics (Kismet → alerts → MQTT)

```sh
docker compose --profile analytics up -d --build      # starts rf-analytics as well
docker compose --profile analytics logs -f rf-analytics
docker compose exec mqtt mosquitto_sub -t 'detection/rf/#' -v   # watch what fusion would receive
docker compose --profile analytics stop rf-analytics  # stop only the analytics service
```

`rf-analytics` reads its site, rules and friendly files from `../analytics/config` (mounted read-only). Choose them with `RF_SITE`, `RF_RULES` and `RF_FRIENDLY` in `.env`; the defaults are the `*.example.yaml` files for `wbt-1`–`wbt-4`, and the example site geometry is a placeholder. Analytics events (`/data/events.jsonl`) and the durable outbox (`/data/rf-outbox.jsonl`) live in the `rf-analytics-data` volume. It logs a stats line every minute: `observations`, `stale` (old per-node entries dropped), `alerts`, `node_status` and so on.

> `docker compose up` for this stack recreates `mqtt` when the compose file changes, which disconnects subscribers. Messages already accepted by the broker are not lost, but a subscriber that reconnects only sees new messages.

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

- Leave `BIND_ADDR` set to `127.0.0.1` or the host's Tailscale IP. Never use a public interface.
- The Mosquitto configuration allows anonymous clients and is for development only.
- `.env` is git-ignored. Never commit real credentials.

## Kismet version upgrades

The Kismet apt repository only carries the current release. When it moves on, the pinned version stops installing. To upgrade, change `KISMET_VERSION` in `.env.example`, `docker-compose.yml`, and `kismet/Dockerfile`, and update the nodes in the same change.
