# Black Relay 2026

Black Relay 2026 is a four-month, open-source build to create a portable, survivable safehouse operating stack for temporary sites. Fabrication, Detection, Survivability, and TAK teams will integrate fieldable hardware, sensor awareness, resilient edge systems, and operator workflows for a culminating field event.

[Learn more](https://arrowheadresearch.org/black-relay/)

## Central server

One command sets up (or updates) the central server: the image registry and device secrets store
(`detection/central-store`), the team's central MQTT broker (`detection/central-broker`, 1883 and 8883 with TLS)
and the fusion service (API on 8090), all from `compose.yaml` at the repo root. On the central server:

```sh
./setup-central.sh <address> --domain reg.bulexa.com --email <you>
```

It asks once for a Cloudflare API token (Zone:Read + DNS:Edit) and to accept Let's Encrypt's terms, gets a
publicly trusted certificate (so no host has to install a CA), creates every login, adds a daily renewal to
cron and starts everything. Without `--domain` it uses its own CA instead. Running it again keeps all logins and
data. It prints where the logins are at the end.

After that:

```sh
detection/central-store/server.sh add-client <name> [--secrets]   # a host that pulls images (+ its secrets)
detection/central/share-broker-login.sh <name>                    # a bridge's broker login, fetched on the
                                                                  # bridge with deploy/scripts/fetch-broker-login.sh
```
