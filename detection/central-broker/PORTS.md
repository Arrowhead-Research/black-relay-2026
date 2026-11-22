# Ports published by central-broker

Claimed on the central node, as the RF Compose overlay asks for every published port.

| Port | Protocol | Bound to | Service | Clients |
|---|---|---|---|---|
| 1883 | TCP, MQTT 3.1.1 / 5, password-protected | `BIND_ADDRESS` in the repo-root `.env` (`BROKER_BIND_ADDRESS` when `compose.yaml` here runs alone) | `broker` (`mosquitto` when run alone) | fusion (read), physical bridge, RF console, rf-analytics, camera relay API, camera and RF nodes |
| 8883 | TCP, MQTT over TLS 1.2+, password-protected | `BIND_ADDRESS` | `broker` | the same clients, from the LAN; the certificate is the central store's |

Port 8883 exists only in the repo-root `compose.yaml`: `entrypoint.sh` adds the listener when a
certificate is mounted at `/tls`. `compose.yaml` in this directory publishes 1883 only.

Everything else the central server publishes is listed in the table in `DEPLOY.md` (firewall).
