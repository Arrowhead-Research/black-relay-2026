# Fusion Service

## Ownership

- **Subsystem co-leads:** George Sithole and Chris Dew.
- **Cross-subsystem reviews:** involve the camera, RF and physical leads when their interfaces change.

## Purpose and scope

Runs on the central node alongside the subsystem central services. Consumes camera, RF and physical MQTT
messages, validates and normalizes them into `fusion.event.v1`, stores them in SQLite, and exposes
JSON over REST. Consumers poll with a cursor. The service also keeps the latest reported sensor
node status and reports broker/storage health.

The subsystems may use different brokers. Fusion does not replace the camera relay, Kismet,
WBT Console or physical bridge. Detection, recording and RF analytics remain with their subsystems. Correlation,
track aggregation, commands, video proxying, TAK/CoT conversion and SSE are deferred.

See [DESIGN.md](DESIGN.md) for architecture and [API-CONTRACT.md](API-CONTRACT.md) for the consumer
contract. Existing subsystem directories are unchanged.

## File tree

```text
fusion-service/
  README.md                 operating instructions
  DESIGN.md                 source mapping, persistence and failure behavior
  API-CONTRACT.md           JSON/REST contract
  pyproject.toml, uv.lock    package, command and locked dependencies
  config.example.yaml       broker connections, source sites and limits
  config.physical.example.yaml  physical-only connection for a local bench
  .env.example              secret names and Compose settings
  Dockerfile                non-root container build
  Dockerfile.dockerignore   restricts the parent build context to code and schemas
  compose.yaml              API container and persistent data volume
  compose.fusion.yaml       optional existing fusion Docker network attachment
  src/fusion_service/
    app.py                  create_app() factory and main() entrypoint (serve, check-config)
    runtime.py              builds store, ingest, connections and retention; starts and stops them
    lifespan.py             FastAPI lifespan that owns the runtime
    settings/               config schema (models.py) and YAML + environment loader (loader.py)
    api/                    HTTP layer
      security.py           bearer token dependency
      errors.py             error envelope and exception handlers
      dependencies.py       typed access to the runtime from routes
      v1/                   /api/v1 router: events.py, nodes.py, health.py, schemas.py
    domain/                 shared vocabulary: event envelope, node snapshot, topics, time, errors
    ingest/                 contracts.py (reference schema validation), adapters/ (field mapping),
                            normalize.py (envelope, site identity, timestamps), pipeline.py (decode, normalize, store)
    mqtt/                   broker connections and acknowledgements
    storage/                SQLite: store.py, retention.py, schema.py, sql/001_initial.sql
  tests/                    smoke tests over the HTTP surface
```

## Physical assembly and prerequisites

No physical assembly. Use the central Linux host with access to the configured brokers, persistent local
storage and an accurate clock. Run one fusion process/worker per database. SQLite must not be
shared over a network filesystem.

- **Containers:** Docker with Compose v2.17+. The build context is `detection/` so the image can
  include the existing camera/RF/physical reference schemas. Base images support amd64 and arm64.
- **Source:** Python 3.12+ and uv. Run the following commands from `detection/fusion-service/`.

## Configuration

```sh
cp config.example.yaml config.yaml
cp .env.example .env
openssl rand -hex 32
```

Put the generated token in `FUSION_API_TOKEN` in `.env`, fill in camera MQTT credentials, and edit
`config.yaml`. Both local files are gitignored. Fusion never reads another subsystem's secrets
automatically. If running outside Docker, load `.env` using uv's `--env-file` option below.

| Setting | Purpose/default |
|---|---|
| `brokers[].id` | Connection label in provenance/health |
| `namespace` | Stable source identity namespace for deduplication; retain across host moves |
| `host`, `port` | Reachable MQTT endpoint; port 1883 |
| `client_id` | Fixed persistent session ID unique to this fusion instance |
| `subsystems` | Any combination of `camera`, `rf`, `physical` on this broker |
| `physical_node_id` | Required with physical: stable bridge ID, distinct from sensor `zone.id` |
| `site_id`, `node_sites` | Explicit fallback site and optional node-to-site overrides |
| `username_env`, `password_env` | Secret environment variable names; omit both for anonymous RF |
| `tls_ca`, `tls_cert`, `tls_key` | Optional verified MQTT TLS and client certificate/key |
| `database` | SQLite path, `data/fusion.sqlite3` |
| `camera_schemas`, `rf_schemas`, `physical_schemas` | Existing source schema directories |
| `retention_s`, `max_event_bytes` | Events: 7 days or 256 MiB encoded bodies, whichever comes first |
| `dedupe_s`, `max_dedupe_entries` | Source IDs: 14 days, at most 1,000,000 entries |
| `max_database_bytes` | Main SQLite file cap: 512 MiB; excludes WAL/temp overhead |
| `max_nodes` | Latest-node cap: 1,000, evicting least recently received |
| `max_message_bytes`, `max_message_depth` | Input limits: 65,536 bytes, 32 levels |
| `future_tolerance_s` | Reject messages more than 60 s ahead of central time |
| `camera_stale_s`, `rf_stale_s`, `physical_stale_s` | Freshness: 90 s / 180 s / 120 s |

Paths resolve relative to the YAML file. Compose overrides the database/schema paths using
`FUSION_DATABASE`, `FUSION_CAMERA_SCHEMAS`, `FUSION_RF_SCHEMAS`, `FUSION_PHYSICAL_SCHEMAS`; one config works in both
environments. For MQTT TLS, mount certificates read-only and configure their container paths.

RF/physical's explicit `site.id` wins; otherwise a node mapping wins over the connection's fallback.
There is no implicit default site. Use matching site IDs for the same building: RF's `default`
is not automatically mapped to camera's `site-01`. Drain camera outboxes before changing site
mappings, since an old camera message has no site ID and would otherwise receive the new site.

Size limits for actual message volume: one unique message/second produces 1,209,600 dedupe
entries in 14 days, exceeding the example cap. At dedupe capacity ingestion pauses instead of
forgetting live IDs. SQLite needs disk headroom beyond its main-file cap for WAL/temp files.

## Startup

### Source

```sh
uv sync --frozen
uv run --env-file .env fusion-service check-config --config config.yaml
uv run --env-file .env fusion-service serve --config config.yaml --host 127.0.0.1 --port 8090
```

`check-config` checks YAML, required secret variables and reference schemas. It does not contact
brokers or create the database. Missing storage/config/schema files fail startup clearly.

### Central host containers

Ensure `config.yaml` exists as a file. Keep camera relay/broker and RF Kismet/Console/broker
running. To reach the current RF broker (`rf-mqtt`) on its external `fusion` network:

```sh
docker compose -f compose.yaml -f compose.fusion.yaml config --quiet
docker compose -f compose.yaml -f compose.fusion.yaml up -d --build
```

The RF deployment supplies that external network. Camera can stay at the central LAN IP in
the YAML. Camera credentials need read access to the three `detection/camera/` topics, QoS 1
subscriptions and a persistent session. Use fusion's own client ID, never the relay API's ID.

Without the external network requirement, use `docker compose up -d --build`. With a single
shared broker, configure one connection with the required subsystems. Keep namespaces stable so old
messages do not gain new deduplication identities.

The API binds host loopback port 8090 by default, avoiding the existing 8080 APIs. Set
`FUSION_BIND_ADDRESS` / `FUSION_API_PORT` for the intended consumer network. The API serves HTTP;
use the deployment's protected network or existing TLS proxy as appropriate.

## Using it

### Physical MQTT integration

Fusion reads the versioned `detection/physical/...` feed directly from Mosquitto, not the
physical REST API, SQLite file, panel, or native `blackrelay/v1/...` topics. Physical's optional
team publisher must be enabled (`fusion.enabled: true` and its `fusion` Compose profile).
The default physical publisher uses `rf-mqtt:1883`, so the example subscribes to RF and physical
using one connection. Physical's own TLS broker on 8883 is a different interface.

Set `physical_node_id` to a stable identity for the physical bridge, such as
`physical-bridge-01`. All physical messages use that top-level `node_id`; individual sensor
identities remain in `data.zone.id`. Only panel status updates `/nodes`; sensor events and zone
status remain in `/events`. Panel status becomes stale after 120 seconds without a fresh report.
The v0 source contract lacks a panel/bridge ID: assume one bridge per site on a connection.
Multiple bridges in the same site need separate topic routing/identity agreement first.

Physical's `site.id` is authoritative. `data.seq` and `data.prev_seq` remain the physical log
sequence/chain; they never replace fusion's REST cursor. Keep `synthetic` and sensor `stale`
flags when interpreting detections. A zone is an enrolled sensor, not automatically a shared
camera/RF spatial zone. Fusion does not create alerts or infer motion duration from panel delays.

`config.physical.example.yaml` configures only the physical bench's team broker at loopback
11883 for local use. It needs only `FUSION_API_TOKEN`, not camera credentials. Run fusion
from source on the same host as the bench, or update the broker address for container networking.

### REST consumers

All three API routes require a bearer token. These examples assume it is exported in the shell.

```sh
curl -H "Authorization: Bearer $FUSION_API_TOKEN" http://127.0.0.1:8090/api/v1/health
curl -H "Authorization: Bearer $FUSION_API_TOKEN" 'http://127.0.0.1:8090/api/v1/events?limit=100'
curl -H "Authorization: Bearer $FUSION_API_TOKEN" 'http://127.0.0.1:8090/api/v1/events?after=123&limit=100'
curl -H "Authorization: Bearer $FUSION_API_TOKEN" http://127.0.0.1:8090/api/v1/nodes
```

Process each page before saving `next_cursor`. Continue while `has_more`, then wait and poll
again. Late events use new cursors with their original source timestamps. See
[API-CONTRACT.md](API-CONTRACT.md) for filters and explicit retention-gap recovery.

`/docs` offers interactive OpenAPI with bearer authorization. `/openapi.json` is the generated
schema. Documentation routes are public and contain no deployment secrets.

## Verification and troubleshooting

Smoke tests cover the HTTP surface (authentication, error envelope, empty pages, health) with
the MQTT connections disabled. Static/package checks:

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

After deployment, inspect `/health`, confirm every broker is connected/subscribed, wait for
node heartbeats, then poll `/events` and `/nodes`. A broker connection alone does not establish
sensor liveness: inspect message times and node freshness.

| Symptom | Check |
|---|---|
| Startup fails | Config/schema paths, required variables, storage permissions and database version |
| Degraded health, REST works | Broker `last_error`, network membership, subscription grants |
| No RF events | RF Console profile enabled; `rf-mqtt` reachable on `fusion` |
| No camera events | Camera host/credentials/topic permissions, unique persistent client ID |
| Rejections increase | Logged reason codes, source versions, site mappings and clock |
| Storage not ready | Disk and dedupe/database capacity; QoS 1 waits for retry |
| Replayed node is stale | Source timestamps remain old even if just received |
| HTTP 410 | Explicitly accept the retention gap using returned `resume_after` |
| HTTP 400 `cursor_ahead` | Cursor belongs to a later/different database; coordinate reset |

For offline backup, stop fusion, copy its whole data volume, then restart. Do not copy only a
live `.sqlite3` file while ignoring WAL. Preserve the volume on upgrades. Database restoration
or replacement requires cursor coordination with consumers.

## Change notes

- 2026-11-17: Initial camera/RF normalization, SQLite persistence and JSON/REST deployment bundle.
- 2026-11-17: Added physical team-MQTT events, panel/zone status, bridge freshness and configuration.
