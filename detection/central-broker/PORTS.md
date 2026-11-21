# Ports published by central-broker

Claimed on the central node, as the RF Compose overlay asks for every published port.

| Port | Protocol | Bound to | Service | Clients |
|---|---|---|---|---|
| 1883 | TCP, MQTT 3.1.1 / 5, password-protected | `BROKER_BIND_ADDRESS` only (the central node's Detection-network address) | `mosquitto` | fusion (read), physical bridge on the same host, camera kits (no TLS yet) |
| 8883 | TCP, MQTT over TLS 1.2+, password-protected | same address | `mosquitto` | RF nodes, bridges on other hosts: `reg.bulexa.com:8883` |

8883 serves the Let's Encrypt certificate for `reg.bulexa.com` from `cert.sh` (root compose only; it
mounts `state/tls`). Without a certificate the broker runs 1883 alone. 1883 stays for clients that
can't do TLS yet; once they can, it can be closed.
