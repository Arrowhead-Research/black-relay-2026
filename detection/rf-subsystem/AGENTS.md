# RF subsystem agent notes (detection/wifi-dev)

Living handoff for agents working on Black Relay `detection/rf-subsystem`. Keep this factual; **never add credentials, tokens, or secrets**.

## Current state (validated)

- **Nodes:** `wbt-1`–`wbt-4` on Raspberry Pi OS, running Kismet remote capture helpers (**`2026-10-R1`**, same as central) to central Kismet. Datasource names map to node IDs: `wbt-1:wifi|bt|tpms` etc. Capture targets: `wlan1` (Wi‑Fi, external mt7921u), `hci0` (BT), `rtl433-0` (TPMS). **Do not capture `wlan0`** (backhaul/SSH). Live capture may be `nohup`/non-persistent until repo systemd units in `node/systemd/` are installed with per-node `/etc/rf-node/rf-node.env` (**no secrets**).
- **Central:** Kismet + MQTT via `central/docker-compose.yml`. Kismet creds live in ignored `central/.env` (gitignored; see `central/README.md`). Use env vars / `--password-env` for CLI; do not commit `.env`.
- **Analytics ingest v1:** `rfanalytics kismet` polls Kismet REST and emits validated `rf.observation.v0` JSONL (one record per emitter × seeing node when `last_time` advances). Node ID = datasource name prefix before `:`. CLI is lazy-loaded so `kismet` works without `pyyaml`/full analytics deps; `run/score/friendly-from-truth` still require their deps (`uv sync` in `analytics/`).
- **Validation:** adapter mapping/contract/dedupe tests in `analytics/tests/test_kismet.py` pass; live smoke previously produced 695 valid observations across `wbt-1..4`. Starter site mapping: `analytics/config/site.wbt-nodes.example.yaml` (placeholder geometry — replace with real coordinates/zones + `calibration_db`).

## How to run (examples)

```sh
# Poll Kismet once (no secrets on command line in shared envs)
cd detection/rf-subsystem/analytics
PYTHONPATH=src python3 -m rfanalytics.cli kismet --once \
  --kismet-url http://<kismet-host>:2501 --user "$KISMET_USER" --password-env KISMET_PASSWORD \
  -o observations.jsonl

# Full analytics (requires deps): uv sync && uv run rfanalytics ...
# kismet ... -o - | uv run rfanalytics run --site config/site.wbt-nodes.example.yaml --input - -o events.jsonl --rules config/rules.example.yaml
```

## Known gaps / next work

- **Per-node RSSI:** Kismet per-seenby signal was unpopulated in observed data; adapter falls back to aggregate device RSSI and sets `attributes.rssi_is_aggregate=true`. This reduces multi-node localization fidelity — investigate per-source signal fields/config in Kismet/capture.
- **TPMS/RTL433:** mapping is best-effort until verified with real TPMS decodes in Kismet device JSON; refine `_tpms_id`/attributes when live RTL433 devices appear.
- **Persistence:** install/enable repo systemd capture units on Pis; add `rf-node-agent`, registration/heartbeats, Tailscale/MagicDNS as designed.
- **Integration:** fusion MQTT message standard, camera cue routing/volume, TAK-pushed config TBD (`analytics/src/rfanalytics/formats.py` is the wire boundary). Identity linking/co-traveler/persistence/UI are later.

## Safety

- Do not commit `central/.env`, passwords, API keys, or local dumps like `/tmp/rf-live-obs.jsonl`.
- `central/.env` and local artifacts should remain gitignored; verify with `git status` before committing.
