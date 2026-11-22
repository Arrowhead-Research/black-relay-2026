# Central fusion service v1

## Architecture and boundaries

One central process consumes existing camera, RF and physical output, normalizes it and serves
JSON/REST. It runs alongside the camera relay, RF Kismet/Console and physical bridge. Those
services keep ownership of video, footage, detection, RF localization and alert rules. Fusion
does not infer tracks/relationships, send commands, publish CoT or offer SSE in v1.

Code is layered by package: `api` (HTTP), `ingest`, `mqtt` and `storage` depend on `domain` and
`settings`, never on each other's internals; `storage` is the only importer of `sqlite3` and
raises `StorageUnavailable` across its boundary; `runtime.py` wires them together. Known
follow-up: per-subsystem settings (`camera_schemas`, `camera_stale_s`, ...) are flat fields
accessed through `Settings.schema_dir()`/`stale_after_s()`; a nested `subsystems:` mapping would
be cleaner but changes the config format and the `FUSION_*_SCHEMAS` names Compose sets.

```text
Camera Pi bridge → camera broker ────────────┐
RF Pis → central Kismet/Console → RF broker ──┼→ normalize → SQLite → REST consumers
Physical bridge → team MQTT broker ──────────┘                 └→ latest node status
```

On the central server all three use one broker (`broker`, repo-root `compose.yaml`) and one fusion connection (`central`).

## Contracts and normalization

Accepted topics are the three camera outputs, RF positions/alerts/node status and physical
events/panel status/zone status, as mapped in
[API-CONTRACT.md](API-CONTRACT.md). Internal `rf.observation.v0` and provisional RF camera cues
are not inputs. Camera cue acknowledgements are accepted; existing cue routing is independent.

Load reference JSON Schemas from the owning subsystems at startup. Permit additive properties
recursively while enforcing known required fields, types, enum values, topic/schema pairing and
date-time formats. New enum values or schema versions may require an adapter update. Reject
oversized/deep input, invalid Unicode, nonfinite JSON numbers, empty/oversized source IDs and
timestamps beyond the configured future tolerance. Old source timestamps remain valid history.
Physical's v0 contract explicitly permits new `event_type` strings, so validate those as
nonempty strings rather than a fixed enum. Other source enum constraints remain enforced.

Map camera `event_id` and RF/physical `id` into `source_id`. The API returns a common `fusion.event.v1`
envelope; `data` retains event-specific content and units. Preserve the decoded source payload.
RF positions/alerts have null node IDs; do not invent one from the contributing nodes. Do not
compare RF location uncertainty with camera classifier confidence or invent geolocation.

Explicit RF/physical `site.id` wins, then per-node site mapping, then connection fallback. Reject events
without a site. Site mapping changes require draining camera outboxes: camera messages lack
site identity. Source timestamps retain their source semantics and are normalized to UTC;
`received_at` records the receipt of the delivery successfully inserted.

Physical subscriptions use the existing versioned team feed directly from Mosquitto. They do
not access the panel, read physical's database, poll its API or consume the native TLS broker's
`blackrelay/v1/...` feed. Enable physical's optional team publisher; its example routing is the
broker RF uses (`rf-mqtt:1883` in the generic example). Fusion can subscribe to both with one persistent connection.

Every physical connection requires `physical_node_id`, a stable configured bridge identity
because its source messages lack one. Individual sensors remain `data.zone.id`. The v0 contract
identifies one bridge per site on that connection; multiple panels in one site require a source
identity/routing agreement. Only panel status updates the node projection, with a 120-second
default freshness threshold. Zone status cannot overwrite panel status. Sensor `data.stale` and
derived node freshness have distinct meanings.

Keep physical `seq`, `prev_seq`, `synthetic` and `source` in data. `source` here names the panel
adapter, not MQTT provenance. REST ordering stays ingestion-based; source event chronology uses
`seq`, and statuses use timestamps. The source chain may later support loss analysis, but v1
does not reorder REST history, detect gaps or backfill via the physical API. First enabling the
physical team publisher starts at its log head, and source outbox caps can drop older messages.

## Runtime and delivery

FastAPI, one retention thread and one synchronous MQTT network thread per configured broker run
in one process. There is no application message queue. Callbacks validate/commit before returning;
SQLite access is protected by a shared lock. HTTP synchronous routes use FastAPI's thread pool.
Run exactly one worker/database owner with stable, unique MQTT client IDs.

MQTT 3.1.1 connections use persistent sessions and QoS 1 subscriptions. Publisher QoS 0 remains
best-effort: RF positions and camera updates lost upstream cannot be recovered. The broker must
support persistent sessions/queues. Events published before the first subscription are unavailable.

Manual PUBACK follows the SQLite commit. On transient storage/internal errors, reconnect without
acknowledging and allow persistent-session redelivery. Backoff grows from 1 to 30 seconds.
Upstream queue caps and broker crash behavior still limit delivery; this is not an exactly-once
end-to-end claim. A forced shutdown can produce duplicates handled by the same mechanism.

Invalid, unsupported or conflicting messages are acknowledged after counting/logging safe reason
codes so poison messages do not replay forever. No dead-letter payload queue is stored. Payloads,
credentials and validator exception values are excluded from ingestion logs. Event/source data
remain in the local database and authenticated API.

## Persistence

SQLite uses WAL and `synchronous=FULL`. A database whose schema version differs from the one this build knows fails startup.

| Table | Purpose |
|---|---|
| `events` | Monotonic ingestion cursor, filters, normalized body, encoded size |
| `seen` | Source namespace/schema/ID identity, canonical payload hash, expiry |
| `nodes` | Latest status per site/subsystem/node |
| `metadata` | Schema version, global retention floor, event byte accounting |

Event insertion, dedupe record and node projection commit in one transaction. Matching identity
and source-object hash is a duplicate. Matching identity with changed content is a permanent
conflict; first event wins. Canonical JSON ignores whitespace/key order. Keep namespaces stable
across broker moves, and distinct between independent fleets.

Latest node state uses greatest source timestamp; ingestion order breaks ties. Old replay stays
in history without replacing newer state or refreshing its receipt time. Duplicate status also
does not refresh freshness. Freshness considers source and receipt age; source clocks cannot
earn extra lifetime. Keep source health fields rather than deriving an overall cross-subsystem score.

## Polling and retention

SQLite AUTOINCREMENT cursors order committed ingestion and survive event deletion. The API
injects the assigned cursor when reading a stored body. Bound query parameters implement filters;
the store lock keeps a query and retention consistent. Consumers save cursors after processing.

Retention deletes an ingestion prefix only, on inserts and every 30 seconds in bounded batches.
Age uses central receipt time; long downtime may need several maintenance passes. Encoded-body
byte pressure can remove younger events. A global floor records the last deletion. Older client
cursors get HTTP 410; starting without a cursor starts at the retained floor. Database restores
and replacements require consumer coordination because integer cursors are database-local.

Dedupe expires by first successful receipt, not by repeated replay. It outlives event bodies:
14 days versus 7 by default. After expiry, replay can create a new ID/cursor. A separate entry cap
pauses ingestion at capacity instead of forgetting live identities. Size it for actual traffic;
one million entries is less than fourteen days at one unique message/second.

Node projections are count-bounded, with least recently received nodes evicted. They can outlive
their events. Event byte accounting excludes indexes, node projections and dedupe. SQLite's
main-file page cap is independent; WAL/temp overhead needs additional disk headroom. Checkpoints
reuse main-file pages; there is no automatic VACUUM or promise of filesystem shrinkage. Storage
operations roll back on failure; new reliable messages remain unacknowledged for retry.

## Deployment and operations

YAML settings reference secrets by environment-variable name. Missing required values fail
startup. Duplicate YAML mapping keys are rejected instead of silently overriding connections.
Bearer authentication protects all REST data routes; docs/OpenAPI are public. Error responses
carry reason codes only. Request validation failures in particular do not include the offending
input or the validation library's messages: the former reflects caller-controlled data, the latter
would tie the public contract to pydantic's wording. This mirrors the ingest path, which logs reason
codes and never payloads or exception text. Default
host binding is loopback. MQTT supports a verified CA and optional client certificate. Deployment
provides any HTTP TLS termination.

Compose runs non-root with a read-only root filesystem, persistent data volume, bounded log
rotation, dropped capabilities and a temporary `/tmp`. Docker builds from `detection/` using an
allowlisted Dockerfile-specific ignore file; only fusion code/metadata and source schemas enter
the context. Existing subsystem files/configuration remain unchanged.

Startup loads schemas and opens storage before MQTT connections. Broker outages degrade health
while retained REST data remain readable. Storage write errors remain visible until a successful
insert. Counters reset on restart. Connection health is distinct from node freshness.

Shutdown stops retention, joins MQTT threads, then closes storage. Connect timeouts are bounded,
but DNS resolution duration is OS-dependent. Compose grants 45 seconds before forced termination.
Stop the process before offline volume backup; do not copy a live database without its WAL.

## Verification status

A pytest smoke suite exercises the HTTP surface with MQTT disabled; ingest, storage and
connection behaviour are still verified by review and by deployment. Also verify lint/format,
config/schema loading, package contents, generated OpenAPI and Compose rendering. Live camera/RF/physical and hardware integration
require the deployment. TAK integration is outside this version's scope.
