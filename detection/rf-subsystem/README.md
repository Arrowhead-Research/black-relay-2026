# RF Subsystem

## Ownership

- **Subsystem lead:** Bryan Jones

## Purpose and Scope

The RF subsystem gives the safehouse passive, receive-only awareness of nearby Wi-Fi, Bluetooth/BLE, and TPMS emitters. Distributed WBT nodes stream their captures to a central Kismet server. An analytics service characterizes the emitters, places them in zones, learns patterns of life, and publishes alerts and camera cues to the fusion service over MQTT. The full design is in [`DESIGN.md`](DESIGN.md).

**Current state:** the central Kismet stack, a scenario simulator, and `rf-analytics` (ingest, friendly exclusion, visits, zone localization, alert rules, camera cues, and durable MQTT output) are built and tested on simulated data. **The live pipeline is validated end to end on the four WBT nodes:** `rfanalytics live` (compose service `rf-analytics`) polls Kismet, runs analytics and alert rules on real devices with true per-node RSSI, and publishes alerts to MQTT (see [`analytics/README.md`](analytics/README.md#live-pipeline-kismet--alerts--mqtt)). TPMS and camera cues have not yet been exercised live, and the site geometry is still a placeholder. **Direction finding:** [`dfviz/`](dfviz/README.md) is a live web page (compose service `rf-dfviz`) that places Wi-Fi, Bluetooth and TPMS devices on the floor plan from per-node RSSI. Its model is fitted automatically from live calibration with a carried device, and the page reports the measured error at the calibration spots (see [Direction finding](#direction-finding-rf-dfviz)). Site files (layout, walls, calibration) stay local to each deployment. Example `systemd` capture units are in [`node/`](node/README.md); the `rf-node-agent` (registration, heartbeats) is not built yet. Node assembly remains draft; on Raspberry Pi 5 WBT nodes (Debian 13/trixie, arm64) the Kismet `2026-10-R1` capture helpers were installed and **legacy TCP remote capture to the central server (port 3501) was validated** for Wi‑Fi, Bluetooth, and TPMS (see [`central/README.md`](central/README.md)). WebSocket remote capture and production node provisioning still need validation.

## File Tree

```text
rf-subsystem/
├── AGENTS.md                    # Current-state handoff notes for agents and contributors
├── DESIGN.md                    # Subsystem design: requirements, architecture, interfaces
├── README.md                    # This file
├── schemas/
│   └── observation.v0.schema.json   # Normalized observation contract (ingest → analytics)
├── analytics/                   # rf-analytics: ingest, exclusion, visits, localization, alerts (Python, uv)
│   ├── src/rfanalytics/         # Kismet ingest, analyzer, localization, alert rules, wire format, outbox, scorer, CLI
│   ├── config/                  # Example friendly list, alert rules, and WBT node site mapping
│   └── tests/                   # Contract, business-rule, and scenario tests
├── central/                     # Docker Compose stack for the central server
│   ├── docker-compose.yml       # kismet-server + development MQTT broker
│   ├── .env.example             # Bind address, Kismet version, admin credentials
│   ├── kismet/                  # Kismet image: Dockerfile, site config, entrypoint
│   └── mosquitto/               # Development broker config
├── dfviz/                       # rf-dfviz: live RSSI direction-finding page (web UI, container)
│   └── site/                    # per-deployment layout and notes (local, gitignored)
├── node/                        # WBT node artifacts
│   ├── systemd/                 # Example Kismet remote-capture units and env template (no secrets)
│   ├── scripts/                 # rf-bt-capture (adapter by address/kind), rf-bt-doctor (diagnose/fix silent Bluetooth)
│   └── tests/                   # Adapter-selection tests (bash, fake sysfs)
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

Direction-finding settings (`central/.env` and the local `dfviz/site/layout.yaml`):

| Setting | Required | Description / example |
|---|---|---|
| `DFVIZ_PORT` | No | Host port of the direction-finding page (default `8080`, bound to `BIND_ADDR`). |
| `DFVIZ_DEMO` | No | `1` adds synthetic emitters with known positions (testing); empty for real data only. |
| `DFVIZ_SITE` | No | Name of the deployment site (default `default`). Walls, calibration spots and fitted models are stored per site, so a new location starts clean. |
| `dfviz/site/layout.yaml` | Yes, per site | Local, not committed: node positions in feet, how well they are known (`node_sd_ft`), the nodes' radio MACs and starting path-loss values. Copy from `dfviz/layout.example.yaml`. Without it the page runs on the example. |

Node configuration (`/etc/rf-node/node.yaml`) is specified in DESIGN.md section 6.4 and is not implemented yet.

## Startup

**Prerequisites:** Docker with Compose v2, plus Tailscale for node connectivity. The simulator also needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

1. Start the central stack: `cd central && cp .env.example .env && docker compose up -d --build --wait`. See [`central/README.md`](central/README.md).
2. Without node hardware, feed the stack simulated data: `cd simulator && uv sync --extra mqtt && uv run rfsim run scenarios/loiterer.yaml --start-now --realtime --speed 60 --mqtt-host 127.0.0.1 -o /dev/null`. See [`simulator/README.md`](simulator/README.md).
   To process the feed and publish alerts, run `cd analytics && uv sync --extra mqtt && uv run rfanalytics run --site ../simulator/sites/dev-site.yaml --mqtt-host 127.0.0.1 -o events.jsonl --rules config/rules.example.yaml --publish-host 127.0.0.1`. See [`analytics/README.md`](analytics/README.md).
   With live nodes instead, start the analytics service next to Kismet: `cd central && docker compose --profile analytics up -d --build`, and watch its output with `docker compose exec mqtt mosquitto_sub -t 'detection/rf/#' -v`. Set `RF_SITE`, `RF_RULES` and `RF_FRIENDLY` in `central/.env` to the real site files first; the example site geometry is a placeholder. See [`central/README.md`](central/README.md#live-analytics-kismet--alerts--mqtt).
3. **Bring up WBT nodes.** This is a draft until it is validated on hardware. The node agent is not built yet. Steps 3.4–3.5 can be run manually, or capture can be made persistent with the example systemd units in [`node/README.md`](node/README.md).
   1. Make sure the central stack is running and the central server is on the tailnet as `rf-central`.
   2. **Power order:** power the USB hub **first**, wait about 5 seconds, then power the Pi. This way all radios are present when the Pi enumerates USB devices.
   3. **First boot only:** SSH in, then:
      - run `sudo apt update && sudo apt full-upgrade`
      - install Tailscale and join with the tagged auth key: `sudo tailscale up --auth-key=<key> --hostname=<node-id>` (use a tagged, pre-authorized key; never commit it)
      - add the Kismet apt repository for the node's OS release (for example `trixie` on Debian 13 / recent Raspberry Pi OS) and install the capture helpers pinned to the **same release as the central server** (currently `2026-10-R1`). Install `rtl-433` as well: `kismet-capture-rtl433-v2` depends on the `rtl_433` decoder binary. See [`central/README.md`](central/README.md) for working remote-capture commands.
      - add the capture user to the `kismet` group if running helpers without root: `sudo usermod -aG kismet <user>` (new login required)
   4. **Check the radios** using the commands under [Testing and Verification](#testing-and-verification).
   5. **Start capture** against `rf-central`, using the stand-in node commands in [`central/README.md`](central/README.md). Confirm that each data source shows as running in the Kismet web UI (*Data Sources*).
   6. **Shutdown:** run `sudo poweroff` on the Pi, wait for the green LED to stop, then unplug the Pi and the hub.

### Direction finding (rf-dfviz)

1. **New site:** `cp dfviz/layout.example.yaml dfviz/site/layout.yaml` and enter the nodes' positions (feet, any origin); set `DFVIZ_SITE` in `central/.env`.
2. Start it with the central stack: `cd central && docker compose --profile dfviz up -d --build`, then open `http://<BIND_ADDR>:8080`. Do a hard refresh after updates.
3. **Map:** each located device is a dot (circle Wi-Fi, triangle Bluetooth, square TPMS); faint dots are heard by only two nodes. Click a device to focus on it (others hide; range rings, trail, per-node RSSI). Click empty space or press Esc to return. Positions are smoothed over time so they do not jump.
4. **Walls:** *Walls* → *Wall* (click start, end), *Room* (two corners), *Erase*. Choose how accurately you are drawing (±0.5 to ±4 ft); the model treats wall positions as uncertain.
5. **Calibrate** (makes the model fit the site):
   1. Turn on a phone hotspot and carry the phone. Select the hotspot's network in the device list.
   2. Walk to a suggested spot (**A, B, C** on the map, or *Go* in the panel), stand still, click your spot, choose how sure you are (exact, ±2 ft, ±5 ft), and press **Record** (15 s).
   3. Repeat until the uncertainty chips (n, wall, offsets) turn green. The Calibrate button shows how many spots are suggested.

   The model is re-chosen automatically after every change: the best of the candidate fits by measured (leave-one-out) error, used only if it beats the default model. There are no manual parameters. Spots and walls are saved on the server with a journal; deleted spots can be restored.

## Testing and Verification

Focus checks on business rules, event definitions, and contracts with connected subsystems. Include manual checks when automation is not practical.

- **Automated checks:**
  - `cd simulator && uv run pytest` validates every scenario's output against `schemas/observation.v0.schema.json` and checks the scenario properties that analytics tests rely on.
  - `cd analytics && uv run pytest ../dfviz/tests` covers the direction-finding server: RSSI smoothing, the calibration and walls APIs (journal, restore, stale-page races), recording spots from the Kismet packet log (including while it is write-locked), and position-accuracy fields.
  - `cd analytics && uv run pytest` covers the ingest contract, the analyzer and alert-rule business rules, the outbound wire format and outbox, and end-to-end quality on scenarios. That includes the phase 3 target of at least 80% localization accuracy and the expected alerts per scenario.
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

- **Live end-to-end check (manual):** with nodes streaming, start `rf-analytics` (above) and subscribe to `detection/rf/#`. Within a minute the stats line in `docker compose --profile analytics logs rf-analytics` shows `observations` growing and `stale` small; alerts appear on the subscriber when rules fire. To force one, put a nearby access point's MAC on the `watchlist` rule. `wc -l /data/rf-outbox.jsonl` inside the container should return to 0.
- **Events or interfaces verified:** the observation contract (`rf.observation.v0`); Kismet → `rf-analytics` with per-node RSSI; alerts on `detection/rf/alerts` received from the development broker (2026-11-07). The fusion message format is not yet defined.

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
| `kismet_cap_sdr_rtl433` fails with `rtl_433 binary not installed` | The `rtl-433` decoder package is missing | Install it: `sudo apt install rtl-433`. `kismet-capture-rtl433-v2` provides the Kismet helper, not the decoder binary. |
| TPMS helper cycles / Kismet shows "IPC connection closed" for the TPMS source | A standalone `rtl_433` process or `rtl-433` service is claiming the RTL-SDR, or the helper cannot hold the device | Stop competing processes/services (`ps aux \| grep rtl_433`, `systemctl status '*rtl*433*'`), confirm SDR access (`rtl_test -t`), then restart `kismet_cap_sdr_rtl433`. Check `/tmp/kcap-tpms.log` (or your log path). |
| Captures stop after a node reboot | Helpers were started ad hoc (`nohup`/foreground) and no persistent unit is installed | Install and enable the example `systemd` units from [`node/README.md`](node/README.md), or restart helpers after boot. See [`central/README.md`](central/README.md) for validated commands. |
| Direction-finding page behaves like an older version | The browser kept old page code | Hard refresh (Cmd+Shift+R). The server now sends `Cache-Control: no-cache`, so this is needed only once after the change. |
| Every device sits in the middle, nodes report the same RSSI | Kismet is not keeping per-node signal | `keep_per_datasource_stats=true` must be in `central/kismet/kismet_site.conf`; rebuild `kismet-server`. |
| Calibration *Record* fails with "Kismet log busy" | Kismet held the packet log's write lock for the whole retry period | Record again. Reads already wait and retry up to about 10 s. |
| Bluetooth devices only show as rings, or the page shows *⚠ N silent* | A node's `:bt` source runs (no error in Kismet) but delivers no packets. Cause found: on TP-Link UB500 dongles a scan stays "discovering" after the capture helper exits (e.g. Kismet restarted), so every new scan gets "Busy" | Run Bluetooth capture through `kismet-cap-bt.service` (`rf-bt-capture` clears discovery at every start and reconnect). To diagnose: `sudo rf-bt-doctor`; to repair: `sudo rf-bt-doctor --fix`. See [`node/README.md`](node/README.md#bluetooth-helper-scripts). |
| Bluetooth adapter differs between nodes (`hci0` is the onboard radio on one node, the dongle on another) | `hci` numbering follows enumeration order | Set `BT_ADDR` (exact adapter) or `BT_PREFER=usb`/`onboard` in `/etc/rf-node/rf-node.env`; `kismet-cap-bt.service` resolves it through `rf-bt-capture`. |
| A recorded spot is flagged ⚠ | Device transmitted rarely, or RSSI did not change between distant spots (device not carried) | Delete and re-record with a phone hotspot. |
| Wi-Fi capture drops SSH/backhaul | The helper was pointed at the backhaul interface | Capture only on the external monitor adapter (verify interface names per node). Do not put the onboard/backhaul Wi-Fi interface into monitor mode. |

## Interfaces and Dependencies

| Interface | Direction | Transport | Status |
|---|---|---|---|
| WBT node → `kismet-server` | Inbound | Kismet remote capture (TCP 3501 or WebSocket 2501) over Tailscale | Server ready. Node mode to be confirmed. |
| `kismet-server` → `rf-analytics` | Internal | Kismet REST API (polled every 5 s) | Implemented and validated live (`rfanalytics live`). Per-node RSSI requires `keep_per_datasource_stats=true` (set in `central/kismet/kismet_site.conf`). TPMS mapping best-effort until live decodes are verified. |
| `rf-analytics` ingest | Internal | `rf.observation.v0` ([`schemas/observation.v0.schema.json`](schemas/observation.v0.schema.json)) over JSONL or MQTT | Implemented. Validated against the schema. |
| `rf-analytics` events | Internal | `rf.analytics-event.v0` (see [`analytics/README.md`](analytics/README.md)) | Implemented. Feeds the alert engine (next). |
| Alerts, node status → fusion service | Outbound | MQTT QoS 1, `detection/rf/alerts` and `detection/rf/status/nodes`, with a durable outbox | Implemented with the draft format and validated live against the development broker. Waiting on the fusion message standard (none published on any branch as of 2026-11-07). |
| TPMS camera cue → camera subsystem | Outbound | MQTT `detection/rf/cues/camera` | Implemented. Routing (directly or through fusion) and expected cue volume still to be agreed (DESIGN.md open question 3). |
| Alert configuration from TAK | Inbound | To be decided | Waiting on the TAK team. |
| `rf-dfviz` web page and API | Internal / operators | HTTP on `DFVIZ_PORT` (8080): `/api/state`, `/api/samples`, `/api/calibration/*`, `/api/walls/*`; reads Kismet REST and the packet log (read-only) | Implemented; no authentication, so keep it on the LAN or tailnet only. |

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
| 2026-11-08 | Bluetooth capture fixed: UB500 dongles stayed "discovering" after a helper exited, so their sources delivered nothing after Kismet restarts. `rf-bt-capture` (systemd unit) clears discovery before every start, reconnects through systemd (`--disable-retry`), and picks the adapter by address instead of `hci` index; `rf-bt-doctor` diagnoses and repairs; the direction-finding page warns about silent or stopped capture sources. Verified: all four nodes recover within 10 s of a Kismet restart. |
| 2026-11-07 | Added `rf-dfviz`, a live direction-finding page (compose profile `dfviz`): Wi-Fi, Bluetooth and TPMS positions from per-node RSSI with smoothing; walls with uncertain positions; live calibration recorded from the Kismet packet log, with automatic model selection by measured error, suggested next spots, position-accuracy weighting and a journal with restore; demo emitters. |
| 2026-11-07 | Added `rfanalytics live` and the `rf-analytics` compose service; enabled per-node RSSI in Kismet; validated Kismet → alerts → MQTT on the WBT nodes. Fixed an outbox deadlock with paho-mqtt and false node-offline alerts from stale per-node entries. |
| 2026-11-07 | Added Kismet ingest v1 (`rfanalytics kismet`), example node `systemd` capture units, and validated legacy TCP remote capture on WBT nodes. |
| 2026-11-02 | Added `rf-analytics` alerting: six rule types, TPMS camera cues, a draft fusion wire format, and durable MQTT output. |
| 2026-11-02 | Added `rf-analytics` phase 1: ingest, friendly exclusion, visits, zone localization, and a ground-truth scorer. |
| 2026-11-02 | Added the subsystem design (`DESIGN.md`), the central Kismet and MQTT stack, the observation schema, and the `rfsim` simulator. Drafted node assembly and startup steps; these are not yet validated on hardware. |
