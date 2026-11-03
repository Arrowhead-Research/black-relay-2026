# RF Subsystem

## Ownership

- **Subsystem lead:** Bryan Jones

## Purpose and Scope

The RF subsystem gives the safehouse passive, receive-only awareness of nearby Wi-Fi, Bluetooth/BLE, and TPMS emitters. Distributed WBT nodes stream their captures to a central Kismet server. An analytics service characterizes the emitters, places them in zones, learns patterns of life, and publishes alerts and camera cues to the fusion service over MQTT. The full design is in [`DESIGN.md`](DESIGN.md).

**Current state:** the WBT node hardware is not yet available. The central Kismet stack and a scenario simulator are built, so analytics development can start now. The node software, `rf-analytics`, and the hardware sections below are still to do.

## File Tree

```text
rf-subsystem/
├── DESIGN.md                    # Subsystem design: requirements, architecture, interfaces
├── README.md                    # This file
├── schemas/
│   └── observation.v0.schema.json   # Normalized observation contract (ingest → analytics)
├── central/                     # Docker Compose stack for the central server
│   ├── docker-compose.yml       # kismet-server + development MQTT broker
│   ├── .env.example             # Bind address, Kismet version, admin credentials
│   ├── kismet/                  # Kismet image: Dockerfile, site config, entrypoint
│   └── mosquitto/               # Development broker config
└── simulator/                   # rfsim: multi-node observation simulator (Python, uv)
    ├── src/rfsim/               # Engine, propagation model, devices, scenario loader, CLI
    ├── sites/                   # Site layouts (zones, node positions)
    ├── scenarios/               # Scripted test scenarios with expected analytics behavior
    └── tests/                   # Contract and scenario tests
```

## Physical Assembly

Document this so a person unfamiliar with the project can assemble the system from its parts. Include diagrams or photos where they clarify placement or connections.

### Required Tools and Conditions

- **Tools:** TODO
- **Power requirements:** TODO

### Assembly Steps

1. TODO: Identify and inspect all required parts.
2. TODO: Assemble the mechanical structure in a clear, ordered sequence.

## Configuration

Document settings or other configuration required before operation. Do not include secrets or credentials.

Central stack settings are in `central/.env` (copy it from `central/.env.example`):

| Setting | Required | Description / example |
|---|---|---|
| `BIND_ADDR` | Yes | Host address to expose ports on. Use `127.0.0.1` for development or the host's Tailscale IP. |
| `KISMET_VERSION` | Yes | Pinned Kismet release, `2026-10-R1`. Nodes must use the same version. |
| `KISMET_ADMIN_USER` / `KISMET_ADMIN_PASSWORD` | Yes | Kismet web admin account. Seeded on first start only. |

Node configuration (`/etc/rf-node/node.yaml`) is specified in DESIGN.md section 6.4 and is not implemented yet.

## Startup

**Prerequisites:** Docker with Compose v2, plus Tailscale for node connectivity. The simulator also needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

1. Start the central stack: `cd central && cp .env.example .env && docker compose up -d --build --wait`. See [`central/README.md`](central/README.md).
2. Without node hardware, feed the stack simulated data: `cd simulator && uv sync --extra mqtt && uv run rfsim run scenarios/loiterer.yaml --start-now --realtime --speed 60 --mqtt-host 127.0.0.1 -o /dev/null`. See [`simulator/README.md`](simulator/README.md).
3. WBT node startup and power-on order: TODO once the Pi 5 hardware arrives.

## Testing and Verification

Focus checks on business rules, event definitions, and contracts with connected subsystems. Include manual checks when automation is not practical.

- **Automated checks:** `cd simulator && uv run pytest`. These validate every scenario's output against `schemas/observation.v0.schema.json` and check the scenario properties that analytics tests rely on.
- **Manual verification:** `docker compose ps` in `central/` shows `kismet-server` as healthy. `curl -u <user>:<pass> http://127.0.0.1:2501/system/status.json` returns the Kismet version. The Kismet log shows `Launching remote capture server on 0.0.0.0 3501`.
- **Events or interfaces verified:** the observation contract (`rf.observation.v0`). MQTT publishing to the development broker has been checked manually. The fusion message format is not yet defined.

## Change Notes

As needed
