# WBT Console

The operator web page for the RF subsystem. It shows where Wi-Fi, Bluetooth and TPMS devices are from the RSSI each WBT node hears, smoothed so estimates don't jump around. Each building the system is set up in is a **site** with its own node layout, walls, calibration and history, which can be exported, cleared or wiped when the team moves on. The path-loss model is fitted automatically from **live calibration** (a carried device recorded at known spots), and its accuracy is the **measured** error at those spots. It runs as a container (`rf-console`) next to the central Kismet server and reads Kismet's per-node signal history and packet log through the `rfanalytics` Kismet adapter. It does not publish alerts yet: see *Roadmap*.

## Start

```sh
mkdir -p site && cp layout.example.yaml site/layout.yaml   # team template: node radio MACs, first layout
cd ../central
# in .env: CONSOLE_OPERATOR_PASSWORD and CONSOLE_ADMIN_PASSWORD (empty = sign-in off)
docker compose --profile console up -d --build
open http://<BIND_ADDR>:8080        # e.g. http://127.0.0.1:8080
# with synthetic emitters that have known positions (BLE walker, still laptop and headset, passing car with 4 TPMS sensors):
CONSOLE_DEMO=1 docker compose --profile console up -d --build
```

Kismet must have `keep_per_datasource_stats=true` (already set in `central/kismet/kismet_site.conf`). Otherwise every node reports the same RSSI and nothing can be located.

## Layout of the page

- **Header:** the site menu (site name ▾), Kismet and device counts, capture-source warnings, the **Live / Setup** switch, Pause, and the signed-in role.
- **Live** (default): the map, the selected device, the model card and the device list. Nothing on the map can be changed by accident.
- **Setup:** a toolbar on the map with **Nodes** (drag nodes to where they are; place nodes Kismet knows that are not on the map yet; remove nodes), **Walls** and **Calibrate**. One tool is active at a time.

## Sites (moving between buildings)

The site menu lists every site. For the active site it shows its counts and history size, and lets you rename it; admins also set how long history is kept, **Export** a zip of everything stored for it, and **Clear history** (keeps the map, walls and calibration). Other sites can be switched to, or **wiped** by an admin, who must type the site's name. The active site cannot be wiped: switch first. **New site** starts with the current node set (positions to be re-placed) and nothing learned: no walls, spots, fits or history.

Data per site lives in the `rf-console-data` volume under `/data/sites/<id>/`; each change is journaled with who made it. History is purged automatically once a whole day is older than the site's retention (default 7 days; deployments last days). Node radio MACs belong to the team's hardware and come from the template (`site/layout.yaml`), not from a site.

## Sign-in

Two roles, with passwords from `central/.env`: **operator** (watch, set up the site, calibrate, create and switch sites) and **admin** (also export, clear, wipe and retention). Sessions are signed cookies valid for 12 hours. With neither password set, sign-in is off and the header says *sign-in off*: use that only in the lab. Every POST must be JSON (a cross-site form cannot send it). Keep the console on the LAN or tailnet; it does not serve HTTPS itself. Survivability's identity stack (Pocket ID OIDC with team groups) is the likely future sign-in.

## What the page shows

- **All devices (nothing selected):** each located device is a dot (circle = Wi-Fi, triangle = Bluetooth, square = TPMS). Faint dots are heard by only two nodes, which fixes a curve rather than a point. Nodes come from the site layout (see *Site files*), in feet.
- **Selected device (click a dot or a list row):** the other devices hide, and the selected one shows range rings (the distance each node's RSSI implies under the model), a trail, per-node RSSI bars (hover: smoothed and raw values, spread, outliers dropped), the model fit residual, and the average movement per update, which directly measures jumping. **To go back to all devices:** click empty map space, press Esc, or use *← All*.
- **Devices heard by one node:** distance only, no direction. When selected, they show as a ring around that node.
- **Model card (read-only):** per band, n, σ, wall loss and the measured error, or *default* when no calibration beats the defaults. There are no manual model parameters: they are always the best fit to the calibration.
- **Node radios** (the nodes' own onboard Wi-Fi, `self_emitters` in the site layout) are shown at their nodes, because that is where they are. Selecting one circles its node and shows, labelled *model check*, where the model would have placed it and how far off that is. Recording a node radio in calibration uses its node's exact position (no click needed).
- **Demo emitters** (`CONSOLE_DEMO=1`): their true position is a hollow cyan diamond joined to the estimate, and the panel shows the error.
- **Source warning:** the header shows *⚠ N silent* (or *sources down*) when a Kismet capture source is stopped, in error, or running without delivering packets for 5 minutes. Hover for the list. TPMS sources are exempt, since sensors transmit rarely.
- Space pauses. Node positions are changed only in *Setup → Nodes*, and are saved to the site for every page.

## How positions are estimated and smoothed

1. **Per-node RSSI (server).** Each node's per-second RSSI history (Kismet `signal_rrd`, the last 60 s) is kept per device. Samples further than 3×MAD from the median (multipath fades) are dropped, and the rest are averaged with exponential time weights. A node not heard recently is dropped.

   | Band | History | Time constant | Node dropped after |
   |---|---|---|---|
   | Wi-Fi, Bluetooth | 60 s | 12 s | 30 s |
   | TPMS (sparse bursts) | 180 s | 60 s | 120 s |

2. **Likelihood (browser).** Log-distance path loss with walls: predicted RSSI at node *k* is `P0 + c_k − 10·n·log10(d_k in m) − W·E[walls_k]` (`c_k` = per-node offset, `W` = wall loss, both from calibration, else 0 and the default). For each grid cell, `cost = Σ (e_k − o)²` with `e_k = rssi_k − predicted_k`, where `o` is the device's unknown transmit-power difference. `o` has a Gaussian prior of spread τ (fitted by calibration from ±4/8/16 dB, default ±8 dB), so `cost = Σe² − (Σe)²/(K + σ²/τ²)`. **Why not a free offset:** with `o` unconstrained, the surface has a far-away "mirror" valley almost as good as the true spot; on noise-free test data the grid picked it for spots up to 32 ft from the truth. With the prior, the same test gives ≤ 0.3 ft (≤ 6 ft for a device 6 dB louder than the model). Likelihood = `exp(−cost / 2σ²)`. Two nodes only constrain a curve; three or more give a point.
3. **Temporal smoothing (browser).** Each device's cost surface is averaged over time (fixed time constant 15 s, scaled by type: fixed APs ×2, TPMS ×0.25 so a passing car is not dragged behind), updated only when new samples arrive. A model or layout change restarts it.
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

The fit maths was checked on synthetic spots generated from known parameters (n = 2.3, P0 = −52 dBm, offsets +3/−2/0/−1 dB): it recovers them exactly. With `CONSOLE_DEMO=1`, the *demo calibration beacon* hops between six known spots every 45 s, so the whole workflow can be practised without walking around.

## Site files

Everything about a particular location stays out of the repository:

| What | Where |
|---|---|
| Node positions, walls, calibration spots, fitted models, history, change journals | `rf-console-data` volume, `/data/sites/<id>/` (Site menu: export, clear, wipe) |
| Team template: node radio MACs, first site's layout and model defaults | `console/site/layout.yaml` (gitignored) |
| Notes on a site | `console/site/NOTES.md` (gitignored) |

The code and the defaults are general; the example layout is a placeholder. Node radio MACs (`self_emitters`) belong to the node hardware and travel with it.

## Configuration

| Setting | Where | Default |
|---|---|---|
| Team template: node radio MACs (`self_emitters`), `node_sd_ft`, per-band model defaults (incl. `wall_db`), first site's node positions | `console/site/layout.yaml`, mounted at `/site`. Template: [`layout.example.yaml`](layout.example.yaml) | the example, if no file |
| `CONSOLE_OPERATOR_PASSWORD`, `CONSOLE_ADMIN_PASSWORD` | `central/.env` | empty: sign-in off |
| `CONSOLE_SITE` | `central/.env` | `default`: the site used before one is chosen in the Site menu |
| `CONSOLE_DATA` | container environment | `/data` (the `rf-console-data` volume) |
| Kismet packet logs (read-only, for recording spots) | `kismet-logs` volume at `/kismet-data/logs` (`CONSOLE_KISMET_LOGS`) | — |
| `CONSOLE_DEMO` | `central/.env` | empty (`1` adds demo emitters, `only` uses them without Kismet) |
| `CONSOLE_PORT` | `central/.env` | 8080 (bound to `BIND_ADDR`) |
| `CONSOLE_POLL_S` | container environment | 3 s poll |
| Kismet login | `KISMET_ADMIN_USER` / `KISMET_ADMIN_PASSWORD` from `central/.env` | — |

The older `DFVIZ_*` names are still read. Data from the earlier single-site layout (`/data/<name>/calibration.json`) is moved into `/data/sites/<name>/` on start.

## Tests

```sh
# from detection/rf-subsystem (no uv on the host? use the uv container as for analytics)
cd analytics && uv run pytest ../console/tests
```

`tests/test_server.py` covers RSSI smoothing (outlier rejection, time weighting), per-second history from `signal_rrd`, per-band windows (TPMS kept longer), calibration pairs, the demo source, `/api/state` and the page (no-cache headers) against a fake Kismet; the calibration API (per-spot add/delete/restore, journal, two-page race, fits, position-accuracy fields); recording and rebuilding spots from a Kismet-format SQLite log, including retrying while the log is write-locked; and walls (one operation at a time, stale-page race, persistent version). `tests/test_sites.py` covers site workspaces (first start, legacy migration, new site copies nodes but nothing learned, node validation, retention purge by whole days, export, clear, wipe guards) and sign-in (tokens, expiry, tampering, role checks over HTTP, JSON-only posts, slow wrong-password replies). The browser-side fit maths (path-loss, wall loss, offsets, weighted by position accuracy) was checked by recovering known parameters from synthetic spots in the page.

## Lessons learned (apply to any site)

Site-specific findings (spot positions, nearby networks, per-node results) belong in `console/site/NOTES.md`, which is local like the rest of `console/site/`.

- **Kismet needs `keep_per_datasource_stats=true`**, or every node reports the device's aggregate RSSI and nothing can be located.
- **Kismet's per-second signal history is a poor measurement source:** it keeps one value per second (the peak) for the last minute. In one test it gave 3 samples per node where the packet log had 8–17 and compressed the difference between nodes from 34 dB to 9 dB. Calibration therefore records from the packet log.
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

Agreed plan (2026-11-07). Wire formats for TAK, cameras and alerts are drafted in [`../INTERFACES-DRAFT.md`](../INTERFACES-DRAFT.md) for the other teams to review.

| Phase | Delivers | Done when |
|---|---|---|
| **1. Console + site workspaces** (done) | Rename; sites (new, switch, rename, export, clear, wipe, retention); node placement; Live/Setup; sign-in with operator/admin roles | A building can be set up from the page and wiped with nothing left over |
| **2. Server positions + history** | Localizer ported to Python (must match the page on recorded data); one Kismet feed for map and analytics; SQLite baseline and visits per site; replay of recorded data for tuning | The page shows server positions; the baseline survives a restart; analytics can be re-run on a past day |
| **3. Marking + alerting** | Friendly (team or site scope) and priority marks from the device card; alert areas drawn on the map; new-device-in-range and priority alerts; alert feed; the alert pipeline runs inside the console; MQTT alerts with lat/lon | A new phone walked into the inner area alerts within seconds; a marked device alerts wherever it is heard |
| **4. Cameras + TAK** | Cameras placed with heading, field of view and range; cues only to cameras that can see the target; snapshot results; TAK config topic | A simulated vehicle in view cues only that camera; a test TAK message marks a device |
| **5. Co-travelers + vehicles** | TPMS sensors grouped into vehicles; co-arrival scoring against chance; "arrived with" alerts | Precision/recall measured on the simulator, then a live test |
| **6. Randomized MACs** | Identity linking | A known group still matches after one member's MAC changes |

Deployments last days, so retention is short, but analytics need tuning as data arrives: Phase 2 records each site's raw feed (within retention) so a rule or model change can be replayed against what was actually seen, and Export keeps a copy for offline tuning before a site is wiped.
