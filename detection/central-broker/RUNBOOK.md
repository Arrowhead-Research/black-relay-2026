# Runbook: one central broker, physical first, then RF

Step-by-step for the first two moves in [README.md](README.md): stand up the broker, point the
physical bridge at it, then point the RF publishers at it. Camera and the final clean-up are
later steps and are not covered here.

Placeholders used throughout:

| Placeholder | Meaning | Where it comes from |
|---|---|---|
| `CENTRAL_ADDR` | The central node's IP on the network the physical bridge host and the WBT nodes reach | `rf-subsystem/console/site/NOTES.md` (gitignored, RF lead) |
| `BRIDGE_HOST` | The physical bridge host (Pi 5 or N100) | physical lead |

Nothing here needs TLS. Every listener is password-protected from the first start; TLS is a
later change to the one listener and is noted where it would go.

## 0. Where the broker runs

On the central node: the JOC VM that already runs the RF stack and is where fusion deploys.
Reasons, from the current configs:

- The WBT nodes already reach that VM's LAN address through the site's subnet router for Kismet
  (port 3501) and the console (8080). The broker's port joins that same Headscale rule.
- The physical bridge host's Ethernet is on the Detection network by design
  (`physical-subsystem/README.md`, assembly step 5), so it reaches `CENTRAL_ADDR` directly.
- Fusion runs there too, so it reaches the broker by address and no longer needs the Docker
  network `fusion`.

The broker binds to `CENTRAL_ADDR` only, not `0.0.0.0`, so it is never exposed on any other
interface the VM has.

Before starting:

1. Get `CENTRAL_ADDR` from the RF lead's notes and confirm the bridge host can reach it:
   `nc -zv CENTRAL_ADDR 3501` from `BRIDGE_HOST` should connect (Kismet's port, already open).
2. Claim TCP 1883 on the central host's `PORTS.md`, as the RF Compose overlay asks for every
   published port.
3. Ask whoever holds the Headscale policy to add `1883` to the `tag:sensor-fusion` rule for the
   routed network, so the WBT nodes could reach it later. Not needed for physical or RF central,
   which are not tailnet-only clients, but it is the same change and best done once.

## 1. Stand up the broker

All of this is the camera relay's broker setup (`camera-subsystem/relay`) with three users
instead of two. Create these files in `detection/central-broker/`.

### 1.1 `mosquitto.conf`

```conf
listener 1883
allow_anonymous false
password_file /run/mosquitto-auth/passwd
acl_file /run/mosquitto-auth/aclfile
persistence true
persistence_location /mosquitto/data/
autosave_interval 30
max_packet_size 262144
max_queued_messages 100000
max_queued_bytes 67108864
log_dest stdout
log_type error
log_type warning
log_type notice
```

The limits match the camera relay's. `max_queued_*` is what holds QoS 1 messages for fusion's
persistent session while fusion is down; 64 MiB is well above a day of physical and RF output.
When TLS comes, it is `cafile`/`certfile`/`keyfile` lines on this one listener.

### 1.2 `aclfile`

```text
# Substituted by entrypoint.sh from .env. One publisher per prefix; fusion reads everything.
user @FUSION_USER@
topic read detection/#

user @PHYSICAL_USER@
topic write detection/physical/#

user @RF_USER@
topic write detection/rf/#
topic read detection/rf/sim/#      # simulator observations for rf-analytics

user @CAMERA_USER@                 # all camera kits, one login
topic write detection/camera/#
topic read detection/rf/cues/camera
```

Mosquitto's ACL denies by default: a client with the physical credential cannot publish under
`detection/rf/`, and no publisher can read. The RF console does not subscribe to anything, and
the physical bridge "subscribes to nothing and never reads from the broker" by contract, so
write-only is exact. Camera nodes get their own lines when they move (they also read
`detection/rf/cues/camera`).

### 1.3 `entrypoint.sh`

```sh
#!/bin/sh
# Builds the password and ACL files from .env on a tmpfs. Passwords go in on stdin so they never
# appear in a process list.
set -eu
umask 077
auth=/run/mosquitto-auth

for user in "$BROKER_FUSION_USER" "$BROKER_PHYSICAL_USER" "$BROKER_RF_USER" \
    "$BROKER_CAMERA_USER"; do
    case "$user" in
    *:* | *\&* | *\|* | *\\* | *[[:space:]]*)
        echo "a broker user cannot hold a colon, &, |, backslash or space" >&2
        exit 1
        ;;
    esac
done

printf '%s\n%s\n' "$BROKER_FUSION_PASSWORD" "$BROKER_FUSION_PASSWORD" |
    mosquitto_passwd -c "$auth/passwd" "$BROKER_FUSION_USER" >/dev/null
printf '%s\n%s\n' "$BROKER_PHYSICAL_PASSWORD" "$BROKER_PHYSICAL_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$BROKER_PHYSICAL_USER" >/dev/null
printf '%s\n%s\n' "$BROKER_RF_PASSWORD" "$BROKER_RF_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$BROKER_RF_USER" >/dev/null
sed -e "s|@FUSION_USER@|$BROKER_FUSION_USER|" \
    -e "s|@PHYSICAL_USER@|$BROKER_PHYSICAL_USER|" \
    -e "s|@RF_USER@|$BROKER_RF_USER|" \
    /broker/aclfile >"$auth/aclfile"
chown -R mosquitto:mosquitto "$auth" /mosquitto/data
unset BROKER_FUSION_USER BROKER_FUSION_PASSWORD BROKER_PHYSICAL_USER BROKER_PHYSICAL_PASSWORD \
      BROKER_RF_USER BROKER_RF_PASSWORD

exec mosquitto -c /broker/mosquitto.conf
```

### 1.4 `compose.yaml`

```yaml
name: central-broker

services:
  mosquitto:
    image: eclipse-mosquitto:2.1.2-alpine
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    cap_add: [CHOWN, SETUID, SETGID]
    security_opt: ["no-new-privileges:true"]
    entrypoint: ["/bin/sh", "/broker/entrypoint.sh"]
    environment:
      BROKER_FUSION_USER: ${BROKER_FUSION_USER:?run ./setup-credentials.sh}
      BROKER_FUSION_PASSWORD: ${BROKER_FUSION_PASSWORD:?run ./setup-credentials.sh}
      BROKER_PHYSICAL_USER: ${BROKER_PHYSICAL_USER:?run ./setup-credentials.sh}
      BROKER_PHYSICAL_PASSWORD: ${BROKER_PHYSICAL_PASSWORD:?run ./setup-credentials.sh}
      BROKER_RF_USER: ${BROKER_RF_USER:?run ./setup-credentials.sh}
      BROKER_RF_PASSWORD: ${BROKER_RF_PASSWORD:?run ./setup-credentials.sh}
    mem_limit: 256m
    tmpfs:
      - /tmp
      - /run/mosquitto-auth:mode=0700
      - /mosquitto/log
    volumes:
      - ./mosquitto.conf:/broker/mosquitto.conf:ro
      - ./aclfile:/broker/aclfile:ro
      - ./entrypoint.sh:/broker/entrypoint.sh:ro
      - mosquitto-data:/mosquitto/data
    ports:
      - "${BROKER_BIND_ADDRESS:?set BROKER_BIND_ADDRESS to the central node's Detection address}:1883:1883"
    logging:
      driver: json-file
      options: {max-size: 10m, max-file: "3"}

volumes:
  mosquitto-data:
```

### 1.5 `setup-credentials.sh`

```bash
#!/usr/bin/env bash
# Writes .env with three random broker passwords, mode 0600. Prints no secret.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/.env"
[[ -e "$out" && "${1:-}" != "--force" ]] && { echo "$out exists; use --force to replace it" >&2; exit 1; }
random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
umask 077
cat >"$out" <<EOF
# central-broker credentials. Never commit. Each publisher gets exactly its own line.
BROKER_BIND_ADDRESS=${BROKER_BIND_ADDRESS:-CHANGE-ME}
BROKER_FUSION_USER=fusion
BROKER_FUSION_PASSWORD=$(random 40)
BROKER_PHYSICAL_USER=physical-subsystem
BROKER_PHYSICAL_PASSWORD=$(random 40)
BROKER_RF_USER=rf-console
BROKER_RF_PASSWORD=$(random 40)
EOF
echo "wrote $out; set BROKER_BIND_ADDRESS, then hand each password to its subsystem's lead"
```

Add `.env` to a `.gitignore` in this directory.

### 1.6 Start and check

On the central node, in `detection/central-broker/`:

```sh
chmod +x entrypoint.sh setup-credentials.sh
BROKER_BIND_ADDRESS=CENTRAL_ADDR ./setup-credentials.sh
docker compose up -d
docker compose logs mosquitto          # expect "mosquitto version 2.1.2 running" and "Opening ipv4 listen socket on port 1883"
```

Check authentication and the ACL from the host itself, using the container's own client tools.
Read the fusion password from `.env` into the shell first, so it never sits in a shell history
line:

```sh
set -a; . ./.env; set +a
docker compose exec mosquitto mosquitto_sub -h 127.0.0.1 -u "$BROKER_FUSION_USER" -P "$BROKER_FUSION_PASSWORD" -t 'detection/#' -v
```

In a second shell, publish as the physical user. The first message must appear in the
subscriber; the second must not (the ACL drops it, and the publisher is not told):

```sh
set -a; . ./.env; set +a
docker compose exec mosquitto mosquitto_pub -h 127.0.0.1 -u "$BROKER_PHYSICAL_USER" -P "$BROKER_PHYSICAL_PASSWORD" -t detection/physical/test -m allowed
docker compose exec mosquitto mosquitto_pub -h 127.0.0.1 -u "$BROKER_PHYSICAL_USER" -P "$BROKER_PHYSICAL_PASSWORD" -t detection/rf/test -m denied
```

Anonymous must be refused:

```sh
docker compose exec mosquitto mosquitto_pub -h 127.0.0.1 -t detection/physical/test -m anon   # "Connection error: Connection Refused: not authorised."
```

From `BRIDGE_HOST`, confirm the port is reachable across the network: `nc -zv CENTRAL_ADDR 1883`.

### 1.7 Let fusion see it

So the physical move in section 2 is observable, add the broker to fusion's configuration now.
The two existing entries stay until RF and camera have moved. In fusion's `config.yaml`:

```yaml
brokers:
  # ... existing camera and rf entries unchanged ...
  - id: central
    namespace: central
    host: CENTRAL_ADDR
    port: 1883
    client_id: fusion-central-v1
    subsystems: [physical]            # add rf in section 3, camera later
    physical_node_id: physical-bridge-01
    site_id: site-01
    username_env: FUSION_CENTRAL_MQTT_USER
    password_env: FUSION_CENTRAL_MQTT_PASSWORD
```

Put the fusion credential from `.env` into fusion's `.env` under those two names, restart
fusion, and check `/api/v1/health` lists broker `central` as connected and subscribed.

Note the `subsystems` list: while physical is still also configured on the `rf` entry, fusion
would accept physical messages from both. Remove `physical` from the `rf` entry's `subsystems`
at the same moment the bridge is switched over (section 2.4), so each message has one path.

## 2. Physical: write to the central broker

Everything the bridge needs already exists in its code (`src/br2/outbound/fusion.py` reads
`fusion.username` and `fusion.password_file`, and sets `username_pw_set`). This is
configuration on `BRIDGE_HOST`, in `physical-subsystem/deploy/`.

### 2.1 The secret

```sh
cd detection/physical-subsystem/deploy
umask 077
printf '%s\n' '<BROKER_PHYSICAL_PASSWORD from the central node .env>' > secrets/fusion/fusion_password
sudo chown 10003:10000 secrets/fusion secrets/fusion/fusion_password      # uid of the outbound/fusion image, as gen-certs.sh does for the others
chmod 0400 secrets/fusion/fusion_password
```

`secrets/` is already gitignored and already holds `mqtt_password` and `control4_token`
with the same handling.

### 2.2 `config/config.yaml`, the `fusion` block

```yaml
fusion:
  enabled: true
  host: CENTRAL_ADDR
  port: 1883
  tls: null                         # TLS is a later change on the broker; then: {ca_file: /certs/central-ca.crt}
  username: physical-subsystem
  password_file: /run/secrets/fusion_password
  topic_prefix: detection/physical
  client_id: physical-subsystem
  status_interval_s: 60
  metrics_port: 9101
```

The bridge logs `fusion: username is set but TLS is off` at startup. That is expected until
the broker has TLS; the credential crosses the Detection network in clear, which is the same
exposure the camera relay's broker has today.

### 2.3 `docker-compose.yml`, the `fusion` service

Nothing to edit any more: the `fusion` service already has its own outbound network (`fusion-out`) instead
of the external `fusion` network, and mounts `./secrets/fusion` at `/run/secrets`.

The egress firewall needs no change: `nftables/blackrelay-egress.nft` fences uids 10001, 10002
and 10004 only; uid 10003 (outbound and fusion) is unrestricted, which is what carries the
REST API today.

### 2.4 Switch over

```sh
docker compose --profile fusion up -d                  # recreates only the fusion container
docker compose ps fusion                               # healthy after start_period (20 s)
docker compose logs fusion | grep -E 'fusion publisher up|connected|refused'
```

Expected log lines: `fusion publisher up: CENTRAL_ADDR:1883 prefix=detection/physical site=...`
then `fusion: connected to CENTRAL_ADDR:1883`. A wrong password shows as a connect failure with
reason code 5 and the healthcheck going unhealthy once the backlog ages.

At the same time, remove `physical` from the `rf` broker entry's `subsystems` in fusion's
configuration and restart fusion, so the physical topics are consumed from `central` only.

### 2.5 Confirm end to end

On the central node, the subscriber from 1.6 shows `detection/physical/status/panel` and one
`detection/physical/status/zones` per zone within 60 seconds. Then, from the panel or the
harness simulator, trip a sensor:

```sh
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" 'http://127.0.0.1:8090/api/v1/events?type=physical.event' | python3 -m json.tool
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" http://127.0.0.1:8090/api/v1/health | python3 -c 'import json,sys; h=json.load(sys.stdin); print([(b["id"], b["connected"], b["counts"]) for b in h["brokers"]])'
```

The `central` broker's `accepted` count rises; the `rf` entry's physical counts stop.

Outage test, worth doing once: `docker compose stop mosquitto` on the central node, trip a
sensor, `docker compose start mosquitto`. The bridge's outbox replays the event with its
original `id` and `timestamp`, and fusion stores it once.

### 2.6 Documentation

`physical-subsystem/README.md` configuration table: `fusion.host` default and the "Team broker
on the `fusion` Docker network" wording. `MQTT-CONTRACT.md` section 1: the broker row, and the
transport row loses "anonymous". Section 10's "broker auth plan not decided" item closes.

## 3. RF: write to the central broker

This is the one step with a code change: neither RF publisher can authenticate today. Both
create their paho client with only a client id (`console/publish.py` `LiveClient`,
`analytics/src/rfanalytics/outbox.py` `MqttPublisher`). The change is small and the console's
contract tests cover the publish path, so it can be made and unit-tested without the live
system; the live cut-over below is for the RF lead.

### 3.1 Code: optional username and password on both publishers

`console/publish.py`:

```python
class LiveClient:
    def __init__(self, host: str, port: int = 1883, client_id: str = "rf-console-live",
                 username: str | None = None, password: str | None = None) -> None:
        ...
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        ...
```

```python
class Publisher:
    @classmethod
    def mqtt(cls, host: str, port: int, outbox_path: Path, topic_prefix: str = TOPIC_PREFIX,
             username: str | None = None, password: str | None = None) -> "Publisher":
        from rfanalytics.outbox import MqttPublisher, Outbox
        return cls(LiveClient(host, port, username=username, password=password),
                   MqttPublisher(Outbox(outbox_path), host, port, client_id="rf-console-out",
                                 username=username, password=password),
                   topic_prefix)
```

`analytics/src/rfanalytics/outbox.py`, the same two keyword arguments on `MqttPublisher.__init__`
and the same `if username: self.client.username_pw_set(username, password)` before
`connect_async`.

`console/server.py`, where the publisher is built (near line 1632):

```python
publisher = Publisher.mqtt(env("MQTT_HOST"), int(env("MQTT_PORT", 1883)), data_dir / "rf-outbox.jsonl",
                           env("TOPIC_PREFIX", TOPIC_PREFIX),
                           username=env("MQTT_USER"), password=env("MQTT_PASSWORD"))
```

`env()` resolves `CONSOLE_MQTT_USER` and `CONSOLE_MQTT_PASSWORD`. Add the two names to the
docstring's environment list next to `CONSOLE_MQTT_HOST`.

`analytics/src/rfanalytics/cli.py`: add `--publish-user` and `--publish-password-env` to the
`run`, `kismet` and `live` argument groups beside `--publish-host`, and in `_MessageSink`:

```python
password = os.environ.get(args.publish_password_env) if args.publish_password_env else None
self.publisher = MqttPublisher(outbox, args.publish_host, args.publish_port,
                               username=args.publish_user, password=password)
```

Password by environment-variable name, never by value, matches how the physical bridge and
the RF CLI's `--password-env` already work.

Unit tests, from `detection/rf-subsystem` (the container form from `AGENTS.md`, if `uv` is
not installed):

```sh
docker run --rm -v "$PWD":/src:ro ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim sh -c 'cp -r /src /work && cd /work/analytics && uv sync -q --frozen --extra dev --extra mqtt --extra console && uv run --frozen pytest -q && uv run --frozen pytest -q -p no:cacheprovider ../console/tests'
```

Add one test to `console/tests/test_publish.py` that constructs `LiveClient` with a username
against a fake client and asserts `username_pw_set` was called, so the path is covered.

### 3.2 Configuration on the central node

`rf-subsystem/central/docker-compose.yml`, `rf-console` environment, two new lines:

```yaml
      CONSOLE_MQTT_USER: ${CONSOLE_MQTT_USER:-}
      CONSOLE_MQTT_PASSWORD: ${CONSOLE_MQTT_PASSWORD:-}
```

and the same pair on `rf-analytics` as `--publish-user` / `--publish-password-env` in its
command, if that profile is run.

`central/.env`:

```sh
CONSOLE_MQTT_HOST=CENTRAL_ADDR
CONSOLE_MQTT_USER=rf-console
CONSOLE_MQTT_PASSWORD=<BROKER_RF_PASSWORD from the central-broker .env>
```

The console container reaches `CENTRAL_ADDR` through the host like any other outbound address.
`.env` is already gitignored and already holds the Kismet and console passwords.

### 3.3 Cut over, for the RF lead

1. Add `rf` to fusion's `central` broker entry `subsystems` and remove `rf` from the old `rf`
   entry; restart fusion.
2. `docker compose up -d rf-console` (and `--profile analytics` if used). Console logs:
   `console: publishing positions and node status to CENTRAL_ADDR`.
3. The subscriber from 1.6 shows `detection/rf/status/nodes` within 60 seconds and
   `detection/rf/positions` as soon as a device has readings.
4. Fusion's `/api/v1/health` shows the `central` broker's `accepted` count rising and `nodes`
   listing the four WBT nodes with `subsystem: rf`.
5. Once camera has also moved and fusion has no entry pointing at `rf-mqtt`: remove the `mqtt`
   service and `mqtt-data` volume from `central/docker-compose.yml`, the `mqtt` block from
   `docker-compose.fusion.yml`, and `COMPOSE_FILE` no longer needs the overlay for the broker's
   sake (it still keeps Kismet's 2501 on loopback).

### 3.4 Documentation

`rf-subsystem/MQTT-CONTRACT.md` section 1: broker, address and authentication rows, plus a
change-log row. `central/README.md` *Shared fusion host*: the `rf-mqtt` sentence. `AGENTS.md`
current state: the "verified from the `fusion` network as `rf-mqtt`" note.

## What this leaves

- Camera nodes and the relay API still use the camera relay's broker. Moving them is the next
  step in `README.md` and needs a node onboarding.
- Fusion still carries the old `camera` and `rf` entries until then; the `central` entry
  carries physical and RF.
- TLS on the broker's listener, and `fusion.tls` / a CA file on each publisher, is one change
  once a CA exists for the Detection network.
