# Camera Subsystem Design (v0)

Owner: Dmitri Warner (Camera Subsystem Lead)
Status: Draft for review. No build has started.
Related: `../rf-subsystem/` (RF contract this mirrors — see dependency note in 1.1),
`./README.md` (operating instructions, filled after build),
`../camera-relay-servive/` (central relay, now in this subsystem's scope — section 4.2).

## 1. Intent and why

Black Relay is a layered detection system for a fixed site ("safehouse"). Three
detection subsystems — physical (alarm panel), RF (Kismet/SDR), and camera —
publish events to one compute node, where a fusion service normalizes them to a
common schema and serves local clients and remote operators.

The camera subsystem is the **visual layer**. It answers "what is actually there
and what is it doing" to pair with the RF layer's "which device is present." A
person loitering shows up on RF as a phone dwelling in a zone; the camera
confirms a person is there, classifies them, and holds the footage.

The design is driven by one property above all: **survivability**. If the central
compute node dies, is unplugged, or loses its link, each camera must keep
detecting and keep its own footage. The system degrades (lose one camera, lose
that camera's coverage) instead of going blind (lose the central box, lose
everything). This is why detection and recording live at the edge, one
self-contained node per camera, rather than on a shared central box.

### 1.1 Dependency note — RF contract lives on an unmerged branch

The RF subsystem's concrete contract (`DESIGN.md`, `schemas/observation.v0.schema.json`,
`formats.py`, the `rf.camera-cue.v0` wire format, and the shared site zones in
`simulator/sites/dev-site.yaml`) currently exists **only on branch
`detection/wifi-dev`**, not on `detection/develop` or `detection/camera`. On this
branch `rf-subsystem/` is still a template. "Mirror RF" therefore references
wifi-dev. Two items are **hard dependencies on that branch merging** (or on a
joint agreement with the RF lead) before the camera contract can be frozen:

- the exact fields of `rf.camera-cue.v0` (section 5.4), and
- the shared site zone vocabulary (section 6).

Everything else in this contract is self-consistent and can proceed.

## 2. Goals, non-goals, anti-goals

**Goals**
- One self-contained edge node per camera: detect and record locally, forward
  events and video to the central node best-effort.
- Publish a normalized, versioned detection contract to the fusion broker that
  mirrors the RF subsystem's envelope, so fusion sees one consistent pattern.
- Durable, idempotent event delivery: no significant detection event is silently
  lost across a broker outage or a Wi-Fi drop (within the outbox's size/age caps —
  on hard overflow the oldest durable events are shed FIFO; see 5.5), and replays
  never corrupt fusion.
- Headless field onboarding: a non-technical operator provisions a kit onto a
  network, VPN, and central node with no keyboard, monitor, or shell.
- Own and build the central RTSP relay (`camera-relay-server`) that aggregates
  every node's restream for local clients.
- Reuse maintained software (Frigate, go2rtc, MediaMTX, Balena wifi-connect,
  NetworkManager, Tailscale). Write only the glue and the contract.

**Non-goals (v0)**
- The second per-kit camera (Raspberry Pi AI Camera / IMX500, in the BOM). v0 is
  **RLC-520A only**: one camera per node, `camera_id == node_id`, single Hailo
  inference path. The backup camera, its failover, and the on-sensor (IMX500)
  inference path are a later phase.
- Local alert rules on the edge. Deferred to phase 2 as `camera.alert.v0`. v0
  emits raw detections, cue acknowledgements, and status only; fusion owns
  alerting policy for now.
- Face recognition, re-identification across cameras, license-plate reading.
- Pan/tilt/zoom control. The RLC-520A is a fixed turret; there is nothing to aim.
- Bulk footage replication to the central node (footage stays at the edge; see 5.6).

**Anti-goals (would make this a failure even if it technically works)**
- A design where losing the central node blinds the cameras, or flips edge nodes
  into setup-AP mode (see the watchdog definition in 7.4).
- Cameras reachable from the site LAN. The camera↔Pi link stays point-to-point
  and is not routable from the LAN.
- Footage that exists only on the central node, so a Wi-Fi loss means lost evidence.
- A provisioning flow where one mistyped Wi-Fi password permanently bricks a field
  kit with no way to recover without physical access to a shell.
- Fusion delivery that depends on the WAN/Tailscale path, so a WAN loss stops
  local event delivery (see 7.3).
- Rewriting Frigate's recording / tracking / zones / restream from scratch.

## 3. Constraints

- **Hardware per node (one kit per camera):** CanaKit Raspberry Pi 5 8GB AI kit
  (Pi 5 8GB, AI HAT+ with Hailo-8 **26 TOPS**, active cooler + heatsink, PCIe
  case, 128GB microSD, 45W USB-C PSU) + Reolink RLC-520A (5MP PoE fixed turret,
  H.264, ONVIF/RTSP). Camera powered by PoE injector or 12V DC.
- **Links:** camera↔Pi is wired point-to-point (not routable from LAN). Pi↔central
  is Wi-Fi (the emitting, less reliable link). Every Pi radiates Wi-Fi from a
  fixed position — an emissions consideration for the site.
- **Streams (from slides):** main H.264 ≤5MP @ 15fps for record + restream; sub
  H.264 640×480 @ 5fps for detection.
- **Compute headroom:** one 640×480@5fps stream on a 26-TOPS Hailo is a trivial
  load with large margin. There is no detection bottleneck; C++ would buy no
  throughput over Frigate's native inference path (`libhailort`).
- **Clock:** the Pi 5 has no battery-backed RTC. A node that boots before it has an
  uplink has no trusted wall clock (see 7.5). Fusion's cross-layer matching depends
  on RF and camera clocks agreeing, so time handling is explicit, not assumed.
- **Power resiliency:** no UPS is specified anywhere in Black Relay. On mains loss,
  every Pi drops immediately. See risks (section 9).
- **Integration branch:** `detection/develop`. Work on `detection/camera`, small
  PRs, no direct pushes to develop.

## 4. Architecture

### 4.1 Edge node (one per camera)

```
RLC-520A ──wired P2P──▶ Pi 5 + Hailo-8 (26 TOPS)
                         ├─ Frigate: object detection (Hailo) ──▶ MQTT events
                         ├─ Frigate: local recording ──▶ SD/USB (system of record)
                         └─ go2rtc: restream ──▶ central MediaMTX (live view)
                        Pi ──Wi-Fi (site LAN)──▶ central node (events + video, best-effort)
```

- **Detect:** Frigate pulls the camera sub-stream (640×480@5fps), runs the Hailo
  detector, tracks objects, applies zones.
- **Record:** Frigate records the main stream locally. **Local storage is the
  system of record.** The central node's view is convenience, not durable storage.
- **Restream:** go2rtc restreams to the central MediaMTX RTSP server for live
  clients. If Wi-Fi drops, restreaming stops but local detection and recording
  continue.
- **Publish:** a thin bridge translates Frigate's MQTT events into the normalized
  `camera.*` contract and publishes to the fusion broker through a durable outbox
  (section 5.5).

### 4.2 Central relay (`camera-relay-server`) — in scope

The camera subsystem owns the central receiving side:

- **MediaMTX** RTSP server aggregates every node's restream; local clients watch
  all cameras from one endpoint.
- Camera nodes publish `detection/camera/*` to the central MQTT broker (the broker
  itself is the shared compute node's; the camera subsystem is a publisher and
  provides the relay config, not the broker software).
- Deliverable: the MediaMTX config, the node→relay restream convention (paths,
  auth), and how fusion/clients address streams. This is new work pulled in by the
  ownership decision.

### 4.3 Degradation behavior

| Failure | Edge node | Result |
|---|---|---|
| Central node down | detect + record locally, outbox queues durable events | no central live view / fusion; footage safe; events replay on recovery; **node stays on network (does not open AP)** |
| Wi-Fi link down | detect + record locally, outbox queues durable events | no restream; footage safe; events replay |
| Camera down | Frigate reports camera offline via status | one camera lost; node reports it; others unaffected |
| Edge Pi down | — | that one camera's coverage lost; no cascade |
| Mains loss | all nodes drop | total outage until power returns (no UPS; see risks) |

## 5. Interfaces (contract)

All envelopes mirror `rf.observation.v0`: a `schema` const string carrying the
version, RFC 3339 UTC timestamps with `Z`, `node_id` matching
`^[a-z0-9][a-z0-9-]*$`, top-level `additionalProperties:false`, and an open
`attributes` sub-object. Topic scheme is `detection/<subsystem>/<type>`. One
module owns the wire format (mirroring RF's `formats.py`), so adopting the final
fusion standard means changing that one file.

**Addition over the RF envelope:** every camera message carries an `event_id`
(UUIDv4). Delivery is at-least-once (durable replay), which produces duplicates;
`event_id` lets fusion dedupe idempotently. The RF envelope has no such field; the
camera subsystem adds it and proposes RF and fusion adopt it.

### 5.1 Topics

| Direction | Topic | Schema | Durability |
|---|---|---|---|
| out | `detection/camera/detections` | `camera.detection.v0` | start/end durable (QoS 1, outbox); update best-effort (QoS 0) |
| out | `detection/camera/cue-acks` | `camera.cue-ack.v0` | durable (QoS 1, outbox) |
| out | `detection/camera/status/nodes` | `camera.node-status.v0` | durable (QoS 1, outbox) |
| in | `detection/rf/cues/camera` | `rf.camera-cue.v0` | — (RF-owned; see 5.4) |

### 5.2 `camera.detection.v0`

One detection of one object by one camera, over its track lifecycle.

```json
{
  "schema": "camera.detection.v0",
  "event_id": "b1e2...-uuid4",
  "timestamp": "2026-11-04T19:40:00Z",
  "node_id": "cam-01",
  "camera_id": "cam-01",
  "event": "object_start | object_update | object_end",
  "object_id": "1730749200.12-abc",
  "label": "person",
  "confidence": 0.87,
  "zones": ["perimeter-north"],
  "attributes": {
    "bbox": [120, 80, 64, 180],
    "area_px": 11520,
    "stationary": false,
    "sub_label": null,
    "snapshot_url": null,
    "clip_url": null,
    "rf_cue_id": null
  }
}
```

- `event` mirrors Frigate's new / update / end object lifecycle, parallel to RF's
  `visit_start` / `visit_end`.
- `object_id` is Frigate's stable per-track id. In v0, `camera_id == node_id`.
- `label` is a coco-80 subset (person, car, truck, bicycle, motorcycle, …); exact
  set is a tuning decision, see section 11.
- `zones` is an **array** (0..n), because Frigate zones overlap and objects are
  often outside every zone. Empty means "detected, outside all defined zones."
  Values use the **shared site zone vocabulary** (section 6).
- `rf_cue_id` is set when this detection correlates to an inbound RF cue (see 5.4).
- **Durability:** `object_start` and `object_end` go through the durable outbox;
  `object_update` is best-effort, so a long outage does not backlog a flood of
  position updates.

### 5.3 `camera.node-status.v0`

Edge heartbeat, parallel to `rf.node-status.v0`.

```json
{
  "schema": "camera.node-status.v0",
  "event_id": "c3d4...-uuid4",
  "timestamp": "2026-11-04T19:40:00Z",
  "node_id": "cam-01",
  "camera_online": true,
  "link_online": true,
  "recording_ok": true,
  "detector_ok": true,
  "attributes": {
    "frigate_version": null,
    "detection_fps": null,
    "storage_free_pct": null,
    "uplink_rssi_dbm": null
  }
}
```

### 5.4 Inbound RF cue + `camera.cue-ack.v0`

RF publishes a cue on a TPMS `first_heard` event to `detection/rf/cues/camera`.

- **Cue contents (`rf.camera-cue.v0`):** RF-owned and defined on `detection/wifi-dev`
  (unmerged — section 1.1). Expected to carry at least a cue id, a zone, a source
  node, a timestamp, and the TPMS sensor id. Exact fields to be confirmed with the
  RF lead before freeze.
- **Routing:** every node receives the single topic; **each node filters by zone**
  (the node knows which site zones it covers). The cue is not addressed to a
  specific camera.
- **Matching:** a cue corresponds to a detection when an open track shares the
  cue's zone within a time window (default ±5 s, tunable). On a match, the node
  sets `attributes.rf_cue_id` on that detection.
- **Acknowledgement (`camera.cue-ack.v0`):** because a cue may arrive with *no*
  visual match, and `camera.detection.v0` requires `object_id`/`label`/`confidence`,
  cues are reported on a dedicated topic. Every cue the node acts on produces a
  cue-ack, matched or not, so fusion always sees the cue was received:

```json
{
  "schema": "camera.cue-ack.v0",
  "event_id": "d5e6...-uuid4",
  "timestamp": "2026-11-04T19:40:01Z",
  "node_id": "cam-01",
  "rf_cue_id": "rf-cue-...",
  "zone": "perimeter-north",
  "matched": true,
  "object_id": "1730749200.12-abc",
  "attributes": { "snapshot_url": null }
}
```

- On a cue, the node grabs a snapshot and (if matched) tags the detection. It does
  **no heavy work**.
- **Volume (answer to RF's deferred question):** the camera can absorb
  every-TPMS volume (~400/day on the dev site ≈ 1 per 3.6 min) at negligible cost.
  For review-noise reasons, RF should `zones`-filter cues to near-building
  approaches only. Decision: **zone-filtered cues**, RF's per-site cooldown retained.

### 5.5 Durable delivery (outbox)

Mirrors RF's outbox: each durable message is written to an on-disk outbox before
publishing, sent with QoS 1, and removed only on broker acknowledgement. Messages
queued during an outage are replayed with their **original bytes** (so `event_id`
and `timestamp` survive unchanged) and in per-node order; fusion dedupes on
`event_id`.

**What is persisted.** `object_update` is QoS 0 and is **never written** to the
outbox — a long outage must not backlog a flood of stale object positions, and a
replay must never resurrect an out-of-date track location. The outbox therefore
holds only the durable kinds: `object_start`, `object_end`, `cue-ack`, and
`node-status`.

**Overflow policy (FIFO drop-oldest-first).** The queue is bounded by size
(`max_bytes`) and per-entry age (`max_age_s`). Because `object_update` is never
written, there is nothing to "evict first"; on **hard overflow** the oldest queued
durable entries are shed — the stalest events of a long outage — with a logged
warning, and the newest are kept in order. So a durable event is dropped only
under hard overflow and only oldest-first; in normal operation (and across any
bounded outage within the caps) no durable event is dropped. This supersedes any
earlier wording that durable messages are "never dropped": they are bounded, and
the bound is shed oldest-first rather than growing the queue without limit.

### 5.6 Snapshot / clip references

`snapshot_url` and `clip_url` reference the **edge node's Frigate** (the footage
owner). Local clients reach them over the site LAN; remote operators reach them
over Tailscale. Edge Frigate is **not** exposed to the WAN directly. v0 does not
copy footage to the central node; central-side event-clip upload is a later-phase
option.

## 6. Shared zone vocabulary (cross-subsystem)

For fusion to correlate layers ("zone-2 has an RF device and a camera person"),
zone values must be a **single site vocabulary** shared by RF and camera. RF
defines site zones in `../rf-subsystem/simulator/sites/dev-site.yaml` (on
`detection/wifi-dev` — section 1.1). Camera (Frigate) zones must use the **same
zone names**. This is a hard dependency and a coordination item with the RF lead
and fusion co-leads; it must be agreed before the contract freezes.

## 7. Field onboarding (headless provisioning)

Each kit ships blind. First boot with no saved network brings up a setup access
point; the operator joins it, opens a captive portal, and configures the node.

### 7.1 Setup AP
`hostapd` + `dnsmasq`, wrapped by **Balena wifi-connect** (Rust, maintained).
SSID is the kit name, e.g. `BlackRelay-CAM-<serial>`. The AP is **WPA2-protected,
not open** — the portal sets secrets. The AP passphrase is **unique per kit**,
derived from the serial and printed on a label on the kit, not shared across kits.

### 7.2 Portal fields
The portal collects, and writes to node config:
- Wi-Fi client SSID + password (scanned via `nmcli dev wifi`) → a NetworkManager
  profile.
- VPN (Tailscale auth key or login) and hostname.
- **Central endpoints:** MQTT broker address + credentials/TLS, MediaMTX address.
- **`node_id`:** defaults to a value derived from the hostname/serial, editable.

wifi-connect handles the Wi-Fi flow; the VPN, hostname, and central-endpoint
fields are an extension on top. After a successful join the portal is disabled.

### 7.3 Traffic paths
- **Fusion traffic (events + restream, node→central) goes over the site LAN/Wi-Fi
  (local).** It must not depend on the WAN.
- **Tailscale is only for the remote-ops consumer class** reaching the site over
  the WAN, and for remote operators reaching edge Frigate (5.6). A WAN loss must
  not break local event delivery.

### 7.4 AP-fallback watchdog (required)
"Uplink established" means **Wi-Fi associated + a DHCP lease on the site network
(L3)** — nothing more. It explicitly does **not** mean the broker, Tailscale, or
the central node is reachable. A central-node or broker outage must never push a
node back into setup-AP mode (that would defeat survivability and start every node
emitting a setup AP). If no L3 uplink is established within a timeout after boot,
the node relaunches the setup AP. Re-provisioning a already-configured node (e.g.
the site Wi-Fi password changed) is triggered **physically** — a GPIO button held
during boot — not automatically.

### 7.5 Time
Once an uplink exists, the node syncs NTP from the central node or the site
gateway. Because the Pi 5 has no battery RTC, events created before NTP sync carry
an untrusted clock; they are held in the outbox and stamped at publish using an
NTP-corrected clock. An optional RTC module is listed as hardening (section 9).

## 8. Testing and verification

Following the repo's guidance — focus on business rules (event definitions) and
contracts, not exhaustive coverage.

- **Contract tests:** published `camera.detection.v0` / `camera.cue-ack.v0` /
  `camera.node-status.v0` messages validate against their JSON Schemas; the schema
  accepts exactly what the bridge emits (mirrors RF's `test_contract.py`).
- **Bridge business rules:** Frigate event → normalized event mapping
  (object_start/update/end, zones assignment, confidence pass-through), RF-cue
  matching (zone + time window) and cue-ack emission (matched and unmatched).
- **Durable delivery:** outbox acknowledges, survives a restart, resends after a
  disconnect, preserves order, drops only `object_update` on overflow, and fusion
  dedupe on `event_id` holds (mirrors RF's `test_outbound.py`).
- **Onboarding:** AP comes up on blank config; a valid profile joins and persists
  across reboot; an invalid profile falls back to AP within the timeout; a central
  or broker outage does **not** trigger AP fallback.
- **Degradation (manual/field):** kill the central node and the Wi-Fi link
  independently; confirm local detection and recording continue, the node stays on
  the network, and durable events replay on recovery.

## 9. Risks and open questions

- **No UPS (survivability gap).** Mains loss drops every Pi immediately. A small
  per-node UPS (e.g. a UPS HAT) would let a node ride out brief outages and shut
  down cleanly. Flagged for the survivability decision; out of scope for v0 unless
  the team adopts it.
- **No RTC (clock gap).** Mitigated in 7.5; an RTC module is the hardening option.
- **Wi-Fi is the weak + emitting link.** Congestion or jamming stops restream and
  delays events (local detect/record continue by design). Emissions from each Pi
  are a fixed-position signature.
- **RF contract is on an unmerged branch** (section 1.1): exact `rf.camera-cue.v0`
  fields and the shared zone vocabulary are hard dependencies before freeze.
- **Fusion standard not yet defined.** The camera contract is v0 and self-consistent;
  it changes in one module when the fusion standard lands.
- **microSD endurance.** Continuous recording to microSD wears it out; a USB SSD
  for the record volume is the likely answer (section 11).

## 10. Phases

1. **v0 (this design):** edge node (Frigate + Hailo + RLC-520A), local record,
   go2rtc restream, central MediaMTX relay (`camera-relay-server`),
   `camera.detection.v0` + `camera.cue-ack.v0` + `camera.node-status.v0` over a
   durable outbox, inbound RF cue handling, headless onboarding, contract + bridge
   tests.
2. **Phase 2:** local alert rules on the edge (`camera.alert.v0`), mirroring RF's
   declarative rule engine, so a node still alerts with the central node down.
3. **Later:** the second per-kit camera (Pi AI Camera / IMX500) as a low-power
   failover sensor; central event-clip upload; richer attributes (re-id, sub-labels);
   storage / UPS / RTC hardening per team decisions.

## 11. Left to the builder

- Exact `label` set to track and per-label confidence thresholds (tune on real
  footage; person + vehicle classes are the floor).
- Frigate zone geometry per camera (depends on physical mounting / FOV), drawn
  against the shared zone names from section 6.
- Retention policy and record storage medium (microSD vs USB SSD).
- Which Hailo model / `.hef` (Frigate's bundled Hailo YOLO is the default start).
- Portal implementation detail (extending wifi-connect vs a thin companion app for
  the VPN / hostname / central-endpoint fields).
- RLC-520A bring-up: v0 pre-configures each camera at the bench as a documented
  assembly step (sub-stream 640×480@5fps, main ≤5MP@15fps, RTSP + ONVIF enabled,
  admin password, NTP) plus the Pi-side static addressing on the point-to-point
  link. Note: current RLC-520A firmware ships RTSP/ONVIF **disabled** (reachable
  only over Baichuan/port 9000), so enabling is mandatory at the bench; automatic
  enable-on-first-boot over Baichuan (`reolink_aio`) is a proven, feasible
  enhancement, not v0.
```