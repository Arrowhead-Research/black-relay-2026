# Central relay

The camera subsystem's central side, as containers for any Linux host on amd64 or arm64: a Pi, a
laptop or a server. It assumes nothing else about the host. Design: `../DESIGN.md`, 4.2 and 7.3.

| Service | Does | Listens on (host) |
|---|---|---|
| `api` | The one way in for users: node status, events, and each node's Frigate | TCP 8080 |
| `mediamtx` | One video endpoint for all cameras. Pulls a node's stream only while someone watches | TCP 8554 (RTSP), TCP 8889 and UDP 8189 (WebRTC) |
| `chrony` | NTP for the site, from this host's clock, without touching it | UDP 123 |
| `mosquitto` | The broker, under the `broker` profile, until the shared compute node's broker exists | TCP 1883 |

TAK integration is out of scope.

## Install

Needs Docker with Compose v2.17 or later, and this repository's `relay/` and `bridge/schemas/`.
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

3. In `.env`, set `RELAY_WEBRTC_HOSTS` to the addresses browsers reach this host on, for example
   `192.168.8.10,100.101.102.103`. Without them, WebRTC signalling answers but no media flows.
4. Start it. Compose builds the API and chrony images on this host, for its own architecture.

   ```bash
   docker compose --profile broker up -d --build   # with the bundled broker
   docker compose up -d --build                    # with another broker: see below
   ```

5. In each node's portal, set the broker address to this host's LAN IP, the broker login to
   `RELAY_BROKER_USER` and `RELAY_BROKER_PASSWORD` from `.env`, and leave the time server on its
   default, which follows the broker address.
6. Check it. A node appears within a status interval, about 30 s.

   ```bash
   . ./.env
   curl -H "Authorization: Bearer $RELAY_API_TOKEN" http://localhost:8080/nodes
   ```

To use the shared compute node's broker instead, leave out the profile, and set `RELAY_BROKER_HOST`,
`RELAY_BROKER_PORT`, `RELAY_BROKER_USER` and `RELAY_BROKER_PASSWORD` to it. The API needs read access
to `detection/camera/#`, QoS 1, and a broker that keeps a persistent session for its client id
`relay-api`, or events published during an API restart are not replayed to it.

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
as well, so leave it at `0.0.0.0` on a host where they reach it that way, and use the host's firewall to
limit it if needed. Change the control network's subnet only if `172.31.250.0/29` is in use on the host.

## Using it

**API.** Every route needs `Authorization: Bearer <RELAY_API_TOKEN>`. With no token, the API refuses
to start. It never runs open.

| Route | Gives |
|---|---|
| `GET /nodes` | Each node's newest heartbeat, the broker's state, and counts of dropped messages. Reads SQLite only: it never reaches a node or starts a video pull |
| `GET /events?since=&limit=` | Detections and cue acks. `since` is a `seq`, which misses nothing, or an RFC 3339 UTC time. With no `since`, the newest |
| `GET /events/stream` | The same as server-sent events. A reconnect with `Last-Event-ID` resumes |
| `GET /nodes/{node_id}/snapshot/{event_id}.jpg` | The snapshot from the node, by message `event_id` or Frigate's id |
| `/nodes/{node_id}/frigate/{path}` | The node's Frigate, as Frigate's `viewer` role |

Reading `/nodes`: each field is what the node sent, plus a derived `heartbeat_age_s` and `stale`. Once
`stale` is true, about 90 s, the other values are the node's last words and not its current state.
`detector_ok: false` with no `reason` means "not yet confirmed", usually a node that has seen no motion
since Frigate started. It is not a fault. A `503` from a node's Frigate route with
`central_proxy_unavailable` is expected on a node set up with an anonymous broker: it holds no proxy
secret, and its live view stays on its loopback.

The Frigate route needs a client that sends the bearer token with each request. A browser opening it
directly does not. WebSockets are not proxied, so Frigate's live updates do not arrive through it.

**Events.** SQLite keeps recent events only: the node's local storage is the system of record.
`object_update` is thinned first (20,000 kept), and start, end and cue-ack events last longer (200,000).

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
it has internet, its usual time service does that. A heartbeat stamped more than 5 minutes ahead of this
host's clock is dropped and counted as `future_heartbeat`, so a wrong clock here shows up there.

## Changing the broker password

**The broker password also drives the Frigate proxy secret, and is the login MediaMTX pulls video
with, so it changes on this host and on all nodes together.** Until both ends match, a node's Frigate
route returns a `502` saying the secret was refused, and its video will not start. Change it in `.env`,
run `docker compose up -d`, then re-provision each node. `./setup-credentials.sh --force` replaces every
secret at once, and all API and video clients need the new ones too.

## What is not encrypted

On the site's WiFi, these travel in the clear. Anyone who can capture that traffic can read them:

- **MQTT:** the broker login, and every event and heartbeat.
- **Node RTSP:** the video pulled from a node, and its login, sent as Basic auth. That login is the broker's.
  Anyone who captures it can derive the Frigate proxy secret.
- **Reader traffic on the LAN:** RTSP on 8554 and the API on 8080, with their logins, have no TLS either.
  Over Tailscale they are inside its tunnel.

Frigate traffic is TLS, with a self-signed certificate the API does not verify, as it has nothing to
verify it against. The secret therefore relies on the network path for protection from an active attacker.
Protection against this rests on the site's WiFi security and on node firewalls admitting this host only.

All nodes share one broker login in v0. A node that is compromised can therefore report as another node, and
can claim any address for it, which would send that node's Frigate and video requests there.

## Testing

```bash
cd api && uv run pytest      # the API, with fakes
tests/test_relay.sh          # the bundle, in real containers
```

`tests/test_relay.sh` needs Docker, ffmpeg, curl, openssl, python3 and uv. It uses its own compose project,
ports that are free at run time on loopback only, and a real go2rtc as the node. It touches no other host
or container, and always tears down.
