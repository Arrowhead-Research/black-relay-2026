# RF subsystem agent notes (detection/wifi-dev)

Living handoff for agents working on Black Relay `detection/rf-subsystem`. Keep this factual; **never add credentials, tokens, or secrets**.

## Current state (validated)

- **Nodes:** `wbt-1`–`wbt-4` on Raspberry Pi OS, running Kismet remote capture helpers (**`2026-10-R1`**, same as central) to central Kismet. Datasource names map to node IDs: `wbt-1:wifi|bt|tpms` etc. Capture targets: `wlan1` (Wi‑Fi, external mt7921u), `hci0` (BT), `rtl433-0` (TPMS). **Do not capture `wlan0`** (backhaul/SSH). Live capture may be `nohup`/non-persistent until repo systemd units in `node/systemd/` are installed with per-node `/etc/rf-node/rf-node.env` (**no secrets**).
- **Central:** Kismet + MQTT via `central/docker-compose.yml`. Kismet creds live in ignored `central/.env` (gitignored; see `central/README.md`). Use env vars / `--password-env` for CLI; do not commit `.env`.
- **Analytics ingest v1:** `rfanalytics kismet` polls Kismet REST and emits validated `rf.observation.v0` JSONL (one record per emitter × seeing node when `last_time` advances). Node ID = datasource name prefix before `:`. CLI is lazy-loaded so `kismet` works without `pyyaml`/full analytics deps; `run/score/friendly-from-truth` still require their deps (`uv sync` in `analytics/`).
- **Live pipeline (validated on real nodes):** `rfanalytics live` = Kismet poll → friendly exclusion → analyzer → alert rules → MQTT via durable outbox, one process. Compose service `rf-analytics` (profile `analytics`); config from `analytics/config/` chosen by `RF_SITE`/`RF_RULES`/`RF_FRIENDLY` in `central/.env`.
- **Per-node RSSI:** requires `keep_per_datasource_stats=true` in `central/kismet/kismet_site.conf` (Kismet's default leaves every seenby signal empty). With it, `attributes.rssi_is_aggregate` should not appear.
- **Node radio MACs** (node hardware, not site data): `analytics/config/friendly.wbt-nodes.example.yaml` and `self_emitters` in the dfviz layout. Confirm with `ip link show wlan0`.
- **Direction finding (`dfviz/`, compose profile `dfviz`, port 8080):** server smooths per-node RSSI (Kismet `signal_rrd`, 3×MAD outliers, exponential weights, per-band windows) and serves it; browser localizes with log-distance path loss, per-node offsets, wall loss over **expected** wall crossings (wall `sd_ft`), a transmit-power prior, and a 15 s temporal filter. No heat map and no model-derived error circle (user decisions); accuracy = calibration's leave-one-out error.
  - **Calibration:** spots recorded from the Kismet packet log (selected device, median per node, `pos_sd_ft` chosen at record time); weighted LS (weights from spot and `node_sd_ft` position accuracy); `calBest()` fits candidates (offsets × walls × tx spread 4/8/16) and `calAutoApply` saves the best by LOO median if it beats the default model (no manual parameters; gated on spots being loaded). `calSuggest()` = greedy Ds-optimal design → targets A–C; quality chips vs goals.
  - **Data:** server-owned, journaled, one operation per change (`/api/calibration/*`, `/api/walls/*`; whole-list `POST /api/walls` returns 410); pages adopt server state only when `calibration_version` is not older than their last edit.
  - **Keep it site-agnostic.** Site data stays local: `dfviz/site/layout.yaml` and `dfviz/site/NOTES.md` (gitignored), and walls/spots/fits in the `rf-dfviz-data` volume under `/data/<DFVIZ_SITE>/`. Committed docs carry only general lessons (see `dfviz/README.md`, *Lessons learned*).
  - **Gotchas:** read Kismet's live log with `mode=ro` + retries, never `immutable=1` (malformed pages); serve static files `no-cache`; never let a page save whole lists.
- **Bluetooth capture (fixed 2026-11-08):** root cause = UB500 (RTL8761BU) keeps "discovering" after its helper exits, so after a Kismet restart the reconnected helper gets "Busy" forever (source shows running, 0 packets). `node/scripts/rf-bt-capture.sh` (kismet-cap-bt.service ExecStart) pins the adapter (`BT_ADDR` via `btmgmt info`; sysfs has no address on these kernels), clears discovery, runs the helper with `--disable-retry` so systemd handles reconnects. `btmgmt` needs an open stdin (`< <(sleep N)`), or it exits before answering. `rf-bt-doctor.sh [--fix]` diagnoses/repairs. dfviz `SourceHealth` flags silent/stopped sources (any counter change = activity; TPMS exempt). Wi-Fi and TPMS helpers are still started by hand on the nodes (not persistent across reboot).
- **Tests:** run in a container if uv is not installed: from `detection/rf-subsystem`, `docker run --rm -v "$PWD":/src:ro ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim sh -c 'cp -r /src /work && cd /work/analytics && uv sync -q --frozen --extra dev --extra mqtt && uv run --frozen pytest -q && uv run --frozen pytest -q -p no:cacheprovider ../dfviz/tests'`.

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

- **Per site:** measure node positions/zones (analytics site file and `dfviz/site/layout.yaml`), set `calibration_db`, draw walls, and calibrate with a carried phone hotspot. Live zones are meaningless until then.
- **Live direction-finding map** still uses Kismet's per-second RSSI history (peaks, last 60 s); calibration uses the packet log. Moving the live map to the packet log (~10 s lag) would make them consistent.
- **Live gotchas fixed 2026-11-07 (keep in mind):** (1) paho-mqtt calls `on_publish` under its own mutex, so `Outbox` must never hold its lock across `publish()` (deadlocked the service). (2) Kismet seenby `last_time` can be far older than the device's `last_time`; `live` drops observations older than `--max-age-s` (first poll: `--backfill-s`) or it replays history and raises false node-offline alerts.
- **Alert location on visit-start rules:** watchlist/new-device/repeat-visitor alerts have `location.zone: null` (they fire before the first zone estimate). Consider delaying or following up with a zone.
- **TPMS/RTL433:** mapping is best-effort until verified with real TPMS decodes in Kismet device JSON; refine `_tpms_id`/attributes when live RTL433 devices appear.
- **Persistence:** install/enable repo systemd capture units on Pis; add `rf-node-agent`, registration/heartbeats, Tailscale/MagicDNS as designed.
- **Integration:** fusion MQTT message standard (none published on any branch as of 2026-11-07; survivability branches define the Detection VLAN `10.73.200.0/24` via a Headscale subnet router), camera cue routing/volume, TAK-pushed config TBD (`analytics/src/rfanalytics/formats.py` is the wire boundary). Identity linking/co-traveler/persistence/UI are later.

## Safety

- Do not commit `central/.env`, passwords, API keys, or local dumps like `/tmp/rf-live-obs.jsonl`.
- `central/.env` and local artifacts should remain gitignored; verify with `git status` before committing.
