# Central broker

One MQTT broker on the central node that every detection subsystem publishes to and the fusion
service reads from. This directory will hold its configuration: a Compose file, the mosquitto
configuration, an access-control template, a credentials script and this README. Nothing else
runs here; the broker carries events and status, never video.

Status: the broker files exist and start under Compose; nothing is deployed yet. See
[RUNBOOK.md](RUNBOOK.md) for the step-by-step on the central node, the physical move and the RF move.

## 1. Rationale

Today there are two brokers and the physical subsystem cannot reach either from where its
design puts it.

- The camera relay bundle ships its own mosquitto (`camera-relay-service`), authenticated with
  a password file and ACL, published on the site LAN. Camera nodes publish there. The camera
  design itself calls this a stopgap: "the broker itself is the shared compute node's; the camera
  subsystem is a publisher and provides the relay config, not the broker software."
- The RF stack ships another mosquitto (`rf-subsystem/central`), anonymous, reachable only on the
  Docker network `fusion` as `rf-mqtt`. The RF console publishes there, and the physical bridge
  is configured to publish there too (`fusion.host: rf-mqtt`).
- The physical bridge's recommended topology is a separate host (Pi 5 or N100) whose Wi-Fi joins
  the panel's access point and whose Ethernet goes to the Detection VLAN "as the link to the
  team broker" (`physical-subsystem/README.md`, assembly step 5). A separate host cannot resolve
  a Docker network alias, and the RF Compose overlay removes the broker's host port
  (`docker-compose.fusion.yml`, `ports: !reset []`). As shipped, physical only works if its
  entire stack runs on the RF host, which defeats its isolation design.
- Fusion subscribes to both brokers with different credentials and client ids, and the
  architecture chart needs two hexagons to explain why.

Both RF and physical contracts already flag the anonymous plaintext broker as acceptable only
because it is confined to a Docker network. Moving the broker onto the VLAN, which physical
needs, means authentication has to come with it.

A single broker with one address on the Detection VLAN, one authentication model, one ACL and one
owner removes all of this. Every subsystem already supports what it needs except the RF
publishers, which lack authentication (section 4). Fusion's configuration already supports
several brokers at once, so the migration can run old and new side by side.

The broker lives in its own directory rather than inside `fusion-service` because fusion is one
consumer of it, not its owner. Tying the broker to fusion's Compose file would make a fusion
rebuild an outage for three publishers, and would put every other lead's credential changes
through fusion's code review.

## 2. Plan

Five steps, each shippable and reversible on its own. Fusion's multi-broker configuration is the
migration tool: it can subscribe to the old and new brokers at the same time, and its
deduplication is keyed per broker namespace, so nothing is counted twice.

1. **Stand up the broker.** Mosquitto in this directory, one listener on the central node's
   Detection VLAN address with a password file and ACL, derived from the camera relay's existing
   `mosquitto.conf`, `aclfile` and `setup-credentials.sh`. Optionally a second listener on a
   Docker network for same-host services. Fusion adds it as a third broker entry.
2. **Move physical.** Point the bridge's `fusion.host` at the VLAN address with a username and
   password file. Its `fusion` container drops the external Docker network. This is the step
   that fixes the Ethernet topology.
3. **Move RF.** Add username and password support to the console's and rf-analytics' publishers,
   point them at the new broker, then remove the RF stack's mosquitto and the `rf-mqtt` alias.
4. **Move camera.** At the next node onboarding, give nodes the new address and credential;
   point the relay API's `RELAY_BROKER_HOST` at it; leave the relay's `broker` Compose profile
   off.
5. **Retire the old entries.** Remove the camera and `rf-mqtt` broker entries from fusion's
   configuration. Update `FUSION-ARCHITECTURE.md` to one broker.

Ownership of this directory is for the team lead to assign. The fusion co-leads are the natural
operators since fusion is the only subscriber, but every publisher's lead needs to be able to
change their own credential and ACL line by pull request here.

## 3. What goes in this directory

| File | Purpose | Source |
|---|---|---|
| `compose.yaml` | One `mosquitto` service, hardened as the camera relay's is, publishing the VLAN listener | `camera-relay-service/compose.yaml`, `mosquitto` service |
| `mosquitto.conf` | `allow_anonymous false`, password file, ACL, persistence, queue and message size limits | `camera-relay-service/mosquitto/mosquitto.conf` |
| `aclfile` | Per-publisher write on its own prefix; fusion read on `detection/#` | `camera-relay-service/mosquitto/aclfile`, extended |
| `setup-credentials.sh` | Writes `.env` with one random password per client | `camera-relay-service/setup-credentials.sh` |
| `entrypoint.sh` | Builds the password and ACL files from `.env` inside the container at each start | `camera-relay-service/mosquitto/entrypoint.sh` |
| `.env.example` | Credential names, bind address, port | new |
| `PORTS.md` | The one published port, claimed as the RF overlay asks | new |

ACL shape:

```text
user fusion
topic read detection/#

user physical-subsystem
topic write detection/physical/#

user rf-console
topic write detection/rf/#
topic read detection/rf/sim/#      # simulator observations for rf-analytics

user relay                     # all camera kits share one login: their Frigate proxy secret derives from it
topic write detection/camera/#
topic read detection/rf/cues/camera
```

Client ids stay as each subsystem already uses them: `fusion-*`, `physical-subsystem`,
`rf-console-live`, `rf-console-out`, `rfanalytics-out`, and the camera bridges' node ids. They
must be unique on the broker; the fusion configuration already enforces this for its own.

## 4. Changes per system

### fusion-service

Configuration only.

- `config.yaml`: add a broker entry for the central broker with `subsystems: [camera, rf,
  physical]`, `username_env`/`password_env` for the fusion credential, and the VLAN host. During
  migration keep the two existing entries; each broker has its own `namespace`, so dedupe is
  per source.
- `.env.example` and `compose.yaml`: the new credential variables.
- `compose.fusion.yaml`: no longer needed once `rf-mqtt` is gone; fusion reaches the broker by
  address.
- `FUSION-ARCHITECTURE.md` and the README's deployment facts: one broker.

### physical-subsystem

Configuration only. The publisher already supports username, password file and TLS
(`src/br2/outbound/fusion.py`).

- `deploy/config/config.yaml`: `fusion.host` becomes the central node's VLAN address;
  `fusion.username: physical-subsystem`; `fusion.password_file` pointing at a mounted secret.
- `deploy/docker-compose.yml`: the `fusion` service drops `networks: [fusion]` and mounts the
  password secret; the external `fusion` network declaration goes away.
- `MQTT-CONTRACT.md` section 1: broker address and transport updated; the "plaintext because the
  team broker is plaintext today" note is retired.
- `scripts/bench.sh` is unaffected; its local team broker stays plaintext for the bench.

### rf-subsystem

One code change plus configuration.

- `console/publish.py` and `analytics/src/rfanalytics/outbox.py`: the paho clients are created
  with no authentication. Add `username_pw_set` from environment variables (for example
  `MQTT_USER` / `MQTT_PASSWORD`, alongside the existing `MQTT_HOST` / `MQTT_PORT`), and an
  optional CA file for TLS later. About ten lines in each.
- `central/docker-compose.yml`: `CONSOLE_MQTT_HOST` points at the VLAN address; the `mqtt`
  service and `mqtt-data` volume are removed once step 3 completes.
- `central/docker-compose.fusion.yml`: the `mqtt` network and alias block is removed; the
  Kismet port override stays.
- `MQTT-CONTRACT.md` section 1: broker row and the "not acceptable for a field deployment" note
  updated. Section 10's open item on broker auth closes.
- Kismet itself is untouched; nodes still reach it over the tailnet.

### camera-subsystem

Configuration only. The relay's bundled broker is already optional (`profiles: [broker]`) and
the relay API reads `RELAY_BROKER_HOST` and `RELAY_BROKER_PORT`.

- `camera-relay-service/.env`: `RELAY_BROKER_HOST` set to the central broker; the `broker` profile not started.
- `camera-relay-service/compose.yaml`: no change to files; the `mosquitto` service, its ACL and
  `setup-credentials.sh` become the template this directory copies and can be removed from the
  relay afterwards.
- Node onboarding (`provision` portal, `bridge.yaml`'s `broker:` block): the central broker's
  address and the per-node credential. Existing nodes are re-pointed at their next onboarding;
  the bridge code does not change.
- `DESIGN.md` section 4.2: the "until that broker exists" sentence is retired.

### Not changed

- Video: MediaMTX keeps pulling node streams directly; it never used the broker.
- Kismet remote capture: Tailscale to the Kismet server, unrelated to MQTT.
- Message contracts: topics, schemas, QoS and client ids are unchanged. Only the address and
  the credential move.

## 5. Open questions

- TLS on the VLAN listener from day one, or password-only first with TLS as a follow-up. Both
  physical and fusion support TLS now; RF would gain it with the same change that adds
  passwords. The camera relay's broker is password-only today.
- Whether the camera nodes' `readwrite detection/#` ACL line should narrow to write on
  `detection/camera/#` plus read on `detection/rf/cues/camera`, now that other subsystems share
  the broker.
- Where the broker's persistence volume and logs live on the central node, and who rotates the
  credentials.
