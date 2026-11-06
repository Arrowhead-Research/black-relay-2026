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
        -f "$relay/compose.yaml" -f "$here/compose.test.yaml" --profile broker "$@"
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
sys.stdout.write(text)' "$broker_password" "$token" "$read_password" "$control_password" "$secret"
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
check "compose accepts the file setup writes" docker compose --env-file "$credentials" \
    -f "$relay/compose.yaml" --profile broker config --quiet
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
control_prefix=""
for candidate in 172.31.249 172.31.248 172.31.247 172.31.246 10.254.249 10.254.248; do
    if ! grep -qF "$candidate." <<<"$used_subnets"; then
        control_prefix="$candidate"
        break
    fi
done
[[ -n "$control_prefix" ]] || { log "no free control subnet"; exit 2; }

mapfile -t ports < <(probe free-ports 6)
api_port="${ports[0]}" rtsp_port="${ports[1]}" webrtc_port="${ports[2]}" media_port="${ports[3]}"
broker_port="${ports[4]}" ntp_port="${ports[5]}"
token="$(openssl rand -hex 24)"
broker_user=relay
# Every character a node's portal accepts that means something in a URL, so that a working pull
# proves the percent-encoding, including a literal %2F which must not decode to a slash.
broker_password="t3st%2F/p#w@rd:x?&=$(openssl rand -hex 6)"
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
RELAY_CONTROL_SUBNET=$control_prefix.0/29
RELAY_MEDIAMTX_CONTROL_IP=$control_prefix.2
RELAY_API_CONTROL_IP=$control_prefix.3
RELAY_API_TOKEN='$token'
RELAY_BROKER_USER='$broker_user'
RELAY_BROKER_PASSWORD='$broker_password'
RELAY_READ_USER='$read_user'
RELAY_READ_PASSWORD='$read_password'
RELAY_MEDIAMTX_API_PASSWORD='$control_password'
RELAY_TEST_DIR=$work
ENV
chmod 600 "$work/env"
export PROBE_USER="$broker_user" PROBE_PASSWORD="$broker_password"

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

container_ip() { # the service's address on the bundle's edge network
    docker inspect "$(compose ps -q "$1")" --format \
        "{{(index .NetworkSettings.Networks \"${project}_edge\").IPAddress}}"
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
for route in /nodes /events /events/stream /nodes/x/snapshot/y.jpg /nodes/x/frigate/api/stats /docs /openapi.json; do
    check "no token: $route is refused" test "$(status "$api_url$route")" = 401 -o "$(status "$api_url$route")" = 404
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

log "--- heartbeats and paths"
node_ip="$(container_ip node-sim)"
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

probe heartbeat --port "$broker_port" --node cam-01 --address 192.0.2.99 --age 3600
sleep 2
check "an old replay does not move the address in /nodes" test "$(node_field cam-01 node_address)" = "\"$node_ip\""
check "an old replay does not move the path" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"
probe heartbeat --port "$broker_port" --node cam-01 --address 192.0.2.98
wait_equal 10 "192.0.2.98:8554/cam-01" path_source_host cam-01 || true
check "a newer heartbeat does move the path" test "$(path_source_host cam-01)" = "192.0.2.98:8554/cam-01"
probe heartbeat --port "$broker_port" --node cam-01 --address "$node_ip"
wait_equal 10 "$node_ip:8554/cam-01" path_source_host cam-01 || true
check "and back" test "$(path_source_host cam-01)" = "$node_ip:8554/cam-01"

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
mediamtx_ip="$(container_ip mediamtx)"
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
check "they are counted" python3 -c 'import json,sys; assert json.loads(sys.argv[1]).get("not_json", 0) >= 3' "$dropped"
check "the API did not restart" test "$(docker inspect "$(compose ps -q api)" --format '{{.RestartCount}}')" = 0

log "--- Frigate proxy, over real TLS with a self-signed certificate"
frigate_ip="$(container_ip frigate-sim)"
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
wait_until 20 probe heartbeat --port "$broker_port" --node cam-03 --address 192.0.2.5 || true
wait_equal 10 '"192.0.2.5"' node_field cam-03 node_address || true
check "broker back: new heartbeats arrive" test "$(node_field cam-03 node_address)" = '"192.0.2.5"'

mediamtx_restart_before="$(path_source_host cam-01)"
compose restart -t 3 mediamtx >/dev/null 2>&1
wait_equal 30 "$mediamtx_restart_before" path_source_host cam-01 || true
check "a restarted MediaMTX gets its paths back" test "$(path_source_host cam-01)" = "$mediamtx_restart_before"

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

log "--- secrets in logs"
logs="$work/all.log"
compose logs --no-color >"$logs" 2>&1
leaked=0
encoded_password="$(python3 -c 'import sys,urllib.parse as u;print(u.quote(sys.argv[1],safe=""))' "$broker_password")"
for value in "$broker_password" "$encoded_password" "$token" "$read_password" "$control_password" "$secret"; do
    grep -qF "$value" "$logs" && leaked=$((leaked + 1))
done
check "no secret in any container's log" test "$leaked" = 0

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
