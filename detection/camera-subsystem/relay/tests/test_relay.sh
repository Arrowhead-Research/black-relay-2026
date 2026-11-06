#!/usr/bin/env bash
# Smoke test of the relay bundle: real containers on this machine, on ports chosen free at run
# time and bound to loopback. Touches no other host, and no container, image or network it did
# not create. Always tears down, and fails if anything is left behind.
#
# Needs docker with compose v2, ffmpeg, curl, openssl, python3 and uv.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
relay="$(dirname "$here")"
project="relaytest-$$"
work="$(mktemp -d)"
failures=0
torn_down=0
hold_pid=""

log() { printf '%s\n' "$*"; }
ok() { log "ok   - $*"; }
not_ok() { log "FAIL - $*"; failures=$((failures + 1)); }
check() { # check <description> <command...>
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then ok "$description"; else not_ok "$description"; fi
}
refuted() { # refuted <description> <command...>: the command must fail
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then not_ok "$description"; else ok "$description"; fi
}
wait_until() { # wait_until <seconds> <command...>
    local deadline=$((SECONDS + $1))
    shift
    until "$@" >/dev/null 2>&1; do
        ((SECONDS >= deadline)) && return 1
        sleep 0.5
    done
}

test_positive() { (($("$@" 2>/dev/null) > 0)); }
wait_equal() { # wait_equal <seconds> <expected> <command...>: runs the command again each time
    local deadline=$((SECONDS + $1)) expected="$2"
    shift 2
    until [[ "$("$@" 2>/dev/null)" == "$expected" ]]; do
        ((SECONDS >= deadline)) && return 1
        sleep 0.5
    done
}

for tool in docker ffmpeg ffprobe curl openssl python3 uv; do
    command -v "$tool" >/dev/null || { log "this test needs $tool"; exit 2; }
done

compose() {
    docker compose -p "$project" --project-directory "$relay" --env-file "$work/env" \
        -f "$relay/compose.yaml" -f "$here/compose.test.yaml" "$@"
}
others() { # every container this test did not create
    docker ps --format '{{.ID}} {{.Names}}' | grep -v " $project" | sort
}

other_containers_before="$(others)"
pulled_images=()
for image in bluenviron/mediamtx:1.21.3 eclipse-mosquitto:2.1.2-alpine alexxit/go2rtc:1.9.14 \
    alpine:3.23.8 python:3.13.13-slim-trixie; do
    docker image inspect "$image" >/dev/null 2>&1 || pulled_images+=("$image")
done

teardown() {
    local status=$?
    ((torn_down)) && return
    torn_down=1
    [[ -n "$hold_pid" ]] && kill "$hold_pid" 2>/dev/null
    log "--- tearing down"
    if ((failures)) && [[ -f "$work/env" ]]; then
        compose logs --no-color --tail 25 2>&1 | scrub_secrets
    fi
    compose down --volumes --remove-orphans --timeout 5 >/dev/null 2>&1
    docker image ls --format '{{.Repository}}:{{.Tag}}' | grep "^$project" |
        xargs -r docker image rm -f >/dev/null 2>&1
    for image in "${pulled_images[@]}"; do
        docker image rm "$image" >/dev/null 2>&1
    done
    local left=""
    left+="$(docker ps -a --format '{{.Names}}' | grep "^$project")"
    left+="$(docker network ls --format '{{.Name}}' | grep "^$project")"
    left+="$(docker volume ls --format '{{.Name}}' | grep "^$project")"
    left+="$(docker image ls --format '{{.Repository}}' | grep "^$project")"
    if [[ -n "$left" ]]; then
        log "FAIL - left behind: $left"
        status=1
    else
        log "ok   - no container, network, volume or test image left behind"
    fi
    if [[ "$(others)" == "$other_containers_before" ]]; then
        log "ok   - every other container on this machine is still running, unchanged"
    else
        log "FAIL - the set of other running containers changed"
        status=1
    fi
    rm -rf "$work"
    exit "$status"
}
trap teardown EXIT
trap 'exit 130' INT TERM

scrub_secrets() {
    if [[ -f "$work/env" ]]; then
        python3 -c '
import sys
from urllib.parse import quote
text = sys.stdin.read()
for s in sys.argv[1:]:
    text = text.replace(s, "<secret>").replace(quote(s, safe=""), "<secret>")
sys.stdout.write(text)' "$broker_password" "${old_broker_password:-unset}" "$api_broker_password" \
            "$client_id" "$token" "$read_password" "$control_password" "$secret"
    else
        cat
    fi
}

log "--- setup-credentials.sh"
credentials="$work/credentials.env"
output="$("$relay/setup-credentials.sh" --output "$credentials" 2>&1)"
check "setup writes the file" test -s "$credentials"
check "setup writes it 0600" test "$(stat -c %a "$credentials")" = 600
token_line="$(grep '^RELAY_API_TOKEN=' "$credentials")"
generated="${token_line#*=}"
generated="${generated//\'/}"
if ((${#generated} >= 32)); then ok "setup generates a token"; else not_ok "setup generates a token"; fi
if grep -qF "$generated" <<<"$output"; then not_ok "setup prints no secret"; else ok "setup prints no secret"; fi
before="$(sha256sum "$credentials")"
refuted "setup refuses to overwrite without --force" "$relay/setup-credentials.sh" --output "$credentials"
check "a refused overwrite leaves the file alone" test "$(sha256sum "$credentials")" = "$before"
check "setup overwrites with --force" "$relay/setup-credentials.sh" --force --output "$credentials"
if [[ "$(sha256sum "$credentials")" != "$before" ]]; then ok "--force makes new secrets"; else not_ok "--force makes new secrets"; fi
check "compose accepts the file setup writes, with a site subnet" env RELAY_SITE_SUBNET=192.168.1.0/24 \
    docker compose --env-file "$credentials" -f "$relay/compose.yaml" config --quiet
refuted "compose refuses it with no site subnet" env -u RELAY_SITE_SUBNET \
    docker compose --env-file "$credentials" -f "$relay/compose.yaml" config --quiet
services="$(RELAY_SITE_SUBNET=192.168.1.0/24 docker compose --env-file "$credentials" \
    -f "$relay/compose.yaml" config --services)"
check "the file setup writes turns on the broker with no --profile flag" grep -qx mosquitto <<<"$services"
shim="$work/shim"
mkdir -p "$shim"
printf '#!/bin/sh\nexit 1\n' >"$shim/head"
chmod +x "$shim/head"
failed_output="$work/failed.env"
if PATH="$shim:$PATH" "$relay/setup-credentials.sh" --output "$failed_output" >/dev/null 2>&1; then
    not_ok "setup fails when it cannot make a secret"
else
    ok "setup fails when it cannot make a secret"
fi
refuted "and it leaves no file" test -e "$failed_output"
for name in RELAY_API_BROKER_PASSWORD RELAY_MQTT_CLIENT_ID; do
    if grep -qE "^$name='.{20,}'$" "$credentials"; then ok "setup generates $name"; else not_ok "setup generates $name"; fi
done
check "the example file holds no secret" test -z "$(grep -E "^RELAY_[A-Z_]*(PASSWORD|TOKEN)=.+" "$relay/.env.example")"
if grep -qE '(^|[^0-9])1935([^0-9]|$)|\blatest\b' "$relay/compose.yaml" "$relay/mediamtx.yml"; then
    not_ok "no port 1935 and no latest tag in the bundle"
else
    ok "no port 1935 and no latest tag in the bundle"
fi

log "--- bringing the bundle up"
uv venv -q "$work/venv" --python 3.13 2>/dev/null || uv venv -q "$work/venv"
uv pip install -q --python "$work/venv/bin/python" 'paho-mqtt==2.2.0' || exit 2
py="$work/venv/bin/python"
probe() { "$py" "$here/probe.py" "$@"; }

# A control subnet nothing here is using.
used_subnets="$(docker network inspect $(docker network ls -q) \
    --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)"
free_prefixes=()
for candidate in 172.31.249 172.31.248 172.31.247 172.31.246 172.31.245 10.254.249 10.254.248; do
    grep -qF "$candidate." <<<"$used_subnets" || free_prefixes+=("$candidate")
done
((${#free_prefixes[@]} >= 3)) || { log "no free subnets"; exit 2; }
control_prefix="${free_prefixes[0]}" edge_prefix="${free_prefixes[1]}" site="${free_prefixes[2]}"

mapfile -t ports < <(probe free-ports 6)
api_port="${ports[0]}" rtsp_port="${ports[1]}" webrtc_port="${ports[2]}" media_port="${ports[3]}"
broker_port="${ports[4]}" ntp_port="${ports[5]}"
token="$(openssl rand -hex 24)"
broker_user=relay
# Every character a node's portal accepts that means something in a URL, so that a working pull
# proves the percent-encoding, including a literal %2F which must not decode to a slash.
broker_password="t3st%2F/p#w@rd:x?&=$(openssl rand -hex 6)"
api_broker_user=relay-api-login
api_broker_password="$(openssl rand -hex 16)"
client_id="relay-api-$(openssl rand -hex 16)"
read_user=viewer
read_password="$(openssl rand -hex 16)"
control_password="$(openssl rand -hex 16)"
secret="$(printf '%s' "black-relay-frigate-proxy:$broker_password" | sha256sum | cut -d' ' -f1)"
cat >"$work/env" <<ENV
RELAY_IMAGE_PREFIX=$project
RELAY_BIND_ADDRESS=127.0.0.1
RELAY_API_PORT=$api_port
RELAY_RTSP_PORT=$rtsp_port
RELAY_WEBRTC_HTTP_PORT=$webrtc_port
RELAY_WEBRTC_UDP_PORT=$media_port
RELAY_WEBRTC_HOSTS=127.0.0.1
RELAY_BROKER_PUBLISH_PORT=$broker_port
RELAY_NTP_PORT=$ntp_port
COMPOSE_PROFILES=broker
RELAY_SITE_SUBNET=$site.0/24
RELAY_CENTRAL_IP=$site.250
RELAY_EDGE_SUBNET=$edge_prefix.0/24
RELAY_CONTROL_SUBNET=$control_prefix.0/29
RELAY_MEDIAMTX_CONTROL_IP=$control_prefix.2
RELAY_API_CONTROL_IP=$control_prefix.3
RELAY_API_TOKEN='$token'
RELAY_BROKER_USER='$broker_user'
RELAY_BROKER_PASSWORD='$broker_password'
RELAY_API_BROKER_USER='$api_broker_user'
RELAY_API_BROKER_PASSWORD='$api_broker_password'
RELAY_MQTT_CLIENT_ID='$client_id'
RELAY_READ_USER='$read_user'
RELAY_READ_PASSWORD='$read_password'
RELAY_MEDIAMTX_API_PASSWORD='$control_password'
RELAY_TEST_DIR=$work
ENV
chmod 600 "$work/env"
export PROBE_USER="$broker_user" PROBE_PASSWORD="$broker_password"
export PROBE_API_USER="$api_broker_user" PROBE_API_PASSWORD="$api_broker_password"

cat >"$work/go2rtc.yaml" <<YAML
api:
  listen: "127.0.0.1:1984"
rtsp:
  listen: ":8554"
  username: "$broker_user"
  password: "$broker_password"
streams:
  cam-01:
    - "exec:ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=10 -c:v libx264 -preset ultrafast -tune zerolatency -g 10 -f rtsp {output}"
YAML
chmod 644 "$work/go2rtc.yaml"
mkdir -p "$work/sim"
cp "$here/frigate_sim.py" "$work/sim/"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=frigate-sim" \
    -keyout "$work/sim/key.pem" -out "$work/sim/cert.pem" 2>/dev/null
chmod 755 "$work/sim" "$work"
chmod 644 "$work/sim/"*

clock_before="$(probe clock)"
if ! compose up -d --build --quiet-pull >"$work/up.log" 2>&1; then
    tail -30 "$work/up.log" | scrub_secrets
    not_ok "the bundle starts"
    exit 1
fi
ok "the bundle starts"

api() { curl -sS --max-time 10 -H "Authorization: Bearer $token" "$@"; }
api_url="http://127.0.0.1:$api_port"
nodes() { api "$api_url/nodes"; }
wait_until 60 nodes || not_ok "the API answers"

container_ip() { # container_ip <service> <network>
    docker inspect "$(compose ps -q "$1")" --format \
        "{{(index .NetworkSettings.Networks \"${project}_$2\").IPAddress}}"
}
# Runs inside the API container, the one place the control API admits, with its own login.
control_api() {
    compose exec -T api python -c '
import base64, json, os, sys, urllib.request
request = urllib.request.Request(os.environ["RELAY_MEDIAMTX_API_URL"] + sys.argv[1])
login = os.environ["RELAY_MEDIAMTX_API_USER"] + ":" + os.environ["RELAY_MEDIAMTX_API_PASSWORD"]
request.add_header("Authorization", "Basic " + base64.b64encode(login.encode()).decode())
print(urllib.request.urlopen(request, timeout=5).read().decode())' "$1"
}
path_status() { # the control API's HTTP status for this path's configuration
    compose exec -T api python -c '
import base64, os, sys, urllib.error, urllib.request
request = urllib.request.Request(os.environ["RELAY_MEDIAMTX_API_URL"] + "/v3/config/paths/get/" + sys.argv[1])
login = os.environ["RELAY_MEDIAMTX_API_USER"] + ":" + os.environ["RELAY_MEDIAMTX_API_PASSWORD"]
request.add_header("Authorization", "Basic " + base64.b64encode(login.encode()).decode())
try:
    print(urllib.request.urlopen(request, timeout=5).status)
except urllib.error.HTTPError as error:
    print(error.code)' "$1"
}
path_source_host() { # the host part only, as the URL holds the broker's credentials
    control_api "/v3/config/paths/get/$1" | python3 -c '
import json, sys
print(json.load(sys.stdin)["source"].rsplit("@", 1)[1])'
}
path_field() {
    control_api "/v3/config/paths/get/$1" | python3 -c '
import json, sys
print(json.load(sys.stdin)[sys.argv[1]])' "$2"
}
readers_of() {
    control_api "/v3/paths/get/$1" | python3 -c '
import json, sys
print(len(json.load(sys.stdin).get("readers", [])))'
}
# Established TCP connections from other hosts to the node's RTSP port, as the node's kernel
# sees them. go2rtc's own loopback publisher does not count.
source_connections() {
    # go2rtc listens on [::], so outside connections show in tcp6 as well as tcp.
    docker exec "$(compose ps -q node-sim)" cat /proc/net/tcp /proc/net/tcp6 | python3 -c '
import sys
count = 0
for line in sys.stdin.read().splitlines():
    fields = line.split()
    if len(fields) < 4 or fields[0] == "sl":
        continue
    local, remote, state = fields[1], fields[2], fields[3]
    loopback = remote.split(":")[0] in (
        "0100007F", "0000000000000000FFFF00000100007F", "00000000000000000000000001000000",
    )
    if state == "01" and local.endswith(":216A") and not loopback:
        count += 1
print(count)'
}
node_field() { nodes | "$py" "$here/probe.py" node-field "$1" "$2"; }
broker_field() { nodes | "$py" "$here/probe.py" broker-field "$1"; }

log "--- the API's login"
status() { curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$@"; }
for route in /nodes /events /events/stream /nodes/x/snapshot/y.jpg /nodes/x/frigate/api/stats; do
    check "no token: $route is refused with 401" test "$(status "$api_url$route")" = 401
done
for route in /docs /openapi.json; do
    code="$(status "$api_url$route")"
    check "no token: $route does not exist or is refused ($code)" test "$code" = 401 -o "$code" = 404
    code="$(status -H "Authorization: Bearer $token" "$api_url$route")"
    check "with the token: $route does not exist ($code)" test "$code" = 404
done
check "a wrong token is refused" test "$(status -H 'Authorization: Bearer wrong' "$api_url/nodes")" = 401
check "the right token reads" test "$(status -H "Authorization: Bearer $token" "$api_url/nodes")" = 200
image="$project-api:0.1.0"
no_token_log="$work/no-token.log"
if docker run --rm --network none "$image" >"$no_token_log" 2>&1; then
    not_ok "with no token the API refuses to start"
else
    check "with no token the API refuses to start, and says why" grep -q RELAY_API_TOKEN "$no_token_log"
fi
no_site_log="$work/no-site.log"
if docker run --rm --network none -e RELAY_API_TOKEN=x -e RELAY_BROKER_USER=x -e RELAY_BROKER_PASSWORD=x \
    -e RELAY_API_BROKER_USER=y -e RELAY_API_BROKER_PASSWORD=x -e RELAY_MQTT_CLIENT_ID=x \
    -e RELAY_MEDIAMTX_API_URL=http://x -e RELAY_MEDIAMTX_API_USER=x -e RELAY_MEDIAMTX_API_PASSWORD=x \
    "$image" >"$no_site_log" 2>&1; then
    not_ok "with no site subnet the API refuses to start"
else
    check "with no site subnet the API refuses to start, and says why" grep -q RELAY_SITE_SUBNET "$no_site_log"
fi

log "--- MQTT"
wait_equal 40 true broker_field connected || true
if [[ "$(broker_field connected)" != true ]]; then
    not_ok "the API logged in to the broker"
    exit 1 # Everything after this depends on it, and would only wait out its timeouts.
fi
ok "the API logged in to the broker"
check "a login with the password works" probe login --port "$broker_port"
refuted "an anonymous login is refused" probe login --port "$broker_port" --anonymous
refuted "a wrong password is refused" probe login --port "$broker_port" --wrong-password
check "the ACL allows detection/ and blocks everything else" probe acl --port "$broker_port"
check "the API's own login works" env PROBE_USER="$api_broker_user" PROBE_PASSWORD="$api_broker_password" \
    "$py" "$here/probe.py" login --port "$broker_port"
check "the API's login can only read" probe acl-read-only --port "$broker_port"

log "--- heartbeats and paths"
node_ip="$(container_ip node-sim site)"
check "publishing a heartbeat works" probe heartbeat --port "$broker_port" --node cam-01 --address "$node_ip"
wait_equal 10 "\"$node_ip\"" node_field cam-01 node_address || true
check "the heartbeat appears at GET /nodes" test "$(node_field cam-01 node_address)" = "\"$node_ip\""
check "with its raw detector_ok" test "$(node_field cam-01 detector_ok)" = false
check "and not stale" test "$(node_field cam-01 stale)" = false
wait_equal 10 "$node_ip:8554/cam-01" path_source_host cam-01 || true
check "it creates the path" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"
check "the path pulls on demand" test "$(path_field cam-01 sourceOnDemand)" = True
check "the path closes after 10 s" test "$(path_field cam-01 sourceOnDemandCloseAfter)" = 10s
check "the path uses TCP" test "$(path_field cam-01 rtspTransport)" = tcp

probe heartbeat --port "$broker_port" --node cam-01 --address "$site.99" --age 3600
sleep 2
check "an old replay does not move the address in /nodes" test "$(node_field cam-01 node_address)" = "\"$node_ip\""
check "an old replay does not move the path" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"
probe heartbeat --port "$broker_port" --node cam-01 --address "$site.98"
wait_equal 10 "$site.98:8554/cam-01" path_source_host cam-01 || true
check "a newer heartbeat does move the path" test "$(path_source_host cam-01)" = "$site.98:8554/cam-01"
probe heartbeat --port "$broker_port" --node cam-01 --address "$node_ip"
wait_equal 10 "$node_ip:8554/cam-01" path_source_host cam-01 || true
check "and back" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"

log "--- F1, F2, F3: addresses that must never reach the proxy or MediaMTX"
broker_edge_ip="$(container_ip mosquitto edge)"
mediamtx_edge_ip="$(container_ip mediamtx edge)"
attacks=(
    "::ffff:$broker_edge_ip%25x]:1883?"
    "::ffff:$control_prefix.2%25x]:9997?"
    "fd00::1%25x]:9997?"
    $'fd00::1%\n'
    "$site.21:1883"
    "$site.21:9997"
    "$site.21"$'\n'
    "8.8.8.8"
    "2606:4700::1111"
    "2002:7f00:1::"
    "::ffff:8.8.8.8"
    "100.100.100.100"
    "$control_prefix.2"
    "$edge_prefix.3"
    "172.17.0.1"
    "$site.255"
    "$site.0"
    "$site.250"
    "127.0.0.1"
    "169.254.1.1"
)
broker_connections_before="$(compose logs --no-color mosquitto 2>/dev/null | grep -c "New connection from $mediamtx_edge_ip")"
index=0
for attack in "${attacks[@]}"; do
    probe heartbeat --port "$broker_port" --node "attack-$index" --address "$attack" >/dev/null
    index=$((index + 1))
done
sleep 7
stored=0
paths=0
proxied=0
for ((i = 0; i < index; i++)); do
    [[ "$(node_field "attack-$i" node_address)" == null ]] || stored=$((stored + 1))
    [[ "$(path_status "attack-$i")" == 404 ]] || paths=$((paths + 1))
    code="$(status -H "Authorization: Bearer $token" "$api_url/nodes/attack-$i/frigate/x")"
    [[ "$code" == 503 ]] || proxied=$((proxied + 1))
done
refused_count="$(broker_field dropped | python3 -c 'import json,sys;print(json.load(sys.stdin).get("refused_address",0))')"
log "     measured: of ${index} attack addresses: $stored stored, $paths paths created, $proxied not refused by the proxy; refused_address=$refused_count"
check "no attack address is stored" test "$stored" = 0
check "no attack address made a path" test "$paths" = 0
check "the proxy refuses every one of those nodes" test "$proxied" = 0
check "each is counted" test "$refused_count" -ge "$index"
refuted "reading an attack's path fails" ffprobe -v error -rtsp_transport tcp -timeout 8000000 \
    "rtsp://$read_user:$read_password@127.0.0.1:$rtsp_port/attack-0"
sleep 2
broker_connections_after="$(compose logs --no-color mosquitto 2>/dev/null | grep -c "New connection from $mediamtx_edge_ip")"
log "     measured: connections from MediaMTX to the broker: $broker_connections_before before, $broker_connections_after after"
check "MediaMTX never dialled port 1883" test "$broker_connections_before" = "$broker_connections_after"
mediamtx_logs="$(compose logs --no-color mediamtx 2>&1)"
check "MediaMTX's log holds no source URL or refused source" test -z "$(grep -E 'is not a valid URL|rtsp://' <<<"$mediamtx_logs")"

log "--- F4: nesting and size"
check "a heartbeat 33 deep is published" probe deep --port "$broker_port" --node deep-33 --depth 33
check "one 9992 deep is published" probe deep --port "$broker_port" --node deep-9992 --depth 9992
sleep 1
check "GET /nodes still answers" test "$(status -H "Authorization: Bearer $token" "$api_url/nodes")" = 200
check "neither is stored" test "$(node_field deep-33 stale)" = absent -a "$(node_field deep-9992 stale)" = absent
dropped="$(broker_field dropped)"
log "     measured: too_deep and oversize counters: $dropped"
check "they are counted" python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["too_deep"]>=1 and d["oversize"]>=1' "$dropped"
# As a database from before this rule would hold them: put in directly, past ingest.
compose exec -T api python -c '
import json, os, sqlite3, time
db = sqlite3.connect(os.environ.get("RELAY_DB_PATH", "/data/relay.sqlite3"))
base = {"schema": "camera.node-status.v0", "timestamp": "2026-01-01T00:00:00Z", "camera_online": True,
        "link_online": True, "recording_ok": True, "detector_ok": True, "attributes": "@"}
text = json.dumps(base)
for depth in range(9985, 10001):
    nested = "{\"a\":" * depth + "1" + "}" * depth
    db.execute("INSERT OR REPLACE INTO nodes VALUES (?, 1, ?, NULL, ?)",
               (f"row-{depth}", time.time(), text.replace("\"@\"", nested)))
db.execute("INSERT OR REPLACE INTO nodes VALUES (?, 1, ?, NULL, ?)", ("row-garbage", time.time(), "{nope"))
db.commit()'
stored_bad_status="$(status -H "Authorization: Bearer $token" "$api_url/nodes")"
unreadable="$(nodes | probe top-field unreadable_nodes)"
log "     measured: GET /nodes with 17 bad rows stored: HTTP $stored_bad_status, unreadable_nodes=$unreadable"
check "stored bad rows do not break the listing" test "$stored_bad_status" = 200
check "they are counted, and good nodes are listed" test "$unreadable" -ge 17 -a "$(node_field cam-01 node_address)" = "\"$node_ip\""
check "a bad row's own route is a clean refusal" test "$(status -H "Authorization: Bearer $token" "$api_url/nodes/row-9992/frigate/x")" = 503

log "--- F5: names MediaMTX treats specially, and length"
probe heartbeat --port "$broker_port" --node all --address "$site.31"
probe heartbeat --port "$broker_port" --node "$(printf 'a%.0s' {1..64})" --address "$site.32"
long_ok="$(printf 'b%.0s' {1..63})"
probe heartbeat --port "$broker_port" --node "$long_ok" --address "$site.33"
wait_equal 10 200 path_status "$long_ok" || true
check "a 63-character id makes a path" test "$(path_status "$long_ok")" = 200
check "all makes no path" test "$(path_status all)" = 404
check "all is not listed" test "$(node_field all stale)" = absent
check "a 64-character id is not listed" test "$(node_field "$(printf 'a%.0s' {1..64})" stale)" = absent
ghost_log="$work/ghost.log"
ffprobe -v error -rtsp_transport tcp -timeout 8000000 \
    "rtsp://$read_user:$read_password@127.0.0.1:$rtsp_port/ghost-path" >"$ghost_log" 2>&1
ghost_starts="$(compose logs --no-color mediamtx 2>/dev/null | grep -c 'path ghost-path.*source')"
log "     measured: reading a name nobody configured: $(tail -1 "$ghost_log" | sed 's/.*: //' | cut -c1-40); source starts on it: $ghost_starts"
check "reading an unknown name starts no source" test "$ghost_starts" = 0

log "--- F6: a stamp ahead of the clock"
probe heartbeat --port "$broker_port" --node clock-test --address "$site.40" --age -299
sleep 1
check "a stamp 299 s ahead is dropped" test "$(node_field clock-test stale)" = absent
probe heartbeat --port "$broker_port" --node clock-test --address "$site.41"
wait_equal 5 "\"$site.41\"" node_field clock-test node_address || true
check "the truthful heartbeat after it is accepted" test "$(node_field clock-test node_address)" = "\"$site.41\""
log "     measured: heartbeat_age_s right after receipt: $(node_field clock-test heartbeat_age_s)"

log "--- video pulled only on demand"
rtsp_url="rtsp://127.0.0.1:$rtsp_port/cam-01"
idle_max=0
for _ in 1 2 3 4 5 6 7 8; do
    nodes >/dev/null
    connections="$(source_connections)"
    ((connections > idle_max)) && idle_max="$connections"
    sleep 1
done
log "     measured: $idle_max source connections at most over 8 s idle, listing nodes each second"
check "idle: zero source connections, even while listing nodes" test "$idle_max" = 0
refuted "a read with no login is refused" ffprobe -v error -rtsp_transport tcp -timeout 8000000 "$rtsp_url"
refuted "a read with the node's login is refused" ffprobe -v error -rtsp_transport tcp -timeout 8000000 \
    "rtsp://$broker_user:$(python3 -c 'import sys,urllib.parse as u;print(u.quote(sys.argv[1],safe=""))' "$broker_password")@127.0.0.1:$rtsp_port/cam-01"
check "a refused read opens no source connection" test "$(source_connections)" = 0

reader_log="$work/reader.log"
ffmpeg -nostdin -hide_banner -loglevel info -rtsp_transport tcp \
    -i "rtsp://$read_user:$read_password@127.0.0.1:$rtsp_port/cam-01" -t 8 -f null - >"$reader_log" 2>&1 &
reader_pid=$!
wait_equal 15 1 source_connections || true
during="$(source_connections)"
readers="$(readers_of cam-01)"
sleep 3
during_later="$(source_connections)"
log "     measured: $during then $during_later source connections while reading, $readers reader(s) on MediaMTX"
check "reading: exactly one source connection" test "$during" = 1 -a "$during_later" = 1
check "reading: MediaMTX counts the reader" test "$readers" = 1
wait "$reader_pid"
reader_status=$?
read_end=$SECONDS
check "the read with the reader login succeeds" test "$reader_status" = 0
frames="$(grep -oE 'frame= *[0-9]+' "$reader_log" | tail -1 | grep -oE '[0-9]+')"
log "     measured: the reader decoded ${frames:-0} frames"
check "and real video arrived" test "${frames:-0}" -gt 0
wait_equal 30 0 source_connections || true
closed_after=$((SECONDS - read_end))
log "     measured: source connection closed ${closed_after} s after the last reader left"
check "it closes after the timeout, not at once" test "$closed_after" -ge 7 -a "$closed_after" -le 20
check "after closing, zero source connections" test "$(source_connections)" = 0

log "--- WebRTC signalling and what is not served"
webrtc_url="http://127.0.0.1:$webrtc_port/cam-01/whep"
check "WebRTC signalling answers with a login" test "$(status -u "$read_user:$read_password" -X OPTIONS "$webrtc_url")" = 204
check "WebRTC signalling refuses without a login" test "$(status -X POST -H 'Content-Type: application/sdp' --data 'v=0' "$webrtc_url")" = 401
check "signalling opened no source connection" test "$(source_connections)" = 0
global_config="$(control_api /v3/config/global/get)"
for listener in rtmp hls srt moq playback metrics pprof; do
    check "$listener is off" test "$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])[sys.argv[2]])' "$global_config" "$listener")" = False
done

log "--- who can reach the control API"
control_url="http://$control_prefix.2:9997/v3/config/global/get"
check "the API container can" test "$(control_api /v3/config/global/get | wc -c)" -gt 10
check "no published port for it" test -z "$(compose port mediamtx 9997 2>/dev/null)"
host_status="$(status -u "relay-api:$control_password" --max-time 5 "$control_url")"
log "     measured: from this host, with the right password: HTTP $host_status (000 = no connection)"
check "this host cannot, even with its password" test "$host_status" != 200
mediamtx_ip="$(container_ip mediamtx edge)"
refuted "a container on the edge network cannot, by the control address" \
    compose exec -T mosquitto wget -q -T 3 -O /dev/null \
    "http://relay-api:$control_password@$control_prefix.2:9997/v3/config/global/get"
refuted "a container on the edge network cannot, by the edge address" \
    compose exec -T mosquitto nc -z -w 3 "$mediamtx_ip" 9997
refuted "port 1935 is closed on MediaMTX" compose exec -T mosquitto nc -z -w 3 "$mediamtx_ip" 1935

log "--- events"
event_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
sse_log="$work/sse.log"
curl -sS -N --max-time 6 -H "Authorization: Bearer $token" "$api_url/events/stream" >"$sse_log" 2>&1 &
sse_pid=$!
sleep 1
probe detection --port "$broker_port" --node cam-01 --event-id "$event_id"
probe detection --port "$broker_port" --node cam-01 --event-id "$event_id"
wait "$sse_pid"
check "the event reaches the server-sent stream" grep -q "$event_id" "$sse_log"
event_count() { api "$api_url/events?limit=100" | probe event-count "$1"; }
check "a redelivered event is kept once" test "$(event_count "$event_id")" = 1

log "--- bad payloads"
check "publishing deep nesting and huge numbers works" probe huge --port "$broker_port"
probe raw --port "$broker_port" --topic detection/camera/detections --payload 'not json'
sleep 1
check "the API survives them" test "$(status -H "Authorization: Bearer $token" "$api_url/nodes")" = 200
dropped="$(broker_field dropped)"
log "     measured: dropped counters: $dropped"
check "they are counted" python3 -c '
import json, sys
dropped = json.loads(sys.argv[1])
assert dropped["not_json"] >= 2 and dropped["oversize"] >= 2 and dropped["schema"] >= 1' "$dropped"
check "the API did not restart" test "$(docker inspect "$(compose ps -q api)" --format '{{.RestartCount}}')" = 0

log "--- Frigate proxy, over real TLS with a self-signed certificate"
frigate_ip="$(container_ip frigate-sim site)"
probe heartbeat --port "$broker_port" --node cam-02 --address "$frigate_ip"
wait_equal 10 "\"$frigate_ip\"" node_field cam-02 node_address || true
proxied="$work/proxied.json"
curl -sS --max-time 10 -o "$proxied" -D "$work/proxied.headers" -H "Authorization: Bearer $token" \
    -H 'Remote-Role: admin' -H 'Cookie: a=b' "$api_url/nodes/cam-02/frigate/api/config?x=1"
check "the proxy reaches a node with a self-signed certificate" grep -q '"path"' "$proxied"
secret_hash="$(printf '%s' "$secret" | sha256sum | cut -d' ' -f1)"
check "the node received the right secret" grep -q "\"secret_sha256\": \"$secret_hash\"" "$proxied"
if grep -qF "$secret" "$proxied" "$work/proxied.headers"; then
    not_ok "the secret is stripped from the response"
else
    ok "the secret is stripped from the response"
fi
if grep -qE '"(authorization|remote-role|cookie)"' "$proxied"; then
    not_ok "the client's login and role headers stay here"
else
    ok "the client's login and role headers stay here"
fi
for raw in '..' '%2e%2e' '..%2fx' '%252e%252e/x' '//evil.example/x' 'http://evil.example/x'; do
    code="$(status --path-as-is -H "Authorization: Bearer $token" "$api_url/nodes/cam-02/frigate/$raw")"
    check "uvicorn path: $raw is refused ($code)" test "$code" = 400 -o "$code" = 404
done
check "an unknown node is refused" test "$(status -H "Authorization: Bearer $token" "$api_url/nodes/ghost/frigate/x")" = 404

log "--- F7: the API's session"
reconnects() { broker_field reconnects; }
before="$(reconnects)"
check "a node login using the old public client id connects" probe takeover --port "$broker_port" --client-id relay-api --seconds 5
after="$(reconnects)"
log "     measured: API reconnects while a node login held the id relay-api: $((after - before))"
check "it does not disturb the API" test "$before" = "$after"
check "and the API stays connected" test "$(broker_field connected)" = true
before="$(reconnects)"
probe takeover --port "$broker_port" --client-id "$client_id" --seconds 8
after="$(reconnects)"
log "     measured: API reconnects in 8 s while a client held the API's secret id itself: $((after - before)); the id's secrecy is what prevents this"
wait_equal 40 true broker_field connected || true
check "after that the API reconnects by itself" test "$(broker_field connected)" = true

log "--- a broker outage and an API restart"
compose stop -t 5 api >/dev/null 2>&1
replay_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
check "a durable event is published while the API is down" probe detection --port "$broker_port" --node cam-01 --event-id "$replay_id"
compose start api >/dev/null 2>&1
wait_equal 40 1 event_count "$replay_id" || true
check "after its restart the API receives it" test "$(event_count "$replay_id")" = 1
check "nodes survive the restart" test "$(node_field cam-01 node_address)" = "\"$node_ip\""
check "paths are in place after the restart" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"

compose stop -t 5 mosquitto >/dev/null 2>&1
wait_equal 40 false broker_field connected || true
check "broker down: the API says so" test "$(broker_field connected)" = false
check "broker down: reads still work" test "$(status -H "Authorization: Bearer $token" "$api_url/events")" = 200
check "broker down: nodes are still listed" test "$(node_field cam-01 node_address)" = "\"$node_ip\""
compose start mosquitto >/dev/null 2>&1
wait_equal 60 true broker_field connected || true
check "broker back: the API reconnects by itself" test "$(broker_field connected)" = true
wait_until 20 probe heartbeat --port "$broker_port" --node cam-03 --address "$site.5" || true
wait_equal 10 "\"$site.5\"" node_field cam-03 node_address || true
check "broker back: new heartbeats arrive" test "$(node_field cam-03 node_address)" = "\"$site.5\""

mediamtx_restart_before="$(path_source_host cam-01)"
compose restart -t 3 mediamtx >/dev/null 2>&1
wait_equal 30 "$mediamtx_restart_before" path_source_host cam-01 || true
check "a restarted MediaMTX gets its paths back" test "$(path_source_host cam-01)" = "$mediamtx_restart_before"

log "--- F8: the broker's queue limit"
compose stop -t 5 api >/dev/null 2>&1
check "400 messages of 200 KB are published with the API down" probe flood --port "$broker_port" --count 400 --size 200000
sleep 1
broker_memory="$(docker stats --no-stream --format '{{.MemUsage}}' "$(compose ps -q mosquitto)")"
broker_bytes="$(python3 -c '
import re, sys
value, unit = re.match(r"([0-9.]+)(\w+)", sys.argv[1]).groups()
print(int(float(value) * {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30}[unit]))' "$broker_memory")"
log "     measured: the broker's memory after 80 MB queued against a 64 MiB limit: $broker_memory"
check "it stays near the limit" test "$broker_bytes" -lt 134217728

log "--- F9: a kill loses at most the autosave window"
compose start api >/dev/null 2>&1
wait_equal 40 true broker_field connected || true
sleep 3
compose stop -t 5 api >/dev/null 2>&1
killed_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
check "an event is published with the API down" probe detection --port "$broker_port" --node cam-01 --event-id "$killed_id"
log "     waiting out the 30 s autosave"
sleep 33
docker kill -s KILL "$(compose ps -q mosquitto)" >/dev/null
compose start mosquitto >/dev/null 2>&1
compose start api >/dev/null 2>&1
wait_equal 60 1 event_count "$killed_id" || true
check "it is delivered after the broker was killed" test "$(event_count "$killed_id")" = 1

log "--- F10: a full disk acknowledges nothing"
wait_equal 40 true broker_field connected || true
compose exec -T api sh -c 'dd if=/dev/zero of=/data/fill bs=1M >/dev/null 2>&1; true'
full_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
check "an event is published on a full disk" probe detection --port "$broker_port" --node cam-01 --event-id "$full_id"
store_full() { broker_field store_full; }
wait_until 15 test_positive store_full
held="$(store_full)"
unacked="$(broker_field unacked)"
log "     measured: on a full disk: store_full=$held unacked=$unacked events found=$(event_count "$full_id")"
check "it is counted as store_full" test "$held" -ge 1
check "it is held unacknowledged" test "$unacked" -ge 1
check "it is not stored" test "$(event_count "$full_id")" = 0
compose restart -t 5 api >/dev/null 2>&1
wait_equal 40 true broker_field connected || true
# A clean stop folds SQLite's write-ahead log away, which can free enough space to take the event.
redelivered() { (($(store_full) > 0 || $(event_count "$full_id") == 1)); }
wait_until 15 redelivered
log "     measured: after an API restart on the full disk: store_full=$(store_full) unacked=$(broker_field unacked) events found=$(event_count "$full_id")"
check "the broker delivered it again after the restart, as it was never acknowledged" redelivered
compose exec -T api rm -f /data/fill
wait_equal 15 1 event_count "$full_id" || true
check "with space again it is stored" test "$(event_count "$full_id")" = 1
wait_equal 10 0 broker_field unacked || true
check "and nothing is held" test "$(broker_field unacked)" = 0

log "--- NTP and the host's clock"
ntp="$(probe sntp --port "$ntp_port")"
log "     measured: $ntp"
check "an NTP query returns a time, at stratum 10" python3 -c '
import json, sys
answer = json.loads(sys.argv[1])
assert answer["stratum"] == 10 and abs(answer["offset"]) < 1' "$ntp"
chrony_container="$(compose ps -q chrony)"
capabilities="$(docker exec "$chrony_container" sh -c "grep CapEff /proc/1/status" | awk '{print $2}')"
log "     measured: chronyd CapEff $capabilities"
check "chronyd has no CAP_SYS_TIME" python3 -c '
import sys
assert not int(sys.argv[1], 16) & (1 << 25)' "$capabilities"
check "chronyd runs with -x" sh -c "docker exec '$chrony_container' cat /proc/1/cmdline | tr '\\0' ' ' | grep -q -- ' -x '"
clock_after="$(probe clock)"
drift="$(python3 -c 'import sys;print(abs(float(sys.argv[1])-float(sys.argv[2])))' "$clock_before" "$clock_after")"
log "     measured: the host's wall clock moved ${drift} s against its monotonic clock during the run"
check "the host's clock was not changed" python3 -c 'import sys; assert float(sys.argv[1]) < 0.5' "$drift"

log "--- F12: changing the broker password as the README says"
old_broker_password="$broker_password"
new_broker_password="n3w%2F/p#w@rd:x?&=$(openssl rand -hex 6)"
python3 - "$work/env" "$new_broker_password" <<'SWAP'
import re, sys
path, new = sys.argv[1:]
text = open(path).read()
text = re.sub(r"(?m)^RELAY_BROKER_PASSWORD=.*$", lambda m: f"RELAY_BROKER_PASSWORD='{new}'", text)
open(path, "w").write(text)
SWAP
compose up -d >/dev/null 2>&1
wait_equal 60 true broker_field connected || true
check "the API is connected after the change" test "$(broker_field connected)" = true
refuted "the old password is refused" probe login --port "$broker_port"
check "the new password works" env PROBE_PASSWORD="$new_broker_password" "$py" "$here/probe.py" login --port "$broker_port"
source_uses_new() {
    control_api /v3/config/paths/get/cam-01 | python3 -c '
import json, sys, urllib.parse as u
print(u.quote(sys.argv[1], safe="") in json.load(sys.stdin)["source"])' "$new_broker_password"
}
wait_equal 30 True source_uses_new || true
check "MediaMTX's pull uses the new password" test "$(source_uses_new)" = True
# go2rtc still holds the old one, so this read fails, as the README says until nodes change too.
ffprobe -v error -rtsp_transport tcp -timeout 8000000 \
    "rtsp://$read_user:$read_password@127.0.0.1:$rtsp_port/cam-01" >/dev/null 2>&1
export PROBE_PASSWORD="$new_broker_password"
broker_password="$new_broker_password"

log "--- F3 and F15: secrets in logs, after every failure above"
status "$api_url/nodes?token=$token" >/dev/null
sleep 1
logs="$work/all.log"
compose logs --no-color >"$logs" 2>&1
leaked=0
encode() { python3 -c 'import sys,urllib.parse as u;print(u.quote(sys.argv[1],safe=""))' "$1"; }
for value in "$old_broker_password" "$(encode "$old_broker_password")" "$new_broker_password" \
    "$(encode "$new_broker_password")" "$api_broker_password" "$token" "$read_password" \
    "$control_password" "$secret"; do
    grep -qF -- "$value" "$logs" && leaked=$((leaked + 1))
done
log "     measured: $(wc -l <"$logs") log lines, $leaked secret values found"
check "no password, token or proxy secret in any container's log" test "$leaked" = 0
check "the broker password is not in MediaMTX's log, raw or encoded" test -z "$(compose logs --no-color mediamtx 2>&1 |
    grep -F -e "$old_broker_password" -e "$(encode "$old_broker_password")" -e "$new_broker_password" \
        -e "$(encode "$new_broker_password")")"
# The broker's own log names each client, so it holds the API's client id, and nothing else does.
check "the API's client id is in no log but the broker's" test -z "$(grep -F "$client_id" "$logs" | grep -v '^.*mosquitto-1')"
check "no proxied URL or access line in the API's log" test -z "$(grep -E 'api-1.*(GET |HTTP Request)' "$logs")"

log "--- a published port that clashes"
compose stop -t 3 chrony >/dev/null 2>&1
probe hold-port "$ntp_port" --udp --seconds 120 >/dev/null &
hold_pid=$!
sleep 1
clash_log="$work/clash.log"
if compose up -d --no-build chrony >"$clash_log" 2>&1; then
    not_ok "a clashing port stops the bundle"
else
    ok "a clashing port stops the bundle"
    if grep -qiE "address already in use|port is already allocated" "$clash_log"; then
        ok "and the failure says why"
    else
        not_ok "and the failure says why"
        tail -5 "$clash_log" | scrub_secrets
    fi
fi

log "--- result: $failures failure(s)"
((failures == 0))
