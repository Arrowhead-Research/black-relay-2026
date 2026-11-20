# Black Relay 2026

Black Relay 2026 is a four-month, open-source build to create a portable, survivable safehouse operating stack for temporary sites. Fabrication, Detection, Survivability, and TAK teams will integrate fieldable hardware, sensor awareness, resilient edge systems, and operator workflows for a culminating field event.

[Learn more](https://arrowheadresearch.org/black-relay/)

## Central server

The central server runs the team's central MQTT broker (`detection/central-broker`, port 1883 with a login per
publisher) and the fusion service (API on 8090), both from `compose.yaml` at the repo root. On the central server:

```sh
detection/central/make-env.sh <address>    # once: writes .env with the bind address, broker logins and fusion token
docker compose up -d --wait
```

`<address>` is the server's address on the network the other hosts use. `./setup-central.sh <address>` runs the
same two steps. Running either again keeps every login and all data.

To run the physical (IQ5) bridge on the same server, use `./setup-central.sh <address> --with-bridge --panel-ip
<panel address>`. It builds the bridge, generates its certificates (asks for sudo once), gives it the central broker
login, points it at the central broker and starts it; the bridge's own broker (Home Assistant / Node-RED) is on port
8884. The server needs a network interface that reaches the panel, and the panel pairing (pair once, or
`detection/central-store/secrets.sh fetch detection/physical-subsystem/deploy` to restore it).

A bridge on another host gets its broker login by setting `FUSION_PASSWORD` (the value of `BROKER_PHYSICAL_PASSWORD`
in the server's `.env`) in its own `deploy/.env`.

To work against the API without any hardware, `detection/simulator-deploy/deploy-simulator-api.sh` starts simulated
RF, panel and camera sources with their own broker and fusion on one machine.

## Images and device secrets on GitHub (ghcr.io)

Images are built for amd64 and arm64 by `.github/workflows/publish-images.yml` on every push to `main` and pushed to
`ghcr.io/arrowhead-research/...`; the compose files pull from there by default (`IMAGE_PREFIX=` uses local builds).
There is no local registry: a host needs internet to pull, or builds from a checkout. If the packages are private, run
`docker login ghcr.io` (a classic token with `read:packages`) on each host first.

Device secret bundles go through GitHub without GitHub ever being able to open them:

```sh
# on the device, after pairing: encrypt to this device's own key, into a text file
detection/central-store/secrets.sh seal kit-01 detection/physical-subsystem/deploy data/pki secrets --out kit-01.age
# hand it to GitHub as a secret of the `secrets` environment, then run Actions > publish-secrets
gh secret set BUNDLE_KIT_01 --env secrets < kit-01.age
# on the device (once): point secrets.sh at ghcr.io with a read:packages token, then fetch any time
detection/central-store/secrets.sh setup ghcr.io/arrowhead-research/blackrelay-secrets kit-01 - <github-user>
detection/central-store/secrets.sh fetch detection/physical-subsystem/deploy
```

One-time GitHub setup: create the `secrets` environment (Settings > Environments) with yourself as required
reviewer, and after the first run check that the `blackrelay-secrets/*` packages are **private**
(Organization > Packages). Workflows run only on GitHub-hosted runners and never on pull requests.
