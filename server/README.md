# TAK Server and Gateway

A containerized TAK Server with a Python gateway (`pytak-gateway`) that pushes node data into it as Cursor-on-Target (CoT) events. The following [`docker-compose.yaml`](./docker-compose.yaml) spins up the following services:

- `takserver-core` - TAK Server 5.6 (config, messaging, API, retention, plugin manager)
- `pki` - Builds the CA, the server certificates and the gateway's client certificate once, into the `tak-pki` volume, and keeps the public part synced to the gateway (see [Certificates and rotation](#certificates-and-rotation))
- `takserver-db` - PostgreSQL 15 + PostGIS, schema managed by TAK's `SchemaManager`
- `gateway-http` - FastAPI server that turns REST into CoT, and keeps the registry of sites and nodes (see [Create a Site](#create-a-site))
- `gateway-http-monitor` - Polls the Fusion server (see [Fusion](#fusion)), reports whether Fusion is up as CoT, and reads its event log. Started only when `FUSION_URL` is set
- `mqtt-broker`, `gateway-mqtt` - Mosquitto and the gateway's mqtt service. Not started by `start.sh`; see [MQTT](#mqtt). Still a demo path until the Detection team confirms MQTT.

## Getting Started

### Download TAK Server Source

First download [TAKSERVER-DOCKER-5.6-RELEASE-57.zip](https://tak.gov/rails/active_storage/blobs/redirect/eyJfcmFpbHMiOnsiZGF0YSI6NzcxNzYsInB1ciI6ImJsb2JfaWQifX0=--47d6197bda24df7bf088fc77ec387c1ea5f1f0e1/takserver-docker-5.6-RELEASE-57.zip?disposition=attachment) from the [tak.gov products page](https://tak.gov/products/tak-server?product_version=tak-server-5-6-0-RELEASE-57).

Next, unzip the download and copy its contents to the tak-server/tak/ directory in this repository. The following should do the trick assuming you saved the .zip to the root of this project.

```bash
unzip takserver-docker-5.6-RELEASE-57.zip
cp -r takserver-docker-5.6-RELEASE-57/tak/. tak-server/tak/
rm -r takserver-docker-5.6-RELEASE-57.zip takserver-docker-5.6-RELEASE-57/
```

### Configure

First, copy the [`.env.example`](.env.example) to .env. Change the example `ChangeMe-*` passwords for your deployment. TAK's certificate password and database password default to `atakatak` and are hardcoded in [`tak-server/CoreConfig.xml`](./tak-server/CoreConfig.xml) (lines 9, 49, and 52), `tak-server/tak/certs/cert-metadata.sh` and the client [`.pref`](./tak-server/tak-server-configurator-template.pref) template. Changing `TLS_PASSWORD` in your `.env` means changing it in all three places. Do this before first start if the server is reachable from untrusted networks.

```bash
cp .env.example .env
```

You'll need to generate a admin password and generate a hash. Use uv to generate a pw hash.

```bash
cd gateway && uv run pytak-gw hash-password
```

Then, paste the result into your `.env` file.

Set `BIND_ADDR` to the one address every published port binds to: the host's VPN (Tailscale) address on the host, `127.0.0.1` on a laptop. The gateway's TLS certificate is made for this address unless you set `GATEWAY_TLS_NAME`.

### Run Server and Gateway

This script will run `docker compose` for you. It starts `pki` and TAK Server first, runs the configurator, and then starts the gateway server.

```bash
sudo ./start.sh
```

The `pki` service creates the CA and every certificate ([pki-init.sh](./tak-server/pki-init.sh)), and the takserver container creates the admin/client accounts with the [configurator.sh](./tak-server/configurator.sh) script. It also creates `./shared` and `./plugins` as shared volumes. The gateway keeps a registry of sites and nodes in a SQLite file on the `gateway-data` volume.

Go to https://10.0.0.39:8446/ to view the TAK Web UI.

Port 8000 is HTTPS with mutual TLS, so every call must present a certificate signed by the TAK CA. The examples below use the gateway platform's own client certificate (`pytak-gateway`), which the `pki` service already creates, and the public certs that the configurator copies into `./shared` (`root-ca.pem`, `intermediate.pem`, `pytak-gateway.pem/.key/.p12`). In production, these will be ATAK client certs. The server certificate is only valid for `GATEWAY_TLS_NAME`, which defaults to `BIND_ADDR`. If your clients use a DNS name instead, set `GATEWAY_TLS_NAME` to it. Use the following snippet to do a health check.

```bash
sudo curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak https://127.0.0.1:8000/health
```

### Create a Site

To create a new site, use the `/sites` endpoint. Don't forget to use the admin password you set earlier in the [Configure](#configure) step.

```bash
curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak \
     -X POST https://127.0.0.1:8000/sites -u admin:<admin password> -H "Content-Type: application/json" \
     -d '{"site_id":"sh1","lat":38.8895,"lon":-77.0353,"mission_package_name":"sh1-mission","mission_package_password":"<mission package password>"}'
```

Every site has an ATAK mission package: a `mission_package_name` (letters, digits, `.`, `_`, `-`, up to 128) and a `mission_package_password` (8 to 128 characters). The gateway keeps the name and only an Argon2id hash of the password; the password is never returned or logged. Both are required by the database (`NOT NULL`), so no site exists without a mission package, and `PUT` and `DELETE` always need its password. Everything the gateway sends to TAK for a site carries that site's mission package (`<marti><dest mission="..."/></marti>`) and no other site's. Building the site makes the mission package on TAK Server (`TAK_API_URL`, over mTLS with the gateway's own client cert). If that fails nothing is kept: a name TAK Server already holds is a `409`, any other failure a `502`, and no site is left behind.

The reply is `{"site":"sh1","nodes":0,"mission_package":"sh1-mission"}`. Use Fusion's id as the `site_id` if the site is also a Fusion site.

To change a site, `PUT /sites/<site>` with the whole package again (the name cannot change). Like `DELETE`, it takes the mission package password in the `X-Mission-Package-Password` header, and no admin password: no HTTP Basic. A wrong password is the usual `401`.

Every call on this page except `GET /health`, `PUT /sites/<site>` and `POST /sites/<site>/clear` needs the admin password (HTTP Basic, any username). A missing or wrong one gets the same `401` with `WWW-Authenticate: Basic`.

### Register Nodes

The site, subsystem and node id are in the URL.

```bash
curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak \
     -X PUT https://127.0.0.1:8000/sites/sh1/nodes/rf/rf-north -u admin:<admin password> \
     -H "Content-Type: application/json" -d '{"lat":38.8901,"lon":-77.0340,"range_m":300}'
```

The subsystem is one of `camera`, `rf`, `physical`, `wifi`, `bt`, `tpms`; only a camera may have a `stream`. The position defaults to the site's. Calling it again updates the node. A site that does not exist is a `404`. Registering a node also raises any detection Fusion sent for it before it was registered (see [Fusion](#fusion)). A node leaves the map when its site is removed.

Detections and health reports are not posted to the gateway: they arrive from Fusion (the [http monitor](#fusion)) or over [MQTT](#mqtt), for nodes registered here.

### Removing a Site

```bash
curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak \
     -X DELETE https://127.0.0.1:8000/sites/sh1 -u admin:<admin password> \
     -H "X-Mission-Package-Password: <mission package password>"
```

Deleting needs both passwords; a missing or wrong mission package password is the same `401`. It removes the mission package from TAK Server, then the site and its nodes, and sends delete messages to TAK so they leave the map. If TAK Server cannot remove the mission package the reply is a `502` and the site stays; a package already gone from TAK Server counts as removed.

### End a trip (clear)

An operator ends a trip with `POST /sites/<site>/clear`. It is scoped to one site and one node: it ends only that node's trip, and only the trip it names (`tripped`, when given, is the `updated` of the trip being ended), so a newer detection is never cleared by a stale request.

It takes the admin password, or that site's mission package password in the `X-Mission-Package-Password` header (as `PUT` does), so a phone that follows the site can end its trips without the admin password. Another site's mission package password is the same `401` as a wrong one. A node the site does not have (or a site that does not exist) is a `404`.

```bash
curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak \
     -X POST https://127.0.0.1:8000/sites/sh1/clear -u admin:<admin password> -H "Content-Type: application/json" \
     -d '{"subsystem":"rf","node_id":"rf-north","time":"2026-10-08T14:05:00Z"}'
```

### Allow a friendly emitter

A site's own devices (its router, a team radio) would trip its RF nodes for ever. An admin allows an emitter at a site by its MAC address, as a trip's CoT names it (`emitter`):

```bash
curl ... -X POST https://127.0.0.1:8000/sites/sh1/emitters -u admin:<admin password> -H "Content-Type: application/json" \
     -d '{"emitter":"0a:ec:f3:7d:6b:94","name":"kitchen TV"}'
```

From then on an `rf.alert` that emitter causes at that site trips nothing, and the trips it had caused there, still open, are ended in the same call (the reply's `cleared` counts them). An alert that names no emitter still trips: there is nothing to match. `GET /sites/<site>/emitters` lists what is allowed and `DELETE /sites/<site>/emitters/<mac>` takes one off. The list is per site, holds at most 500, and goes with the site. All three need the admin password; the mission package password opens none of them, since any phone that follows the site would then be able to switch RF detection off for a device. An allowed address is a way past the RF nodes for anyone who copies it, so the list is never sent in CoT.

### What the gateway remembers

Node state (`ok`, `tripped`, `offline`) is kept in the registry database. The gateway and the http monitor are separate processes sharing on state in a SQlite file. 

| State | Becomes it when |
|---|---|
| `tripped` | a detection newer than the last one arrives. It stays tripped until `POST /sites/<site>/clear`: health reports, silence and a node going offline never end it, because losing a node can be the incident. |
| `offline` | a health report says down, or (Fusion) the node has gone quiet or Fusion itself has been out of reach for a while. |
| `ok` | a health report says up after `offline`, or a clear. |

A node with no state is unknown until it first speaks. Detections are ordered by their own time, but a detection older than a health report that arrived before it still trips the node: only an older *detection* counts as a replay.

### How a change becomes CoT

Two parts of the code, kept apart on purpose:

- [`state.py`](./gateway/src/pytak_gateway/state.py) (`GatewayState`) **decides**. It holds the rules above and the node state in the database, and for each detection, health report or clear it says what the node's new state is, or that nothing changes. It sends nothing.
- [`cot_publisher.py`](./gateway/src/pytak_gateway/cot_publisher.py) (`CotPublisher`) **sends**. It asks `GatewayState` first and, only if the state changed, builds the CoT and sends it to TAK. Because the ATAK plugin keeps no state, it also re-sends every site, node and active threat every 30 seconds. `gateway-http` (operator clears and site/node changes) and the MQTT service use it.

Both reach TAK through [`tak_connection.py`](./gateway/src/pytak_gateway/tak_connection.py) (`TakConnection`), the PyTAK client: it connects over mTLS and queues CoT bytes, and knows nothing about state or CoT rules.

The Fusion path skips the publisher: `http_monitor.py` reads Fusion, `fusion_mapper.py` applies each event through `GatewayState` and builds the CoT, and the monitor sends it.

The database schema is managed by Alembic (`gateway/src/pytak_gateway/migrations/`) and brought up to date whenever a service starts. The `gateway-data` volume must be a local Docker volume: SQLite's WAL mode does not work on network filesystems.

## Certificates and rotation

Port 8000 uses mutual TLS. There is **one CA chain** (`root-ca` → `intermediate`), built once by the `pki` service ([pki-init.sh](./tak-server/pki-init.sh)).

| Certificate | Made by | Lasts | Used for |
|---|---|---|---|
| `root-ca`, `intermediate` | `pki`, first start | 10 y, 730 d | sign everything below |
| `takserver`, gateway server cert | `pki` | 730 d | the servers prove who they are (the gateway's name is `GATEWAY_TLS_NAME`) |
| `pytak-gateway` | `pki` | 730 d | the gateway's own client cert, towards TAK |
| **ATAK device certs** | **TAK Server**, when a device enrolls on 8446 | **30 d** (`validityDays` in `CoreConfig.xml`) | a phone's identity, to TAK and to the gateway |
| Node platform certs | you, with `makeCert.sh` (below) | 730 d | non-ATAK callers |

Two volumes hold them: `tak-pki` has everything, **CA private keys included** (back it up: losing it means a new CA and every device re-enrolls); `gateway-certs` has only public and leaf files, and is all the gateway sees. `pki` copies changed files across every 30 s and `gateway-http` re-reads them every 60 s ([tls.py](./gateway/src/pytak_gateway/tls.py)), so a CA, CRL or gateway cert change is live in about 90 s with no restart.

### Give a device or node platform access

- **ATAK phone:** enroll on 8446 as usual. TAK signs it a cert with the same intermediate, so it works on 8000 **at once**: there is nothing to register or sync on the gateway. It lasts 30 days, then the phone re-enrolls with its saved login (not verified end to end here).
- **Node platform:** issue it a cert, copy it out, hand over three files:

```bash
docker compose exec pki sh -c 'cd /opt/tak/certs && ./makeCert.sh client rf-platform-1 && chown -R 1001:0 files'
docker compose cp pki:/opt/tak/certs/files/rf-platform-1.pem .  
docker compose cp  pki:/opt/tak/certs/files/rf-platform-1.key .
docker compose cp  pki:/opt/tak/certs/gateway/ca-bundle.pem .
```

The platform needs `rf-platform-1.pem`, `rf-platform-1.key` (password `TLS_PASSWORD`, default `atakatak`) and `ca-bundle.pem` to verify the gateway. Test it with the `curl --cert --key --cacert` pattern above.

### Something went wrong: revoke

```bash
docker compose exec pki /opt/configurator/pki-init.sh revoke rf-platform-1   # the file name without .pem
docker compose logs -f gateway-http                                          # "reloaded the CRL" within ~90 s
```

New connections from that cert are refused from then on (connections already open stay until they reconnect). For a **lost ATAK phone**, revoke is what cuts it off: its 30-day cert otherwise runs out by itself. Not verified: whether `revoke` finds an ATAK-enrolled cert (TAK may not keep it in `files/`) or whether TAK's admin-UI revoke rewrites `intermediate.crl`, the only file the gateway checks.

### Routine upkeep

- `docker compose exec pki /opt/configurator/pki-init.sh status` shows every expiry and the CRL's next update. Check it monthly.
- **An expired CRL locks every client out** (730 days; the gateway warns 14 days before). The intermediate and all 730-day certs expire the same way; replacing the intermediate means every device needs a new truststore.
- Changed `GATEWAY_TLS_NAME` or renewing the gateway's server cert: `docker compose up -d pki`, then `pki-init.sh rotate-gateway`. The server cert has one name only, so clients must use exactly that name or IP.
- A CA is never dropped from a live listener: adding one is picked up, removing one needs `docker compose restart gateway-http`.

## MQTT

The broker listens on 8883 with TLS and nowhere else. Every node has its own login, named `<site>/<subsystem>/<node>`
(for example `sh1/rf/rf-north`), and may publish only under `nodes/<its own name>/`; only the gateway's login reads
([`gateway/mosquitto.conf`](./gateway/mosquitto.conf), [`gateway/mosquitto.acl`](./gateway/mosquitto.acl)).
A message's topic is `nodes/<site>/<subsystem>/<node>/...` and its JSON body names the node by `subsystem` and
`node_id` (no site: the topic's is the site). The gateway takes a message only for the node whose topic it arrived
on, and only if that node is registered (`PUT /sites/<site>/nodes/<subsystem>/<node>`): a node needs a broker login **and** a registration.

Both services are in the compose profile `mqtt`, so a plain `docker compose up` never starts them. Before the first `docker compose --profile mqtt up -d mqtt-broker gateway-mqtt`, make `./mqtt/` (gitignored):

```bash
mkdir -p mqtt/certs
# 1. The broker's certificate, from the TAK Server's CA (CN and SAN = the name clients connect to):
#    mqtt/certs/ca.pem  mqtt/certs/server.pem  mqtt/certs/server.key
#    The gateway connects as `mqtt-broker`, so the certificate carries that name as well.
# 2. One login per registered node (named site/subsystem/node), and one for the gateway:
touch mqtt/passwords
docker build -t blackrelay-mqtt-broker ./mqtt-broker   # the broker image the stack runs
docker run --rm -it -v "$PWD/mqtt:/work" blackrelay-mqtt-broker mosquitto_passwd /work/passwords gateway
docker run --rm -it -v "$PWD/mqtt:/work" blackrelay-mqtt-broker mosquitto_passwd /work/passwords sh1/rf/rf-north
# 3. Put the gateway's login in .env: MQTT_USERNAME=gateway and MQTT_PASSWORD=<what you typed>
```

If the gateway's login is not called `gateway`, change the `user` line in `gateway/mosquitto.acl`.
`gateway/tests/mqtt_broker_check.sh` proves the rules against a real broker with throwaway
certificates (needs Docker).

## Fusion

The http monitor (`gateway-http-monitor`) reads the Fusion server's JSON/REST API (`/api/v1/events`, `/nodes` and `/health`). Set these in `.env` and `./start.sh` starts it:

| Variable | |
|---|---|
| `FUSION_URL` | The server's `https://` URL. Plain `http://` is refused unless `FUSION_ALLOW_HTTP=true`, because the bearer token rides on it; use that only on a trusted network (the monitor warns at start-up). |
| `FUSION_TOKEN` | The bearer token. (Outside compose, `FUSION_TOKEN_FILE` may name a file holding it instead.) |
| `FUSION_CAFILE` | Optional. A CA file inside the container, if Fusion's certificate is not publicly signed. |
| `FUSION_POLL_INTERVAL` | Seconds between polls, default 10. It backs off, up to 5 minutes, while Fusion is failing. |
| `FUSION_PHYSICAL_TRIP_TYPES` | Physical event types that trip a node (see below). Default `tamper`. |
| `FUSION_PHYSICAL_TRIP_KINDS` | Panel zone kinds that trip a node when they leave their resting state. Default `contact,motion,glass_break`. |
| `FUSION_HOLD_MINUTES` | How long a detection from a node nobody has registered is kept, to be raised when it is. Default 15; 0 keeps none. |

Fusion's words are ours, so there is nothing to map: an event for `(site_id, subsystem, node_id)` lands on the registered node with that address (and node `*` takes what names no node). A site and its nodes have to be registered first, like any other (see [Create a Site](#create-a-site), [Register Nodes](#register-nodes)). Use the Fusion site id as the `site_id`.

**What Fusion sends for that is not registered yet is not lost track of.** Every site, subsystem and node it has sent for that has no registered node is remembered (first and last seen, how many events, the last type), and any detection or alert from one is held for `FUSION_HOLD_MINUTES`. No CoT is sent for them: there is no site or position to put it on. The health marker says how many are waiting (`HealthCheck-fusion ... 3 unregistered node(s)`). An operator lists them and registers them by dropping them on the map; the plugin's drop-down and drag-and-drop use the same routes as everyone else:

```bash
# what is waiting: sites, each with its subsystems and, under them, its nodes (admin password)
curl ... -u admin:<admin password> https://127.0.0.1:8000/unregistered

# drop a site: it becomes a site at that point (admin password)
curl ... -u admin:<admin password> -X POST https://127.0.0.1:8000/sites -H "Content-Type: application/json" \
     -d '{"site_id":"building-a","lat":38.8895,"lon":-77.0353}'

# drop a node (or a subsystem, with node "*"): a node at that point (admin password)
curl ... -u admin:<admin password> -X PUT https://127.0.0.1:8000/sites/building-a/nodes/camera/cam-01 \
     -H "Content-Type: application/json" -d '{"lat":38.8895,"lon":-77.0353}'
```

- `GET /unregistered` says, for each site, whether it is `registered`. A node a subsystem's `*` node takes is not listed.
- Dropping a **site** registers it; nodes can then be dropped into it.
- Dropping a **node** or a **subsystem** (`*`) registers it at the point.
- Registering a node raises what was held for it: the latest held detection for each node trips it, with the usual rules (a clear is respected, nothing is raised twice). In one transaction. Its health arrives from `/nodes` on the next poll. Events older than the hold window are not recovered.
- What is remembered is bounded: at most 1,000 unregistered nodes, 100 held detections per node, and nodes not heard from for 7 days leave the list.

Each cycle the monitor does three things:

- **Is Fusion up?** It asks `/health` and sends a `HealthCheck-fusion` marker to TAK (`healthy`, `degraded` or `unreachable`). A Fusion that is down must not look like a quiet site.
- **What happened?** It reads `/events` by cursor, a page at a time. The cursor is kept in the registry database, saved in the same transaction as the page's changes, so a restart goes on where it left off and never skips a page.
- **Who has gone quiet?** It reads `/nodes`, the latest status of every node. A node Fusion calls `stale` (or whose camera or link is offline, or whose RF or panel state is `offline`) is offline here, and online again when it reports. This is the only place silence shows. Every status report is dated when it was read, not when the node said it, so it is always newer than the last.

What a Fusion event does to a node:

| Event | Effect |
|---|---|
| `camera.detection` with `event: object_start` | trips the camera. Other lifecycle values do nothing yet. |
| `rf.alert` | `node_offline`: the RF node in `context.node_id` goes offline. Any other rule trips the RF node its `location.zone` names (`zone-wbt-1` is node `wbt-1`); with no zone, the site's RF `*` node. A node not registered falls back to the site's RF `*` node, if there is one. |
| `physical.event` | trips the panel's node if the zone reports `tamper`, or its `event_type` is in `FUSION_PHYSICAL_TRIP_TYPES` (default `tamper`), **or a zone of a tripping zone kind leaves its resting state**: a `contact` zone opening, motion, glass breaking (resting is `closed` or `idle`). Never on `zone_added` or `zone_removed` (the bridge listing its zones), on events the bridge inferred after a reconnect (`synthetic`), or from a zone the bridge calls `stale`. `motion_cleared` and `zone_closed` only mean the zone reads at rest again, so they end nothing. |
| `camera.node_status`, `rf.node_status`, `physical.panel_status` | online or offline. `detector_ok: false` and `recording_ok: false` are not "down". |
| `rf.position` | says where an emitter is: `position.x_ft` and `position.y_ft`, feet from the site's middle point. Kept for 10 minutes per emitter and site; a trip that emitter causes carries it, and an open trip is sent again when it moves. Trips nothing. |
| `camera.cue_ack`, `physical.zone_status`, anything new | counted, not acted on yet |

A detection takes Fusion's own severity when the event has one (`rf.alert`: `medium` is 5, `high` 8), and 5 otherwise. A trip's `detail` line is built from the event's fields in the gateway's own words (`trip_words.py`), never Fusion's text: `Wi-Fi device lingering for 10 min`, `Bluetooth device back 3 times in 24 h`, `Person seen on camera · 87% sure`, `Door or window opened`, `Motion detected`, `Sensor tampered with`.

A tripped node draws no circle of its own: a Fusion detection does not say where the thing detected is, and the node's own position is not it. The phone draws the trip where it is: at the emitter for RF, and for a camera or the panel at the node's watch point, which every node that has one carries in its CoT (`watch_lat`, `watch_lon` on `<__safehouse>`). A build package needs no `range_m` or `accuracy_m` any more; one that still sends them is taken and they draw nothing. Only a detection that carries its own position (a node that reports to the gateway directly) still has a threat area, at that position.

A node tripped by an `rf.alert` names the emitter in its CoT: `emitter` on `<__safehouse>` is the alert's `emitter.id` when that is a MAC address (sent lower case, with colons; anything else is left out, so none of Fusion's text reaches a phone), with `x_ft` and `y_ft` when Fusion has given that emitter a position within 10 minutes of the alert. The gateway does not turn feet into a map position: the phone does, from the site's position.

The panel's arming state is not in the data, so a door opened at home trips the node as much as one opened by an intruder, until someone clears it. Which event types and zone kinds count is yours to set (`FUSION_PHYSICAL_TRIP_TYPES`, `FUSION_PHYSICAL_TRIP_KINDS`). All of a panel's zones report as one node, so a trip says the panel, not which door.

If Fusion is out of reach for more than three polls (at least a minute), what it last said stops being evidence: every node Fusion reports on goes offline (a tripped one stays tripped) and returns when Fusion does. Nodes that only report to the gateway directly are left alone. A site must not read "all quiet" because its source of news went down.

Fusion's history is deleted by retention. If the monitor's cursor falls behind it, the monitor resumes from the first cursor Fusion still holds, accepting the loss, and records the gap (`fusion_gaps`) and an error in the log. If the cursor is *ahead* of Fusion's log, Fusion's database was replaced or restored: the monitor stops reading events (it keeps checking health) until an operator deletes the `events` row of `fusion_sources` to start over. A 401 (wrong token) stops the monitor rather than retrying.

## Ports

This repository uses the following ports.

| Port | Service | Purpose |
|---|---|---|
| 8089 | takserver-core | CoT streaming over TLS (client cert) |
| 8443 | takserver-core | Web UI / API (client cert) |
| 8446 | takserver-core | Certificate enrollment (username/password) |
| 8444 | takserver-core | Federation |
| 8000 | gateway-http | REST ingestion over mTLS (TAK-issued client cert) |
| 8883 | mqtt-broker | MQTT over TLS, a login per node (profile `mqtt`; see [MQTT](#mqtt)) |

PostgreSQL (5432) is only reachable inside the compose network. Publish it yourself if you need host access.

## Security Notes

Some important notes to keep in mind before production deployment.

We attempt to provide some security/protection from an adversary with a compromised EUD to gateway server. Every route that changes a site or its nodes needs the admin password or that site's mission package password; ending a trip takes either (a phone that follows a site holds its mission package password, and so can end its trips and no other site's), and the gateway has no endpoint that accepts detections or health reports: those come from Fusion and MQTT only. On top of that, port 8000 requires a TAK-issued client certificate (mTLS, see [Certificates and rotation](#certificates-and-rotation)), so the admin password no longer travels in cleartext and a caller needs a valid device certificate before it can even try it. An adversary who has a stolen EUD still has that EUD's certificate until it is revoked (see [Something went wrong: revoke](#something-went-wrong-revoke)) or expires.

- Nothing refuses the shipped `ChangeMe-*` values in `.env`: change them before deploying. Compose only refuses an unset `BIND_ADDR`, `SITE_ADMIN_HASH`, passwords and certificate subject.
- Every port in the table above is published on `BIND_ADDR` only.
- TAK's certificate password and database password default to `atakatak` and are hardcoded in [`tak-server/CoreConfig.xml`](./tak-server/CoreConfig.xml), `cert-metadata.sh` and the client `.pref` template. Changing them means editing all three consistently (and `TLS_PASSWORD`). Do this before first start if the server is reachable from untrusted networks.
- The gateway authenticates to TAK with a client certificate. Anyone who can read the `gateway-certs` volume (`pytak-gateway.p12`) can inject CoT. That certificate is also a TAK Server admin (`tak-server/configurator.sh` runs `certmod -A` on it) so the gateway can remove a site's mission package; whoever holds it has admin on TAK Server's API too.
- `gateway-http` has one credential, the admin password, stored only as an Argon2id hash in `SITE_ADMIN_HASH`. It is needed to create and delete sites, register nodes and list unregistered nodes; a trip can be cleared with it or with the site's mission package password. **There is no rate limit on those admin calls.** Port 8000 is mTLS by default; if you turn that off (remove `GATEWAY_TLS_CERT`) the admin password travels in cleartext HTTP, so keep it on the VPN.
- Mosquitto takes TLS on 8883 only, refuses anyone without a login, and lets a login write only `nodes/<login>/`. `gateway-mqtt` resolves nodes from the same registry, so only registered nodes are accepted.
- The registry database (`gateway-data` volume) lists every site and node position. Treat it like the rest of the VPS data.
- Everything on the VPS (private keys, the `.env` passwords, the database, the Docker volumes) is readable by root. An adversary with root can decrypt and read all traffic and data, so we don't try to defend against that. For example, The gateway authenticates to TAK with a client certificate. Anyone who can read the `gateway-certs` volume (`pytak-gateway.p12`) can inject CoT.



```
  curl --cacert shared/intermediate.pem --cert shared/pytak-gateway.pem --key shared/pytak-gateway.key --pass atakatak \
       -X POST https://172.20.100.151:8000/sites -u admin:test -H "Content-Type: application/json" \
       -d '{
             "site_id": "sh1",
             "lat": 38.8895,
             "lon": -77.0353,
             "mission_package_name": "sh1-mission",
             "mission_package_password": "testtest"
           }'
```