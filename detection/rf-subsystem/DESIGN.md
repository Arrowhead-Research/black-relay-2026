# RF Subsystem Design

| | |
|---|---|
| **Subsystem** | `rf-subsystem` |
| **Owner** | RF Subsystem Lead |
| **Status** | Draft. Open for team review. |
| **Last updated** | 2026-11-02 |
| **Related documents** | [`../README.md`](../README.md), [`../docs/BOM.md`](../docs/BOM.md), [`README.md`](README.md) |

> **Note on certainty.** This document separates three kinds of statement. **Decisions** are settled. **Proposals** are the recommended approach and still need team review. **Open questions** are tracked in [Section 12](#12-open-questions). Anything that depends on another team's standard, such as the fusion message format or the TAK configuration format, is marked **TBD** and should not be built against until that standard is published.

---

## 1. Purpose

The RF subsystem gives the safehouse passive awareness of the radio-frequency emitters around it. These are mostly the phones, wearables, and vehicles of people who come near the site. It answers four questions for operators:

1. **What** emitters are present right now, and what kind of device are they likely to be?
2. **Where** are they relative to the safehouse, at least to the level of a zone?
3. **When** do they appear, how long do they stay, and how often do they come back?
4. **Who travels together:** which emitters keep showing up at the same time?

The subsystem turns these observations into alerts and status messages for the fusion service. The fusion service correlates them with the camera and physical subsystems and forwards them to the TAK server.

## 2. Scope

### 2.1 In scope

- **WBT nodes:** distributed Raspberry Pi 5 collectors for Wi-Fi (2.4, 5, and 6 GHz), Bluetooth and BLE, and TPMS.
- **WBT central service:** the containerized Kismet server plus an analytics service on the safehouse central server.
- Interfaces to the fusion service (MQTT), the camera subsystem (TPMS-triggered capture cues), and the TAK server (alert configuration).
- A web interface for configuring the subsystem.

### 2.2 Out of scope for the current phase

- **HackRF nodes.** Wideband monitoring outside the Wi-Fi, Bluetooth, and TPMS bands is planned for a later phase. See [Section 11](#11-future-work-hackrf-nodes).
- Any transmission, jamming, deauthentication, or other active RF technique. **The subsystem only receives.**
- Precise geolocation. The goal is zone-level estimates, not coordinates.

## 3. Terminology

| Term | Meaning |
|---|---|
| **WBT** | Wi-Fi, Bluetooth, and TPMS: the bands covered in the current phase. |
| **WBT node** | A Raspberry Pi 5 with its attached radios, running Kismet remote capture. |
| **Central service** | The containerized Kismet server and analytics service on the safehouse central server. |
| **Emitter / device** | Any transmitter Kismet observes, identified by a MAC address, a BLE address, or a TPMS sensor ID. |
| **Friendly device** | An emitter known to belong to the team or its infrastructure. It is excluded from alerting and analytics. |
| **Observation** | One detection of an emitter by one node at one time, with its signal strength (RSSI). |
| **Visit** | A continuous period in which an emitter is present, built from observations with gaps filled in. |
| **Co-traveler** | Two or more emitters whose visits overlap more often than chance would explain. |
| **Zone** | A named area around the safehouse, such as `north-entrance`. Location estimates are reported as zones. |

## 4. System Overview

```text
              WBT nodes (x4, Raspberry Pi 5)                    Safehouse central server
 ┌──────────────────────────────────────────┐        ┌───────────────────────────────────────────────┐
 │ Alfa AWUS036AXM ── Wi-Fi 2.4/5/6 GHz     │        │  ┌───────────────┐      ┌──────────────────┐  │
 │ TP-Link UB500   ── Bluetooth / BLE       │        │  │ kismet-server │ ───▶ │  rf-analytics    │  │
 │ NESDR SMArt     ── TPMS (rtl_433)        │        │  │  (container)  │ REST │   (container)    │  │
 │                                          │Tailscale│  └───────────────┘  /WS │ - identity       │  │
 │ kismet_cap_* (remote capture) ───────────┼────────┼─────────▲                │ - exclusion      │  │
 │ node-agent (registration, health) ───────┼────────┼─────────┼──────────────▶ │ - localization   │  │
 └──────────────────────────────────────────┘        │         │                │ - pattern-of-life│  │
                                                     │         │                │ - co-traveler    │  │
                                                     │         │                │ - alerting       │  │
                                                     │         │                │ - config web UI  │  │
                                                     │         │                └──┬────────▲──────┘  │
                                                     └─────────┼───────────────────┼────────┼─────────┘
                                                               │                   │        │
                                               Fusion MQTT ◀───┴── alerts, cues ───┘        │
                                                    │                                       │
                                                    ▼                                       │
                                               TAK server ──── alert configuration (TBD) ───┘
```

**Data flow:**

1. Each WBT node's radios capture traffic locally. Kismet remote-capture helpers stream it over the Tailscale tailnet to the central Kismet server.
2. The Kismet server merges observations from every node, tracks devices, and records which node saw each device and at what signal strength.
3. The analytics service reads from Kismet through its REST and event-bus APIs. It removes friendly devices, enriches and stores observations, and runs localization, pattern-of-life, and co-traveler analysis.
4. When an emitter matches an alert rule, the analytics service publishes an alert to the fusion MQTT broker. TPMS detections also publish a camera cue.
5. The fusion service forwards alerts to TAK. TAK can push alert-rule configuration back to the analytics service.

## 5. Requirements

These requirements come from the original design notes. Each has an ID so it can be traced to implementation and tests. Priority is **M** (must) or **S** (should).

### 5.1 WBT node requirements

| ID | Requirement | Priority |
|---|---|---|
| NODE-01 | The node starts Tailscale and Kismet remote capture automatically on boot, with no operator action. | M |
| NODE-02 | The node recovers by itself from power loss, network loss, and the loss or reconnection of a USB radio. | M |
| NODE-03 | Each node has a unique identifier and a configured location label (zone and placement) that is attached to every observation it reports. | M |
| NODE-04 | The node discovers the central Kismet server without a hard-coded IP address. | M |
| NODE-05 | The node registers its own radio identifiers (MAC and Bluetooth addresses) with the central service so that they are excluded automatically. | M |
| NODE-06 | The node reports health (uptime, attached radios, capture status, temperature) to the central service. | S |
| NODE-07 | The node never transmits on the monitored bands except for its own backhaul link, if it has one. | M |

### 5.2 Central service requirements

| ID | Requirement | Priority |
|---|---|---|
| CEN-01 | Collect and merge data from all WBT nodes. | M |
| CEN-02 | Characterize emitters: device class, manufacturer (OUI), protocol, and behavior. | M |
| CEN-03 | Exclude friendly devices, both registered automatically and added by operators. | M |
| CEN-04 | Estimate an emitter's zone from relative signal strength across nodes. | M |
| CEN-05 | Build pattern-of-life statistics: time of day, visit frequency, dwell or loiter time. | M |
| CEN-06 | Detect co-traveler groups. | S |
| CEN-07 | Cue the camera subsystem when a TPMS sensor is detected. | M |
| CEN-08 | Keep every statistic and inference robust to capture noise (see [Section 8](#8-noise-and-data-quality)). | M |
| CEN-09 | Publish configured alerts to the fusion MQTT broker in the fusion message standard (**TBD**). | M |
| CEN-10 | Host a web interface for configuration: friendly lists, zones, nodes, and alert rules. | M |
| CEN-11 | Accept alert configuration pushed from the TAK server in a standard to be defined (**TBD**). | S |
| CEN-12 | Ship as Docker containers so the service can move between host machines. | M |
| CEN-13 | Keep collecting and buffering if the fusion broker is unavailable. | S |

### 5.3 Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-01 | **Latency:** an alert reaches the fusion broker within 10 seconds of the triggering observation. This is a proposed target. |
| NFR-02 | **Scale:** handle at least 4 nodes and about 2,000 distinct emitters per hour, the expected load at a busy roadside site. These are proposed targets to validate in field testing. |
| NFR-03 | **Survivability:** after a full power cycle, the subsystem returns to collecting without operator action. |
| NFR-04 | **Portability:** the central service runs on any x86_64 or arm64 Linux host with Docker and Tailscale. |
| NFR-05 | **Security:** all node-to-central traffic travels over the tailnet. No subsystem port is exposed outside it. |

## 6. WBT Node Design

### 6.1 Hardware

| Component | Part | Role |
|---|---|---|
| Compute | Raspberry Pi 5, 8 GB | Runs the capture helpers and the node agent. |
| Wi-Fi radio | Alfa AWUS036AXM (MediaTek MT7921AU chipset) | Monitor-mode capture on 2.4, 5, and 6 GHz. |
| Bluetooth radio | TP-Link UB500 Plus (BT 5.3) | Classic Bluetooth inquiry and BLE advertising capture. |
| Sub-GHz SDR | Nooelec NESDR SMArt v5 (RTL-SDR) | TPMS reception through `rtl_433`. |
| USB hub | Powered hub with per-port switches | Supplies power to the radios beyond what the Pi can provide. |
| Cabling | 10 ft USB extension cables | Moves the radios away from the Pi and hub to reduce self-interference and allow better placement. |

**Hardware considerations:**

- **Power budget.** The Pi 5 limits total USB current unless it is powered by the official 27 W supply, and even then a tri-band Wi-Fi adapter under load is close to the limit. **All radios connect through the powered hub.** Brown-outs show up as radios disconnecting at random, and they are hard to diagnose in the field.
- **TPMS frequency.** US vehicles transmit TPMS at **315 MHz**. Most other markets use 433.92 MHz. Set `rtl_433` for the expected market. The NESDR antenna supplied with the radio is adequate at short range, but TPMS signals are weak and intermittent. Expect detection mainly when a vehicle is moving or has just started, and within roughly tens of meters. Validate this in testing.
- **Antenna separation.** Keep at least 30 cm (1 ft) between the Wi-Fi adapter, the Bluetooth adapter, and the SDR. USB 3 ports and cables produce noise around 2.4 GHz. Use USB 2 ports for the Bluetooth adapter and the SDR wherever possible.
- **Thermals.** Use the active cooler included in the Pi 5 kit. Nodes may run sealed or in direct sun.

### 6.2 Software stack

| Layer | Component | Notes |
|---|---|---|
| OS | Raspberry Pi OS Lite (64-bit, Bookworm or later) | Headless. |
| Networking | Tailscale | Joins the tailnet with a pre-authorized, tagged auth key (`tag:rf-node`). |
| Capture | `kismet_cap_linux_wifi`, `kismet_cap_linux_bluetooth`, `kismet_cap_sdr_rtl433` (or `rtl_433` feeding Kismet) | Kismet remote-capture helpers only. The node does not run a full Kismet server. |
| Agent | `rf-node-agent` (to be written) | Registration, self-exclusion, health reporting, and server discovery. |
| Supervision | systemd | One unit per capture source plus the agent. Units restart on failure. |

### 6.3 Boot and resilience (NODE-01, NODE-02)

**Proposal:**

- Each radio gets its own systemd unit, for example `kismet-cap-wifi.service`, `kismet-cap-bt.service`, and `kismet-cap-tpms.service`. A failed radio then restarts on its own without affecting the others.
- Units declare `After=tailscaled.service network-online.target` and use `Restart=always` with a short `RestartSec` that backs off.
- **Stable device names.** udev rules give each radio a persistent name based on USB vendor and product ID or serial, such as `wlan-mon0` or `hci-wbt0`. Without this, the onboard Wi-Fi and the Alfa adapter can swap `wlan0` and `wlan1` between boots.
- A udev `ACTION=="add"` rule restarts the matching capture unit when a radio is reconnected, which covers hub resets and loose cables.
- A hardware watchdog (`dtparam=watchdog=on` with systemd `RuntimeWatchdogSec`) recovers the node if it hangs.
- Use log rotation or volatile journald storage to limit SD-card wear.

### 6.4 Node identity and location labeling (NODE-03)

Each node has a small configuration file at `/etc/rf-node/node.yaml`. It is provisioned once and never contains secrets:

```yaml
node_id: rf-node-01          # Unique. Also used as the Tailscale hostname.
zone: north-entrance         # Must match a zone defined in the central config.
placement:
  description: "NE corner, 2nd floor window, facing road"
  bearing_deg: 45            # Optional. Approximate facing direction of the antennas.
  height_m: 4                # Optional.
  position: { x_m: 12.0, y_m: 3.5 }   # Optional. Position in the safehouse local grid, used for localization.
```

- The node's Kismet data source names carry the node ID, for example `rf-node-01:wifi`. Every observation can then be traced back to a node and its zone.
- **The physical label on each node must match `node_id`.** A mislabeled node quietly corrupts every location estimate.

### 6.5 Central server discovery (NODE-04)

**Proposal:** use **Tailscale MagicDNS**.

- The central server joins the tailnet with a fixed hostname, such as `rf-central`. Nodes connect to `rf-central` (or `rf-central.<tailnet>.ts.net`) and never to an IP address.
- Tailscale ACLs allow `tag:rf-node` to reach only the Kismet remote-capture port and the agent registration endpoint on `tag:rf-central`.
- If the server moves to another host, only that host's Tailscale hostname needs to change. Nodes do not need reconfiguring.
- **Fallback:** `node.yaml` can override the server hostname for testing off the tailnet.

### 6.6 Self-registration and automatic exclusion (NODE-05)

A node can leak its own identifiers into the data in two ways. Its radios may appear as emitters, for example the UB500 if it is ever used actively. And if the node uses Wi-Fi for backhaul, its onboard Wi-Fi interface will be seen by its own and neighboring nodes.

**Proposal:**

1. At startup, and whenever a radio is reconnected, `rf-node-agent` lists the MAC and Bluetooth addresses of every local interface, including the onboard Wi-Fi and Bluetooth.
2. It sends them to the central service at `POST /api/v1/nodes/{node_id}/register`, together with its software version and its attached radios.
3. The central service adds these addresses to the friendly list with `source: node-registration` and a reference to the node. Operators can see them in the web interface but cannot accidentally delete them.
4. The backhaul access point (the safehouse AP) should also be on the friendly list. Add it through the web interface.

### 6.7 Health reporting (NODE-06)

`rf-node-agent` sends a heartbeat to the central service every 30 seconds (proposed). The heartbeat includes uptime, CPU temperature, the state of each radio (attached, capturing, error), and packet counts. The central service treats a node as **degraded** after 3 missed heartbeats and **offline** after 10, and publishes node-status messages to the fusion broker so operators can see coverage gaps.

## 7. Central Service Design

### 7.1 Deployment

The central service is deployed as a Docker Compose stack on the safehouse central server:

| Container | Image | Purpose |
|---|---|---|
| `kismet-server` | Kismet (pinned release) | Receives remote captures, tracks devices, provides the REST and WebSocket APIs. |
| `rf-analytics` | Built in this repository | Identity, exclusion, localization, pattern-of-life, co-traveler, alerting, and the config UI and API. |
| `rf-db` | PostgreSQL (TimescaleDB is optional) or SQLite | Stores observations, visits, and configuration. See [Section 12](#12-open-questions). |

- Tailscale runs on the host, or as a sidecar container. Containers bind only to the tailnet interface or to localhost.
- Configuration lives in a mounted volume. Secrets (Kismet API key, MQTT credentials) come from an `.env` file that is excluded from git.
- **Pin the Kismet version.** The remote-capture protocol and API change between releases. The node capture helpers must run a version compatible with the server.

### 7.2 Kismet server

- Remote capture is enabled. Recent Kismet releases accept remote sources over WebSockets on the main web port (2501). Older releases use the legacy TCP remote-capture port (3501). **Confirm which applies to the pinned release.**
- The analytics service reads through an API key with read-only permissions.
- Kismet's own log (`.kismet` SQLite) is kept as the raw record so data can be reprocessed if the analytics logic changes. Rotate it daily.

### 7.3 Analytics service components

#### 7.3.1 Ingest (CEN-01)

- Subscribe to Kismet's event bus (`/eventbus/events.ws`) for new and updated devices, and poll the device-view APIs for RSSI per data source.
- Normalize each record into an **observation**: `{emitter_id, emitter_type, node_id, timestamp, rssi, channel/frequency, attributes}`.
- Write observations in batches. Apply friendly exclusion **before** storage so friendly data is never kept.

#### 7.3.2 Identity and characterization (CEN-02)

| Emitter type | Identifier | Stability | Characterization |
|---|---|---|---|
| Wi-Fi access point | BSSID | High | SSID, encryption, vendor, channel. Mobile hotspots (phone, vehicle) are of particular interest. |
| Wi-Fi client | MAC | **Low.** Modern phones randomize their MAC addresses. | Vendor OUI (if not randomized), probed SSIDs, and information-element fingerprint. |
| Bluetooth Classic | BD_ADDR | Medium | Device class, name, vendor. |
| BLE | Advertising address | **Low.** Most devices rotate their addresses. | Advertised services, manufacturer data (Apple Continuity, Microsoft, Tile, AirTag, and so on), appearance. |
| TPMS | Sensor ID (32-bit) | **High.** IDs are fixed for the life of the sensor. | Protocol/manufacturer from `rtl_433`, pressure and temperature. Four sensors seen together indicate one vehicle. |

**Proposal:** add a **logical device** layer above raw identifiers.

- Randomized MACs (the locally administered bit is set) are flagged as such and are never treated as long-term identities by themselves.
- Where possible, link short-lived identifiers into one logical device using a fingerprint: Wi-Fi information elements, probe-request SSID lists, BLE manufacturer-data patterns, and timing. Every link carries a **confidence score**. Downstream logic and alerts expose that confidence rather than hiding it.
- Group TPMS sensors that are seen together repeatedly into a **vehicle** entity.
- Classify devices with a simple, transparent rule set first: phone, wearable, vehicle, tracker tag, infrastructure AP, unknown. Consider ML only once rules prove insufficient.

> **Design caution:** pattern-of-life and co-traveler results are only as good as identity persistence. Expect strong persistence for TPMS, Wi-Fi APs, and Bluetooth Classic, and weak persistence for phones' Wi-Fi and BLE. Present findings to operators with that in mind.

#### 7.3.3 Friendly device exclusion (CEN-03)

- Friendly entries can match an exact MAC or BLE address, a TPMS ID, an SSID or BSSID, or (with care) a vendor OUI.
- Entry sources: `node-registration` (automatic), `operator` (web UI), `tak` (pushed config, if supported).
- Each entry has a label, the person or system that added it, an optional expiry, and notes.
- **Learning mode (proposal):** during setup, an operator can run a time-boxed baseline (for example 30 minutes with only the team present). The system then suggests friendly candidates for the operator to confirm. It never adds them automatically.

#### 7.3.4 Localization (CEN-04)

With 4 nodes and indoor or near-building propagation, RSSI-based positioning is coarse. The design commits to **zone-level** output with a confidence value.

**Proposed approach, in order of sophistication:**

1. **Strongest node.** Assign the emitter to the zone of the node with the highest smoothed RSSI. This always works, including with a single node.
2. **Weighted centroid.** When 2 or more nodes see the emitter within a time window, compute a position weighted by RSSI (converted to linear power) from each node's `placement.position`, and map it to the nearest zone.
3. **Calibrated fingerprinting (stretch).** Walk a known reference device through each zone during setup, record the RSSI vector per zone, and classify live observations against those references.

- Smooth RSSI per (emitter, node) with a rolling median or an exponential moving average before using it. A single packet's RSSI is too noisy.
- Correct for per-adapter RSSI offsets using a calibration step at install.
- Report: `zone`, `confidence` (0–1), `nodes_contributing`, and `method`.
- When an emitter goes silent, mark its zone *unknown* rather than keeping the last estimate. TPMS sensors stop transmitting while a vehicle is parked, so keeping the last estimate would hold the zone where the vehicle arrived.

> **Simulation results (2026-11-02, `analytics/README.md`):** strongest-node scores 87–94% on subjects at the edges of the site but only about 58% for devices between nodes. Centroid shows the reverse: about 95% between nodes and below 30% at the edges. A hybrid of the two is a candidate improvement. Zones without a node, such as the road, cannot be reported by either method.

#### 7.3.5 Pattern of life (CEN-05)

- **Visit construction.** Group a logical device's observations into visits. Bridge gaps shorter than a configurable timeout (proposed default 5 minutes) so that brief dropouts do not split a visit.
- Statistics per device:
  - first seen, last seen, total visits
  - time-of-day and day-of-week histograms
  - visit frequency (visits per day or week) and how regular it is
  - dwell time per visit, both total and per zone
  - **loitering:** dwell above a threshold (for example over 10 minutes) in a zone where people normally pass through
- **Baseline and anomaly.** Learn site norms, such as the typical number of emitters per hour, and flag deviations: a new device during unusual hours, a returning device after a long absence, or a device whose visits have become regular.

#### 7.3.6 Co-traveler detection (CEN-06)

- For each pair of logical devices (limited to devices with at least N visits to keep the computation manageable), compute visit overlap. Use a Jaccard index over time buckets, or count co-occurrences within ±T minutes of arrival.
- Mark a pair as co-travelers when the overlap is well above what each device's presence rate would produce by chance. Report support (the number of joint appearances) and lift.
- Combine pairs into groups using connected components with a minimum edge weight.
- Typical value: a phone, a wearable, and a vehicle's TPMS set that arrive together repeatedly are probably one person in one vehicle.

#### 7.3.7 TPMS camera cue (CEN-07)

- When a non-friendly TPMS sensor or vehicle is observed, publish a **camera cue** to the fusion broker with the zone estimate and timestamp.
- Deduplicate cues per vehicle within a cool-down window (proposed 60 seconds).
- **Interface with the camera subsystem:** whether the camera subsystem subscribes to cues directly or the fusion service sends them on is an open question. See [Section 12](#12-open-questions).

#### 7.3.8 Alerting (CEN-09, CEN-13)

- **Alert rules** are declarative and configurable through the web UI or TAK. Examples:
  - a new, never-seen device in zone X between 22:00 and 05:00
  - a device loitering in any zone for more than 10 minutes
  - a known *watchlisted* identifier is present
  - a vehicle (TPMS group) returns more than 3 times in 24 hours
  - a co-traveler group whose member is watchlisted is detected
  - a node is offline (system-health alert)
- Each rule has a severity, a cool-down or deduplication window, and an enabled or disabled state.
- Alerts are published to MQTT with QoS 1. If the broker cannot be reached, alerts go into a durable local outbox and are replayed on reconnect, keeping their original timestamps.

#### 7.3.9 Configuration web interface (CEN-10)

The interface is served only on the tailnet. Its sections are:

- **Nodes:** status, last heartbeat, radios, zone, and the location label.
- **Zones:** definitions and the local grid positions of nodes.
- **Friendly list:** add, edit, expire, import, and export entries.
- **Alert rules:** create, edit, test against recent data, enable, and disable.
- **Live view:** current emitters by zone, recent alerts.
- **Device detail:** history, visits, pattern-of-life charts, co-travelers.

Authentication uses a local admin login at first. Tailscale identity headers are a possible later option.

#### 7.3.10 TAK-pushed configuration (CEN-11)

TAK operators should be able to push alert rules and watchlist or friendly entries to the subsystem. The transport and schema are **TBD** and depend on the TAK team. The likely path is TAK → fusion → MQTT config topic → `rf-analytics`. The analytics service will validate every pushed configuration against the same schema the web UI uses, reject invalid payloads, and send an acknowledgement.

## 8. Noise and Data Quality

CEN-08 requires all inferences to tolerate the noise that comes with passive capture. Specific measures:

| Noise source | Mitigation |
|---|---|
| RSSI fluctuation (multipath, body shadowing, antenna orientation) | Smooth per (emitter, node). Never base location on a single packet. Report confidence. |
| Missed packets and intermittent detection | Bridge gaps when building visits, using a longer gap for TPMS. Require a minimum number of transmissions before confirming a visit. Mark the zone stale after a period of silence. |
| MAC and BLE address randomization | Flag randomized addresses. Link identities by fingerprint with explicit confidence. Exclude low-confidence identities from long-term statistics by default. |
| Duplicate observations across nodes | Deduplicate per emitter per time window, keeping the RSSI from each node. |
| Through-traffic (passing cars, pedestrians) | Use dwell and repeat thresholds so that one-off passes do not trigger pattern alerts. |
| Spurious decodes (corrupt `rtl_433` frames) | Require a valid CRC and repeat detection before accepting a TPMS ID. |
| Clock skew between nodes | Timestamp at the central Kismet server where possible. Nodes run `systemd-timesyncd` over the tailnet. |

Every derived statistic is stored together with the sample size it was computed from, so the UI and alerts can show how much evidence supports it.

## 9. Interfaces

### 9.1 Summary

| Interface | From → To | Transport | Status |
|---|---|---|---|
| Remote capture | WBT node → `kismet-server` | Kismet remote-capture protocol over Tailscale | Defined by Kismet |
| Node registration and heartbeat | `rf-node-agent` → `rf-analytics` | HTTPS/JSON over Tailscale | Proposed below |
| Device data | `kismet-server` → `rf-analytics` | Kismet REST and event-bus WebSocket | Defined by Kismet |
| Alerts, cues, status | `rf-analytics` → fusion MQTT | MQTT, QoS 1 | **Envelope TBD with fusion team** |
| Alert configuration | TAK → `rf-analytics` | **TBD** | **TBD with TAK team** |

### 9.2 Node registration API (proposed)

```http
POST /api/v1/nodes/{node_id}/register
{
  "node_id": "rf-node-01",
  "zone": "north-entrance",
  "agent_version": "0.1.0",
  "radios": [
    {"role": "wifi", "interface": "wlan-mon0", "mac": "00:c0:ca:xx:xx:xx", "model": "AWUS036AXM"},
    {"role": "bt",   "interface": "hci-wbt0",  "address": "xx:xx:xx:xx:xx:xx", "model": "UB500"},
    {"role": "tpms", "serial": "00000001", "model": "NESDR SMArt v5"}
  ],
  "self_identifiers": ["d8:3a:dd:xx:xx:xx", "d8:3a:dd:xx:xx:xy"]
}
```

### 9.3 MQTT topics and message shape (draft, pending the fusion standard)

Proposed topic layout. The alert, cue and node-status topics are implemented in `analytics/src/rfanalytics/formats.py`:

```text
detection/rf/alerts            # Rule-triggered alerts
detection/rf/cues/camera       # TPMS-triggered camera cues
detection/rf/status/nodes      # Node health / coverage
detection/rf/config/in         # Inbound configuration (TAK via fusion)
detection/rf/config/ack        # Configuration acknowledgements
```

Illustrative alert payload. **This is a placeholder and will be replaced by the fusion team's standard:**

```json
{
  "schema": "rf.alert.v0",
  "id": "uuid",
  "timestamp": "2026-11-02T19:45:12Z",
  "rule_id": "loiter-any-zone",
  "severity": "medium",
  "emitter": {
    "logical_id": "dev-7f3a",
    "type": "vehicle",
    "identifiers": ["tpms:0x1A2B3C4D", "tpms:0x1A2B3C4E"],
    "identity_confidence": 0.9
  },
  "location": { "zone": "north-entrance", "confidence": 0.6, "nodes": ["rf-node-01", "rf-node-02"] },
  "context": { "dwell_s": 840, "visits_24h": 3, "co_travelers": ["dev-91bc"] }
}
```

## 10. Security and Data Handling

- **Receive only.** The subsystem never transmits on monitored bands except for its backhaul (NODE-07). Deauthentication, injection, and active probing are prohibited.
- **Network isolation.** All services listen only on the tailnet or on localhost. Tailscale ACLs restrict nodes to the specific ports they need.
- **Secrets.** Tailscale auth keys, the Kismet API key, and MQTT credentials are never committed. Provide `*.example` templates instead.
- **Data minimization and retention.** The captured data includes identifiers from bystanders' devices. The proposed approach:
  - Store only metadata: no payloads and no packet bodies beyond what Kismet requires for device tracking.
  - Expire raw observations after a configurable window (proposed 14 days). Keep aggregated statistics and alert-linked records for the length of the exercise.
  - Document retention settings in the subsystem README.
- **Legal review.** Before field use, confirm that passive capture at the deployment site complies with applicable law and with program guidance.

## 11. Future Work: HackRF Nodes

HackRF nodes will extend awareness beyond the WBT bands. They are **not part of the current build**, but the central design should leave room for them.

- **Hardware (from the BOM):** one HackRF Pro with an Opera Cake antenna switch, broadband (700–5800 MHz) and sub-GHz (75–1000 MHz) antennas.
- **Likely capabilities:** spectrum sweeping with `hackrf_sweep` to detect energy in unexpected bands, such as drone control and video links, push-to-talk radios, or unknown ISM transmitters. Direction finding using antenna switching on the Opera Cake.
- **Integration:** report energy detections as a new emitter type through the same analytics service and the same alerting and MQTT path. Keep the observation model (`emitter_type`, `node_id`, `frequency`, `power`) generic enough to accept them.

## 12. Open Questions

| # | Question | Owner | Notes |
|---|---|---|---|
| 1 | What is the fusion MQTT message standard (envelope, topics, QoS, auth)? | Fusion leads | Blocks CEN-09. Section 9.3 is a placeholder. |
| 2 | What is the TAK alert-configuration format, and does it arrive directly or through fusion? | TAK team / Fusion | Blocks CEN-11. |
| 3 | Do camera cues go directly to the camera subsystem or through fusion? What cue volume can the cameras handle? | Camera / Fusion leads | Affects CEN-07. Cueing on every TPMS reading, as specified, produces about 400 cues a day on the simulated site, mostly passing cars; filtering by zone is available. |
| 4 | Do nodes use wired Ethernet or Wi-Fi for backhaul? | RF lead | Wired removes self-interference and simplifies exclusion. Strongly preferred where feasible. |
| 5 | Which Kismet release do we pin? Does it use WebSocket or legacy TCP remote capture? | RF lead | **Partly resolved:** `2026-10-R1` is pinned in `central/`, with the legacy TCP listener (3501) enabled. Legacy TCP remote capture (`--tcp --connect <central>:3501`) was validated on 2026-10-R1 for `kismet_cap_linux_wifi`, `kismet_cap_linux_bluetooth`, and `kismet_cap_sdr_rtl433`; WebSocket remote capture on 2501 with API key/user auth still needs validation. |
| 6 | Which database: PostgreSQL/TimescaleDB or SQLite? | RF lead | SQLite is simpler and more portable. Postgres scales better for co-traveler queries. |
| 7 | How many nodes, and where are they placed at the field site? | RF lead / Fabrication | Localization quality depends heavily on placement. |
| 8 | Is the UB500 with Kismet's Linux Bluetooth source sufficient for BLE advertising capture, or do we need a dedicated BLE sniffer? | RF lead | **Validate early.** Kismet's HCI-based Bluetooth capture is limited compared with dedicated sniffers. |
| 9 | What retention period and data-handling policy apply to the exercise? | Team lead | Affects Section 10. |

## 13. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| MAC/BLE randomization undermines pattern-of-life analysis for phones | High | Concentrate on stable identifiers (TPMS, APs, Bluetooth Classic). Link identities by fingerprint with confidence. Set operator expectations. |
| RSSI localization is too coarse to be useful | Medium | Commit to zone-level output. Calibrate at install. Add nodes if needed. |
| USB power or interference instability on the Pi 5 | Medium | Powered hub, cable separation, USB 2 ports for BT and SDR, udev-triggered restarts. |
| Limited TPMS detection range | Medium | Place a node near the vehicle approach. Consider a better 315 MHz antenna. |
| Fusion or TAK standards arrive late | Medium | Build against an internal adapter layer so that the MQTT payload mapping can be swapped out. |
| Kismet version mismatch between nodes and server | Low | Pin the version and bake it into the node image and the container image. |

## 14. Proposed Delivery Phases

| Phase | Goal | Exit criteria |
|---|---|---|
| **1. Single-node capture** | One Pi node streams Wi-Fi, BT, and TPMS to a containerized Kismet over Tailscale. | Devices from all three radios appear in Kismet. Capture survives a reboot. |
| **2. Multi-node and node agent** | 4 nodes, MagicDNS discovery, self-registration, heartbeat. | Power-cycling any node recovers it without intervention. Node identifiers never appear as non-friendly emitters. |
| **3. Analytics core** | Ingest, friendly exclusion, identity layer, zone localization, visits. | Live view shows emitters by zone. A walk test places a reference device in the correct zone at least 80% of the time (proposed target). |
| **4. Alerting and integration** | MQTT alerts, camera cues, configuration web UI. | Alerts reach fusion in the agreed format. A TPMS detection produces a camera cue. |
| **5. Pattern analysis** | Pattern-of-life, loiter, and co-traveler detection. | Scripted scenarios (repeat visitor, loiterer, paired devices) are detected correctly. |
| **6. TAK configuration and hardening** | TAK-pushed configuration, retention, field-readiness. | End-to-end field rehearsal passes. |
