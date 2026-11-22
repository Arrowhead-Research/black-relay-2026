# Harness convention (what an implementation must provide)

Run: `harness/run.sh <impl_dir> [pytest args, e.g. -k stall -x]`

An implementation directory must contain an executable **`<impl_dir>/harness_start.sh`**:

    harness_start.sh <config.yaml>

* runs the bridge **in the foreground** (the harness SIGTERMs it, waits 10 s, then SIGKILLs; process-group kill is used, so
  child processes / compose-less multi-process designs are fine). Must `exec` or otherwise stay alive while the bridge runs.
* may build on first call (the harness gives it up to 600 s to become healthy the first time, 60 s afterwards);
  cwd is `<impl_dir>`.
* stdout/stderr are captured to `harness/work/<run>/bridge-<ms>.log` (one file per start).
* the harness sets env `BR_HARNESS=1`; the impl may use it to relax things that only make sense in compose (nothing in the
  one-way / TLS / failover logic may differ).
* the config.yaml follows `harness/config.example.yaml` plus the contract's test hooks, with these harness values:
  `iq_remote.panel_ip: 127.0.0.1`, `iq_remote.panel_port: <port>`, `iq_remote.pki_dir: <work>/pki`
  (contains one dir `<12hex>/` with `<id>.key`, `<id>.secure`, `<id>.qolsys`), `iq_remote.ca_file: <work>/sim-ca.crt`,
  `control4.enabled: true`, `control4.host: 127.0.0.1`, `control4.port: <port>`, `control4.token_file`, `control4.cert_sha256`,
  short timers (`silence_probe_s: 4, probe_timeout_s: 2, backoff_initial_s: 1, backoff_max_s: 2, fallback_after_s: 3`),
  `mqtt.host/port` = harness Mosquitto (TLS, user `blackrelay`, `ca_file`, `password_file`), `api.port`, `api.tls_cert/tls_key`
  (harness-generated, SAN 127.0.0.1), `api.metrics_port`, `storage.data_dir: <work>/data` (persistent across restarts within one test),
  `storage.retention_days: 30` (a prune test restarts with 1 and a pre-aged DB — see test; the prune test finds the
  events table by scanning the data dir for `*.db`, `*.sqlite`, `*.sqlite3`, and is SKIPPED if none is found; a
  `harness-prune-schema:` line in HARNESS.md is not read).
* API keys: the config's `api.keys` holds an argon2 hash; harness uses the key `br_test_key_0123456789` and generates
  the argon2id hash with `argon2-cffi` (installed in harness venv) — keys list name `harness`. Impl must accept it as `Authorization: Bearer ...` and `X-API-Key`.
* Readiness: harness polls `GET https://127.0.0.1:<api.port>/v1/health` (CA = harness CA).
* Optional `<impl_dir>/HARNESS.md` documents quirks; the current harness reads none of it.
* Pairing PKI: harness creates it; the impl just reads it. The sim panel CA is `<work>/sim-ca.crt`.

## Scenarios (conformance/, each is one pytest test; output shows PASS/FAIL/SKIP per test)
test_00_broker: broker TLS-only+auth; consumers (consumer/ha users, MQTT 3.1.1 and 5, QoS0/1, retained, empty-retained wipe) cannot publish
  to blackrelay/# or homeassistant/# (v5 PUBACK reason >= 0x80, nothing delivered, retained untouched); bridge user may write only its prefixes.
test_10: initial sync (MQTT retained == REST == simulator, overrides, ignore, no broker subscriptions by the bridge); 401 matrix + key headers + TLS-only API;
  no mutating verbs (404/405); metrics.
test_20: live changes (gap-free seq from 1, MQTT+SSE+REST agree, duplicate suppressed, chains consistent, arming/alarm/battery); Last-Event-ID resume; heartbeat;
  burst with concurrent readers.
test_30: primary outage -> control4 (status degraded) -> primary back (online, no spurious events); TCP stall (SIGSTOP); app-level silence; total outage => stale
  everywhere + recovery; flapping primary race test.
test_40: reconnect with diverged state => synthetic events == diff; C4-only removal caught by SUMMARY diff; wrong C4 pin => no bytes sent; null pin => refuse;
  untrusted IQ cert => no commands sent; IQ cert rotation recovers.
test_50: SQLite persistence + seq continuity; SIGKILL durability + SSE resume; retention prune (finds an events table with `seq` + timestamp column in any *.db/*.sqlite
  under storage.data_dir and ages it 40 days; SKIPPED if not discoverable); 500 SSE open/close => RSS/fd bounded.
test_zz: static grep audit + whole-run zero state-changing messages. EVERY test additionally fails if the simulator audit log records one violation.

## Contract details the harness assumes where CONTRACT.md is silent (tell harness owner if your impl differs)
* /v1/zones, /v1/partitions, /v1/events return either a bare JSON list or {"zones"|"partitions"|"events": [...]}; zone record has `id` ("zone-12"), `panel_zone_id`, `kind`, `state`, `name`, `stale`, `source`, `battery_low`...
* event `subject` is the zone id string ("zone-12") for zone.* and the partition id ("0") for partition.*; `from`/`to` are scalar values (zone.state: open|closed|active|idle).
  zone.added / zone.removed / zone.battery may carry any `to`. Synthetic (diff-derived) events have `synthetic: true`; live ones false.
* partition id is "0"/"partition-0"; arming values per CONTRACT. /v1/health: status, active_source, last_seq (int), seconds_since_panel_message.
* every MQTT zone payload has the CONTRACT fields. Removed zones: retained state cleared (empty payload) or online=false.
* The sim panel CA has a NON-critical BasicConstraints and no KeyUsage (like the real panel CA) and Python 3.13/rustls/Go strict verifiers may need
  relaxed X.509 checks for that CA only (the library clears VERIFY_X509_STRICT). Its leaf certs carry SAN IP:127.0.0.1 and AKI.
* Control4: SHA-256 is over the DER leaf cert; framing = concatenated JSON objects (PANEL_PROTOCOL.md part 2).
Standalone simulator for manual work: `harness/.venv/bin/python -m sim.run_standalone` (see sim/run_standalone.py).
Self-test of the simulator: `harness/.venv/bin/python -m pytest harness/sim/tests` (the single-file reference bridge `harness/dummy` is not in this tree).
