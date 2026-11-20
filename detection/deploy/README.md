# sf-dev bundle

One compose project for a single tailnet host: the central broker, fusion, the physical simulator
bench and the RF simulator with rf-analytics. Built here, moved as one tarball, no registry.

On the build machine:

```sh
PLATFORM=linux/amd64 ./build.sh      # linux/arm64 if `uname -m` on the host says aarch64
scp out/sf-dev-bundle.tgz USER@HOST:~/
```

On the host:

```sh
tar -xzf sf-dev-bundle.tgz && cd bundle
docker load -i images.tar
./make-env.sh HOST_ADDRESS           # the address the broker and API bind to; writes .env
docker compose up -d --wait
docker compose ps
```

Then, with the token from `.env`:

```sh
curl -s -H "Authorization: Bearer $TOKEN" http://HOST_ADDRESS:8090/api/v1/health
docker compose exec physical-sim sh -c 'echo "zone 12 Open" > /app/fusion-service/bench/sim.in'
curl -s -H "Authorization: Bearer $TOKEN" 'http://HOST_ADDRESS:8090/api/v1/events?type=physical.event'
curl -s -H "Authorization: Bearer $TOKEN" 'http://HOST_ADDRESS:8090/api/v1/events?subsystem=rf'
```

Only two ports leave the host: the broker on 1883 and the fusion API on 8090, both bound to the
address given to make-env.sh. Simulator observations travel over the same broker on `detection/rf/sim/#`,
which only the RF credential can read; fusion never subscribes to it.

To pull from the central image registry instead of loading `images.tar`, set
`IMAGE_PREFIX=SERVER:5000/` in `.env` (after `docker login SERVER:5000`), then `docker compose pull`.
See `../central-store/README.md`.

Camera video and the kits' NTP are a second compose project in `camera-relay-service/` inside the
bundle (its images are in `images.tar` too). Fill its `.env` and `kits`, run `./render.sh`, then
`docker compose up -d` there. See its README.
