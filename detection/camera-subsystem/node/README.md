# Camera node stack

The software of a camera node (Pi 5, Hailo-8, one Reolink on eth0) as a Docker Compose
project, `black-relay-camera`. All services use the host network.

| Where | What |
| --- | --- |
| Compose | `mosquitto` (127.0.0.1:1883, anonymous, replaces the host package), `camerabridge` (Frigate events to the central broker, runs as the user that owns bridge.yaml), `camera-link` (adopts the camera, renders Frigate's config, drives Frigate; root) |
| Host units | `frigate.service` (Frigate in Docker, `/dev/hailo0`), `camera-link-dhcp.service` (dnsmasq on eth0), `run-frigate\x2dcache.mount`; from `../cameralink/ops` |
| Host files | `/etc/camerabridge/bridge.yaml`, `/var/lib/camera-provision/node.json`, `/var/lib/camerabridge`, `/var/lib/camera-link`, `/var/lib/frigate` |

`camera-link` runs `systemctl` and `docker` from inside the container. It reaches host systemd
through the mounted `/run/systemd` (private socket, and `system/` so systemctl sees a booted
systemd) and `/run/dbus/system_bus_socket`, and Frigate's container through
`/var/run/docker.sock`. Both mounts are root-equivalent on the host, as the old unit was.

## With Ansible

The fleet playbook (`detection/rf-subsystem/node/fleet`) copies `detection/camera-subsystem`
(without relay/, docs, .venv, .env*) to `/opt/black-relay/src`, writes `bridge.yaml`, `node.json`
and `node/.env`, creates the state directories, and runs `docker compose up -d --build` here.
It restarts `camerabridge` and `camera-link` when bridge.yaml changes. `bridge.yaml` may carry
`relay: {username, password}`: the shared login for RTSP and the Frigate proxy secret, while
`broker:` is the node's own login.

## By hand

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
