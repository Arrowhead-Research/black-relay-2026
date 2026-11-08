# WBT Console user guide

The WBT Console is the operator page of the RF subsystem. It shows where nearby Wi-Fi, Bluetooth and TPMS (tire sensor) devices are, based on what the four WBT nodes hear, and raises alerts for devices you care about. This guide covers using it. For installing and running it, see [`README.md`](README.md).

*Last updated 2026-11-08.*

## Contents

1. [Opening the console](#1-opening-the-console)
2. [The screen](#2-the-screen)
3. [Reading the map](#3-reading-the-map)
4. [Looking at one device](#4-looking-at-one-device)
5. [Marking devices](#5-marking-devices)
6. [Alerts](#6-alerts)
7. [Setting up a building](#7-setting-up-a-building)
8. [Calibration](#8-calibration)
9. [Sites: moving between buildings](#9-sites-moving-between-buildings)
10. [Troubleshooting](#10-troubleshooting)
11. [Terms](#11-terms)

## 1. Opening the console

Open `http://<central host>:8080` in a browser on the team network or the tailnet (on the shared host: `http://sf-dev:8080`). Sign in with the operator or admin password.

| Role | Can |
|---|---|
| **Operator** | Everything on the map, marking devices, alerts, setting up nodes, walls, areas and calibration, creating and switching sites |
| **Admin** | Everything an operator can, plus exporting a site, clearing its history, changing how long history is kept, and wiping a site |

A session lasts 12 hours. *Sign out* is at the top right. If the header shows **sign-in off**, the console has no passwords set and anyone on the network can use it: tell whoever runs the central host.

## 2. The screen

**Header (left to right):** the site name (click it for the site menu), the connection to Kismet (green *Kismet live* is normal), the number of devices heard and located, a red alert count when there are unacknowledged alerts, a warning when a capture source is silent or down, the **Live / Setup** switch, and **Pause**.

- **Live** (the normal mode): the map, the *Alerts* card, the selected device and the device list. Nothing on the map can be moved by accident.
- **Setup:** adds a toolbar over the map with **Nodes**, **Areas**, **Walls** and **Calibrate**. One tool is open at a time; **Done** closes it.
- **Pause** (or the space bar) freezes the display; the console keeps recording.

**Right side:** *Alerts* (only when there are some), the selected device, display options (*Rings*, *Trail*, *Labels*, *Hide friendly*), and *Devices*: every device heard right now, strongest first, with a filter box and buttons to show or hide each device type.

## 3. Reading the map

- **Squares with a name** are the nodes. The dashed outline joins them.
- **Dots are devices:** circle = Wi-Fi, triangle = Bluetooth, square = TPMS. The colour also shows the type (see the buttons above the device list).
- **Faint dots** are heard by only two nodes. Two nodes only narrow a device down to a curve, not a point, so treat their position as rough.
- **Very faint dots** are devices marked friendly. *Hide friendly* removes them.
- **A red ring** marks a priority device.
- **Red dashed shapes** are alert areas.
- Devices heard by only one node are listed but not drawn: one node gives a distance, not a direction.
- Dots glide to new positions rather than jump. Until the site is calibrated (section 8), positions use rough defaults; after calibration, the calibration panel shows the measured accuracy.

## 4. Looking at one device

Click a dot or a row in the device list. The other devices fade out and the device's card opens:

- **Name**, address and type. Randomized addresses (most phones) are marked *random MAC*.
- **Position** in feet on the floor plan, and **Lat, lon** once the site has node GPS points (section 7).
- **Nodes:** how many nodes hear it.
- **Signal bars:** how strongly each node hears it.
- **Mark buttons and linger alarm** (section 5).
- On the map: dashed **rings** around each node (the distance its signal suggests), the **trail** of recent positions, and the signal at each node.

To go back to all devices, click empty map space, press **Esc**, or click **← All** at the top left of the map.

## 5. Marking devices

On the device's card:

| Control | What it does |
|---|---|
| **Friendly** | A team device (phones, laptops, vehicles). Dimmed on the map, hidden with *Hide friendly*, never raises area alerts. |
| **Priority** | A device of interest. Red ring, and an alert whenever it shows up (section 6). |
| **Linger alarm** (*no linger alarm*, 5 min, 15 min, 30 min, 1 h) | Alert when this device has been around that long. Works with or without a mark, also on a friendly device. |
| **this site / team** | *this site*: applies here only and is erased when the site is wiped. *team*: applies at every site. Choose it before clicking a mark. |
| **Clear** | Removes the mark and the linger alarm. |

Clicking the highlighted mark button again removes just that mark and keeps the linger alarm.

**Phones change their address.** Most phones use a random Wi-Fi and Bluetooth address that changes every few minutes, so a mark on it stops applying when it changes. Mark devices that keep their address: laptops, hotspots, tracker tags, car TPMS sensors, car audio. For a team phone, mark its hotspot. The nodes' own radios are always treated as team devices; you never need to mark them.

## 6. Alerts

| Alert | When | Severity |
|---|---|---|
| **entered *area*** | a device that is not friendly enters an alert area | medium, high for a priority device |
| **priority device heard** | a priority device shows up | high |
| **in *area* for N min** / **here for N min** | a device has stayed in an area longer than the area's linger time, or has been around longer than its own linger alarm | medium, high for a priority device |
| ***node* offline** | a node has stopped capturing | high |

**One alert, then quiet.** Once a device has alerted, it alerts again only when it reaches a linger time, and after that not at all until it has been gone for 10 minutes. A priority device walking into an area gives one high alert, not two. Walking into a second area, or back into the same one, stays quiet.

**Area alerts need three nodes.** A device heard by only two nodes has no reliable position, so it never triggers an area alert. Friendly devices never trigger area alerts.

**Working with alerts:** new alerts appear at the top of the *Alerts* card and the header count flashes. Click an alert to select its device. **Acknowledge all** clears the list. Alerts are also sent to the fusion service (see [`../MQTT-CONTRACT.md`](../MQTT-CONTRACT.md)).

Nothing else raises alerts. Unmarked devices passing by do not alert unless they enter an area.

## 7. Setting up a building

Do this once per building, in **Setup**. Create a site first (section 9) so the building's data is kept separately.

### Nodes

**Setup → Nodes.** Drag each node to where it really is (any corner of the building can be 0, 0; distances are in feet; positions snap to half a foot). Nodes that Kismet hears but that are not on the map yet show as **+ wbt-…** buttons: click one, then click the map where it is. **×** removes a node from this site (a site needs at least two).

Measure node positions with a tape or laser if you can. Wrong node positions make every position on the map wrong, and calibration cannot fully fix them.

**GPS (optional, for latitude/longitude):** in the same bar, choose a node, paste its coordinates as `lat, lon` (decimal degrees, as map apps copy them) and click **Set**. With two or more nodes set, the console works out how the floor plan sits on the map. With only one, also enter **Up**: the compass bearing (0–360°) of the map's up direction. The status next to it shows how well the points agree (`fit ±1.2 m`) and warns if they don't. Take coordinates outdoors or from a satellite map: GPS inside a building is often 5–15 m off. Coordinates are kept only with the site and are erased when it is wiped.

### Walls

**Setup → Walls.** Walls weaken signals, so drawing the main ones improves positions.

- **Wall:** click the start, then the end. It keeps chaining from the last point; **Esc** ends the chain.
- **Room:** click two opposite corners to draw four walls.
- **Erase:** click a wall to remove it.
- Choose how accurately you are drawing (**±0.5** to **±4 ft**) before drawing. Rough walls are fine; say so.
- **Accept drafts** keeps sketched (dashed) walls; **Clear all** removes every wall.

### Areas

**Setup → Areas.** Type a name, choose its **Linger** time (2 min, 5 min, 15 min or 1 h), click the corners of the area on the map, and click the first corner again to finish. **×** on an area's name deletes it. Good areas: the approach to the door, the parking area, the inner perimeter.

## 8. Calibration

Calibration measures how signals fade in this building, so positions become accurate. The console does the maths itself: you only record **spots** (a device held still at a known place).

**What to carry:** your phone's **Wi-Fi hotspot** turned on (it transmits constantly at a fixed power). A Bluetooth tag works for Bluetooth. Avoid devices that go quiet (idle phones, scooters connected to their app, earbuds in their case).

**Recording a spot:**

1. **Setup → Calibrate.**
2. Select the device you are carrying in the device list (the panel shows it next to *Device*).
3. Stand still. Click the map where you are standing, and choose how sure you are of that spot (**exact**, **±2 ft**, **±5 ft**).
4. Click **Record** and stay still: 15 seconds for Wi-Fi, 45 for Bluetooth. The panel says how many nodes heard it.
5. Move to the next spot and repeat.

**Where to stand:** the panel suggests the next spots as **A**, **B**, **C** (also drawn on the map). Click **Go** to set that spot, walk there, and record. Spread spots over the whole area, including the middle of rooms and places separated from nodes by walls. Eight or more spots give a trustworthy result. The panel shows **done** when calibration is good enough.

**What happens next:** within about 30 seconds the server tries every model and keeps the one that places your spots best, measured on spots it did not use for fitting. The map uses it automatically. There are no settings to adjust.

**Spot list:** *Spots* lists every spot; **×** deletes one (it can be restored under *Deleted*); **↻** re-records a spot for the selected device from the logged data (useful if you recorded the wrong device). A **⚠** marks a doubtful spot (device too quiet, or signal not changing between distant spots: was it actually being carried?).

**Rebuild from log** recalculates every recorded spot from the logged data, where that data is still kept. Use it after a console update that changes how readings are counted.

**Node links (automatic):** the nodes also hear each other's radios at known distances. The console uses these as extra calibration data without anyone walking, so fewer spots are needed. They do not replace walked spots.

## 9. Sites: moving between buildings

Each building is a **site** with its own nodes, walls, areas, calibration, marks (site scope), alerts and history. Click the site name in the header to open the site menu.

| To | Do |
|---|---|
| Start a new building | Type a name under *New site name*, **Create**. It keeps the current node list (re-place them) and nothing else. |
| Switch to another site | Click its name under *Other sites*. The page reloads. |
| Rename the current site | Edit its name at the top of the menu. |
| Keep a copy (admin) | **Export** downloads everything stored for the site. |
| Clear its history (admin) | **Clear history**, then **Confirm clear**. Keeps nodes, walls, areas and calibration. |
| Change how long history is kept (admin) | **Keep history**: 1 to 30 days. Older days are deleted automatically. |
| Erase a site completely (admin) | **Wipe** next to it, type its name, **Wipe**. You cannot wipe the site you are on: switch first. |

**Leaving a building:** an admin exports the site if the data is needed, creates or switches to the next site, then wipes the old one.

## 10. Troubleshooting

| You see | What it means / what to do |
|---|---|
| Header says **Kismet error** or **console server unreachable** | The console has lost Kismet or the page has lost the console. Check the central host; the page reconnects by itself. |
| **⚠ N silent** / **sources down** in the header | A node's capture has stopped or delivers nothing. Hover to see which. A *node offline* alert follows if a node stops completely. Check the node's power and USB adapters. |
| A device is in the list but not on the map | Only one node hears it, so there is no position. |
| Positions look wrong everywhere | Check node positions first (Setup → Nodes), then calibrate (section 8). Before calibration, positions use rough defaults. |
| One device jumps around | It is heard weakly or by two nodes only (faint dot). Its position is rough. |
| No alert when a device walked into an area | It was friendly, heard by fewer than three nodes, or had already alerted in this visit (section 6). |
| A phone's mark stopped working | Its random address changed. Mark a device with a fixed address instead (section 5). |
| **Record** fails with "only N nodes heard…" | The device was too quiet or too far. The message lists how many times each node heard it. Use a hotspot, keep it awake, and record again. |

## 11. Terms

| Term | Meaning |
|---|---|
| **Node** | One WBT sensor (wbt-1 to wbt-4). It only listens; the one thing it transmits is its own network connection. |
| **Site** | One building's workspace. |
| **Spot** | A calibration measurement: a device held still at a known position. |
| **Visit** | A device being around without a 10-minute gap. |
| **Linger time** | How long a device may stay (in an area, or anywhere for a device's own alarm) before it alerts. |
| **Randomized address** | An address a device changes regularly for privacy (most phones). Shown as *random MAC*. |
| **TPMS** | Tire pressure sensors. Cars transmit them while moving, four per car. |
