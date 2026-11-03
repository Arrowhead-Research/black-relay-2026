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

You don't need a Pi 5 to act as a node. Any Linux machine can stream to the central server using Kismet capture helpers from the **same release**:

```sh
# On the stand-in node (Debian/Ubuntu with the Kismet apt repo configured):
sudo apt install kismet-capture-linux-wifi=2026-10-R1 kismet-capture-linux-bluetooth=2026-10-R1 kismet-capture-rtl433-v2=2026-10-R1

# Wi-Fi (Alfa adapter; monitor mode is handled by the helper):
sudo kismet_cap_linux_wifi --connect rf-central:3501 --source 'wlan1:name=rf-node-01:wifi'
```

The exact remote-capture flags for each helper, including WebSocket mode with an API key, still need to be confirmed against this release (DESIGN.md, open question 5). Record the working commands here once they are verified.

## Security notes

- Leave `BIND_ADDR` set to `127.0.0.1` or the host's Tailscale IP. Never use a public interface.
- The Mosquitto configuration allows anonymous clients and is for development only.
- `.env` is git-ignored. Never commit real credentials.

## Kismet version upgrades

The Kismet apt repository only carries the current release. When it moves on, the pinned version stops installing. To upgrade, change `KISMET_VERSION` in `.env.example`, `docker-compose.yml`, and `kismet/Dockerfile`, and update the nodes in the same change.
