# Camera relay service

Live video and time for the camera kits, on the central node. Three containers: MediaMTX pulls
a kit's RTSP stream only while someone is watching, `netguard` confines what MediaMTX may dial,
and `chrony` serves NTP, which every kit needs before it reports anything.

Events do not pass through here. Each kit's bridge publishes `detection/camera/#` straight to
the central broker (`../central-broker`), fusion stores them, and fusion also serves each
detection's snapshot (`GET /api/v1/events/{id}/snapshot`). This replaces the `relay` bundle in
`../camera-subsystem/relay`: that bundle's broker is the central broker now, and its API, which
kept its own copy of camera events and created video paths from heartbeats, is not run. What
that gives up: the Frigate proxy (clips and recordings through the central node), and video paths
that follow a kit's address by themselves. Here the kits are listed by hand.

## Files

| File | Purpose |
|---|---|
| `compose.yaml` | `netguard`, `mediamtx`, `chrony` |
| `mediamtx.yml` | Base MediaMTX config, no secrets, no paths |
| `render.sh` | Writes `mediamtx.generated.yml`: the base plus one on-demand path per kit |
| `kits` | One line per kit: node id and site LAN address (`kits.example`) |
| `.env` | Subnet, addresses and the two logins (`.env.example`) |
| `netguard/`, `chrony/` | Copied from `../camera-subsystem/relay`, unchanged |

## Run

On the central node, this host must be the kits' broker address: a kit admits RTSP only from
that one address, and asks it for time.

```bash
cp .env.example .env && cp kits.example kits    # fill both in
./render.sh
docker compose up -d                            # add --build unless the images were loaded from the deploy bundle
docker compose ps                               # netguard healthy, mediamtx and chrony up
```

Watch: `http://<RELAY_CENTRAL_IP>:8889/<node id>` in Chrome, or
`ffplay -rtsp_transport tcp rtsp://viewer:<password>@<RELAY_CENTRAL_IP>:8554/<node id>`.
An idle kit sends no video; the first viewer starts the pull and it stops 10 s after the last leaves.

After a kit is added or changes address, edit `kits`, run `./render.sh`, then
`docker compose up -d`. `netguard` needs nf_tables in the host kernel and `CAP_NET_ADMIN`; if its
rules cannot load it exits and MediaMTX never starts, by design.

## Open

- Ownership: the camera lead's call. Interim: Dmitri Warner.
- The Frigate proxy (clips, recordings), if wanted, would be a route in fusion like the snapshot
  one, or the relay API run against the central broker with its own read login.
