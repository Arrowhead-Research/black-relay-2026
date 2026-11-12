# RF subsystem MQTT contract

What the RF subsystem publishes to MQTT, and what a subscriber (the fusion service) can rely on. It describes the implementation on `detection/wifi-dev` as of the date below; any change to topics, payloads or delivery is made here in the same commit.

*Version: draft v0. Last updated 2026-11-11.*

**Status:** draft. The fusion team has not published its message standard yet. When it does, only the wire adapter (`analytics/src/rfanalytics/formats.py`) and the schemas in [`schemas/`](schemas/) change; this document will be updated with them. Until then, the formats below are stable: changes follow the versioning rules in section 8.

## 1. Broker

| | Value |
|---|---|
| Broker | Eclipse Mosquitto 2.0.22, service `mqtt` of the `rf-central` stack ([`central/docker-compose.yml`](central/docker-compose.yml)) |
| Address on the shared host | `rf-mqtt:1883` on the Docker network `fusion` of the host that runs the central stack (join that network to subscribe). The port is not published on the host, so subscribers must run on that host. |
| Protocol | MQTT 3.1.1 / 5, plain TCP |
| Authentication | None (anonymous), no TLS. Access is limited by the network: only containers on `fusion` and the stack itself can reach it. Not acceptable for a field deployment; see section 10. |
| Persistence | On (`mqtt-data` volume): QoS 1 messages queued for a persistent session survive a broker restart |
| Retained messages | None. A new subscriber receives only messages published after it subscribes. |

## 2. Topics

The prefix is `detection/rf` (setting `RF_TOPIC_PREFIX` in `central/.env`). Subscribe to `detection/rf/#` to receive everything.

| Topic | Payload schema | Published by | QoS | When |
|---|---|---|---|---|
| `detection/rf/positions` | [`rf.position.v0`](schemas/position.v0.schema.json) | WBT Console | 0 | A device's position estimate has new readings: at most every 2 s per device, and every 30 s while it moves less than 1 ft |
| `detection/rf/alerts` | [`rf.alert.v0`](schemas/alert.v0.schema.json) | WBT Console (rf-analytics, if run) | 1 | Each alert, once |
| `detection/rf/status/nodes` | [`rf.node-status.v0`](schemas/node-status.v0.schema.json) | WBT Console (rf-analytics, if run) | 1 | A node's state, a capture source's status or a node health problem changes, and every 60 s per node |
| `detection/rf/cues/camera` | `rf.camera-cue.v0` (no schema yet) | rf-analytics only | 1 | TPMS-triggered camera cues. **rf-analytics is not running**; nothing is published here today. |

The topic is the message type: every message on a topic has the same schema. Client IDs used: `rf-console-live` (positions), `rf-console-out` (alerts, node status), `rfanalytics-out` (rf-analytics).

## 3. Common envelope

Every payload is one JSON object (UTF-8), with these fields first:

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Message type and version, e.g. `rf.position.v0`. |
| `id` | string | Unique message id (UUID v4). Use it to drop duplicates (section 6). |
| `timestamp` | string | UTC, RFC 3339 with milliseconds and `Z`, e.g. `2026-11-08T20:55:19.263Z`. Its meaning per type is given below; it is *not* the publish time. |

Common sub-objects:

- `site`: `{"id": "default", "name": "Default"}`: the building (console *site*) the message belongs to. Positions and coordinates are only comparable within one site.
- Device identifiers (`emitter.id`): a lowercase MAC or BLE address (`aa:bb:cc:dd:ee:ff`), or `tpms:` plus 8 hex digits for a tire sensor.

## 4. Payloads

### 4.1 `rf.position.v0` (`detection/rf/positions`)

One device's estimated position. `timestamp` = time of the newest reading the estimate uses.

| Field | Type | Meaning |
|---|---|---|
| `site` | object | See section 3. |
| `emitter.id` | string | Device identifier. |
| `emitter.type` | string | `wifi_ap`, `wifi_client`, `ble`, `bt_classic` or `tpms`. |
| `emitter.band` | string | `wifi`, `bt` or `tpms`. |
| `emitter.name` | string or null | SSID, Bluetooth name or TPMS model, when known. |
| `emitter.manufacturer` | string or null | From the address, when known. |
| `emitter.randomized` | boolean | The address is randomized (most phones): it changes every few minutes, so the same physical device appears under new ids. |
| `emitter.mark` | `friendly`, `priority` or null | The operator's mark. `friendly` = the team's own device. |
| `position.x_ft`, `position.y_ft` | number | Position on the site's floor plan, in feet. The y axis points **down** (screen convention). |
| `position.lat`, `position.lon` | number | WGS84. Present only once the site is geo-referenced (operators enter node GPS points); always both or neither. |
| `position.calibrated` | boolean | `false`: the site's default model is in use and the error is unknown. |
| `position.error_ft` | number or null | Median position error measured at the site's calibration spots (leave-one-out). `null` when not calibrated. Applies to the site, not to this one estimate. |
| `position.error_m` | number or null | `error_ft` in metres combined with the geo-reference fit. Present with `lat`/`lon`. |
| `heard_by` | array of strings | Nodes whose readings placed the device (at least 2). Two nodes fix a curve rather than a point: treat those positions as rough. |

```json
{"schema":"rf.position.v0","id":"f46b523b-dce7-4ecb-994c-f51c37dd9e99","timestamp":"2026-11-08T20:55:19.263Z",
 "site":{"id":"default","name":"default"},
 "emitter":{"id":"9e:cc:b3:ca:16:65","type":"wifi_client","band":"wifi","name":null,"manufacturer":"Randomized","randomized":true,"mark":null},
 "position":{"x_ft":21.56,"y_ft":30.09,"calibrated":false,"error_ft":null},
 "heard_by":["wbt-2","wbt-1","wbt-4","wbt-3"]}
```

Not published: the nodes' own radios, and devices heard by only one node (no position).

### 4.2 `rf.alert.v0` (`detection/rf/alerts`)

One alert. `timestamp` = when the rule fired.

| Field | Type | Meaning |
|---|---|---|
| `rule_id`, `rule_type` | string | Which rule fired (below). |
| `severity` | string | `info`, `low`, `medium`, `high` or `critical`. |
| `site` | object | See section 3 (console alerts). |
| `emitter` | object | For device alerts: `id`, `type`, `band`, `name`, `randomized_address`, `mark` (`friendly`/`priority`/null), `label` (operator's label, or null). |
| `location` | object | For device alerts with a position: `zone` (the alert area's name, or null), `x_ft`, `y_ft`, `nodes`, and `lat`/`lon`/`error_m` when geo-referenced. `confidence` is always null from the console. |
| `context` | object | Rule details, below. |

Rules the console raises:

| `rule_id` | Fires when | Severity | `context` |
|---|---|---|---|
| `area_entry` | A non-friendly device placed by 3+ nodes enters an operator-drawn alert area | medium; high if priority | `{}` (area in `location.zone`) |
| `priority_heard` | A priority-marked device shows up | high | `nodes` |
| `linger` | A device stays in an area past the area's linger time, or past its own linger alarm | medium; high if priority | `dwell_s`; `linger_s` for a device's own alarm |
| `node_offline` | A node's capture stops | high | `node_id`, `sources` |

Per device there is one first alert (`area_entry` or `priority_heard`), then at most one `linger`, then nothing until the device has been unheard for 10 minutes. rf-analytics, if run, publishes other rule types on the same topic with the same envelope (`loiter`, `new_device`, `repeat_visitor`, `watchlist`, `node_offline`); subscribers should accept unknown `rule_id`s.

```json
{"schema":"rf.alert.v0","id":"0b7e6c0e-4f4a-4c9e-9a51-6f1d8f6f2c11","timestamp":"2026-11-08T22:41:07.512Z",
 "rule_id":"area_entry","rule_type":"area_entry","severity":"high","site":{"id":"default","name":"default"},
 "emitter":{"id":"00:1a:11:00:00:77","type":"wifi_client","band":"wifi","name":null,"randomized_address":false,"mark":"priority","label":null},
 "location":{"zone":"Inner","confidence":null,"x_ft":9.1,"y_ft":23.4,"nodes":["wbt-1","wbt-2","wbt-3","wbt-4"]},
 "context":{}}
```

### 4.3 `rf.node-status.v0` (`detection/rf/status/nodes`)

One node's state. `timestamp` = when the status was produced.

| Field | Type | Meaning |
|---|---|---|
| `node_id` | string | `wbt-1` … |
| `state` | string | `online` (all capture sources delivering, no health problem), `degraded` (some sources silent or down, or a health problem), `offline` (no source running, or the node agent silent with no sources). |
| `site` | object | See section 3. |
| `sources` | object | Capture source (`wifi`, `bt`, `tpms`) → `ok`, `silent` (running but delivering nothing for 5 min; not used for TPMS), `stopped` or `error`. |
| `health` | object | From the node's agent, when it reports: `temp_c`, `throttled` (Raspberry Pi `get_throttled` bits), `uptime_s`, `agent_age_s` (seconds since its last report), `problems` (list of strings, e.g. `"undervoltage"`, `"kismet-cap-bt failed"`, `"kismet-cap-bt restarted by watchdog"`: the node restarted a capture unit that delivered no data, listed for 30 min). |
| `last_heard` | string or null | rf-analytics only. |

```json
{"schema":"rf.node-status.v0","id":"5c1d2a7e-93b0-4d6e-8f1a-2b7c9e04d3a5","timestamp":"2026-11-08T22:19:30.004Z","node_id":"wbt-1","state":"online",
 "site":{"id":"default","name":"default"},"sources":{"bt":"ok","tpms":"ok","wifi":"ok"},
 "health":{"temp_c":56.2,"throttled":0,"uptime_s":9984.5,"agent_age_s":8,"problems":[]}}
```

## 5. Ordering and timing

- Messages on one topic from one client arrive in publish order, **except** alerts and node status replayed from the outbox after a broker outage (section 6): those arrive late, with their original `timestamp`. Order by `timestamp`, not by arrival.
- Positions lag real time by a few seconds (readings are read from Kismet's log about 2 s behind, then smoothed).
- A node is reported at least every 60 s. Treat a node with no status for 3 minutes as unknown.

## 6. Delivery guarantees

| Topic | Guarantee | Details |
|---|---|---|
| Positions | At most once, latest wins | QoS 0 on its own connection. While the broker is unreachable, positions are dropped, not queued: an old position is worse than none. |
| Alerts, node status | At least once | Written to a durable outbox on disk before publishing, removed only when the broker acknowledges (QoS 1). After a broker outage or a console restart, unacknowledged messages are re-sent with their original `id` and `timestamp`. **Duplicates are possible: drop repeats by `id`.** |

A subscriber that must not miss alerts while it is down should use a persistent session (clean session off, fixed client id) and subscribe at QoS 1; the broker then queues alerts for it.

## 7. Data handling

- Messages contain identifiers of bystanders' devices (addresses, network names) and, when the site is geo-referenced, the building's coordinates. Treat the feed as sensitive: do not forward it outside the fusion service or store it longer than the exercise requires.
- The nodes' own radios are never published as devices. Devices the team marked `friendly` are published with `mark: "friendly"` so they can be filtered.
- Randomized addresses change every few minutes; the RF subsystem does not link them yet.

## 8. Versioning

- `schema` carries the version (`…v0`). Within a version, changes are **additive only**: new optional fields, new `rule_id`s, new `problems` strings. Subscribers must ignore fields and values they do not know.
- Removing or renaming a field, changing a type or unit, or changing a topic is a breaking change: it gets a new version (`…v1`), announced here first, and v0 is published alongside for an agreed period.
- The JSON Schemas in [`schemas/`](schemas/) are the reference. The RF subsystem's tests validate every message it publishes against them (`console/tests/test_publish.py`, `test_alerting.py`, `test_nodehealth.py`).

## 9. Trying it

From any container on the `fusion` network:

```sh
docker run --rm --network fusion eclipse-mosquitto:2.0.22 \
  mosquitto_sub -h rf-mqtt -t 'detection/rf/#' -v
```

Expect a node status for each node within a minute, and position updates (about 1–2 per second on the first site).

## 10. Open items

| Item | Status |
|---|---|
| Fusion message standard | Waiting on the fusion team; this contract maps onto it when it arrives. |
| Broker authentication and TLS | Not configured (anonymous, network-isolated). Needed before field use: per-client credentials and ACLs (RF publishes on `detection/rf/#`, others subscribe). |
| Shared broker | None on the current central host. If a shared broker replaces ours, only the console's `CONSOLE_MQTT_HOST` changes. Subscribers on other machines need either that, or port 1883 published (with authentication first, see above). |
| Inbound topics (configuration from TAK via fusion) | Not implemented. Draft names from DESIGN.md 9.3: `detection/rf/config/in`, `detection/rf/config/ack`. |
| Camera cues | Produced only by rf-analytics (not running); routing to be agreed with the camera team. |

## Change log

| Date | Change |
|---|---|
| 2026-11-08 | First version: positions, alerts (priority, area entry, linger, node offline), node status with health. |
| 2026-11-09 | Node status `health.problems` may include `"<unit> restarted by watchdog"` (additive). |
| 2026-11-11 | Broker moved with the central stack to the deployment's JOC server; same address on its `fusion` network (`rf-mqtt:1883`). No change to topics or payloads. |
