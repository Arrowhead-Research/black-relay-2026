# Camera node stack

The software of a camera node (Pi 5, Hailo-8, one Reolink on eth0) as a Docker Compose
project, `black-relay-camera`. All services use the host network.

| Where | What |
| --- | --- |
| Compose | `mosquitto` (127.0.0.1:1883, anonymous, replaces the host package), `camerabridge` (Frigate events to the central broker, runs as the user that owns bridge.yaml), `camera-link` (adopts the camera, renders Frigate's config, drives Frigate; root) |
| Host units | `frigate.service` (Frigate in Docker, `/dev/hailo0`; not enabled, since camera-link starts it), `camera-link-dhcp.service` (dnsmasq on eth0), `run-frigate\x2dcache.mount`; from `../cameralink/ops`. The fleet role also writes a Docker drop-in that starts Docker after the cache mount |
| Host files | `/etc/camerabridge/bridge.yaml`, `/var/lib/camera-provision/node.json`, `/var/lib/camerabridge`, `/var/lib/camera-link`, `/var/lib/frigate` |

`camera-link` runs `systemctl` and `docker` from inside the container. It reaches host systemd
through the mounted `/run/systemd` (private socket, and `system/` so systemctl sees a booted
systemd) and `/run/dbus/system_bus_socket`, and Frigate's container through
`/var/run/docker.sock`. Both mounts are root-equivalent on the host, as the old unit was. It sets
`SYSTEMCTL_FORCE_BUS=1`, because systemctl's private socket refuses a caller in another PID namespace.

The bridge checks NTP over D-Bus (`timedatectl`), so it mounts `/run/dbus/system_bus_socket` and
`/run/systemd/system`. Without the second, timedatectl refuses ("System has not been booted with systemd"), the
bridge never sees the clock as synced, and it holds every event.

## With Ansible

The fleet playbook (`detection/rf-subsystem/node/fleet`, role `camera_node`) copies `detection/camera-subsystem`
(without `relay/`, `provision/`, `.venv` and `.env*`) to `/opt/black-relay/src`, writes `bridge.yaml`, `node.json`
and `node/.env`, creates the state directories, and runs `docker compose up -d --build` here. It restarts
`camerabridge` and `camera-link` when `bridge.yaml` or `node.json` changes. `bridge.yaml` always carries
`relay: {username, password}`: the shared kit login, which MediaMTX pulls RTSP with and which the Frigate proxy
secret is derived from. `broker:` is the node's own login, `cam-node-<node>`. The role also installs the host
units above, the eth0 camera link and the Hailo driver; this directory does not. Set `camera_zones` (the site's
RF zone names) and `camera_upside_down` per node in the fleet's `host_vars`.

eth0 changes apply at the reboot the role makes, never live, since an SSH session may be on eth0. The role also
checks that NetworkManager has loaded the `black-relay-camera` profile, and reboots if it has not. The firewall
admits the controller by the IPv4 address it connects from, so Ansible connects over IPv4 (`AddressFamily=inet`).

## Not with the SD image

The SD image (`provision/ops`, and `cameralink/ops/install-node.sh`) runs camerabridge, camera-link and mosquitto as
host units, not in this stack. Both would bind `127.0.0.1:1883` and drive the same camera, so run one or the other
on a node.

## By hand

Needs the host units and the eth0 camera link already in place, which the fleet role does (not `install-node.sh`).
Builds run on the host network, because the node's Docker has no bridge network, so the Pi needs internet for pip
and apk.

```sh
cd detection/camera-subsystem/node
cp .env.example .env        # set CAMERABRIDGE_UID/GID: id camerabridge
sudo install -d -o camerabridge -m 0700 /var/lib/camerabridge
sudo install -d -m 0700 /var/lib/camera-link
sudo install -d -m 0750 /var/lib/frigate /var/lib/frigate/config /var/lib/frigate/media
docker compose up -d --build
docker compose logs -f camera-link
```

To run the published image instead of building, set `CAMERA_NODE_IMAGE` in `.env` and use
`docker compose pull && docker compose up -d`.
