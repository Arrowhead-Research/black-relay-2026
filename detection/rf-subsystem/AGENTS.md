# RF subsystem agent notes (detection/wifi-dev)

Living handoff for agents working on Black Relay `detection/rf-subsystem`. Keep this factual; **never add credentials, tokens, or secrets**.

## Current state (validated)

- **Nodes:** `wbt-1`–`wbt-4` on Raspberry Pi OS, running Kismet remote capture helpers (**`2026-10-R1`**, same as central) to central Kismet. Datasource names map to node IDs: `wbt-1:wifi|bt|tpms` etc. Capture targets: `wlan1` (Wi‑Fi, external mt7921u), `hci0` (BT), `rtl433-0` (TPMS). **Do not capture `wlan0`** (backhaul/SSH). Live capture may be `nohup`/non-persistent until repo systemd units in `node/systemd/` are installed with per-node `/etc/rf-node/rf-node.env` (**no secrets**).
- **Central:** Kismet + MQTT via `central/docker-compose.yml`. Kismet creds live in ignored `central/.env` (gitignored; see `central/README.md`). Use env vars / `--password-env` for CLI; do not commit `.env`.
- **Analytics ingest v1:** `rfanalytics kismet` polls Kismet REST and emits validated `rf.observation.v0` JSONL (one record per emitter × seeing node when `last_time` advances). Node ID = datasource name prefix before `:`. CLI is lazy-loaded so `kismet` works without `pyyaml`/full analytics deps; `run/score/friendly-from-truth` still require their deps (`uv sync` in `analytics/`).
- **Live pipeline (validated 2026-11-07):** `rfanalytics live` = Kismet poll → friendly exclusion → analyzer → alert rules → MQTT via durable outbox, one process. Runs as compose service `rf-analytics` (profile `analytics`) in `central/`; config files from `analytics/config/` chosen by `RF_SITE`/`RF_RULES`/`RF_FRIENDLY` in `central/.env`. Real-device run on `wbt-1..4`: ~100 obs/poll, alerts (watchlist, loiter) received on `detection/rf/alerts` by `mosquitto_sub`, outbox drained to 0.
- **Per-node RSSI works:** requires `keep_per_datasource_stats=true` in `central/kismet/kismet_site.conf` (Kismet default false leaves every seenby signal `type: none`). Now 100% of seenby entries carry signal; the aggregate fallback (`attributes.rssi_is_aggregate=true`) should not appear.
- **Node backhaul MACs (inferred from RSSI, confirm on nodes):** wbt-1 `2c:cf:67:c0:76:71`, wbt-2 `2c:cf:67:c0:76:1b`, wbt-3 `2c:cf:67:c0:76:dd`, wbt-4 `2c:cf:67:c0:72:98` → `analytics/config/friendly.wbt-nodes.example.yaml`.
- **Tests:** run in a container (no uv on the host): from `detection/rf-subsystem`, `docker run --rm -v "$PWD":/src:ro ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim sh -c 'cp -r /src /work && cd /work/analytics && uv sync -q --frozen --extra dev --extra mqtt && uv run --frozen pytest -q'`. Site mapping `analytics/config/site.wbt-nodes.example.yaml` is still placeholder geometry.

## How to run (examples)

```sh
# Poll Kismet once (no secrets on command line in shared envs)
cd detection/rf-subsystem/analytics
PYTHONPATH=src python3 -m rfanalytics.cli kismet --once \
  --kismet-url http://<kismet-host>:2501 --user "$KISMET_USER" --password-env KISMET_PASSWORD \
  -o observations.jsonl

# Live pipeline on the central host (Kismet -> alerts -> MQTT)
cd detection/rf-subsystem/central
docker compose --profile analytics up -d --build
docker compose --profile analytics logs -f rf-analytics          # stats line every ~minute
docker compose exec mqtt mosquitto_sub -t 'detection/rf/#' -v    # what fusion would receive
```

## Known gaps / next work

- **Site geometry and calibration:** measure node positions/zones, set `calibration_db`, then a walk test (phase 3, ≥80% zone accuracy). Live zones are meaningless until then.
- **Live gotchas fixed 2026-11-07 (keep in mind):** (1) paho-mqtt calls `on_publish` under its own mutex, so `Outbox` must never hold its lock across `publish()` (deadlocked the service). (2) Kismet seenby `last_time` can be far older than the device's `last_time`; `live` drops observations older than `--max-age-s` (first poll: `--backfill-s`) or it replays history and raises false node-offline alerts.
- **Alert location on visit-start rules:** watchlist/new-device/repeat-visitor alerts have `location.zone: null` (they fire before the first zone estimate). Consider delaying or following up with a zone.
- **TPMS/RTL433:** mapping is best-effort until verified with real TPMS decodes in Kismet device JSON; refine `_tpms_id`/attributes when live RTL433 devices appear.
- **Persistence:** install/enable repo systemd capture units on Pis; add `rf-node-agent`, registration/heartbeats, Tailscale/MagicDNS as designed.
- **Integration:** fusion MQTT message standard (checked all remote branches 2026-11-07: nothing published; survivability branches only define the Detection VLAN `10.73.200.0/24` via a Headscale subnet router), camera cue routing/volume, TAK-pushed config TBD (`analytics/src/rfanalytics/formats.py` is the wire boundary). Identity linking/co-traveler/persistence/UI are later.

## Safety

- Do not commit `central/.env`, passwords, API keys, or local dumps like `/tmp/rf-live-obs.jsonl`.
- `central/.env` and local artifacts should remain gitignored; verify with `git status` before committing.
