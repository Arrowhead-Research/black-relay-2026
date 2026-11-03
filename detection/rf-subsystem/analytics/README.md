# rf-analytics

`rf-analytics` is the analytics service of the RF subsystem ([`../DESIGN.md`](../DESIGN.md), section 7.3). It consumes normalized observations ([`rf.observation.v0`](../schemas/observation.v0.schema.json)), produces analytics events, and turns them into alerts, camera cues and node status messages for the fusion service. Implemented so far:

| Capability | Design ref | Status |
|---|---|---|
| Ingest from a JSONL file, stdin, or MQTT, with contract validation | 7.3.1 | Done |
| Friendly device exclusion by emitter ID or SSID, with expiry | 7.3.3, CEN-03 | Done (ID-based only) |
| Visit building: confirmation threshold, gap bridging, per-type gaps | 7.3.5, CEN-08 | Done |
| Zone localization: windowed medians, strongest-node or centroid, hysteresis, staleness | 7.3.4, CEN-04 | Done |
| Scoring against `rfsim` ground truth | Phase 3 exit criteria | Done |
| Kismet ingest adapter | 7.3.1 | Not started |
| Alert rules: loiter, new device, repeat visitor, watchlist, node offline | 7.3.8, CEN-09 | Done |
| TPMS camera cues | 7.3.7, CEN-07 | Done |
| MQTT output with a durable outbox (replayed after a broker outage or restart) | 7.3.8, CEN-13 | Done |
| Fusion message format | 9.3 | Draft; waiting on the fusion standard |
| Alert configuration pushed from TAK | 7.3.10, CEN-11 | Waiting on the TAK team |
| Identity linking, pattern of life, co-travelers | 7.3.2, 7.3.5, 7.3.6 | Later |
| Persistence (database), config web UI | 7.1, 7.3.9 | Later |

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
| `test_outbound.py` | The wire envelope and topics; the outbox acknowledging, surviving a restart, and resending after a disconnect. |
| `test_scenarios.py` | End-to-end quality on rfsim scenarios: the ≥80% phase 3 localization target, visit detection, no stable friendly leakage, no dwell by passers-by, no alerts in quiet scenarios, loiter and repeat-visitor alerts only for subjects, and a camera cue on every subject arrival. |
