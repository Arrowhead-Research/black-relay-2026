# Black Relay 2026

Black Relay 2026 is a four-month, open-source build to create a portable, survivable safehouse operating stack for temporary sites. Fabrication, Detection, Survivability, and TAK teams will integrate fieldable hardware, sensor awareness, resilient edge systems, and operator workflows for a culminating field event.

[Learn more](https://arrowheadresearch.org/black-relay/)

## Central server

`compose.yaml` at the repo root runs everything on the central server in one project: the image registry and
device secrets store (`detection/central-store`), the team's central MQTT broker (`detection/central-broker`,
ports 1883 and 8883 with TLS) and the fusion service (API on 8090). On the central server, from the repo root:

```sh
detection/central-store/server.sh init <address> reg.bulexa.com      # registry TLS and logins (once)
detection/central-store/server.sh letsencrypt reg.bulexa.com <email>  # trusted certificate for registry and broker
detection/central/make-env.sh                                       # ./.env: broker logins, fusion API token
docker compose up -d --wait
```

Already running the store from `detection/central-store`? Stop it there first (`docker compose down`, no `-v`); its
data volumes keep their names, so nothing is lost.
