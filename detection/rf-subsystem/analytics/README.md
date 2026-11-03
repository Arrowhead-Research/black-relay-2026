# rf-analytics

`rf-analytics` is the analytics service of the RF subsystem ([`../DESIGN.md`](../DESIGN.md), section 7.3). It consumes normalized observations ([`rf.observation.v0`](../schemas/observation.v0.schema.json)) and produces analytics events. This first phase implements:

| Capability | Design ref | Status |
|---|---|---|
| Ingest from a JSONL file, stdin, or MQTT, with contract validation | 7.3.1 | Done |
| Friendly device exclusion by emitter ID or SSID, with expiry | 7.3.3, CEN-03 | Done (ID-based only) |
| Visit building: confirmation threshold, gap bridging, per-type gaps | 7.3.5, CEN-08 | Done |
| Zone localization: windowed medians, strongest-node or centroid, hysteresis, staleness | 7.3.4, CEN-04 | Done |
| Scoring against `rfsim` ground truth | Phase 3 exit criteria | Done |
| Kismet ingest adapter | 7.3.1 | Not started |
| Alert rules and fusion MQTT output | 7.3.8 | Next |
| Identity linking, pattern of life, co-travelers | 7.3.2, 7.3.5, 7.3.6 | Later |
| Persistence (database), config web UI | 7.1, 7.3.9 | Later |

## Events (`rf.analytics-event.v0`)

These events are internal to the RF subsystem. They feed the alert engine and the scorer. They are **not** the fusion message format.

| Type | When | Key fields |
|---|---|---|
| `visit_start` | An emitter reaches `min_transmissions` distinct transmissions. The timestamp is the first observation. | `randomized_address` |
| `zone_estimate` | The estimated zone changes, or becomes stale (`zone: null`, `reason: stale`) after `stale_after_s` of silence. | `zone`, `confidence`, `method`, `nodes`, `position` (centroid only) |
| `visit_end` | Silence longer than the visit gap, or end of input. | `reason`, `start`, `duration_s`, `transmissions`, `observations`, `nodes`, `dwell_s` (per zone) |

Processing is driven by observation timestamps, not the wall clock, so replays and live feeds behave the same. Events can be up to `settle_s` (1 second) out of order.

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

# Live: subscribe to observations on the central stack's broker (Ctrl+C flushes open visits)
uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --mqtt-host 127.0.0.1 -o events.jsonl
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
| `test_scenarios.py` | End-to-end quality on rfsim scenarios: the ≥80% phase 3 localization target, visit detection, no stable friendly leakage, no dwell by passers-by. |
