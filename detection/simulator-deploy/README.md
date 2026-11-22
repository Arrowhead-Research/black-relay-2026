# Simulator stack

Simulated RF, panel and camera events behind a real fusion API, on one machine. It has its own
broker and fusion, so it never touches the real central server, and it is built from this checkout:
no registry, no bundle.

How to run it: [RUNBOOK.md](RUNBOOK.md).

| Service | What it does |
|---|---|
| `broker` | MQTT for the services below. Not reachable from outside |
| `fusion` | Stores events and serves the API. The only published port |
| `rfsim` | Replays a one-hour RF scenario in real time, then starts again. Its alerts come about 21 minutes in |
| `rf-analytics` | Turns the RF simulator's observations into alerts (three per hour in this scenario) |
| `physical-sim` | A simulated alarm panel with the real panel bridge reading it |
| `mock-camera.sh` | Publishes one fake camera kit's heartbeat and one person walking through (start, update, end) |
| `mock_rf.sh` | Publishes one fake RF alert, or one RF position |
