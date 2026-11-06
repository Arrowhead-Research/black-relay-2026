# Central relay

The camera subsystem's central side, as containers for any Linux host on amd64 or arm64: a Pi, a
laptop or a server. It assumes nothing else about the host. Design: `../DESIGN.md`, 4.2 and 7.3.

| Service | Does | Listens on (host) |
|---|---|---|
| `api` | The one way in for users: node status, events, and each node's Frigate | TCP 8080 |
| `netguard` | Holds MediaMTX's network and its published ports, and limits what it can reach | TCP 8554 (RTSP), TCP 8889 and UDP 8189 (WebRTC) |
| `mediamtx` | One video endpoint for all cameras. Pulls a node's stream only while someone watches | Through `netguard` |
| `chrony` | NTP for the site, from this host's clock, without touching it | UDP 123 |
| `mosquitto` | The broker, under the `broker` profile, which `.env` turns on, until the shared compute node's broker exists | TCP 1883 |

TAK integration is out of scope.

## Install

Needs Docker with Compose v2.17 or later, nf_tables in the host's kernel, and this repository's `relay/` and
`bridge/schemas/`. Current Raspberry Pi OS, Debian and Ubuntu kernels have nf_tables. If it is missing,
`netguard` exits, says so, and MediaMTX does not start.
All images are pinned versions with `linux/amd64` and `linux/arm64`, so the steps are the same on a
Pi, a laptop and a server.

1. **Give the host a fixed site LAN IP**, such as a DHCP reservation. This is required. A node's
   firewall admits SSH, Frigate and RTSP from this one IP, and takes it from the broker address in
   the node's portal. If it moves, re-provision the nodes.
2. Generate the secrets. This writes `.env`, mode 0600, and prints none of them. It refuses to
   overwrite an existing `.env` without `--force`.

   ```bash
   ./setup-credentials.sh
   ```

3. In `.env`, set:
   - `RELAY_SITE_SUBNET`, the site LAN, for example `192.168.8.0/24`. **Required.** It must be a private range
     (RFC 1918). The relay sends logins and video requests only to node addresses inside it, and the API does
     not start without it.
   - `RELAY_CENTRAL_IP`, this host's LAN IP, which no node may then claim.
   - `RELAY_WEBRTC_HOSTS`, the addresses browsers reach this host on, for example
     `192.168.8.10,100.101.102.103`. Without them, WebRTC signalling answers but no media flows.
4. Start it. Compose builds the API and chrony images on this host, for its own architecture.

   ```bash
   docker compose up -d --build
   ```

5. In each node's portal, set the broker address to this host's LAN IP, the broker login to
   `RELAY_BROKER_USER` and `RELAY_BROKER_PASSWORD` from `.env`, and leave the time server on its
   default, which follows the broker address. A `node_id` must be at most 63 lowercase letters, digits
   and hyphens, and not `all`. The portal refuses any other at save, but a node saved before that
   rule keeps its old value until it is saved again, and the relay refuses that value.
6. Check it. A node appears within a status interval, about 30 s.

   ```bash
   . ./.env
   curl -H "Authorization: Bearer $RELAY_API_TOKEN" http://localhost:8080/nodes
   ```

To use the shared compute node's broker instead, empty `COMPOSE_PROFILES` in `.env`, and set
`RELAY_BROKER_HOST` and `RELAY_BROKER_PORT` to it, `RELAY_BROKER_USER` and `RELAY_BROKER_PASSWORD` to the
login nodes hold there, and `RELAY_API_BROKER_USER` and `RELAY_API_BROKER_PASSWORD` to a login of the API's
own. That login needs read access to the three `detection/camera/` topics at QoS 1, and a broker that keeps
a persistent session, or events published during an API restart are not replayed to it.

## Ports

Every published port is set in `.env`. A port a host service already holds is a clear failure:
`docker compose up` stops with `address already in use`, or `port is already allocated`, naming the
port. Nothing starts on a different port by itself. To find the holder: `sudo ss -lntup | grep :<port>`.

| Setting | Default | If it is taken |
|---|---|---|
| `RELAY_API_PORT` | 8080 | Free to change. Only users use it |
| `RELAY_RTSP_PORT` | 8554 | Free to change. Only readers use it. Nodes' own RTSP port, also 8554, is on the nodes |
| `RELAY_WEBRTC_HTTP_PORT` | 8889 | Free to change |
| `RELAY_WEBRTC_UDP_PORT` | 8189 | Free to change. It is set on both sides, as browsers are told this number |
| `RELAY_BROKER_PUBLISH_PORT` | 1883 | Changeable, then set the same port in every node's portal. Or bind it to one address, below |
| `RELAY_NTP_PORT` | 123 | **Cannot change for nodes**, as `systemd-timesyncd` asks port 123. Stop the host's NTP server or bind to one address, below |

A host service on `127.0.0.1` still clashes with a wildcard bind. The laptop stand-in has mosquitto on
`127.0.0.1:1883`, for example. Since nodes only need the LAN address, these bind one address and leave
the host's service alone:

```bash
RELAY_BROKER_BIND_ADDRESS=192.168.8.10
RELAY_NTP_BIND_ADDRESS=192.168.8.10
```

`RELAY_BIND_ADDRESS` does the same for all the others. Remote users need them on the Tailscale address
as well, so leave it at `0.0.0.0` on a host where they reach it that way.

The default wildcard also publishes 1883 and 123 on the Tailscale interface and any other interface the
host has. **`ufw` and `INPUT` rules do not limit this:** Docker handles published ports ahead of them. To
limit a published port, bind it to one address as above, or put the rule in Docker's `DOCKER-USER` chain.

Change `RELAY_EDGE_SUBNET` and `RELAY_CONTROL_SUBNET` only if `172.31.251.0/24` or `172.31.250.0/29` is in
use on the host. The API refuses a site subnet overlapping either, and any node address inside them.

## Using it

**API.** Every route needs `Authorization: Bearer <RELAY_API_TOKEN>`. With no token, the API refuses
to start. It never runs open.

| Route | Gives |
|---|---|
| `GET /nodes` | Each node's newest heartbeat, the state of the broker and of path syncing, and counts of dropped messages. Reads SQLite only: it never reaches a node or starts a video pull |
| `GET /events?since=&limit=` | Detections and cue acks. `since` is a `seq`, which misses nothing, or an RFC 3339 UTC time. With no `since`, the newest |
| `GET /events/stream` | The same as server-sent events. A reconnect with `Last-Event-ID` resumes |
| `GET /nodes/{node_id}/snapshot/{event_id}.jpg` | The snapshot from the node, by message `event_id` or Frigate's id |
| `/nodes/{node_id}/frigate/{path}` | The node's Frigate API, for API clients, as Frigate's `viewer` role |

Reading `/nodes`: each field is what the node sent, plus a derived `heartbeat_age_s` and `stale`. Both
count from when this API received the heartbeat, never from the node's own stamp, so a fast clock gains no
time. Once `stale` is true, after 90 s, the other values are the node's last words and not its current state.
Heartbeats replayed after an outage count as received when they arrive, so right after one, compare
`timestamp`.
`detector_ok: false` with no `reason` means "not yet confirmed", usually a node that has seen no motion
since Frigate started. It is not a fault. A `503` from a node's Frigate route with
`central_proxy_unavailable` is expected on a node set up with an anonymous broker: it holds no proxy
secret, and its live view stays on its loopback.

**A node's address** comes only from its newest heartbeat, and only if it is a canonical IPv4 host address
inside `RELAY_SITE_SUBNET`. It must not be that subnet's network or broadcast address, in the bundle's own
networks, or `RELAY_CENTRAL_IP`. Anything else is counted as `refused_address` and ignored, and the node keeps
any address it already had. A stamp more than 60 s ahead of this host's clock drops the whole heartbeat, counted
as `future_heartbeat`. A node unseen for 7 days (`RELAY_NODE_RETENTION_S`) is removed with its video path. When
the table is full (`RELAY_MAX_NODES`, 1000), the node silent longest makes room, so a new node always gets in.

**The Frigate UI is not available through this API in v0.** The route serves API clients that send the bearer
token. It has no browser login, and it does not proxy WebSockets, which Frigate's live views need. For live view
use MediaMTX, below, and for events and snapshots use the routes above. To reach a node's Frigate UI for
maintenance, tunnel to it. Port 5000 has Frigate's admin role, so this is only for people who already hold SSH
to the node:

```bash
ssh -J <user>@<this host> -L 5000:127.0.0.1:5000 <user>@<node address>
# then open http://localhost:5000
```

**Events.** SQLite keeps recent events only: the node's local storage is the system of record. A message over
16 KB or nested over 32 levels is refused. `object_update` is thinned first (20,000 kept), start, end and
cue-ack events last longer (200,000), and all of them together are held under 256 MiB (`RELAY_RETAIN_BYTES`),
updates going first. At most 8 event streams can be open (`RELAY_MAX_STREAMS`).

**What is lost when.** A QoS 1 message is acknowledged only after SQLite has it on disk, so a power cut on this
host loses no acknowledged event from the API's store. If the disk is full, nothing is acknowledged: the API
counts `store_full`, retries every 5 s, and the broker sends the message again after a reconnect. While the API
is down, the bundled broker holds its messages up to 100,000 or 64 MiB, and past that it drops new ones, though
nodes already hold their PUBACK. A clean stop of the broker loses nothing. **A kill or power cut of the broker
loses what it queued for the API in the last 30 s**, its autosave interval, and for a node the footage in its own
storage is then all that remains of those events.

**Video.** Readers log in with `RELAY_READ_USER` and `RELAY_READ_PASSWORD`, which are not the login
the nodes hold.

```bash
ffplay -rtsp_transport tcp 'rtsp://viewer:<password>@<host>:8554/<node_id>'
# WebRTC in a browser, with the same login:
#   http://<host>:8889/<node_id>
```

An idle node sends no video. MediaMTX connects when the first reader arrives and closes 10 s after the
last one leaves.

## Remote access

Only this host reaches the nodes, and only it runs Tailscale. Nodes do not.

```bash
sudo tailscale up     # with no --advertise-routes
```

**Advertise no routes.** A subnet route would expose the whole site LAN. Remote users reach the API and
MediaMTX on this host's Tailscale address, with the same logins. To reach a node by SSH, use this host as
a jump host. The node's address is in `GET /nodes`.

```bash
ssh -J <user>@<this host> <user>@<node address>
```

Fusion traffic never depends on Tailscale. Events and video from nodes use the site LAN, so a WAN loss
changes nothing on site.

## Time

Nodes must reach this host on UDP 123. A node that cannot sync sends no heartbeat and holds its events
in memory, so a blocked NTP port looks like a dead node (`../DESIGN.md` 7.5). `chrony` serves the host's
clock at stratum 10 and never sets it, so it needs no clock privilege. Keep the host's own clock right. If
it has internet, its usual time service does that. A wrong clock here shows up as `future_heartbeat` in
`GET /nodes`.

## Changing the broker password

**The broker password also drives the Frigate proxy secret, and is the login MediaMTX pulls video
with, so it changes on this host and on all nodes together.** Until both ends match, a node's Frigate
route returns a `502` saying the secret was refused, its video will not start, and it cannot report.

1. Change `RELAY_BROKER_PASSWORD` in `.env`. Keep it inside what a node's portal accepts: printable ASCII with
   no space, quote, backslash or `$`.
2. Run `docker compose up -d`. This recreates the broker and the API, and the API updates every video path within
   15 s. `COMPOSE_PROFILES=broker` in `.env` is what includes the broker. Without it, add `--profile broker`.
3. Re-provision each node with the new password.

With another broker, change it there first. The API's own login is separate, and is unaffected.
`./setup-credentials.sh --force` replaces every secret at once, and keeps the site subnet, ports, user names and
other settings. All API and video clients then need the new secrets, and the new client id loses whatever the
broker has queued for the API.

## What is not encrypted

On the site's WiFi, these travel in the clear. Anyone who can capture that traffic can read them:

- **MQTT:** the broker login, and every event and heartbeat.
- **Node RTSP:** the video pulled from a node, and its login, sent as Basic auth. That login is the broker's.
  Anyone who captures it can derive the Frigate proxy secret.
- **Reader traffic on the LAN:** RTSP on 8554, WebRTC signalling on 8889 and the API on 8080, with their
  logins, have no TLS either. Over Tailscale they are inside its tunnel.

Frigate traffic is TLS, with a self-signed certificate the API does not verify, as it has nothing to
verify it against. The secret therefore relies on the network path for protection from an active attacker.
Protection against this rests on the site's WiFi security and on node firewalls admitting this host only.

All nodes share one broker login in v0, which the WiFi list above exposes. Whoever holds it can report as any
node, and can claim for it any address the rule above allows, so any host on the site LAN. That node's Frigate
and video requests would go there, with the proxy secret and the broker login.

MediaMTX follows an RTSP redirect from whatever answers on a node's address, and sends the broker login with it.
Neither MediaMTX nor its RTSP library has a setting to stop that (gortsplib v5.6.7 `client.go:1524-1547`, MediaMTX
1.21.3 `internal/staticsources/rtsp/source.go:180`). So `netguard` limits MediaMTX's network namespace. New
connections out may go only to TCP 8554 on `RELAY_SITE_SUBNET`, except `RELAY_CENTRAL_IP`, and WebRTC media may
leave from its own UDP port. Everything else is dropped, so a redirect cannot reach another network, the broker,
the control API or the internet. **A redirect can still reach port 8554 on another host on the site LAN, and
that host then gets the broker login**, which holds for anyone who already has the login. The WebRTC rule cannot
narrow its destination, as browsers' addresses are not known ahead. The Frigate proxy follows no redirects.

If `netguard`'s rules cannot load, `docker compose up` fails and MediaMTX does not start. Docker's own restart at
boot does not wait for that check, so there the API also creates no video path until `netguard` has reported ready,
and with no path MediaMTX has nothing to dial. `paths.guard_ready` in `GET /nodes` shows it. Restarting MediaMTX
keeps the rules. If `netguard` itself is recreated, run `docker compose up -d` so that MediaMTX rejoins it: until
then MediaMTX has no network.

Mosquitto lets any login take over a session by connecting with its client id
(`src/handle_connect.c:96-191` in 2.1.2), and has no setting to stop it. So the API's own login keeps nodes from
reading or writing its session's topics through it, and its client id, generated into `.env`, is what keeps them
from taking the session. With the bundled broker that id travels only on the Docker network, and appears in the
broker's own container log. Protect that log as you protect `.env`: reading it takes Docker access on this host,
and anything shipping logs off this host carries the id with it. With another broker
it crosses the LAN in the clear, and anyone capturing it could then push the API off repeatedly. If so,
`reconnects` climbs in `GET /nodes`, the API backs off up to 30 s between tries, and events wait in nodes' outboxes
or the broker until it is back.

## Testing

```bash
cd api && uv run pytest      # the API, with fakes
tests/test_relay.sh          # the bundle, in real containers
```

`tests/test_relay.sh` needs Docker, ffmpeg, curl, openssl, python3 and uv. It uses its own compose project,
ports that are free at run time on loopback only, its own networks, and a real go2rtc as the node. It touches no
other host or container, and always tears down. It takes about 6 minutes.
