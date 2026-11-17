# MQTT contract: physical subsystem

**Status: DRAFT v0.** This follows the pattern the RF and camera subsystems use (`detection/<subsystem>/<type>`,
`schema` / `id` / `timestamp` envelope). The fusion team has not published its standard yet. Until it does, everything
here is provisional and may change; all wire formatting lives in one file
(`src/br2/outbound/formats.py`) so that a change is made in one place. The JSON Schemas in `schemas/` are the
reference; the tests validate every published message against them.

Producer: the physical subsystem (Qolsys IQ5 PowerG sensors: glass break, motion, door/window contacts).
Consumer: the fusion service (subscribes). The bridge is publish-only on this connection.

## 1. Broker

| | |
|---|---|
| Broker | team broker on the Docker network `fusion`, e.g. `rf-mqtt:1883` |
| Transport (v0) | plain TCP, anonymous. TLS and username/password are planned; the bridge supports both through config |
| MQTT version | 3.1.1 |
| Client id | `physical-subsystem` (config `fusion.client_id`; must be unique on the broker) |
| Session | clean session. The bridge keeps its own durable outbox instead of relying on broker session state |
| Direction | publish-only. The bridge subscribes to nothing and never reads from the broker |
| Retained | never. No message is published with the retain flag |

Plaintext is used only because the team broker is plaintext today. The bridge logs a warning at startup whenever TLS is
off, and when a username is set without TLS.

## 2. Topics

`<prefix>` is `detection/physical` (config `fusion.topic_prefix`).

| Topic | Schema | QoS | When |
|---|---|---|---|
| `detection/physical/events` | `physical.event.v0` | 1 | One message per sensor event: zone opened/closed, motion, glass break, tamper, low battery, zone added/removed, zone stale/fresh. Within about a second of the event being committed to the bridge's log |
| `detection/physical/status/panel` | `physical.panel-status.v0` | 1 | When bridge/panel health changes, and every 60 s |
| `detection/physical/status/zones` | `physical.zone-status.v0` | 1 | One message per zone, when that zone's state changes, and every 60 s per zone |

The periodic status messages exist so that a subscriber that starts late converges on the current picture within
60 s without retained messages.

## 3. Common envelope

Every payload is one UTF-8 JSON object. `schema`, `id`, `timestamp` are the first three keys.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `physical.event.v0`, `physical.panel-status.v0` or `physical.zone-status.v0` |
| `id` | string | UUID v4, lower case. Stable across re-sends; subscribers dedupe on it |
| `timestamp` | string | UTC RFC 3339 with milliseconds and a literal `Z`, e.g. `2026-01-01T12:00:01.250Z`. The time of the event (or, for status messages, of the observation), not the time of publishing |
| `site` | object | `{"id": string, "name": string}` from bridge config (`site.id` defaults to `default`) |
| `seq` | integer | **Events only.** The bridge's SQLite event-log sequence number of this event: the log's own gap-free counter (starting at 1), the same number the REST/SSE API shows as `seq`. Status messages carry no `seq` |

Consumers must ignore fields they do not know (see section 8).

## 4. Payloads

### 4.1 `physical.event.v0` on `detection/physical/events`

| Field | Type | Meaning |
|---|---|---|
| `seq` | integer | Event-log sequence number (section 3). For correlating with `GET /v1/events` |
| `prev_seq` | integer or null | `seq` of the event message the bridge published immediately before this one; null for the first one after the publisher's start position (section 5) |
| `event_type` | string | `zone_opened`, `zone_closed` (contact zones), `motion_detected`, `motion_cleared`, `glass_break_detected`, `glass_break_cleared`, `zone_active`, `zone_idle` (zones of kind `other`), `tamper`, `tamper_cleared`, `battery_low`, `battery_ok`, `zone_added`, `zone_removed`, `zone_stale`, `zone_fresh`, `zone_renamed` |
| `zone` | object | `id` (string, `zone-<panel zone number>`), `name`, `kind` (`contact`, `motion`, `glass_break`, `other`), `partition` (integer). `name`, `kind`, `partition` and `stale` are the values AS OF THE EVENT (stored in the log row when the event is committed), not the zone's current values: re-reading an old event after a rename or re-classification does not rewrite it |
| `state` | string | State after the event: `open`, `closed`, `active`, `idle`, `unknown`. For `zone_removed`, the last known state |
| `previous_state` | string or null | State before the event; null for tamper, battery, add, remove, stale and fresh events (for tamper, battery, stale, fresh and renamed events, `state` is the zone's state as of the event, recorded in the log row when the event is committed, not the state at the time the message is built; log rows written by an earlier version have no recorded state and fall back to the live state) |
| `stale` | boolean | The bridge cannot currently vouch for this zone (link down, or data not refreshed since a failover). On `zone_stale` / `zone_fresh` it is the new value |
| `source` | string or null | `iq_remote` (primary) or `control4` (fallback); null if unknown |
| `synthetic` | boolean | True when the bridge inferred the change by comparing a full snapshot after a reconnect, rather than seeing it live |
| `tamper` | boolean, optional | Present on `tamper` / `tamper_cleared` |
| `battery_low` | boolean, optional | Present on `battery_low` / `battery_ok`: the new value |
| `previous_name` | string or null, optional | Present on `zone_renamed`: the name before the rename (`zone.name` is the new name). `state` is the zone's current state, `previous_state` is null |

`zone_stale` / `zone_fresh` are real log events: the normalizer records each stale/fresh flip in the SQLite event log
in the same transaction as the state change (`zone.stale` rows), so they have a true timestamp and `seq`, and they
survive restarts. `zone_stale` is written when the link drops (or the normalizer restarts and has only its cached
copy), `zone_fresh` when a source vouches for the zone again. A zone that is stale while the normalizer process is
itself down is not recorded until the normalizer restarts. `synthetic` is true for a flip found while reconciling a
snapshot.

**Keypad zones.** Keypad / IQ Remote zones (the panel lists every paired keypad-type device in its zone table, including
the bridge's own virtual remote, named like "IQ Remote 1") are not sensors and are not published: no event on
`detection/physical/events`, no message on `status/zones`, by default. The bridge setting `zones.include_keypads: true`
publishes them as kind `other`. A zone that is re-typed into a keypad leaves the published view (`zone_removed`).

**Motion delay.** The panel applies its own motion delay (observed once: 310 s). A motion zone that tripped is not
reported idle (`motion_cleared`, `state: idle`) until the delay has elapsed, and a repeat movement inside the window may
produce no new event. The bridge publishes what the panel reports and adds no delay. Do not read `motion_cleared` as
"movement stopped at that moment"; it is "the panel considers the zone idle again".

**Log-only event types.** The bridge's event log (and its REST/SSE/local MQTT streams) also holds event types that
have no team event in v0 and so leave gaps in `seq` (never in the `prev_seq` chain): `zone.kind` (re-classified),
`zone.partition`, `zone.online`, `bridge.link`, and the partition events `partition.added`, `partition.renamed`,
`partition.arming`, `partition.alarm`, `partition.stale`, `partition.removed`. Every log row records the zone (or
partition) AS OF THAT EVENT, with every event type its own change applied in a fixed order, and `zone.added` carries name,
kind, partition, state, tamper, battery and online. Replaying the log from `seq` 1 rebuilds the bridge's cache exactly.
A fallback (Control4) snapshot does not vouch for partitions only the primary reported: they stay and are flagged stale
(`partition.stale`) until the primary's next snapshot.

**Panel facts that are NOT published as events in v0:** sensor online/offline changes (the zone status carries
`online`), partition arming state and alarm state (out of scope for this subsystem; never published anywhere on this
interface), link up/down of the two panel connections (`status/panel` carries `links`), the panel's raw record, signal
strength, and anything about the panel's own AC/battery/tamper state (only zone sensors are modelled). Battery and
tamper are published as events; battery, tamper and online are also in the zone status.

```json
{
  "schema": "physical.event.v0",
  "id": "b790a210-626a-4be1-b6c4-3578648528a2",
  "timestamp": "2026-01-01T12:00:01.250Z",
  "site": {"id": "home-1", "name": "Home"},
  "seq": 1042,
  "prev_seq": 1040,
  "event_type": "zone_opened",
  "zone": {"id": "zone-12", "name": "Front door", "kind": "contact", "partition": 0},
  "state": "open",
  "previous_state": "closed",
  "stale": false,
  "source": "iq_remote",
  "synthetic": false
}
```

A glass break:

```json
{
  "schema": "physical.event.v0",
  "id": "b683bb76-2d78-4014-85f6-e49bda1f4a4f",
  "timestamp": "2026-01-01T12:00:07.800Z",
  "site": {"id": "home-1", "name": "Home"},
  "seq": 1043,
  "prev_seq": 1042,
  "event_type": "glass_break_detected",
  "zone": {"id": "zone-31", "name": "Lounge glass break", "kind": "glass_break", "partition": 1},
  "state": "active",
  "previous_state": "idle",
  "stale": false,
  "source": "iq_remote",
  "synthetic": false
}
```

### 4.2 `physical.panel-status.v0` on `detection/physical/status/panel`

| Field | Type | Meaning |
|---|---|---|
| `state` | string | `online` (primary source live), `degraded` (running on the fallback source), `offline` (no live source, or the bridge's own pipeline is down) |
| `active_source` | string or null | `iq_remote`, `control4`, or null |
| `links` | object | `iq_remote` and `control4`, each `up`, `down` or `disabled` |
| `stale_zones` | integer | Number of zones currently flagged stale |
| `zones` | integer | Number of zones the bridge knows |
| `last_panel_message_at` | string or null | Timestamp of the last message of any kind received from the panel; null if none since start |

```json
{
  "schema": "physical.panel-status.v0",
  "id": "638eab7c-09cc-4460-9b85-e17aa322b528",
  "timestamp": "2026-01-01T12:00:59.000Z",
  "site": {"id": "home-1", "name": "Home"},
  "state": "online",
  "active_source": "iq_remote",
  "links": {"iq_remote": "up", "control4": "disabled"},
  "stale_zones": 0,
  "zones": 14,
  "last_panel_message_at": "2026-01-01T12:00:58.600Z"
}
```

`last_panel_message_at` changes constantly and is not itself a reason to send a message.

### 4.3 `physical.zone-status.v0` on `detection/physical/status/zones`

One message per zone.

| Field | Type | Meaning |
|---|---|---|
| `zone` | object | As in 4.1 |
| `state` | string | Current state: `open`, `closed`, `active`, `idle`, `unknown` |
| `stale` | boolean | As in 4.1 |
| `source` | string or null | As in 4.1 |
| `tamper` | boolean | Tamper currently reported |
| `battery_low` | boolean | Low battery currently reported |
| `online` | boolean | The sensor is currently reporting to the panel |
| `last_changed` | string or null | Timestamp of the last state change the bridge saw for this zone |

```json
{
  "schema": "physical.zone-status.v0",
  "id": "905d0272-7ac3-418c-9ac2-265f71d1dbf5",
  "timestamp": "2026-01-01T12:00:59.000Z",
  "site": {"id": "home-1", "name": "Home"},
  "zone": {"id": "zone-12", "name": "Front door", "kind": "contact", "partition": 0},
  "state": "open",
  "stale": false,
  "source": "iq_remote",
  "tamper": false,
  "battery_low": false,
  "online": true,
  "last_changed": "2026-01-01T12:00:01.250Z"
}
```

## 5. Ordering and timing

- `seq` (events only) is the event log's own sequence number: gap-free in the log, strictly increasing, never reused
  (it survives restarts and retention pruning). Status messages have no `seq`.
- Detecting loss: log rows of the types listed as not published in 4.1 (online/offline, partition arming/alarm, link
  changes) take a `seq` but produce no message, so `seq` alone can skip numbers without any loss. Use `prev_seq`: each
  event message names the `seq` of the event message published before it. If a message's `prev_seq` is not the `seq`
  of the last message you accepted, messages in between were lost (dropped by the outbox caps, section 6, or never
  received); the missing ones can be fetched from `GET /v1/events?after=<seq>` on the bridge's REST API. The chain
  starts at null: the first event after the publisher first started (or after the log was replaced) has
  `prev_seq: null`.
- Order on the wire is best effort. Messages are sent oldest first with up to 50 unacknowledged at a time, and after a
  reconnect unacknowledged messages are sent again, so a subscriber can see duplicates and out-of-order arrival.
  Order events by `seq` (per `site.id`), not by arrival; status messages by `timestamp`.
- `timestamp` comes from the bridge host's clock. The host must keep time (NTP); a host with no valid clock at boot
  can stamp events with a wrong time until it syncs.
- Events appear on the broker normally within about a second of being committed. After a broker outage they arrive
  late, with their original `id`, `timestamp` and `seq`.
- Status: panel status on every change of the fields in 4.2 and at least every 60 s; zone status on every change and at
  least every 60 s per zone. Silence on `status/panel` for more than 120 s should be read as "bridge down".
- The bridge sends no "going offline" message (no last will in v0).

## 6. Delivery guarantees

- Events and status are delivered at least once. Every message is first written to a durable on-disk outbox, then
  published with QoS 1, and removed from the outbox only when the broker's PUBACK arrives.
- After a broker outage, a bridge restart or a crash, unacknowledged messages are sent again with the same `id`,
  `timestamp` and (for events) `seq` (byte-identical). Subscribers dedupe on `id`.
- Events are produced from the bridge's committed event log by log sequence number, with the position saved in the
  outbox in the same write as the messages it covers. A restart resumes from the saved position; nothing committed is
  skipped. The event id is derived from the log row, so a crash between enqueue and save cannot create a duplicate id.
  The `prev_seq` chain is saved in the same write.
- First start with fusion enabled: the position starts at the current end of the log. Events from before fusion was
  turned on are not replayed onto the team broker.
- The outbox is bounded (defaults: 10 MiB, 20 000 messages, 24 h). Over a cap, the oldest messages are dropped, periodic
  status messages before events, and a counter is incremented (`physical_fusion_outbox_dropped_total{reason=size|age}`
  on the Prometheus endpoint, section 11). A dropped event shows up to subscribers as a broken `prev_seq` chain.
- Crash safety: the outbox is an append-only journal with a CRC per record; a torn last record after a crash or power
  loss is discarded on start, and the journal is compacted on start.
- Not guaranteed: exactly-once delivery, delivery of events that occurred while the bridge or its host was down (the
  bridge only knows what the panel reported to it), or any ordering across a reconnect.

## 7. Data handling

- Published: zone id, name, kind, partition number, state, tamper/battery/online flags, source, and timestamps.
  Zone names are whatever the panel or the bridge config says (for example "Bedroom window"); treat them as
  site-identifying.
- Not published (v0): see the list at the end of 4.1.
- Never published: panel user codes, API keys or tokens, TLS keys, pairing material, arming/disarming state or commands,
  partition arming and alarm events, the raw panel record.
- The connection is publish-only. The bridge never subscribes to the team broker, so nothing on the broker can reach
  the bridge or the panel.
- Plaintext in v0 (section 1). Anyone on the `fusion` network can read these messages. Suggested broker ACL when the
  broker gets one: client `physical-subsystem` may write `detection/physical/#` and nothing else.

## 8. Versioning

- The version is part of the schema name (`...v0`). Within v0 changes are additive only: new optional fields, new
  `event_type` values, new status fields. Existing fields are never renamed, removed or retyped.
- Consumers must ignore unknown fields and unknown `event_type` values.
- A breaking change gets a new schema name (`...v1`); the old one is kept until subscribers have moved.
- v0 is draft. When the fusion standard is published the formats may be changed to match it, and this document will
  say so in the change log.

## 9. Trying it

```sh
mosquitto_sub -h rf-mqtt -p 1883 -v -t 'detection/physical/#'
```

Add `| jq -c .` (or `-F '%p' | jq .`) to pretty-print. With nothing happening you should see a panel status and one
zone status per zone within 60 s of the bridge starting.

## 10. Open items

- Fusion standard not published: envelope field names, event vocabulary, `kind` values, the `site` object and `seq`
  semantics (global here, not per topic) may all change.
- Broker auth/TLS plan and the client-id and ACL convention are not decided. The bridge supports TLS (`fusion.tls`) and
  username + password file, but that is untested against the team broker.
- The panel protocol has only been exercised against a simulator, not the real IQ5. Event content is only as correct as
  that integration; see README.
- Sensor-offline is a status field only, not an event. Whether fusion wants it (and arming data, which is out of
  scope here) as events is open.
- If the outbox directory is lost, the `prev_seq` chain restarts at null and the publisher starts again at the head of
  the log (history is not replayed); `seq` itself never restarts because it is the log's.
- No "bridge offline" last-will message.

## 11. Health and metrics of the standalone `fusion` container

The `fusion` compose service serves `GET /metrics` (Prometheus text) and `GET /healthz` (JSON; 200 healthy, 503
not) on `fusion.metrics_host:fusion.metrics_port` (default `127.0.0.1:9101`, so reachable only inside the container
unless `metrics_host` is changed; `metrics_port: 0` turns it off). The compose healthcheck runs
`python -m br2 healthcheck fusion`. Metrics: `physical_fusion_outbox_depth`, `physical_fusion_outbox_dropped_total{reason}`,
`physical_fusion_last_puback_age_seconds` (-1 = none yet), `physical_fusion_outbox_oldest_age_seconds`,
`physical_fusion_connected`, `physical_fusion_producer_age_seconds`, and the other `physical_fusion_*` counters.
Unhealthy means: the producer loop stopped (`health_max_tick_age_s`, default 30) or the oldest queued message is older
than `health_max_backlog_age_s` (default 300): the broker is unreachable or not acknowledging. In the in-process mode
(inside `outbound`) the same `physical_fusion_*` metrics appear on `api.metrics_port`.

## Change log

| Date | Version | Change |
|---|---|---|
| 2026-11-14 | v0 draft | First version: events, panel status, zone status. |
| 2026-11-14 | v0 draft | `seq` on events is now the SQLite event-log seq (was a bridge-wide counter); added `prev_seq`; removed `log_seq`; status messages no longer carry `seq`; stale/fresh are real log events; added `battery_low` / `battery_ok`; documented unpublished panel facts; section 11 (health and metrics). |
| 2026-11-15 | v0 draft | Documented (no wire change): keypad / IQ Remote zones are not published by default (`zones.include_keypads`); the panel's motion delay (observed 310 s) delays `motion_cleared`; log-only event types (`zone.kind`, `zone.partition`, `partition.*`) and exact log replay; log rows carry the zone as of each event incl. tamper/battery/online. |
| 2026-11-15 | v0 draft | Tamper, battery, stale/fresh and renamed events: `state` is the zone's state as of the event (stored in the log row), not as of message build time. |
