# Simulator runbook

Needs Docker. Run everything from `detection/simulator-deploy`.

## Start

```sh
./deploy-simulator-api.sh
```

Five services come up, then it prints the API's `address:port` and `FUSION_API_TOKEN`. First run
builds the images.

The only thing exposed is the fusion API, on port 8090 of this machine.

For another port: `API_PORT=8091 ./deploy-simulator-api.sh`.

## Check

```sh
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" localhost:8090/api/v1/health
```

Expect `"status":"ok"`.

## Make events

```sh
./mock_rf.sh                 # one rf.alert
./mock_rf.sh position        # one rf.position
./mock-camera.sh             # one camera heartbeat and one person's three detection messages
docker compose exec physical-sim sh -c 'echo "zone 12 Open" > /app/fusion-service/bench/sim.in'
```

RF also plays by itself: three `rf.alert` events about 21 minutes after start, then again each hour.

## Read events

```sh
curl -s -H "Authorization: Bearer $FUSION_API_TOKEN" "localhost:8090/api/v1/events?subsystem=rf"
```

Swap `rf` for `camera` or `physical`.

## Stop

```sh
docker compose down
```

Add `-v` to wipe stored events. The token in `.env` survives either way.
