# rfsim: RF Observation Simulator

`rfsim` generates realistic multi-node RF observations from scripted scenarios, so `rf-analytics` can be developed and tested without WBT node hardware. Its output follows the same normalized observation format that the Kismet ingest adapter will produce ([`../schemas/observation.v0.schema.json`](../schemas/observation.v0.schema.json)). For every run it also writes a **ground truth** file, which records which actor owned each emitter, where that actor was, and for how long. Analytics output can be scored against it.

## What it models

- **Sites:** zones (rectangles) and node positions in a local grid, measured in meters (`sites/`).
- **Actors:** people, vehicles, and fixed installations. They move between timed waypoints over one or more visits.
- **Devices:** kits attached to actors.

  | Kit | Emitters |
  |---|---|
  | `phone` | Wi-Fi client (randomized MAC, probed SSIDs) and BLE advertiser |
  | `smartwatch` | BLE advertiser |
  | `tracker` | BLE tag |
  | `hotspot` | Wi-Fi AP (mobile hotspot) |
  | `access_point` | Fixed Wi-Fi AP |
  | `vehicle` | 4 TPMS sensors at 315 MHz and a Bluetooth Classic head unit |
  | `vehicle_no_bt` | 4 TPMS sensors at 315 MHz |

- **Noise:** log-distance path loss with shadowing, a per-node RSSI offset, packet loss near sensitivity, MAC/BLE address rotation (every 15 minutes by default), and TPMS transmissions only while moving, with a burst when the vehicle starts moving.
- **Background traffic:** random pedestrians and vehicles passing along a road. This is the through-traffic that analytics must *not* alert on.

The radio model is deliberately simple. Its purpose is to reproduce the *kinds* of noise analytics must handle, not to predict RF performance. Calibrate it against real captures once the stand-in or Pi nodes are running.

## Usage

```sh
uv sync                     # add --extra mqtt for MQTT publishing
uv run rfsim summary scenarios/loiterer.yaml
uv run rfsim run scenarios/loiterer.yaml -o out/loiterer.jsonl -t out/loiterer.truth.json

# Live feed for analytics development: stream to the central stack's broker at 60x speed
uv run rfsim run scenarios/repeat-vehicle-co-travelers.yaml \
    --start-now --realtime --speed 60 --mqtt-host 127.0.0.1 -o /dev/null
```

| Option | Effect |
|---|---|
| `-o / --out` | Observation JSONL output. Defaults to stdout. |
| `-t / --truth` | Writes ground truth JSON. |
| `--seed N` | Overrides the scenario seed. Output is fully deterministic for a given seed. |
| `--start-now` | Rebases timestamps so the scenario starts at the current time. |
| `--realtime --speed X` | Paces output to the simulated timestamps, sped up by a factor of X. |
| `--mqtt-host / --mqtt-port / --mqtt-topic` | Also publishes each observation (QoS 1). The default topic is `detection/rf/sim/observations`. |

## Scenarios

| Scenario | Exercises | Expected analytics behavior |
|---|---|---|
| `baseline-friendly` | Team and infrastructure only | No alerts. Every team device is proposed as friendly (learning mode). |
| `passersby` | Heavy through-traffic (negative control) | Visits are built, but no loiter, repeat-visitor, or co-traveler alerts. |
| `loiterer` | One subject loiters at the north entrance, then visits the east lot | Loiter alert. Zone estimates follow the subject from north-entrance to east-lot. |
| `repeat-vehicle-co-travelers` | 24 hours; the same vehicle and occupants visit 3 times | TPMS camera cues, a vehicle entity, a repeat-visitor alert, and a co-traveler group. |

### Scenario format

```yaml
name: example
site: ../sites/dev-site.yaml        # relative to this file
start_time: "2026-11-02T23:00:00Z"  # UTC
duration: "1:00:00"                 # seconds, "MM:SS", or "H:MM:SS"
seed: 1
background:                          # optional through-traffic
  road: { from: [-20, 40], to: [60, 40] }
  pedestrians_per_hour: 10
  vehicles_per_hour: 20
actors:
  - id: subject-1
    role: subject                    # subject | friendly | infrastructure | any label
    friendly: false                  # defaults to true when role is "friendly"
    devices:
      - phone
      - { kit: phone, randomize: false }              # stable MAC
      - { kit: access_point, attributes: { ssid: X } } # override attributes
    path:                            # one visit; use `visits: [[...], [...]]` for several
      - { t: "0:10:00", pos: [-20, 40] }
      - { t: "0:20:00", pos: [20, 27] }
      - { t: end, pos: [20, 27] }    # "end" = scenario duration
```

The actor's position is linearly interpolated between waypoints. Two consecutive waypoints at the same position mean the actor is stationary.

## Tests

```sh
uv run pytest
```

The tests check that every scenario produces schema-valid observations, that output is deterministic, and that the scenario properties analytics will rely on still hold: address rotation, TPMS heard only while moving, loiter dwell, strongest-node agreement with ground truth, and TPMS heard on every vehicle visit.
