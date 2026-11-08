# WBT Console

The operator web page for the RF subsystem. It shows where Wi-Fi, Bluetooth and TPMS devices are from the RSSI each WBT node hears, smoothed so estimates don't jump around. Each building the system is set up in is a **site** with its own node layout, walls, calibration and history, which can be exported, cleared or wiped when the team moves on. The path-loss model is fitted automatically from **live calibration** (a carried device recorded at known spots), and its accuracy is the **measured** error at those spots. It runs as a container (`rf-console`) next to the central Kismet server and reads Kismet's per-node signal history and packet log through the `rfanalytics` Kismet adapter. Positions are computed on the server, stored per site, and get latitude/longitude once node GPS points are set. It publishes device positions and node status to MQTT for the fusion service (see *Publishing*); alerts come later (see *Roadmap*).

## Start

```sh
mkdir -p site && cp layout.example.yaml site/layout.yaml   # team template: node radio MACs, first layout
cd ../central
docker compose --profile console up -d --build
open http://<BIND_ADDR>:8080        # e.g. http://127.0.0.1:8080
# with synthetic emitters that have known positions (BLE walker, still laptop and headset, passing car with 4 TPMS sensors):
CONSOLE_DEMO=1 docker compose --profile console up -d --build
```

Kismet must have `keep_per_datasource_stats=true` (already set in `central/kismet/kismet_site.conf`). Otherwise every node reports the same RSSI and nothing can be located.

## Layout of the page

- **Header:** the site menu (site name ▾), Kismet and device counts, capture-source warnings, the **Live / Setup** switch and Pause.
- **Live** (default): the map, alerts, the selected device and the device list. Nothing on the map can be changed by accident.
- **Setup:** a toolbar on the map with **Nodes** (drag nodes to where they are; place nodes Kismet knows that are not on the map yet; remove nodes; set node GPS points, see *Latitude and longitude*), **Walls** and **Calibrate**. One tool is active at a time.

## Sites (moving between buildings)

The site menu lists every site. For the active site it shows its counts and history size, and lets you rename it; admins also set how long history is kept, **Export** a zip of everything stored for it, and **Clear history** (keeps the map, walls and calibration). Other sites can be switched to, or **wiped** by an admin, who must type the site's name. The active site cannot be wiped: switch first. **New site** starts with the current node set (positions to be re-placed) and nothing learned: no walls, spots, fits or history.

Data per site lives in the `rf-console-data` volume under `/data/sites/<id>/`; each change is journaled with who made it. History is purged automatically once a whole day is older than the site's retention (default 7 days; deployments last days). Node radio MACs belong to the team's hardware and come from the template (`site/layout.yaml`), not from a site.

## Sign-in

Set or change the passwords with `central/set-console-passwords.sh` on the host (it prompts, writes `.env` and restarts the console). Two roles, with passwords from `central/.env`: **operator** (watch, set up the site, calibrate, create and switch sites) and **admin** (also export, clear, wipe and retention). Sessions are signed cookies valid for 12 hours. With neither password set, sign-in is off and the header says *sign-in off*: use that only in the lab. Every POST must be JSON (a cross-site form cannot send it). Keep the console on the LAN or tailnet; it does not serve HTTPS itself. Survivability's identity stack (Pocket ID OIDC with team groups) is the likely future sign-in.

## What the page shows

- **All devices (nothing selected):** each located device is a dot (circle = Wi-Fi, triangle = Bluetooth, square = TPMS). Faint dots are heard by only two nodes, which fixes a curve rather than a point. Nodes come from the site layout (see *Site files*), in feet.
- **Selected device (click a dot or a list row):** the other devices hide, and the selected one shows range rings (the distance each node's RSSI implies under the model), a trail, per-node RSSI bars (hover: smoothed and raw values, spread, outliers dropped), the model fit residual, and the average movement per update, which directly measures jumping. **To go back to all devices:** click empty map space, press Esc, or use *← All*.
- **Devices heard by one node:** distance only, no direction. When selected, they show as a ring around that node.
- **Model:** no manual parameters and no model panel; the server always uses the best fit to the calibration (see *Model selection on the server*), and the calibration panel (Setup) shows the fit.
- **Node radios** (the nodes' own onboard Wi-Fi, `self_emitters` in the site layout) are shown at their nodes, because that is where they are. Selecting one circles its node and shows, labelled *model check*, where the model would have placed it and how far off that is. Recording a node radio in calibration uses its node's exact position (no click needed).
- **Demo emitters** (`CONSOLE_DEMO=1`): their true position is a hollow cyan diamond joined to the estimate, and the panel shows the error.
- **Source warning:** the header shows *⚠ N silent* (or *sources down*) when a Kismet capture source is stopped, in error, or running without delivering packets for 5 minutes. Hover for the list. TPMS sources are exempt, since sensors transmit rarely.
- Space pauses. Node positions are changed only in *Setup → Nodes*, and are saved to the site for every page.

## How positions are estimated and smoothed

1. **Per-node RSSI (server).** Wi-Fi and Bluetooth readings come from every packet and sighting in Kismet's log (about 2 s behind), the same measurements calibration spots use, so the calibrated model applies to the live map unchanged. TPMS (not in the log yet) and any time the log can't be read use Kismet's per-second history (`signal_rrd`, each second's peak) instead. Samples further than 3×MAD from the median (multipath fades) are dropped, and the rest are averaged with exponential time weights. A node not heard recently is dropped.

   | Band | History | Time constant | Node dropped after |
   |---|---|---|---|
   | Wi-Fi, Bluetooth | 60 s | 12 s | 30 s |
   | TPMS (sparse bursts) | 180 s | 60 s | 120 s |

Steps 2-4 run on the server (`locate.py`, every poll) for every device; the page only draws the result.

2. **Likelihood.** Log-distance path loss with walls: predicted RSSI at node *k* is `P0 + c_k − 10·n·log10(d_k in m) − W·E[walls_k]` (`c_k` = per-node offset, `W` = wall loss, both from calibration, else 0 and the default). For each grid cell, `cost = Σ (e_k − o)²` with `e_k = rssi_k − predicted_k`, where `o` is the device's unknown transmit-power difference. `o` has a Gaussian prior of spread τ (fitted by calibration from ±4/8/16 dB, default ±8 dB), so `cost = Σe² − (Σe)²/(K + σ²/τ²)`. **Why not a free offset:** with `o` unconstrained, the surface has a far-away "mirror" valley almost as good as the true spot; on noise-free test data the grid picked it for spots up to 32 ft from the truth. With the prior, the same test gives ≤ 0.3 ft (≤ 6 ft for a device 6 dB louder than the model). Likelihood = `exp(−cost / 2σ²)`. Two nodes only constrain a curve; three or more give a point.
3. **Temporal smoothing.** Each device's cost surface is averaged over time (fixed time constant 15 s, scaled by type: fixed APs ×2, TPMS ×0.25 so a passing car is not dragged behind), updated only when new samples arrive. A model or layout change restarts it.
4. **Position.** The dot is the likelihood-weighted mean within 4 ft of the most likely cell, so it does not hop between near-equal peaks; on screen it eases toward new estimates.

No error circle is drawn around estimates: a radius derived from the model would look rigorous without being an accuracy figure. The accuracy reported is the calibration's **measured** error (leave-one-out median at the recorded spots, an empirical CEP50 for those spots).

## Walls

Press **Walls** to draw the floor plan: **Wall** (click a start and an end; it chains from the last point, Esc ends the chain), **Room** (two opposite corners make four walls), **Erase** (click a wall). Each edit is one server operation (`/api/walls/add`, `/delete`, `/accept`, `/clear`, journaled), shared by every page; a new wall shows immediately, and a page never sends its whole wall list, so a page with an out-of-date list cannot wipe walls drawn elsewhere. Dashed walls are drafts; *Accept drafts* marks them as checked.

Each wall a straight node-to-point path crosses costs the band's **wall loss** (`wall_db` in the site layout until calibrated: 4 dB for Wi-Fi and Bluetooth, 3 dB for TPMS). Calibration **fits** the wall loss with n when spot-to-node paths cross different numbers of walls, shows it as `W ± error`, and keeps it only if the model with walls measures better than without. A wall loss within its error of zero means the walls as drawn do not explain the measurements.

**To measure walls well,** record spots in pairs at similar distance from a node with a different number of walls in between: just inside and just outside a room's wall, for example.

## Uncertainty in positions

Nothing here is surveyed exactly, so the page treats positions as uncertain instead of exact:

- **Spots:** when recording, say how sure you are of your spot (*exact*, *within ±2 ft*, *within ±5 ft*). Node radios use their node's position, ±0.5 ft.
- **Nodes:** `node_sd_ft` in the site layout (default ±2 ft, for positions paced or measured roughly).
- **Walls:** each wall carries its drawing accuracy (`sd_ft`; the Walls toolbar's *Drawn to* choice, default ±2 ft by eye).

How it is used: a position error of δ changes the expected RSSI by about `10·n/(ln10·d)·δ` dB, a lot near a node and little far away. Each calibration reading is weighted by `1 / (σ0² + that variance)`, where σ0 (RSSI scatter not explained by position) is estimated from the residuals; the fit iterates because the slope depends on n. Walls count as the **expected** number of crossings: each wall is jittered into 16 plausible positions (ends moved sideways and along it by its accuracy, deterministic so every page agrees), and a path that only crosses some of them counts fractionally. A spot 0.8 ft from a ±2 ft wall counts about 0.75 of it. On noise-free synthetic data with mixed accuracies the fit still recovers n = 2.2 and W = 6.0 dB exactly.

## Baseline and replay

**Baseline** (`baseline.py`): per site, `history/baseline.sqlite` keeps every device's first and last sighting, visits (a visit ends after 10 minutes unheard), time heard and the hours of the week it is around. Devices first seen in the site's first 2 hours are its background. The device card shows *At this site: first seen 3 h ago · visit 4 · usually 08–10, 17–19* (local time); `GET /api/device?id=` returns it, `GET /api/baseline` summarizes. *Clear history* clears it; *Wipe* removes it.

**Replay** (`replay.py`): Kismet's log is the raw feed, so a past window can be re-run through the console's pipeline (transmitter crediting, smoothing, server positions with the site's model, walls and geo-reference, baseline, alert rules with the site's areas and marks) to tune rules or models against what was seen. It reads site data only and writes positions, alerts and a summary to `--out`:

```sh
docker compose exec rf-console python replay.py --site default \
    --start 2026-11-08T18:00:00Z --end 2026-11-08T20:00:00Z --out /tmp/replay
```

On sf-dev, 30 minutes replay in about 30 s. Its baseline starts empty at `--start` (`--learn-s` sets the learning period); TPMS and node-offline alerts are not replayed. Kismet's logs stay in the `kismet-logs` volume until removed, so a window can be replayed only while its log is kept.

## Node health

Each node's `rf-node-agent` (see [`../node/README.md`](../node/README.md#node-agent-rf-node-agent)) reports every 30 s. Problems raise `node_health` alerts: agent silent for 90 s, 80 °C or hotter, throttling or undervoltage now, a capture unit not running, a capture adapter missing, under 10 % disk free. `/api/state` → `nodes_health` has the details; an admin can queue a capture-unit restart with `POST /api/nodes/<node>/restart`. A node with problems is `degraded` in `rf.node-status.v0`, which also carries its `health`. Heartbeats need `CONSOLE_NODE_TOKEN` once it is set (`node/set-node-token.sh`).

## Vehicles and co-travelers

`cotravel.py`, re-run every 5 minutes on the site's visits (within retention):

- **Vehicles:** TPMS sensors of one model that burst within 30 s of each other (they transmit while the car moves, so on arrival and departure) are one vehicle. One shared burst is enough; a group larger than 5 (two cars of a make passing together) keeps only pairs seen together twice.
- **Co-travelers:** two stable devices (a vehicle counts as one) that arrive or leave within 2 minutes of each other on 2+ different visits, more often than chance (Poisson tail < 0.01 given how often each visits), form a group. Randomized addresses change every few minutes and cannot be followed across visits (identity linking is roadmap phase 6).
- The device card shows *part of a vehicle (4 TPMS)* and *travels with …*; `GET /api/device` returns them; `arrived_with` alerts when a group arrives again.

Measured on the simulator (`tests/cotravel_eval.py`, also run as tests): over 24 h with 536 passing vehicles, 91 % of vehicles are recovered exactly and 98 % of predicted vehicles are pure; the subject vehicle is grouped with its own head unit and nothing else is (pair precision 1.0); the passersby, loiterer and friendly-only scenarios produce no co-traveler groups. The occupants' phones, watch and tag rotate their addresses and are not linked.

## Model selection on the server

The server picks each band's model (`calfit.py`, a port of the page's calibration maths checked against it: `tests/parity/cal_golden.json` from `make_cal_golden.mjs`; every candidate's fit is identical and leave-one-out errors agree to 1e-6 ft). Every 30 s it re-selects a band whose spots, node links, nodes, walls or defaults changed and saves the fit (`"by": "server"`), or removes it when no candidate beats the defaults. So a new site, new walls or fresh node links get a model without a page open; the page shows the same analysis but no longer saves fits.

## Node links (calibration without walking)

Each node's own Wi-Fi radio is a transmitter at a known position, heard by the other nodes: with four nodes, 12 links of known distance and known walls, measured all the time. Every 10 minutes the server reads them from Kismet's log (median over 5 minutes, at least 10 frames per receiver, readings credited to the radio itself) and keeps them per site (`node_links.json`, replaced only when a median moves 2 dB or more). `/api/state` → `node_links`.

The Wi-Fi fit uses them as extra training spots: every candidate is fitted with and without them, and scored, as always, by leave-one-out error **at the walked spots only**, so links can only be adopted if they make walked spots better. The panel shows *Node links (4): X ft without → Y ft with*. A link replaces a hand-recorded spot of the same node radio. Links help most with per-node offsets (each node is both transmitter and receiver, so a weak antenna shows up both ways) and with wall loss (links at similar distance through different walls). They do not replace walked spots: they are Wi-Fi only, run between mounting points at the edges of the building, and cannot measure accuracy in the rooms. Suggested spots take them into account.

**Rebuild from log** (calibration panel) re-reads every recorded spot from Kismet's log with the current rules, where the log still covers it (logs live in the `kismet-logs` volume of the host that recorded them).

## Suggested spots (where to carry the hotspot)

In calibration mode the map shows up to three targets (**A, B, C**) and the panel lists them with what each would pin down ("wall loss", "wbt-3's offset", "how fast signal fades"). The Calibrate button shows *N spots suggested* while the panel is closed. Tap a target or *Go* to make it your spot, walk there, and record.

How targets are chosen: for the full model (n, every node offset, wall loss if walls are drawn) the information matrix of the existing readings (weighted as above) gives how well each parameter is known. Each candidate place on a 2 ft grid (not on a wall, not within 5 ft of an existing spot) is scored by how much one reading there, with all nodes hearing the device, would shrink the joint uncertainty of those parameters (log-determinant of their covariance, a "Ds-optimal" design). Three are picked greedily, at least 8 ft apart. The panel also shows the current uncertainty (n, wall loss, worst node offset) against goals (±0.2, ±1.5 dB, ±2 dB). Once all are met, it says *well calibrated* and stops suggesting.

## Live calibration with a known device

Press **Calibrate** in the header.

1. **Device:** select the device you carry in the list (phone hotspot, laptop, BLE beacon). **What is selected is what gets recorded**: the Record button names it, and each spot stores the device's address and name. Several devices or SSIDs can be used; each gets its own transmit power in the fit.
2. **Stand still at a spot and click that spot on the map** (it snaps to 0.5 ft).
3. **Record 15 s.** The server reads **every packet** the nodes logged from that device in the window from Kismet's packet log (the `kismet-logs` volume, mounted read-only) and stores the median per node. It waits for the log to catch up (Kismet writes about 10 s behind). A node needs 3+ packets; a spot needs 2+ nodes.
4. Repeat at **5–10 spots spread over the area**, at varied distances from the nodes. Node radios can be recorded like any device (their node's position is used automatically).
5. The panel fits (shown with the standard error of n, e.g. `n = 1.8 ± 0.4`; a poorly determined n is flagged), per band, by least squares `rssi = P0[device] + c_k − 10·n·log10(d_k) − W·walls_k`: one transmit power per device, optional per-node offsets `c_k` (average 0), σ = residual standard deviation.
6. **Measured error (leave-one-out):** each spot is located with a model fitted on the other spots; its distance to where you clicked is its error (red line on the map). The median is an **empirical CEP50 for those spots**; with 10+ spots the 90th percentile is shown too. It is accuracy at the spots you recorded, not a guarantee elsewhere.
7. **The model is chosen automatically.** Whenever spots or walls change, the page fits every candidate (per-node offsets on/off × wall loss on/off when walls are drawn × transmit-power uncertainty ±4/8/16 dB) and scores each by its leave-one-out median error. The best plausible candidate (n 1.5–5, σ ≤ 12 dB, wall loss ≥ 0, 5+ spots scored) becomes the model **if it beats the default model at the same spots**; otherwise the defaults stay. It is saved on the server and used by every page. *N candidates compared* in the panel shows them all. Picking the best of several candidates on the same spots makes the winner's error slightly optimistic; more spots reduce that.

**Spots cannot be lost.** The server owns the spot list: every add, delete, restore, rebuild and fit change is a single request, written to `calibration.json` and appended to `calibration.json.journal.jsonl`. Deleted spots are listed under *Recently deleted* and can be restored. *Delete spots of …* only removes the selected device's spots and needs a second click. Open pages reload when another page changes the spots.

**Rebuild a spot for another device (↻):** if a spot was recorded for the wrong device, select the right one and press ↻ on the spot. The server rebuilds it from the packet log for the same time window.

**Guardrails** (added after a first real calibration used a stationary router as the reference device):

- **Live trace:** a per-node RSSI trace of the selected device over the last 60 s. It should change as you walk; if it stays flat, the device is not moving with you.
- **Per-spot checks (⚠ in the table):** fewer than 0.4 frames/s per node (medians too noisy), or RSSI within 3 dB of a spot 10+ ft away at every node (the device probably did not move).
- **RSSI vs distance plot:** your spots, the default model (grey dashed) and the fit (orange). Real data slopes down.
- **Comparison:** the default model's error at the same spots is shown with every candidate; a fit is only used if it beats it.

**Good reference devices:** a phone with its **hotspot on** (select the hotspot network: it beacons about 10 times a second at a fixed power and address), a laptop with steady traffic, or a BLE beacon. Avoid anything that stays put, and devices that rotate their MAC mid-session.

`locate.py` is a port of the page's former localization code, checked against it: `tests/parity/golden.json` holds the page code's own results (run under node by `make_golden.mjs`), and the server matches them to 1e-4 ft. On a live snapshot of the first site (10 walls, 29 devices placed) the two agreed to 1e-13 ft.

**History:** every position is appended to `history/<date>-positions.jsonl` in the site's directory (a device at most every 10 s, and only every 60 s while it stays within 3 ft); `GET /api/history?id=<device>&since=<unix s>` reads it back. It is purged with the rest of the site's history.

## Latitude and longitude

Positions are in the site's own feet. Give the server node GPS points and every position also gets `lat`/`lon` (and `err_m`, the calibration's measured median error combined with how well the GPS points fit the floor plan).

- *Setup → Nodes → GPS*: pick a node, paste `lat, lon` (decimal degrees, as map apps copy them), *Set*. **Two or more nodes** fix the floor plan's rotation and position. With **one node**, also enter *Up*: the compass bearing of the map's up direction.
- Take coordinates outdoors or from a satellite map, not from a receiver inside the building (often 5-15 m off). The status line shows the fit (`±m`, rms of the GPS points against the floor plan) and warns when the points disagree by more than 3 m or their spacing differs from the floor plan's by more than 10 %.
- The coordinates are site data: stored only in the site's `site.json` (journaled), exported and wiped with the site, never in git.

The fit maths was checked on synthetic spots generated from known parameters (n = 2.3, P0 = −52 dBm, offsets +3/−2/0/−1 dB): it recovers them exactly. With `CONSOLE_DEMO=1`, the *demo calibration beacon* hops between six known spots every 45 s, so the whole workflow can be practised without walking around.

## Marks, areas and alerts

- **Mark a device** from its card: *Friendly* (team phones, laptops, vehicles: dimmed on the map, *Hide friendly* hides them, never alerted on) or *Priority* (a device of interest: red ring, alerted wherever it is heard), for *this site* or the whole *team* (team marks apply at every site, kept in `/data/marks.json`; site marks are wiped with the site). The nodes' own radios are always friendly.
- **Areas** (*Setup → Areas*): name the area, choose its linger time, click its corners and click the first corner again to close it. Delete with ×.
- **Alerts** (newest first in the *Alerts* card, count in the header; click one to select its device; *Acknowledge all*):

  | Rule | Fires when | Severity |
  |---|---|---|
  | `priority_heard` | a priority device is heard (again after 10 min unheard) | high |
  | `area_entry` | a non-friendly device placed by 3+ nodes enters an area (once per visit) | medium (high if priority) |
  | `linger` | it stays longer than the area's linger time | medium (high if priority) |
  | `node_offline` / `node_online` | a node's capture stops / comes back | high / info |
  | `node_health` | the node agent reports a new problem (hot, undervoltage, capture unit down, adapter missing, disk nearly full) | medium |
  | `new_device` | a stable (not randomized) address first seen at this site after its 2-hour learning period, heard by 2+ nodes | low |
  | `repeat_visitor` | a stable address starts its 3rd or later visit to this site | low |
  | `arrived_with` | a device or vehicle arrives with its known co-travelers | low (high if priority) |

  A new car's four TPMS sensors raise one `new_device` alert, not four.

  A visit ends after 10 minutes away, so jitter at an area's edge does not repeat alerts. Devices placed by two nodes (a curve, not a point) raise no area alerts. Alerts are stored per site (`history/<date>-alerts.jsonl`, purged with its history) and published on `detection/rf/alerts` ([`rf.alert.v0`](../schemas/alert.v0.schema.json), shared with rf-analytics) with the position, lat/lon, area and mark.

## Publishing

With `CONSOLE_MQTT_HOST` set (compose default: the stack's own broker, which other teams reach as `rf-mqtt:1883` on the `fusion` network), the console publishes:

| Topic | Schema | When | Delivery |
|---|---|---|---|
| `detection/rf/positions` | [`rf.position.v0`](../schemas/position.v0.schema.json) | a device's estimate has new readings: at most every 2 s, and every 30 s while it moves less than 1 ft | QoS 0, dropped while the broker is down (an old position is not replayed) |
| `detection/rf/alerts` | [`rf.alert.v0`](../schemas/alert.v0.schema.json) | every alert (see *Marks, areas and alerts*) | QoS 1 through the durable outbox |
| `detection/rf/status/nodes` | [`rf.node-status.v0`](../schemas/node-status.v0.schema.json) | a node's state (`online`, `degraded`, `offline`) or a source's status changes, and every 60 s | QoS 1 through the durable outbox (`/data/rf-outbox.jsonl`) |

A position carries site feet, `lat`/`lon` once the site is geo-referenced, the measured error (`null` until calibrated) and the nodes that heard the device (two nodes fix a curve, not a point). The nodes' own radios are not published. `analytics/src/rfanalytics/formats.py` is the only code that knows topics and envelopes: when the fusion standard arrives, it and the schemas change. `/api/state` → `publish` shows counters and the connection. Watch the feed: `docker run --rm --network fusion eclipse-mosquitto:2.0.22 mosquitto_sub -h rf-mqtt -t 'detection/rf/#' -v`.

## Site files

Everything about a particular location stays out of the repository:

| What | Where |
|---|---|
| Node positions, node GPS points, walls, calibration spots, fitted models, position history, change journals | `rf-console-data` volume, `/data/sites/<id>/` (Site menu: export, clear, wipe) |
| Team template: node radio MACs, first site's layout and model defaults | `console/site/layout.yaml` (gitignored) |
| Notes on a site | `console/site/NOTES.md` (gitignored) |

The code and the defaults are general; the example layout is a placeholder. Node radio MACs (`self_emitters`) belong to the node hardware and travel with it.

## Configuration

| Setting | Where | Default |
|---|---|---|
| Team template: node radio MACs (`self_emitters`), `node_sd_ft`, per-band model defaults (incl. `wall_db`), first site's node positions | `console/site/layout.yaml`, mounted at `/site`. Template: [`layout.example.yaml`](layout.example.yaml) | the example, if no file |
| `CONSOLE_OPERATOR_PASSWORD`, `CONSOLE_ADMIN_PASSWORD` | `central/.env` | sign-in passwords (operator, admin); neither set = sign-in off |
| `CONSOLE_SITE` | `central/.env` | `default`: the site used before one is chosen in the Site menu |
| `CONSOLE_DATA` | container environment | `/data` (the `rf-console-data` volume) |
| Kismet packet logs (read-only, for recording spots) | `kismet-logs` volume at `/kismet-data/logs` (`CONSOLE_KISMET_LOGS`) | — |
| `CONSOLE_MQTT_HOST` / `CONSOLE_MQTT_PORT` / `CONSOLE_TOPIC_PREFIX` | `central/.env` (`RF_TOPIC_PREFIX` for the prefix) | `mqtt` / 1883 / `detection/rf`; set the host empty to stop publishing |
| `CONSOLE_NODE_TOKEN` | `central/.env` (set by `node/set-node-token.sh`) | empty: node heartbeats accepted without a token |
| `CONSOLE_DEMO` | `central/.env` | empty (`1` adds demo emitters, `only` uses them without Kismet) |
| `CONSOLE_PORT` | `central/.env` | 8080 (bound to `BIND_ADDR`) |
| `CONSOLE_POLL_S` | container environment | 3 s poll |
| Kismet login | `KISMET_ADMIN_USER` / `KISMET_ADMIN_PASSWORD` from `central/.env` | — |

The older `DFVIZ_*` names are still read. Data from the earlier single-site layout (`/data/<name>/calibration.json`) is moved into `/data/sites/<name>/` on start.

## Tests

```sh
# from detection/rf-subsystem (no uv on the host? use the uv container as for analytics)
cd analytics && uv sync --extra console && uv run pytest ../console/tests
```

`tests/test_server.py` covers RSSI smoothing (outlier rejection, time weighting), live readings from the Kismet log (each read once, new log files followed, fallback to `signal_rrd`), per-second history from `signal_rrd`, per-band windows (TPMS kept longer), calibration pairs, the demo source, `/api/state` and the page (no-cache headers) against a fake Kismet; the calibration API (per-spot add/delete/restore, journal, two-page race, fits, position-accuracy fields); recording and rebuilding spots from a Kismet-format SQLite log, including retrying while the log is write-locked; and walls (one operation at a time, stale-page race, persistent version). `tests/test_locate.py` checks server localization against the page's former code (golden values), restarts of smoothing on layout or model changes, and geo-referencing (two points, one point plus heading, a wrong coordinate flagged). `tests/test_server.py` also covers positions in `/api/state`, history throttling and `/api/site/geo`. `tests/test_server.py` also builds real radiotap/802.11 frames to check that spots and live readings are credited to the transmitter (ACKs dropped, relayed frames to the AP), node links (own node excluded, unchanged below 2 dB) and rebuilding spots. `tests/test_nodehealth.py` covers problems from heartbeats, the restart queue, health in node status (schema) and `node_health` alerts, the heartbeat token and admin restart over HTTP, and the agent's collection. `tests/test_cotravel.py` covers vehicles (one burst, same-make split), co-traveler significance, the arrived_with rule, one alert per new car, and the simulator scenarios as thresholds. `tests/test_baseline.py` covers new/visit events and the learning period, persistence and hours of the week, a cleared history, the new_device/repeat_visitor rules, and replaying a synthetic Kismet log end to end. `tests/test_calfit.py` checks model selection against the page's code (golden values: every candidate's fit, errors, problems, the chosen model) and the server's fit loop (saved, unchanged, re-selected after a change). `tests/test_alerting.py` covers the rules (area entry once per visit, linger, two-node fixes ignored, friendly and node radios never alerted, priority, node offline/online), team/site marks, areas, and an end-to-end poll through HTTP (alerts stored, reloaded, acknowledged and published, validated against `schemas/alert.v0`, as are rf-analytics alerts). `tests/test_publish.py` covers position throttling, skipping node radios, dropping while disconnected, node states and heartbeats, and validates every payload (and rf-analytics' node status) against `schemas/`. `tests/test_sites.py` covers site workspaces (first start, legacy migration, new site copies nodes but nothing learned, node validation, retention purge by whole days, export, clear, wipe guards) and sign-in (tokens, expiry, tampering, role checks over HTTP, JSON-only posts, slow wrong-password replies). The browser-side fit maths (path-loss, wall loss, offsets, weighted by position accuracy) was checked by recovering known parameters from synthetic spots in the page.

## Lessons learned (apply to any site)

Site-specific findings (spot positions, nearby networks, per-node results) belong in `console/site/NOTES.md`, which is local like the rest of `console/site/`.

- **Kismet needs `keep_per_datasource_stats=true`**, or every node reports the device's aggregate RSSI and nothing can be located.
- **Kismet's per-second signal history is a poor measurement source:** it keeps one value per second (the peak) for the last minute. In one test it gave 3 samples per node where the packet log had 8–17 and compressed the difference between nodes from 34 dB to 9 dB. Calibration therefore records from the packet log, and since 2026-11-08 the live map reads the same log: on one live check a quarter of Wi-Fi node readings from the peak history were 3 dB or more stronger than the log's, while the log-based live values were within about ±0.6 dB of the calibration median.
- **Credit each Wi-Fi reading to the frame's transmitter, not to the address Kismet files it under.** Kismet logs ACK and CTS frames (which carry only a receiver address) under the station they answer, and frames an access point relays under the original sender. Their signal is the access point's or the ACKing station's. On the first site this was 57 % of all Wi-Fi frames (20 % ACK/CTS, 37 % relayed), and it put the access point's −29 dBm on a node radio whose own frames arrived at −80 dBm. The console reads each frame's address 2 from the raw header in the log (`frame_transmitter`) and drops ACK/CTS; spots recorded before 2026-11-08 evening should be rebuilt or re-recorded.
- **Read Kismet's live packet log with a shared lock and retry.** Unlocked ("immutable") reads see half-written pages and fail with "database disk image is malformed"; locked reads wait for Kismet's write lock (about 1 in 5 attempts, up to ~11 s observed).
- **Do not leave the transmit-power offset free.** With unknown power and no prior, the cost surface has far-away "mirror" solutions; on noise-free test data they misplaced spots by up to 32 ft. A prior of a few dB (fitted from ±4/8/16) removes them.
- **The reference device must be carried and chatty.** A stationary router or an idle phone (a few frames per 15 s) produces flat, noisy calibrations that look like "RSSI does not depend on distance". A phone hotspot (beacons about 10 times a second at a fixed power and address) works. The quality checks (⚠, live trace) catch the bad cases.
- **Fixed transmitters at known positions are valid spots:** access points you can locate, and node radios at their nodes. Record them like any device; summaries from the live view are too inaccurate.
- **A capture source can run without capturing:** Kismet showed Bluetooth sources as running, with no error, while they delivered nothing for hours (UB500 dongles left "discovering" by a previous helper; see `node/README.md`). Watch packet changes per source, not just the running state (the page does). Kismet resets counters when it restarts, so any change counts as activity.
- **Expect large per-node differences** (an 18 dB spread between nodes was seen) from antennas, cables, gain and placement. Per-node offsets absorb them, but a node far off the others is worth checking physically.
- **Accuracy claims need held-out spots.** A node radio located with its own node snaps to it (1–5 ft) and flatters the model; without its own node the same test gave 14–45 ft before calibration. The page reports leave-one-out error for this reason. With few spots, picking the best of many candidates on the same spots is optimistic; 8+ spots make the figure meaningful.
- **Wall loss is only measurable with spots that test it:** pairs at similar distance from a node with different numbers of walls in between. Otherwise it fits as "0 ± several dB".
- **Shared editing must be server-owned:** one journaled operation per change, never whole-list saves from a page, and a refresh may only overwrite local state when it is newer. Whole-list saves lost calibration spots and a drawn room before this was fixed.
- **Serve page code with `Cache-Control: no-cache`**, or browsers keep running old JavaScript after an update.

## Roadmap

Agreed plan (2026-11-07). The TAK, camera and fusion message formats are still to come.

| Phase | Delivers | Done when |
|---|---|---|
| **1. Console + site workspaces** (done) | Rename; sites (new, switch, rename, export, clear, wipe, retention); node placement; Live/Setup; sign-in with operator/admin roles | A building can be set up from the page and wiped with nothing left over |
| **2. Server positions + history** (positions, position history, lat/lon, MQTT publishing, server model selection, baseline and replay done 2026-11-08) | Localizer ported to Python (must match the page on recorded data); one Kismet feed for map and analytics; SQLite baseline and visits per site; replay of recorded data for tuning | The page shows server positions; the baseline survives a restart; analytics can be re-run on a past day |
| **3. Marking + alerting** (marks, areas, area/priority/node alerts in the console and on MQTT done 2026-11-08) | Friendly (team or site scope) and priority marks from the device card; alert areas drawn on the map; new-device-in-range and priority alerts; alert feed; the alert pipeline runs inside the console; MQTT alerts with lat/lon | A new phone walked into the inner area alerts within seconds; a marked device alerts wherever it is heard |
| **4. Cameras + TAK** | Cameras placed with heading, field of view and range; cues only to cameras that can see the target; snapshot results; TAK config topic | A simulated vehicle in view cues only that camera; a test TAK message marks a device |
| **5. Co-travelers + vehicles** (vehicles, co-traveler groups, arrived_with done 2026-11-08; measured on the simulator, live TPMS still to verify) | TPMS sensors grouped into vehicles; co-arrival scoring against chance; "arrived with" alerts | Precision/recall measured on the simulator, then a live test |
| **6. Randomized MACs** | Identity linking | A known group still matches after one member's MAC changes |

Deployments last days, so retention is short, but analytics need tuning as data arrives: Phase 2 records each site's raw feed (within retention) so a rule or model change can be replayed against what was actually seen, and Export keeps a copy for offline tuning before a site is wiped.
