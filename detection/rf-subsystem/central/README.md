# RF Central Stack

The Docker Compose stack for the WBT central service (see [`../DESIGN.md`](../DESIGN.md), section 7). It currently contains:

| Service | Purpose | Ports |
|---|---|---|
| `kismet-server` | Kismet `2026-10-R1` (server and log tools only). Receives remote captures from WBT nodes and provides the REST API. | 2501 (web UI, REST, WebSocket remote capture), 3501 (legacy TCP remote capture) |
| `mqtt` | Mosquitto 2.0.22. A development stand-in for the fusion broker. | 1883 |

`rf-analytics` and its database will be added to this stack when they are built.

## Start

```sh
cp .env.example .env        # set BIND_ADDR and the Kismet admin credentials
docker compose up -d --build --wait
```

Open `http://<BIND_ADDR>:2501` and log in with the credentials from `.env`. The credentials are written into the `kismet-home` volume **on first start only**. To change them later, use the Kismet UI, or run `docker compose down -v` (this also deletes the logs) and start again.

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
