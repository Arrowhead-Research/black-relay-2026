# RF Central Stack

The Docker Compose stack for the WBT central service (see [`../DESIGN.md`](../DESIGN.md), section 7). The repo-root [`compose.yaml`](../../../compose.yaml) includes it, so on the central server one `docker compose up -d` from the repo root starts it beside the central broker and fusion. It also runs on its own, for a lab without the central broker.

| Service | Purpose | Ports |
|---|---|---|
| `rf-bootstrap` | One-shot, on first start: generates the RF passwords into the `rf-secrets` volume (only the missing ones), then exits. See [Passwords](#passwords). | none |
| `kismet-server` | Kismet `2026-10-R1` (server and log tools only). Receives remote captures from WBT nodes and provides the REST API. | 2501 (web UI, REST, WebSocket remote capture), 3501 (legacy TCP remote capture) |
| `rf-console` | WBT Console: live RF map, sites, node placement, walls and calibration, with sign-in: `http://<BIND_ADDR>:8080`. Site data is kept in the `rf-console-data` volume. Publishes to the central broker. See [`../console/README.md`](../console/README.md). | 8080 |
| `rf-analytics` | Live analytics: polls `kismet-server`, runs the alert rules, publishes alerts, camera cues and node status to the central broker. See [Live analytics](#live-analytics-kismet--alerts--mqtt). | none |

Ports bind to `BIND_ADDR` (in `central/.env`), else `BIND_ADDRESS` (in the repo-root `.env`), else `127.0.0.1`.

The analytics database is not part of the stack yet.

`kismet/kismet_site.conf` sets `keep_per_datasource_stats=true`. Without it, Kismet leaves every node's per-device signal (`seenby` signal) empty and analytics cannot tell which node heard a device loudest. It costs some RAM per device per node.

## Start

On the central server, from the repo root, after `detection/central/make-env.sh` has written the root `.env` (`setup-central.sh` does both):

```sh
docker compose up -d
```

Nothing else to prepare: the passwords are generated, and `central/.env` is optional (see [`.env.example`](.env.example)). Kismet's UI is at `http://127.0.0.1:2501` on the host (SSH tunnel; `KISMET_UI_BIND` publishes it elsewhere), the console at `http://<BIND_ADDR>:8080`.

On its own, for a lab without the central broker, from this directory:

```sh
docker compose up -d --build
```

Console publishing stays off without a broker login.

**Upgrading an existing stack.** The volumes keep their old `rf-central_*` names, so Kismet logs, console sites and calibration carry over. `setup-central.sh` first stops an old `rf-central` project (`docker compose -p rf-central down --remove-orphans`); if you start by hand, run that once. Kismet's login is rewritten from the configured password on every start, so it always matches what the console and rf-analytics use: an old `KISMET_ADMIN_PASSWORD` in `central/.env` keeps working, otherwise the generated one applies.

## Passwords

`rf-bootstrap` generates these on first start into the `rf-secrets` volume (mounted at `/run/rf-secrets`), each one only if it is missing:

| File | For |
|---|---|
| `kismet-admin-password` | Kismet web and REST admin (user `KISMET_ADMIN_USER`, default `admin`). The console and rf-analytics use it too. |
| `console-operator-password` | WBT Console sign-in, operator role |
| `console-admin-password` | WBT Console sign-in, admin role |
| `analytics-broker-password` | rf-analytics' login (`rf-analytics`) on the central broker. The broker reads the same file. |

`KISMET_ADMIN_PASSWORD`, `CONSOLE_OPERATOR_PASSWORD` and `CONSOLE_ADMIN_PASSWORD` in `central/.env` win over the generated files. Programs take a secret as `NAME` or from the file named by `NAME_FILE`. Read a generated password with:

```sh
docker compose exec rf-console cat /run/rf-secrets/console-admin-password
```

Console sign-in is always on in the stack. To choose the console passwords yourself, set them in `central/.env` and run `docker compose up -d rf-console`, or use the optional prompt `central/set-console-passwords.sh` (it needs `central/.env` to exist).

Kismet seeds its login only when `kismet-home` is first created. To change the Kismet login later, change it in Kismet's UI and set the same value as `KISMET_ADMIN_PASSWORD` in `central/.env`, then `docker compose up -d`. To wipe everything and start again, run `docker compose down -v` in this directory; that deletes every RF volume (logs, console sites, passwords).

## Shared central host

On the central server the RF services come from the repo-root project, so the host is set up by `setup-central.sh` (see [Start](#start)). To publish the ports on the host's tailnet address, set it in `central/.env`:

```sh
BIND_ADDR=<tailnet address>    # 0.0.0.0 only behind a subnet router (see Security notes)
```

3501 and 8080 bind to `BIND_ADDR`; Kismet's 2501 stays on loopback unless `KISMET_UI_BIND` is set. Claim published ports in the host's `PORTS.md` first. Nodes point `CENTRAL_HOST` in `/etc/rf-node/rf-node.env` at the host's tailnet address.

**Host behind a site's subnet router** (not on the tailnet itself): set `BIND_ADDR` to the host's LAN address (or `0.0.0.0`). Nodes reach the host through the router: they need `tailscale set --accept-routes` and a Headscale rule for ports 3501 and 8080 on the host's network, and `CENTRAL_HOST` is the host's LAN address (see [`../node/README.md`](../node/README.md#tailscale-headscale-tailnet)). When copying the checkout, carry the git-ignored `.env` files over separately: the repo-root `.env` (broker logins) and `central/.env` (it holds the Kismet and console passwords you set). The new host's rf-bootstrap generates its own passwords unless `central/.env` sets them. For a new building, leave out the git-ignored `console/site/` and start the site fresh. To move the same deployment to another host, also carry over `console/site/layout.yaml` and the console's data (sites, calibration, areas, marks, history, sign-in sessions). Stop `rf-console` on both hosts, then:

```sh
# old host
docker run --rm -v rf-central_rf-console-data:/d:ro alpine tar -C /d -czf - . > rf-console-data.tgz
# new host, after copying the file across
docker run --rm -i -v rf-central_rf-console-data:/d alpine sh -c 'find /d -mindepth 1 -delete && tar -C /d -xzf -' < rf-console-data.tgz
```

Bring the old host's stack down with `docker compose down` (no `-v`: its volumes stay until you decide to delete them).

## Live analytics (Kismet → alerts → MQTT)

`rf-analytics` is a service of this stack, so it starts with it. On the central server it reaches the central broker as `broker:1883` with its own login, `rf-analytics` (password in `rf-secrets`). It polls `kismet-server` at `http://kismet-server:2501` on the project network and publishes alerts, camera cues and node status under `detection/rf/`. Standalone there is no `broker` service, so nothing is published.

From the repo root on the central server:

```sh
docker compose logs -f rf-analytics
docker compose exec broker sh -c 'mosquitto_sub -u "$BROKER_FUSION_USER" -P "$BROKER_FUSION_PASSWORD" -t "detection/rf/#" -v'   # watch what fusion would receive
docker compose stop rf-analytics   # stop only the analytics service
```

Site, rules and friendly files come from `../analytics/config` (mounted read-only). Choose them with `RF_SITE`, `RF_RULES` and `RF_FRIENDLY` in `central/.env`; the defaults are the `*.example.yaml` files for `wbt-1`–`wbt-4`, and the example site geometry is a placeholder. Analytics events (`/data/events.jsonl`) and the durable outbox (`/data/rf-outbox.jsonl`) live in the `rf-analytics-data` volume. It logs a stats line every minute: `observations`, `stale` (old per-node entries dropped), `alerts`, `node_status` and so on.

## Verify

```sh
docker compose ps                                           # kismet-server should be "healthy"
docker compose logs rf-bootstrap                            # first start: the "generated" lines
PASS="$(docker compose exec -T rf-console cat /run/rf-secrets/kismet-admin-password)"
curl -s -u "admin:$PASS" http://127.0.0.1:2501/system/status.json | head -c 300
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

- `BIND_ADDR` sets where 3501 (nodes) and 8080 (console) are published. Use `127.0.0.1` or the host's Tailscale IP, or `0.0.0.0` only behind a subnet router. Kismet's 2501 stays on `127.0.0.1` (`KISMET_UI_BIND` to change). Never expose any of them on a public interface.
- Passwords are generated into the `rf-secrets` volume. `.env` files are git-ignored. Never commit real credentials.

## Kismet version upgrades

The Kismet apt repository only carries the current release. When it moves on, the pinned version stops installing. To upgrade, change `KISMET_VERSION` in `.env.example`, `docker-compose.yml`, and `kismet/Dockerfile`, and update the nodes in the same change.
