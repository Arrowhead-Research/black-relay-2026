# Ports published by central-broker

Claimed on the central node, as the RF Compose overlay asks for every published port.

| Port | Protocol | Bound to | Service | Clients |
|---|---|---|---|---|
| 1883 | TCP, MQTT 3.1.1 / 5, password-protected | `BROKER_BIND_ADDRESS` only (the central node's Detection-network address) | `mosquitto` | fusion (read), physical bridge, RF console, camera nodes later |

No other port. TLS, when added, replaces this listener in place rather than adding 8883.
