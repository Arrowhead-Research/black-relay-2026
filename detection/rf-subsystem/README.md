# RF Subsystem

## Ownership

- **Subsystem lead:** Bryan Jones

## Purpose and Scope

The RF subsystem gives the safehouse passive, receive-only awareness of nearby Wi-Fi, Bluetooth/BLE, and TPMS emitters. Distributed WBT nodes stream their captures to a central Kismet server. An analytics service characterizes the emitters, places them in zones, learns patterns of life, and publishes alerts and camera cues to the fusion service over MQTT. The full design is in [`DESIGN.md`](DESIGN.md).

**Current state:** the WBT node hardware is not yet available. The central Kismet stack, a scenario simulator, and the first phase of `rf-analytics` (ingest, friendly exclusion, visits, zone localization) are built and tested on simulated data. Alerting, the Kismet ingest adapter, and the node software are not built yet. The node assembly and startup steps below are drafts that still need to be validated on hardware.

## File Tree

```text
rf-subsystem/
├── DESIGN.md                    # Subsystem design: requirements, architecture, interfaces
├── README.md                    # This file
├── schemas/
│   └── observation.v0.schema.json   # Normalized observation contract (ingest → analytics)
├── analytics/                   # rf-analytics: ingest, exclusion, visits, localization (Python, uv)
│   ├── src/rfanalytics/         # Analyzer, localization, friendly list, scorer, CLI
│   ├── config/                  # Example friendly list
│   └── tests/                   # Contract, business-rule, and scenario tests
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

> **Draft: not yet validated on hardware.** These steps are written from the BOM ([`../docs/BOM.md`](../docs/BOM.md)) and the node design (DESIGN.md section 6). Confirm them on the first assembled node and update this section.

The central service has no physical assembly. It runs as Docker containers on the safehouse central server (see [Startup](#startup)). The steps below are for **WBT nodes**. HackRF nodes are not part of the current build.

### Parts per WBT node

| Qty | Part | Notes |
|---:|---|---|
| 1 | Raspberry Pi 5 8GB Essentials Kit | Pi, case, active cooler, microSD card, USB-C power supply. |
| 1 | Alfa AWUS036AXM Wi-Fi adapter | Includes 2 RP-SMA antennas and a USB cable. |
| 1 | TP-Link UB500 Plus Bluetooth adapter | Includes an external antenna. |
| 1 | Nooelec NESDR SMArt v5 SDR | Includes an SMA antenna. Used for TPMS at 315 MHz. |
| 1 | Powered USB hub with per-port switches | Comes with its own power adapter. |
| 3 | 10 ft USB extension cables | One per radio (from the 5-packs). |
| 1 | Ethernet cable | For wired backhaul, which is preferred (DESIGN.md open question 4). Not in the BOM yet. |

### Required Tools and Conditions

- **Tools:**
  - A computer with a microSD card reader and [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
  - A small Phillips screwdriver for the case.
  - A label maker or tape and a marker.
  - Cable ties or hook-and-loop straps.
  - A tape measure, to record node positions for localization.
  - A laptop with SSH and Tailscale, for setup.
  - Optional: an inline USB power meter, to diagnose power problems.
- **Accounts:** access to the team's Tailscale tailnet, and a pre-authorized auth key tagged `tag:rf-node`. Get the key from the RF lead and never commit it.
- **Power requirements:** two outlets per node.
  - **Pi 5:** USB-C PD supply rated 5 V / 5 A (27 W). Use the kit supply. A weaker supply limits USB output current, and the radios will disconnect at random.
  - **Powered USB hub:** its own wall adapter. All three radios draw power from the hub, not from the Pi.
- **Environment:** indoors or in a weather-protected enclosure. Keep the Pi's cooler unobstructed. Do not seal the node in direct sun without ventilation.

### Assembly Steps

1. **Inventory and inspect.** Lay out one node's parts using the table above. Check for damaged connectors or antennas.
2. **Assign a node ID.** Pick the next unused ID (`rf-node-01`, `rf-node-02`, and so on). **Label the Pi case, the hub, and each radio** with that ID. Every location estimate depends on these labels being correct.
3. **Assemble the Pi.** Fit the active cooler and close the case, following the kit instructions.
4. **Flash the OS.** On the setup computer, use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)** to the microSD card. In the Imager's OS customization settings:
   - set the hostname to the node ID
   - enable SSH with your public key
   - leave Wi-Fi unconfigured if the node will use wired backhaul

   Insert the card into the Pi.
5. **Attach the antennas.** Screw both antennas onto the Alfa adapter, the antenna onto the NESDR, and the antenna onto the UB500 if it is detachable. Finger-tight only.
6. **Connect the hub.** Connect the hub's upstream cable to one of the Pi's **USB 2 (black) ports**. This keeps USB 3 noise away from the 2.4 GHz radios (DESIGN.md 6.1). Plug in the hub's power adapter, but leave it unpowered at the wall for now.
7. **Connect the radios.** Connect each radio to its own hub port through a 10 ft extension cable. Position the radios so that the Alfa adapter, the UB500, and the NESDR are **at least 30 cm (1 ft) apart from each other** and away from the Pi and the hub. Secure the cables with ties.
8. **Connect backhaul.** Plug Ethernet into the Pi. If you use Wi-Fi backhaul instead, use the Pi's onboard Wi-Fi only, and register its MAC as friendly (DESIGN.md 6.6).
9. **Mount the node** at its planned location. Point the Alfa antennas vertically. Raise the radios where possible, for example on a window sill or a high shelf.
10. **Record the placement.** Note the zone, a short description, the approximate antenna facing direction, the height, and the x/y position in meters in the site grid. These values go into the node's `node.yaml` (DESIGN.md 6.4) and into the site layout (`simulator/sites/`).

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
   To process the feed, run `cd analytics && uv sync --extra mqtt && uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --mqtt-host 127.0.0.1 -o events.jsonl`. See [`analytics/README.md`](analytics/README.md).
3. **Bring up WBT nodes.** This is a draft until it is validated on hardware. The node agent and systemd units are not built yet, so steps 3.4–3.5 are manual for now.
   1. Make sure the central stack is running and the central server is on the tailnet as `rf-central`.
   2. **Power order:** power the USB hub **first**, wait about 5 seconds, then power the Pi. This way all radios are present when the Pi enumerates USB devices.
   3. **First boot only:** SSH in, then:
      - run `sudo apt update && sudo apt full-upgrade`
      - install Tailscale and join with the tagged auth key: `sudo tailscale up --auth-key=<key> --hostname=<node-id>`
      - add the Kismet apt repository and install the capture helpers pinned to `2026-10-R1` (see [`central/README.md`](central/README.md))
   4. **Check the radios** using the commands under [Testing and Verification](#testing-and-verification).
   5. **Start capture** against `rf-central`, using the stand-in node commands in [`central/README.md`](central/README.md). Confirm that each data source shows as running in the Kismet web UI (*Data Sources*).
   6. **Shutdown:** run `sudo poweroff` on the Pi, wait for the green LED to stop, then unplug the Pi and the hub.

## Testing and Verification

Focus checks on business rules, event definitions, and contracts with connected subsystems. Include manual checks when automation is not practical.

- **Automated checks:**
  - `cd simulator && uv run pytest` validates every scenario's output against `schemas/observation.v0.schema.json` and checks the scenario properties that analytics tests rely on.
  - `cd analytics && uv run pytest` covers the ingest contract, the analyzer's business rules, and end-to-end quality on scenarios, including the phase 3 target of at least 80% localization accuracy.
- **Manual verification:** `docker compose ps` in `central/` shows `kismet-server` as healthy. `curl -u <user>:<pass> http://127.0.0.1:2501/system/status.json` returns the Kismet version. The Kismet log shows `Launching remote capture server on 0.0.0.0 3501`.
- **Node hardware checks (manual, on each node):**

  | Check | Command | Expected result |
  |---|---|---|
  | Radios enumerated | `lsusb` | Entries for the MediaTek Wi-Fi adapter, the Realtek Bluetooth adapter, and the Realtek RTL2838 SDR. |
  | Wi-Fi adapter present | `iw dev` | A second wireless interface besides the onboard `wlan0`. |
  | Monitor mode supported | `iw phy` | `monitor` is listed under *Supported interface modes* for the Alfa's phy. |
  | Bluetooth adapter present | `bluetoothctl list` | Two controllers: the onboard one and the UB500. |
  | SDR working | `rtl_test -t` | The device is found and no errors are reported. Press Ctrl+C to stop. |
  | TPMS reception | `rtl_433 -f 315M` near a moving car | TPMS decodes appear. |
  | Tailnet reachable | `tailscale ping rf-central` | Replies arrive. |

- **Events or interfaces verified:** the observation contract (`rf.observation.v0`). MQTT publishing to the development broker has been checked manually. The fusion message format is not yet defined.

## Troubleshooting

| Symptom | Likely cause | Recovery |
|---|---|---|
| `kismet-server` is not healthy, or port 2501 refuses connections | Container still starting, or a port conflict on `BIND_ADDR` | Check `docker compose logs kismet-server`. Make sure nothing else uses ports 2501, 3501, or 1883. |
| Kismet login fails with the `.env` credentials | Credentials are only seeded on first start; the volume has older ones | Log in with the old credentials, or run `docker compose down -v` (this **deletes the Kismet logs**) and start again. |
| `docker compose build` fails at `apt-get install kismet-core=...` | The Kismet repository has moved past the pinned release | Bump `KISMET_VERSION` (see [`central/README.md`](central/README.md)) and update the nodes to match. |
| `ICAO database` error in the Kismet log | ADS-B data is not installed | None needed. This error is expected. |
| `rfsim ... --mqtt-host` fails with `MQTT output needs paho-mqtt` | The optional extra is not installed | Run `uv sync --extra mqtt`. |
| A radio disappears from `lsusb` at random | Not enough power | Make sure the radio is on the powered hub, not the Pi, and that the Pi uses the 27 W supply. Check `dmesg` for under-voltage warnings. |
| Wi-Fi interface names swap between boots | No persistent naming yet | Identify interfaces by MAC with `iw dev`. udev naming rules are planned (DESIGN.md 6.3). |
| Many Bluetooth or 2.4 GHz packets dropped | USB 3 interference, or radios too close together | Move the hub to a USB 2 port, separate the radios by at least 30 cm, and use the extension cables. |
| No TPMS decodes | Wrong frequency, or no vehicle moving nearby | Use `-f 315M` in the US. TPMS sensors only transmit while the vehicle moves or just after it starts. |
| A node does not show up on the tailnet | Auth key expired or untagged, or no backhaul | Run `tailscale status` on the node, check the Ethernet link, and re-run `tailscale up` with a valid tagged key. |

## Interfaces and Dependencies

| Interface | Direction | Transport | Status |
|---|---|---|---|
| WBT node → `kismet-server` | Inbound | Kismet remote capture (TCP 3501 or WebSocket 2501) over Tailscale | Server ready. Node mode to be confirmed. |
| `kismet-server` → `rf-analytics` | Internal | Kismet REST API and event bus | Adapter not built yet. |
| `rf-analytics` ingest | Internal | `rf.observation.v0` ([`schemas/observation.v0.schema.json`](schemas/observation.v0.schema.json)) over JSONL or MQTT | Implemented. Validated against the schema. |
| `rf-analytics` events | Internal | `rf.analytics-event.v0` (see [`analytics/README.md`](analytics/README.md)) | Implemented. Feeds the alert engine (next). |
| Alerts, node status → fusion service | Outbound | MQTT (draft topics `detection/rf/...`) | Waiting on the fusion message standard. |
| TPMS camera cue → camera subsystem | Outbound | MQTT, directly or through fusion | Routing to be decided (DESIGN.md open question 3). |
| Alert configuration from TAK | Inbound | To be decided | Waiting on the TAK team. |

**Dependencies:**
- **Tailscale tailnet:** connects nodes and the central server.
- **Docker** on the central server.
- **Kismet `2026-10-R1`** on both nodes and server.
- **Fusion service MQTT broker:** the development broker in `central/` stands in for it until it is available.
- **Camera subsystem:** receives TPMS cues.
- **TAK server:** pushed configuration, planned for later.

## Change Notes

| Date | Change |
|---|---|
| 2026-11-02 | Added `rf-analytics` phase 1: ingest, friendly exclusion, visits, zone localization, and a ground-truth scorer. |
| 2026-11-02 | Added the subsystem design (`DESIGN.md`), the central Kismet and MQTT stack, the observation schema, and the `rfsim` simulator. Drafted node assembly and startup steps; these are not yet validated on hardware. |
