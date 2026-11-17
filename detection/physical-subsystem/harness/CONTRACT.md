# BlackRelay black-box contract (all implementations)

Every candidate implementation is judged ONLY through these external interfaces by the shared
conformance harness in harness/harness. Internal design (language, IPC, cache/diff algorithm,
process split) is free.

## Task (from the owner)
Strictly one-way, locally hosted bridge routing PowerG sensor state (glass break, motion, door/window)
from a Qolsys IQ5 panel to consumers. No data, command or write may ever flow back to the panel beyond the
minimum read-only session handshake/queries the panel protocol requires.
- Ingest: IQ Remote adapter (primary): the panel's local MQTT broker over TLS (client certificate from the
  pairing PKI). Control4 adapter (fallback): JSON over TLS, port 12345, token auth, panel cert pinned by SHA-256.
- Normalizer + state cache: map zones to kind (glass_break | motion | contact | other), track stale flags,
  diff a FULL snapshot after every (re)connect and emit synthetic events for differences.
- Failover: primary not live for `timers.fallback_after_s` -> fallback takes over; primary live again -> back.
- Outbound: 30-day SQLite event log with gap-free monotonic `seq`; read-only Mosquitto broker on TLS 8883;
  REST/SSE API on HTTPS 8443 with API keys; Prometheus metrics on 9100. Docker Compose deployment.

## Configuration
YAML file, schema = harness/config.example.yaml (every key must be honored; ports may be overridden
by the harness). Started as: `<binary-or-module> run --config <path>`.
Test hooks the harness relies on: `iq_remote.panel_ip` + optional `iq_remote.panel_port` (default 8883) and
`iq_remote.pki_dir`; `control4.host/port/token_file/cert_sha256`; all timers; `mqtt.*`; `api.*`; `storage.*`.
Also accept optional `iq_remote.ca_file` (panel CA to trust) when the harness uses a simulated panel.

## Outputs (identical across implementations)
MQTT (prefix blackrelay/v1), JSON payloads:
- `zones/{zone_id}/state` retained, QoS1. zone_id = "zone-<panel zone number>".
  Fields: id, panel_zone_id, name, kind, partition_id, state (open|closed|active|idle|unknown), tamper,
  battery_low, online, last_changed, last_seen, source (iq_remote|control4|null), stale, raw.
- `partitions/{id}/state` retained, QoS1: id, name, arming (disarmed|arm_stay|arm_away|arm_night|exit_delay|
  entry_delay|alarm|unknown), alarm_type, last_changed, source, stale.
- `events` QoS1 not retained: seq, id, ts, type (zone.state|zone.tamper|zone.battery|zone.online|zone.added|
  zone.removed|partition.arming|partition.alarm|bridge.link), subject, from, to, source, synthetic.
- `bridge/status` retained: online | degraded | offline (also the MQTT last will).
- The bridge never subscribes to command topics.
HTTPS (Bearer API key or X-API-Key; /v1/health is unauthenticated):
GET /v1/zones[?kind=&partition=], /v1/zones/{id}, /v1/partitions, /v1/events?after=&limit=,
/v1/stream (SSE, id: = seq, honors Last-Event-ID, heartbeats), /v1/health
(status, active_source, links, last_panel_message_at, seconds_since_panel_message, zones, partitions, last_seq).
No mutating HTTP methods. Metrics: GET :9100/metrics (Prometheus text).

## Deliverables per implementation (inside its own directory)
Source, Dockerfile(s), deploy/docker-compose.yml (+ mosquitto.conf, acl, cert script), SQL schema file,
unit tests, and a `HARNESS.md` saying exactly how the harness starts it locally (command + env).
