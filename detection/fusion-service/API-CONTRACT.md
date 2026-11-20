# Fusion JSON / REST contract v1

All `/api/v1/` routes require `Authorization: Bearer <token>`. Responses are UTF-8 JSON. Envelope
timestamps use RFC 3339 UTC `Z` with microseconds. IDs are opaque strings; cursors belong to one
database history. Within v1, additions may introduce fields/types: consumers should ignore
unknown fields/types. Breaking field or semantic changes require a new version.

## GET /api/v1/events

| Query | Default | Meaning |
|---|---|---|
| `after` | Retained boundary | Cursors strictly greater than this nonnegative integer |
| `limit` | 100 | Page size, 1–1,000 |
| `site_id` | All | Exact site ID |
| `subsystem` | All | `camera`, `rf` or `physical` |
| `type` | All | One event type below |
| `node_id` | All | Exact node ID; RF positions/device alerts have null node IDs |

Filters combine with AND. Example response:

```json
{
  "events": [
    {
      "schema": "fusion.event.v1",
      "cursor": 43,
      "id": "b32cc119-94ba-4f3c-b1a0-8276f55ea24a",
      "source_id": "camera-message-123",
      "source_schema": "camera.detection.v0",
      "timestamp": "2026-11-17T18:30:00.000000Z",
      "received_at": "2026-11-17T18:30:01.142000Z",
      "site_id": "building-a",
      "subsystem": "camera",
      "node_id": "cam-01",
      "type": "camera.detection",
      "data": {
        "camera_id": "cam-01", "event": "object_start", "object_id": "track-123",
        "label": "person", "confidence": 0.87, "zones": ["north-gate"], "attributes": {}
      },
      "source": {
        "broker_id": "camera",
        "topic": "detection/camera/detections",
        "payload": {
          "schema": "camera.detection.v0", "event_id": "camera-message-123",
          "timestamp": "2026-11-17T18:30:00Z", "node_id": "cam-01", "camera_id": "cam-01",
          "event": "object_start", "object_id": "track-123", "label": "person",
          "confidence": 0.87, "zones": ["north-gate"], "attributes": {}
        }
      }
    }
  ],
  "next_cursor": 43,
  "has_more": false
}
```

Every event has the same envelope:

| Field | Meaning |
|---|---|
| `schema` | Always `fusion.event.v1` |
| `cursor` | Central ingestion order |
| `id` | Fusion UUID assigned at first insertion; unchanged on duplicate delivery |
| `source_id` | Camera `event_id`, RF/physical `id` |
| `source_schema` | Original message type/version |
| `timestamp` | Source time normalized to UTC; retains source-specific meaning |
| `received_at` | Receipt time of the delivery successfully inserted |
| `site_id` | Explicit RF/physical site or configured mapping |
| `subsystem` | `camera`, `rf` or `physical` |
| `node_id` | Camera source/RF status node, configured physical bridge ID; otherwise `null` |
| `type` | Selects the data shape below |
| `data` | Source-specific fields after envelope removal |
| `source` | Broker label, topic and original decoded payload |

### Data mapping

| Type | Source schema | Data |
|---|---|---|
| `camera.detection` | `camera.detection.v0` | `camera_id`, lifecycle `event`, `object_id`, `label`, `confidence`, `zones`, `attributes` |
| `camera.cue_ack` | `camera.cue-ack.v0` | `rf_cue_id`, `zone`, `matched`, nullable `object_id`, `attributes` |
| `camera.node_status` | `camera.node-status.v0` | `camera_online`, `link_online`, `recording_ok`, `detector_ok`, `attributes` |
| `rf.position` | `rf.position.v0` | `emitter`, `position`, `heard_by` |
| `rf.alert` | `rf.alert.v0` | `rule_id`, `rule_type`, `severity`, optional `emitter`, `location`, `context` |
| `rf.node_status` | `rf.node-status.v0` | `state`, optional `sources`, `health`, `last_heard` |
| `physical.event` | `physical.event.v0` | `seq`, `event_type`, `zone`, `state`, `previous_state`, `stale`, `source`, `synthetic`; optional `prev_seq`, `tamper`, `battery_low`, `previous_name` |
| `physical.panel_status` | `physical.panel-status.v0` | `state`, `active_source`, `links`, `stale_zones`, `zones`, `last_panel_message_at` |
| `physical.zone_status` | `physical.zone-status.v0` | `zone`, `state`, `stale`, `source`; optional `tamper`, `battery_low`, `online`, `last_changed` |

Camera data excludes `schema`, `event_id`, `timestamp`, `node_id`. RF data excludes `schema`,
`id`, `timestamp`, `node_id`, `site`. Source site metadata remains in `source.payload.site`.
Optional data fields stay absent when absent upstream. Additive fields are preserved.

Physical data excludes only `schema`, `id`, `timestamp`, `site`. Its `source` string remains
`data.source` (panel adapter), distinct from the envelope's `source` object (MQTT provenance).
Unknown nonempty physical `event_type` strings are preserved, as required by its additive v0
contract. All physical messages have the configured bridge `node_id`; `data.zone.id` identifies
the sensor. A physical zone is not automatically a camera/RF spatial zone.

Physical `seq`/`prev_seq` are preserved in `data`, separate from fusion's cursor. Source events
can arrive out of order; use their `seq` when reasoning about physical chronology, while polling
by fusion cursor. Skipped `seq` values alone do not establish loss because some physical log rows
are not published. `prev_seq` supports later chain analysis, accounting for replay/reordering;
fusion does not automatically detect gaps or fetch historical physical events in v1.
`synthetic` identifies changes inferred by the bridge after reconnect. `motion_cleared` means the
panel reports idle again, not that movement stopped at precisely that time.

Source units/meaning remain intact: RF floor-plan positions are feet, camera bounding boxes are
image coordinates, and geographic coordinates are never invented. RF alerts can describe several
nodes; fusion does not derive a node ID from alert context. `source.payload` preserves the JSON
object, not original whitespace or bytes.

### Polling and history gaps

1. Omit `after` initially to start at the retained boundary.
2. Process the returned page, then save `next_cursor`.
3. Continue with `after=next_cursor`, immediately while `has_more`, otherwise after a delay.

Empty pages preserve the supplied cursor. Filters advance through returned matches only. Use
a separate cursor per filter combination: changing filters with an advanced cursor can skip
earlier matches. `has_more` describes the query snapshot; new events arrive on later polls.

Late replay gets a later cursor with its original timestamp. Do not poll by source timestamp.
Identical source IDs within the configured dedupe window are suppressed even after event bodies
expire. After the window, replay may create a new fusion ID/cursor. Consumers should process
page retries idempotently using fusion IDs.

Retention removes a global ingestion prefix. A cursor behind it returns HTTP 410:

```json
{"error":{"code":"cursor_expired","resume_after":2500,"message":"history was deleted; resuming accepts a gap"}}
```

Retrying with `after=2500` explicitly accepts the missing history. This boundary also applies to
filtered requests. A cursor above the database high watermark returns HTTP 400 `cursor_ahead`.
Cursors cannot detect every database replacement/restore; operators must coordinate recovery.

## GET /api/v1/events/{event_id}/snapshot

The still image Frigate saved for a `camera.detection` event, as `image/jpeg`. Fusion fetches it
from the kit on each request: the path comes from the event's `data.attributes.snapshot_url`, and
the kit's address from its latest `camera.node_status` heartbeat (`data.attributes.node_address`).
Nothing is cached centrally; the kit keeps snapshots for 7 days.

| Status | `error.code` | Meaning |
|---|---|---|
| 404 | `not_found` | No such event, not a camera detection, no heartbeat from that kit yet, or the kit no longer has the image |
| 501 | `not_configured` | `camera_snapshot_password_env` is not set on this deployment |
| 502 | `node_unreachable`, `node_refused_secret`, `node_sent_no_image`, `snapshot_too_large`, `node_status_<n>` | The kit could not be reached or answered wrongly |
| 504 | `node_timeout` | The kit did not answer within `snapshot_timeout_s` |

## GET /api/v1/nodes

Optional filters: `site_id`, `subsystem`. Returns `{"nodes": [...]}`, sorted by site/subsystem/node.
Each entry has:

- `site_id`, `subsystem`, `node_id`: composite identity.
- `event_id`: fusion ID of the projected status event.
- `timestamp`, `received_at`: status source/receipt times.
- `source_age_s`, `receipt_age_s`: nonnegative ages at query time.
- `clock_ahead`: source timestamp is ahead of central time.
- `stale`: source or receipt age exceeds 90 s (camera) / 180 s (RF) / 120 s (physical), configurable; also true if
  source time exceeds future tolerance following a central clock change.
- `data`: the source health fields.

Latest means greatest source timestamp, with ingestion order breaking ties. Older replay does
not overwrite state or refresh receipt time. Duplicates do not refresh state. Stale fields are
the node's last words, not a fresh online assertion. Camera `detector_ok: false` can mean
inference is not yet confirmed; it is not converted to an overall failure. Node projections
survive event retention, subject to their count limit.

For physical, only `physical.panel_status` projects into `/nodes`. Its `data.state` reports
online/degraded/offline and remains separate from derived freshness. Zone status/events never
overwrite the bridge's node entry. Sensor `data.stale` remains the bridge's assertion about that
sensor; it is distinct from the node's derived `stale` field.

## GET /api/v1/health

Returns HTTP 200 with:

- `status`: `ok` if storage is ready and every broker is connected/subscribed, otherwise `degraded`.
- `storage`: readiness, last write error class, event count/bytes, main database size/limits,
  dedupe count and retained cursor boundary.
- `brokers`: connection ID, connected/subscribed flags, last message time, last error code,
  connection count and message counters (`accepted`, `duplicate`, `rejected`, `unsupported`,
  `storage_failure`, `internal_error`, present when nonzero).

Counters reset on restart; last message time includes rejected input. Connection health alone
does not prove sensors are reporting. No credentials or broker addresses are returned. Other
API queries return 503 when storage is unavailable.

## Errors

| HTTP | `error.code` | Meaning |
|---|---|---|
| 400 | `cursor_ahead` | Cursor exceeds the database high watermark |
| 401 | `unauthorized` | Missing or incorrect bearer token |
| 410 | `cursor_expired` | Retention removed history; includes `resume_after` |
| 422 | `invalid_request` | Invalid query type, enum or limit |
| 503 | `storage_unavailable` | Database cannot serve the query |

Errors use `{"error":{"code":"..."}}` with optional explanatory fields. A 422 carries only the
code: the response never echoes the rejected parameter value or the validator's own wording, so
consumers should check the documented query parameters rather than parse a message. Undocumented paths
return the framework's normal 404. OpenAPI describes the generated envelope/response models;
this document defines timing, delivery and data semantics.
