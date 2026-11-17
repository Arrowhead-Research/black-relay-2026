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
  provision/     the setup WiFi and setup page, kit seeding, and how the SD image is built (ops/README.md)
  cameralink/    the camera link: adopts the camera, runs Frigate, and the node installer
  bridge/        turns Frigate's events into the contract's messages, with a queue on disk
  relay/         the central node: broker, API, video relay and time server (README.md)
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

A kit is configured from a phone, on its setup page. Nothing is edited on the kit.

| Setting on the setup page | Required | Description / example |
|---|---|---|
| WiFi name and password | Yes | The site WiFi the kit joins |
| Node ID and hostname | Yes | Filled in already, from the kit's serial: `cam-` and six characters. Leave it |
| Covered zones | Yes | The zone names this camera watches, for example `driveway,gate` |
| Frigate address | Yes | Filled in already. Leave it |
| Broker address and port | Yes | The central node's IP on the site LAN, and `1883`. TLS off, anonymous off |
| Broker username and password | Yes | `relay`, and the broker password of the central node |
| Time server | No | Leave blank: the kit takes its time from the central node |
| Camera is mounted upside down | No | Tick it if it is |

**Default logins.** Every kit and every new central node starts with one default password, `BlackRelay2026`, so that
nobody has to guess one. It is in this repository, so change it on a real site.

| Login | Username | Changed with |
|---|---|---|
| A kit's setup WiFi | none | `sudo /opt/black-relay/ops/change-passwords.sh setup-wifi`, on the kit |
| A kit's console | `kit-1`, `kit-2`, and so on: the number on the kit | `sudo /opt/black-relay/ops/change-passwords.sh console`, on the kit |
| Broker, typed on the setup page | `relay` | `./change-passwords.sh broker` in `relay/`, then `... change-passwords.sh broker` on each kit |
| Viewer, for live video | `viewer` | `./change-passwords.sh viewer` in `relay/` |

## Startup

Physical assembly instructions are above, so assume that is complete when looking at this list.

**Once per site: the central node.** A kit has nowhere to report to without it. Any Linux host with Docker on the
site LAN will do: a Pi, a laptop or a server. Give it a fixed address on the LAN, such as a DHCP reservation, because
each kit accepts connections from that one address only. Then follow "Install" in [`relay/README.md`](relay/README.md):
four steps, ending in `docker compose up -d --build`.

**For each kit:**

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

**If something goes wrong:**

| What you see | What to do |
|---|---|
| No setup WiFi five minutes after the first power-on | Power off, and read `black-relay-first-boot.log` on the SD card's boot partition from another computer. It holds the reason, and the kit tries again at the next power-on |
| The setup WiFi comes back two minutes after saving | The WiFi name or password was wrong. Join it again and correct them. This page takes only the WiFi fields |
| The kit joined WiFi and the central node never lists it | The broker address, username or password is wrong, or the central node's address has changed. The setup button is not wired yet, so this needs a screen and keyboard on the kit (log in as `kit-N`), or its SD card written again |
| The node is listed with `camera_link_state` `needs_reset` or `failed` | Hold the camera's reset pinhole until it restarts, with its cable still in the Pi |
| SSH to a kit is refused | Kits take SSH by key only, from the central node's address only, and the image holds only its builder's key. Add your own to `~/.ssh/authorized_keys` at the kit's console |

## Testing and Verification

Focus checks on business rules, event definitions, and contracts with connected subsystems. Include manual checks when automation is not practical.

- **Automated checks:** each part has its own suite. In `bridge/`, `cameralink/`, `provision/` and `relay/api/`, run
  `uv run pytest -q`. `cameralink/tests/container_install_node.sh` runs the installer and the kit seeding on a real
  Debian in a container, and `relay/tests/test_relay.sh` runs the central node's bundle. Both need Docker.
- **Manual verification, after setting a kit up,** on the central node, in `relay/`:

  ```bash
  . ./.env
  # The kit is listed, with stale false, and camera_online, recording_ok and detector_ok all true.
  curl -s -H "Authorization: Bearer $RELAY_API_TOKEN" http://localhost:8080/nodes
  # Live view. Use the kit's node ID. In a browser: http://<central node>:8889/<node id>, in Chrome or Chromium.
  ffplay -rtsp_transport tcp "rtsp://viewer:<viewer password>@localhost:8554/<node id>"
  # Walk in front of the camera. A person event arrives within a few seconds.
  curl -s -H "Authorization: Bearer $RELAY_API_TOKEN" "http://localhost:8080/events?limit=5"
  ```

  Firefox ESR did not play the browser view on the bench: the stream is H.264 High profile, which it does not offer.
- **Events or interfaces verified:** `camera.detection.v0` and `camera.node-status.v0` reach the central broker and
  API from a real kit, and live view is pulled through the central node. `provision/ops/field-run-kit1.md` records
  each check that has run on hardware, and each that has not.

## Change Notes

As needed
