# RF subsystem interfaces: draft for the TAK, camera and fusion teams

**Status: proposal (2026-11-07).** None of these formats has been agreed. They exist so the TAK, camera and fusion teams can review something concrete, instead of each team waiting on the others. In code, the only place that knows the wire format is `analytics/src/rfanalytics/formats.py`, so a change agreed here costs one adapter edit. Everything below extends DESIGN.md section 9.3.

As of this date, no branch in the repository defines a TAK or camera format. Survivability's identity stack (Pocket ID, LLDAP) has a `tak` team group and issues OIDC `groups` claims, which the WBT Console can later use for sign-in.

## Conventions

- **Transport:** fusion MQTT broker, QoS 1, JSON (UTF-8). Every payload has `schema` (name.version), `id` (UUID, used to deduplicate redelivery) and `timestamp` (UTC ISO 8601).
- **Topics** sit under the `detection/rf` prefix (configurable).
- **Position:** each message carries both site coordinates (feet, from the site map's origin) and WGS84 coordinates once the site is georeferenced. `ce_m` is the 50% circular error in metres, measured from calibration (leave-one-out). It is `null` when the site is not calibrated, never a guess.
- **Device selectors** identify a device by one of: `mac` (Wi-Fi or Bluetooth address), `ssid`, `ble_name`, `ble_manufacturer`, `tpms_id`, `oui` (first three bytes; use with care), or later `identity` (a linked identity across randomized addresses).

## 1. TAK → RF: mark devices (priority and friendly)

Topic `detection/rf/config/in`, sent by TAK through fusion. Each message is one operation.

```json
{
  "schema": "rf.config.v0",
  "id": "5b7f0c1e-1d2a-4c55-9a0e-0e3b8f6c2d11",
  "timestamp": "2026-11-07T18:20:00Z",
  "source": {"system": "tak", "operator": "callsign-or-uid"},
  "op": "upsert",
  "list": "priority",
  "entry": {
    "entry_id": "tak-poi-0042",
    "selector": {"mac": "aa:bb:cc:dd:ee:ff"},
    "priority": "high",
    "label": "POI 42 phone",
    "scope": "global",
    "expires": "2026-11-21T00:00:00Z",
    "note": "free text, optional"
  }
}
```

- `op`: `upsert` or `remove` (a remove only needs `entry_id`).
- `list`: `priority` (alert whenever heard) or `friendly` (exclude from alerting and history).
- `priority`: `low`, `medium`, `high` (becomes the alert's `severity`).
- `scope`: `global` (every site; the default for TAK entries) or `site` (only the named `site_id`).
- `entry_id` is assigned by TAK and stays stable, so later updates and removes refer to it. Entries sent by TAK are shown as TAK-owned in the console and can only be changed by TAK, or by an admin overriding locally.
- `expires` is optional. The console's default expiry is 30 days for priority entries.

Acknowledgement on `detection/rf/config/ack`:

```json
{"schema": "rf.config-ack.v0", "id": "...", "timestamp": "...", "ref": "5b7f0c1e-...", "status": "applied", "entry_id": "tak-poi-0042"}
```

`status` is one of `applied`, `rejected` (an `error` field says why), or `duplicate` (this `id` was already applied).

**Questions for the TAK team:** Will this come through fusion or straight from the TAK server? Is a TAK UID or callsign available for `source.operator`? Do you need to read back the current lists (we could publish a retained `detection/rf/config/state`)?

## 2. RF → fusion/TAK: alerts

Topic `detection/rf/alerts`. This extends the existing `rf.alert.v0` with position and the reason the alert fired.

```json
{
  "schema": "rf.alert.v1",
  "id": "c0a8...",
  "timestamp": "2026-11-07T18:31:04Z",
  "site_id": "site-a",
  "rule_id": "new-in-range",
  "rule_type": "new_in_range",
  "severity": "high",
  "emitter": {"id": "aa:bb:cc:dd:ee:ff", "type": "wifi_client", "randomized_address": false,
              "name": "Pixel 8", "manufacturer": "Google"},
  "position": {"x_ft": 31.5, "y_ft": 12.0, "lat": 38.000123, "lon": -77.000456, "ce_m": 2.4,
               "nodes": ["wbt-1", "wbt-2", "wbt-4"], "area": "inner"},
  "match": {"list": "priority", "entry_id": "tak-poi-0042", "label": "POI 42 phone", "source": "tak"},
  "context": {"first_seen_site": true, "dwell_s": 45, "co_travelers": [{"id": "11:22:33:44:55:66", "score": 0.82}]},
  "snapshot": null
}
```

- `rule_type`s planned: `new_in_range` (not in the site's baseline, inside an alert area), `priority` (a marked device heard anywhere), `co_traveler` (arrived together with a priority device), `loiter`, `repeat_visitor`, `node_offline`.
- `match` appears only when a priority entry triggered the alert. `snapshot` is filled when a camera returns an image reference (section 4).
- **CoT mapping (suggestion for fusion):** `lat`/`lon` → `point`, `ce_m` → `ce`, `severity` → CoT type or colour, `emitter.name` or `match.label` → callsign, and `id` as the CoT `uid` so updates replace the marker instead of adding new ones.

## 3. RF → cameras: cues

Topic `detection/rf/cues/camera/<camera_id>`. A cue is sent only to cameras whose coverage (position, heading, field of view and range, drawn in the console) contains the target's estimated position. This replaces today's single topic that cued on every TPMS reading.

```json
{
  "schema": "rf.camera-cue.v1",
  "id": "9e1d...",
  "timestamp": "2026-11-07T18:31:05Z",
  "site_id": "site-a",
  "camera_id": "cam-gate",
  "reason": "vehicle_in_view",
  "target": {"id": "vehicle:tpms-1a2b3c4d", "type": "vehicle", "members": ["tpms:1a2b3c4d", "tpms:1a2b3c4e"]},
  "position": {"x_ft": 80.0, "y_ft": -15.0, "lat": 38.0001, "lon": -77.0002, "ce_m": 6.0},
  "bearing_deg": 212.5,
  "alert_id": "c0a8..."
}
```

- `reason`: `vehicle_in_view`, `priority_in_view`, or `new_in_range`.
- `bearing_deg` is the direction from the camera to the target (degrees true), so a PTZ camera can point at it.
- Cooldown per target and camera, so one parked car gets one cue, not hundreds.

## 4. Cameras → RF: snapshot result (optional)

Topic `detection/rf/cues/camera/<camera_id>/result`.

```json
{"schema": "rf.camera-result.v0", "id": "...", "timestamp": "...", "cue_id": "9e1d...",
 "status": "captured", "media": [{"kind": "image", "url": "http://camera-host/snap/123.jpg", "plate": "ABC1234"}]}
```

The console attaches `media` to the alert and the device card. Media stays on the camera system; RF stores only the reference.

**Questions for the camera team:** Should cues go straight to cameras or through fusion (DESIGN open question 3)? How many cues per minute can a camera handle? Are the cameras PTZ (would `bearing_deg` be used)? Can results include a plate read?

## 5. Retention and wiping

Data is kept per site and deleted when the site is wiped in the console. History older than the site's retention period (default 7 days; deployments last days) is purged automatically. TAK-sent entries are kept until they expire or TAK removes them.
