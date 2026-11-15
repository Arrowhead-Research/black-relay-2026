# F2 harness notes
Start: `harness_start.sh <config.yaml>` (creates `.venv` on first call, then `exec python -m br2 run --config ...`).
`br2 run` is a supervisor that spawns one OS process per service: normalizer, outbound, iq-remote, control4
(IPC = one-way Unix sockets in a private temp dir, `BR2_IPC_DIR`: `iq_remote.sock`, `control4.sock` (one per adapter,
hello-first, duplicate claims refused), `out.sock`; primary-link beacon file in the same dir). Env: none required.
SQLite log: `<storage.data_dir>/blackrelay.db`; schema: `schema.sql`. Outbound state (replay pointer, published-topic
set): `<data_dir>/outbound.*` (or `BR2_OUT_STATE_DIR`).
Team output (draft v0, off by default): `fusion.enabled: true` adds the publisher inside `outbound`; `br2 service fusion` runs it alone. State: `<out-state dir>/fusion/outbox.journal`. The harness leaves it disabled.
harness-prune-schema: schema.sql
