# rf-analytics

`rf-analytics` is the analytics service of the RF subsystem ([`../DESIGN.md`](../DESIGN.md), section 7.3). It consumes normalized observations ([`rf.observation.v0`](../schemas/observation.v0.schema.json)), produces analytics events, and turns them into alerts, camera cues and node status messages for the fusion service. Implemented so far:

| Capability | Design ref | Status |
|---|---|---|
| Ingest from a JSONL file, stdin, or MQTT, with contract validation | 7.3.1 | Done |
| Friendly device exclusion by emitter ID or SSID, with expiry | 7.3.3, CEN-03 | Done (ID-based only) |
| Visit building: confirmation threshold, gap bridging, per-type gaps | 7.3.5, CEN-08 | Done |
| Zone localization: windowed medians, strongest-node or centroid, hysteresis, staleness | 7.3.4, CEN-04 | Done |
| Scoring against `rfsim` ground truth | Phase 3 exit criteria | Done |
| Kismet ingest adapter (polling REST) with per-node RSSI | 7.3.1 | Done; validated on live nodes |
| Live pipeline `rfanalytics live` (Kismet → analytics → alerts → MQTT), compose service | 7.3.1, 7.3.8 | Done; validated on live nodes |
| Alert rules: loiter, new device, repeat visitor, watchlist, node offline | 7.3.8, CEN-09 | Done |
| TPMS camera cues | 7.3.7, CEN-07 | Done |
| MQTT output with a durable outbox (replayed after a broker outage or restart) | 7.3.8, CEN-13 | Done |
| Fusion message format | 9.3 | Draft; waiting on the fusion standard |
| Alert configuration pushed from TAK | 7.3.10, CEN-11 | Waiting on the TAK team |
| Identity linking, pattern of life, co-travelers | 7.3.2, 7.3.5, 7.3.6 | Later |
| Persistence (database), config web UI | 7.1, 7.3.9 | Later |

## Live pipeline: Kismet → alerts → MQTT

`rfanalytics live` is the end-to-end live path in one process: it polls Kismet's REST API, turns devices into `rf.observation.v0` records, runs them through friendly exclusion, the analyzer and the alert rules, and publishes alerts, camera cues and node status over MQTT through the durable outbox. On the central server it runs as the `rf-analytics` compose service (see [`../central/README.md`](../central/README.md#live-analytics-kismet--alerts--mqtt)).

```sh
uv sync --extra mqtt
uv run rfanalytics live --kismet-url http://<kismet-host>:2501 \
    --user "$KISMET_USER" --password-env KISMET_PASSWORD \
    --site config/site.wbt-nodes.example.yaml --rules config/rules.wbt-live.example.yaml \
    --friendly config/friendly.wbt-nodes.example.yaml \
    --publish-host 127.0.0.1 --events-out events.jsonl
```

| Option | Default | Notes |
|---|---|---|
| `--poll` | 5 s | Kismet poll interval. |
| `--backfill-s` | 120 | On start, only devices Kismet heard in the last N seconds (Kismet's relative `last-time`). |
| `--max-age-s` | 30 | Drop observations older than this. Kismet keeps each node's last-heard time per device, which can be minutes older than the device's own; processing those would replay history out of order (for example "node offline" in the past). Counted as `stale`. |
| `--events-out`, `--observations-out`, `--alerts-out` | none | Optional JSONL archives (appended). |
| `--publish-host`, `--topic-prefix`, `--outbox` | none, `detection/rf`, `./rf-outbox.jsonl` | MQTT output, QoS 1, replayed from the outbox after an outage or restart. |
| `--duration` | run forever | Stop after N seconds (tests). |

Time-based rules (loiter, node offline) are also ticked on the wall clock after every poll, so they fire when the site goes quiet. Kismet timestamps are server time: run `live` on (or NTP-synced with) the Kismet host. `SIGTERM` (`docker stop`) flushes open visits and drains the outbox.

### Kismet ingest adapter

`rfanalytics kismet` runs just the adapter: it emits validated `rf.observation.v0` records, one per (emitter, seeing node) whose per-node `last_time` advanced. Use it to inspect or archive what Kismet produces.

```sh
uv run rfanalytics kismet --once --kismet-url http://<kismet-host>:2501 \
  --user "$KISMET_USER" --password-env KISMET_PASSWORD -o observations.jsonl
```

Node IDs are derived from Kismet datasource names (e.g. `wbt-1:wifi` -> `wbt-1`), matching the naming used when starting remote capture helpers. **Do not pass credentials on the command line in shared environments;** prefer `--password-env` or `KISMET_PASSWORD`/`KISMET_USER`.

**Per-node RSSI needs `keep_per_datasource_stats=true` in Kismet** (set in `central/kismet/kismet_site.conf`). With Kismet's default, every per-node signal is empty; the adapter then falls back to the device's aggregate RSSI and sets `attributes.rssi_is_aggregate=true`, which gives every node the same RSSI and makes zone estimates meaningless. RTL433/TPMS mapping is still best-effort until verified with live TPMS decodes.

### Notes from live use

- With per-node RSSI enabled, every observation should carry true per-node signal (`rssi_is_aggregate` absent). Nearby devices typically differ by tens of dB between nodes.
- Each node's own onboard Wi-Fi is heard by the other nodes. Without a friendly list it shows up as a loitering Wi-Fi client; list it in a friendly file (see `config/friendly.wbt-nodes.example.yaml`).
- Alerts from visit-start rules (`watchlist`, `new_device`, `repeat_visitor`) carry `location.zone: null`: they fire on the visit's confirmation, before the first zone estimate.

## Events (`rf.analytics-event.v0`)

These events are internal to the RF subsystem. They feed the alert engine and the scorer. They are **not** the fusion message format.

| Type | When | Key fields |
|---|---|---|
| `first_heard` | The first observation of a new visit, for TPMS only by default. It is not confirmed, so it is used only where speed matters more than noise rejection. | `node_id`, `rssi_dbm`, `zone` (the hearing node's zone) |
| `visit_start` | An emitter reaches `min_transmissions` distinct transmissions. The timestamp is the first observation. | `randomized_address`, `attributes` |
| `zone_estimate` | The estimated zone changes, or becomes stale (`zone: null`, `reason: stale`) after `stale_after_s` of silence. | `zone`, `confidence`, `method`, `nodes`, `position` (centroid only) |
| `visit_end` | Silence longer than the visit gap, or end of input. | `reason`, `start`, `duration_s`, `transmissions`, `observations`, `nodes`, `dwell_s` (per zone) |

Processing is driven by observation timestamps, not the wall clock, so replays and live feeds behave the same. Events can be up to `settle_s` (1 second) out of order.

## Alerts

Alert rules are declarative YAML. See [`config/rules.example.yaml`](config/rules.example.yaml) for an annotated example, and the docstring of `src/rfanalytics/alerts.py` for the full reference.

| Rule type | Fires when | Notes |
|---|---|---|
| `loiter` | An emitter is *heard* in one zone, optionally limited to `zones`, for `min_dwell_s`. | Silence and stale zones don't count as dwell. Keep the threshold below the roughly 15-minute phone address rotation. |
| `new_device` | A never-seen stable emitter starts a visit within the local `hours` and stays for `min_duration_s`. | Nothing fires during the initial `learn_s` baseline. Randomized addresses are excluded by default. |
| `repeat_visitor` | A stable emitter starts `min_visits` visits within `window_s`. | Then cools down for the length of the window. |
| `watchlist` | A listed emitter ID or SSID starts a visit. | |
| `node_offline` | A node sends no observations for `silence_s`. Also publishes `node_status` offline and online messages. | A stand-in until node heartbeats exist. A quiet site can also go silent. |
| `camera_cue` | A `first_heard` event for a TPMS sensor. One cue per `cooldown_s` for the whole site. | The cooldown groups the four sensors of one vehicle. |

Every rule has `id`, `type`, `severity`, `enabled` and `cooldown_s` (per emitter). Most also accept `zones` and `emitter_types` filters.

**Outbound messages** go to the draft topics from DESIGN.md 9.3: `detection/rf/alerts` (`rf.alert.v0`), `detection/rf/cues/camera` (`rf.camera-cue.v0`) and `detection/rf/status/nodes` (`rf.node-status.v0`). `src/rfanalytics/formats.py` is the only module that knows the wire format, so adopting the fusion standard means changing that one file.

**Delivery is durable.** Each message is written to the outbox file (`--outbox`, default `./rf-outbox.jsonl`) before publishing, with QoS 1, and removed only when the broker acknowledges it. Messages queued during a broker outage or a restart are replayed with their original timestamps. This was verified against the development broker.

**Results on simulated data** with `config/rules.example.yaml` and stable friendly IDs registered:

| Scenario | Alerts | Camera cues |
|---|---|---|
| `baseline-friendly` | none | none |
| `passersby` | none | 27, all passing cars |
| `loiterer` | 3 loiter alerts, all for the subject (one per device) | 13, all passing cars |
| `repeat-vehicle-co-travelers` | 9 loiter and 4 repeat-visitor alerts, all for the subject vehicle | 421, including each subject arrival |

**Known limitations:**
- **Alerts are per emitter.** One person with three devices produces three alerts, and one vehicle produces up to five. Grouping them into one alert needs the identity and co-traveler layer (7.3.2, 7.3.6).
- **Camera cue volume.** Cueing on every TPMS reading, as specified, also cues every passing car (about 400 a day on the dev site). Set `zones` on the rule to cue only vehicles heard near the building. Confirm the expected volume with the camera subsystem lead.
- **A cue can be absorbed by the cooldown.** Because the cooldown covers the whole site, a subject vehicle arriving within `cooldown_s` of a passing car shares that car's cue.
- **Engine state is in memory.** The new-device baseline and the repeat-visitor history are lost on restart until persistence is added.

## Usage

```sh
uv sync                                  # add --extra mqtt for MQTT input

# Replay a simulated scenario and score it
(cd ../simulator && uv run rfsim run scenarios/loiterer.yaml -o ../out/loiterer.jsonl -t ../out/loiterer.truth.json)
uv run rfanalytics friendly-from-truth ../out/loiterer.truth.json > ../out/friendly.yaml
uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --friendly ../out/friendly.yaml \
    --input ../out/loiterer.jsonl -o ../out/loiterer.events.jsonl
uv run rfanalytics score --site ../simulator/sites/dev-site.yaml \
    --truth ../out/loiterer.truth.json --events ../out/loiterer.events.jsonl

# With alerting: write outbound messages to a file
uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --input ../out/loiterer.jsonl -o none \
    --rules config/rules.example.yaml --alerts-out ../out/loiterer.alerts.jsonl

# Live: subscribe to observations and publish alerts on the central stack's broker
uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --mqtt-host 127.0.0.1 -o events.jsonl \
    --rules config/rules.example.yaml --publish-host 127.0.0.1
# Watch the output: docker compose -f ../central/docker-compose.yml exec mqtt mosquitto_sub -t 'detection/rf/#' -v
```

`friendly-from-truth` is a testing aid. By default it includes only stable identifiers, which is what operators could realistically register. `--include-randomized` also adds rotating addresses, which operators cannot know in practice.

Configuration defaults are in `AnalyzerConfig` (`src/rfanalytics/analyzer.py`):

| Setting | Default | Notes |
|---|---|---|
| `method` | `strongest` | Or `centroid` (CLI `--method`). |
| `rssi_window_s` | 60 | Window for each node's RSSI median. |
| `stale_after_s` | 60 | Silence before an emitter's zone becomes unknown. |
| `zone_hysteresis` | 2 | Consecutive wins a new zone needs before it replaces the current one. |
| `min_transmissions` | 2 | Transmissions needed to confirm a visit. |
| `visit_gap_s` | 300 s (1800 s for TPMS) | Silence that ends a visit. TPMS bridges a parked vehicle. |

## Current results on simulated data

Default settings, `dev-site`, with stable friendly IDs registered:

| Scenario | Strongest-node accuracy | Centroid accuracy | Subject visits detected |
|---|---|---|---|
| `loiterer` (subject) | **86.7%** | 29.8% | 1/1 |
| `repeat-vehicle-co-travelers` (subjects) | **93.5%** | 1.1% | 4/4 |
| `baseline-friendly` (team in the building) | 57.9% | **95.1%** | — |

**Findings:**
- **The two methods are complementary.** Strongest-node is accurate near the edges of the site but unreliable for emitters between nodes, such as inside the building. Centroid pulls every estimate toward the middle of the node layout, so it handles the building well but almost never reports the edge zones. Strongest-node stays the default because subjects of interest appear at the edges. A hybrid is a candidate improvement.
- **Zones without a node cannot be reported.** The road, for example, is counted as *unreachable* by the scorer rather than as wrong. Covering it would need a node facing the road or a distance model.
- **Phones leak through the friendly list.** Team phones that randomize their addresses cannot be excluded by ID (15–20 leaked emitters per team scenario). This needs fingerprint-based identity linking (7.3.2) or randomization disabled on team devices.
- **Loiter dwell is split by address rotation.** A phone's address changes every 15 minutes, which caps the dwell any single emitter can show. Loiter alerting therefore needs either a per-emitter threshold below the rotation period or identity linking.

## Tests

```sh
uv run pytest
```

| File | Covers |
|---|---|
| `test_contract.py` | The parser accepts exactly what the JSON Schema accepts, including the simulator's output. |
| `test_friendly.py` | ID and SSID matching, expiry, entry validation, YAML round-trip. |
| `test_analyzer.py` | Business rules: noise threshold, gap bridging, TPMS parking, hysteresis, dwell, staleness, exclusion, calibration. |
| `test_alerts.py` | Each rule type's business rules: thresholds, filters, cooldowns, learning period, hours, staleness, node recovery, and config validation. |
| `test_outbound.py` | The wire envelope and topics; the outbox acknowledging, surviving a restart, resending after a disconnect, and not deadlocking when an acknowledgement arrives during a publish (paho-mqtt calls back under its own lock). |
| `test_kismet.py` | Kismet device JSON → observations: node IDs, emitter types, TPMS IDs, aggregate-RSSI fallback, deduplication. |
| `test_live.py` | Per-node RSSI from Kismet into zone estimates and alerts on the wire; wall-clock ticks for silent nodes; `rfanalytics live` against a fake Kismet REST server, including dropping stale per-node entries. |
| `test_scenarios.py` | End-to-end quality on rfsim scenarios: the ≥80% phase 3 localization target, visit detection, no stable friendly leakage, no dwell by passers-by, no alerts in quiet scenarios, loiter and repeat-visitor alerts only for subjects, and a camera cue on every subject arrival. |
