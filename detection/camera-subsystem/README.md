# Camera Subsystem

## Ownership

- **Subsystem lead:** Dmitri Warner
- **Reviewers for cross-subsystem changes:** Contact the leads of affected subsystems.

## Purpose and Scope

Each camera kit is one self-contained node: a Raspberry Pi 5 with a Hailo-8 and one Reolink RLC-520A on a
point-to-point Ethernet cable. The node detects people and vehicles and records by itself, and sends events to the
site's central node over WiFi. The central node gives clients one place for events, node status and live video.
A node keeps detecting and recording when WiFi or the central node is down, and sends its queued events afterwards.

`DESIGN.md` has the reasons and the contract. This page is how to set a kit up.

## File Tree

```text
camera-subsystem/
  DESIGN.md      the design and the message contract
  node/          the node's Docker Compose stack: mosquitto, the bridge and the camera link (node/README.md)
  provision/     the SD image's setup WiFi and setup page, kit seeding, and how the image is built (ops/README.md)
  cameralink/    the camera link: adopts the camera, runs Frigate, and the node installer
  bridge/        turns Frigate's events into the contract's messages, with a queue on disk
```

## Physical Assembly

Document this so a person unfamiliar with the project can assemble the system from its parts. Include diagrams or photos where they clarify placement or connections.

### Required Tools and Conditions

- **Tools:** TODO
- **Power requirements:** Per kit, a 45 W USB-C supply for the Pi 5, and the camera on a PoE injector or 12 V DC (`DESIGN.md`, section 3). No UPS is specified.

### Assembly Steps

1. TODO: Identify and inspect all required parts.
2. TODO: Assemble the mechanical structure in a clear, ordered sequence.

## Configuration

Document settings or other configuration required before operation. Do not include secrets or credentials.

A kit from the SD image is configured from a phone, on its setup page. Nothing is edited on the kit. Kits
provisioned by the fleet tool take the same settings from its inventory instead: `camera_zones` and
`camera_upside_down` in `detection/rf-subsystem/node/fleet/ansible/host_vars/<node>.yml`, and the broker and time
server in `ansible/group_vars/`. Their `node_id` is the inventory name (`cam-1` and so on).

| Setting on the setup page (image kits) | Required | Description / example |
|---|---|---|
| WiFi name and password | Yes | The site WiFi the kit joins |
| Node ID and hostname | Yes | Filled in already, from the kit's serial: `cam-` and six characters. Leave it |
| Covered zones | Yes | The zone names this camera watches, from the site's RF zone layout, for example `north-entrance,building` (the dev site's names are in `../rf-subsystem/simulator/sites/dev-site.yaml`) |
| Frigate address | Yes | Filled in already. Leave it |
| Broker address and port | Yes | The central node's address on the site LAN, and `1883` with **Use TLS** off. With it on, `8883`, and the certificate must chain to a system CA. The login is required |
| Broker username and password | Yes | `relay`, and the broker password of the central node |
| Time server | No | Leave blank: the kit takes its time from the central node |
| Camera is mounted upside down | No | Tick it if it is |

**Default logins.** The SD image and the standalone relay bundle start with one default password, `BlackRelay2026`, so
that nobody has to guess one. It is in this repository, so change it on a real site. The BlackRelay central server
does not use it: its kit and viewer logins are random, in the root `.env` (see
[`../camera-relay-service/README.md`](../camera-relay-service/README.md)).

| Login | Username | Where it is | Changed with |
|---|---|---|---|
| Image kit's setup WiFi | none | the kit's label | `sudo /opt/black-relay/ops/change-passwords.sh setup-wifi`, on the kit |
| Image kit's console | `kit-1`, `kit-2`, and so on: the number on the kit | the kit | `sudo /opt/black-relay/ops/change-passwords.sh console`, on the kit |
| Kit login, shared by all kits | `relay` | image kits: the setup page, as their broker login. Fleet kits: the `relay` section of `bridge.yaml`, from `BROKER_CAMERA_PASSWORD` in the root `.env` (video login; their broker login is per node, `cam-node-<node>`) | Image: `./change-passwords.sh broker` in `../camera-relay-service/` (standalone), then `... change-passwords.sh broker` on each kit. BlackRelay: set `BROKER_CAMERA_PASSWORD` in the root `.env`, run `docker compose up -d` at the repo root, then `./fleet.sh provision camera` |
| Viewer, for live video | `viewer` | standalone: `camera-relay-service/.env`. BlackRelay: `RELAY_READ_PASSWORD` in the root `.env` | Standalone: `./change-passwords.sh viewer` in `../camera-relay-service/`. BlackRelay: edit the root `.env`, then `docker compose up -d` at the repo root |

Fleet kits have no console or setup-WiFi login of this kind. The fleet role makes the admin user `pi` with no
password (key only), and its setup network takes a passphrase at flash time. The `change-passwords.sh` scripts are
not installed on fleet kits.

## Startup

Physical assembly instructions are above, so assume that is complete when looking at this list.

**Once per site: the central node.** A kit has nowhere to report to without it. Any Linux host with Docker on the
site LAN will do: a Pi, a laptop or a server. Give it a fixed address on the LAN, such as a DHCP reservation, because
each kit accepts connections from that one address only. Then follow "Install" in [`../camera-relay-service/README.md`](../camera-relay-service/README.md).
The BlackRelay central server (its address is in `DEPLOY.md`) runs the relay from the root compose, as that README's
"On the BlackRelay central server" section says.

**Each kit, with the fleet tool (current).** Kits are flashed and provisioned from the admin machine by
`detection/rf-subsystem/node/fleet`. Its README, "Camera nodes", has the commands. In short:

1. Set the kit's covered zones and mounting in `ansible/host_vars/cam-N.yml` (`camera_zones`, `camera_upside_down`).
   Use the site's RF zone names.
2. Flash the card with `./fleet.sh flash cam-N <disk>`. Camera nodes need `SETUP_SSID` or `WIFI_SSID` in `fleet.env`,
   since their `eth0` is the camera link. Put the card in the Pi with the camera unplugged, power it on, and wait about
   two minutes.
3. Join the setup network from the admin machine and run `CAMERA_HEF=<path>/yolov6n.hef ./fleet.sh provision cam-N`.
   The first run builds the Hailo driver on the node and ends with a reboot.
4. Plug a factory-fresh camera into the Pi's Ethernet port through a powered PoE injector. camera-link adopts it with
   no further action. If the camera has been set up before, hold its reset pinhole until it restarts first. A kit with
   no camera stays at `no_camera` until one is plugged in.
5. Check it, as "Testing and Verification" below describes.

**Each kit from the SD image (older route, still supported for kits built from it).** These steps use the kit's own
setup page, and nothing is edited on the kit.

**For each image kit:**

1. **Power the Pi, with the camera unplugged, and wait three minutes.** On its first power-on the kit sets itself up
   and restarts once.
2. **Join the kit's setup WiFi from a phone.** Its name is `BlackRelay-CAM-` and the kit's serial, and its passphrase
   is the default above. The setup page should open by itself. If it does not, open `http://10.42.0.1/`.
3. **Fill in the form and save.** The table above has every field. Type the broker address with care: the kit then
   accepts SSH, live view and Frigate connections from that address alone. The kit leaves setup mode and joins the
   site WiFi, and its setup WiFi goes away.
4. **Plug the camera into the Pi's Ethernet port, powered.** The camera must be factory-fresh. If it has ever been set
   up, hold its reset pinhole until it restarts, first. The kit sets the camera's password and streams by itself. This
   takes about two and a half minutes, and the camera restarts once on the way.
5. **Check it from the central node,** as "Testing and Verification" below describes.

**If something goes wrong (image kits):**

| What you see | What to do |
|---|---|
| No setup WiFi five minutes after the first power-on | Power off, and read `black-relay-first-boot.log` on the SD card's boot partition from another computer. It holds the reason, and the kit tries again at the next power-on |
| The setup WiFi comes back two minutes after saving | The WiFi name or password was wrong. Join it again and correct them. This page takes only the WiFi fields |
| The kit joined WiFi and the central node never lists it | The broker address, username or password is wrong, or the central node's address has changed. The setup button (GPIO 17) is untested on hardware (C06 in `provision/ops/README.md`), so this needs a screen and keyboard on the kit (log in as `kit-N`), or its SD card written again |
| The node is listed with `camera_link_state` `needs_reset` or `failed` | Hold the camera's reset pinhole until it restarts, with its cable still in the Pi |
| SSH to a kit is refused | Kits take SSH by key only, from the central node's address only, and the image holds only its builder's key. Add your own to `~/.ssh/authorized_keys` at the kit's console |

## Testing and Verification

Focus checks on business rules, event definitions, and contracts with connected subsystems. Include manual checks when automation is not practical.

- **Automated checks:** each part has its own suite. In `bridge/`, `cameralink/`, `provision/` and `../camera-relay-service/api/`, run
  `uv run pytest -q`. `cameralink/tests/container_install_node.sh` runs the installer and the kit seeding on a real
  Debian in a container, and `../camera-relay-service/tests/test_relay.sh` runs the central node's bundle. Both need Docker.
- **Manual verification, after setting a kit up,** on the central node. Standalone, run it in
  `../camera-relay-service/`. On the BlackRelay central server, run it in the repo root, where the API is on 8091
  and the viewer password is `RELAY_READ_PASSWORD` in the root `.env`:

  ```bash
  . ./.env
  # The kit is listed, with stale false, and camera_online, recording_ok and detector_ok all true.
  curl -s -H "Authorization: Bearer $RELAY_API_TOKEN" http://localhost:${RELAY_API_PORT:-8080}/nodes
  # Live view. Use the kit's node ID. In a browser: http://<central node>:8889/<node id>, in Chrome or Chromium.
  ffplay -rtsp_transport tcp "rtsp://viewer:<viewer password>@localhost:8554/<node id>"
  # Walk in front of the camera. A person event arrives within a few seconds.
  curl -s -H "Authorization: Bearer $RELAY_API_TOKEN" "http://localhost:${RELAY_API_PORT:-8080}/events?limit=5"
  ```

  Firefox ESR did not play the browser view on the bench: the stream is H.264 High profile, which it does not offer.
- **Verified on hardware so far:**
  - Fleet route, cam-3 and cam-4 (`DEPLOY.md`, step 7): the Reolink adopted, Frigate healthy on the Hailo-8, and the
    bridge's clock synced and connected to the broker over TLS.
  - Image route, kit 1, 2026-11-13, with a laptop as the relay (`provision/ops/field-run-kit1.md`): the heartbeat
    reached the relay in under a minute (C52). One walk in view produced `camera.detection.v0` messages on the central
    broker and API (C73, partly run). Live H.264 video and a snapshot were pulled through the relay (C74, central's side).
  - Not recorded in this repository for a fleet kit: events at the API, and live view through the relay. Run the checks
    above on the next fleet kit. `provision/ops/field-run-kit1.md` has each check's result.

## Change Notes

As needed
