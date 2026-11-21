#!/usr/bin/env bash
# Tests node-logins.sh, node-secrets.sh, their rf-* wrappers and make-env.sh against a temp repo root with a fake .env,
# and the login part of central-broker/entrypoint.sh with mosquitto stubbed out. No docker is needed:
# a stub docker stands in and reports the broker as running, stopped or failing.
#   detection/central/tests/test_node_logins.sh
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
detection="$(cd "$here/../.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/node-logins-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
pass=0 fail=0

# check <what> <expected> <actual>
check() {
    if [ "$2" = "$3" ]; then pass=$((pass + 1)); else
        fail=$((fail + 1)); printf 'FAIL: %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3" >&2
    fi
}
# has <what> <text> <fixed string>, lacks <what> <text> <fixed string>
has() {
    if grep -qF -- "$3" <<<"$2"; then pass=$((pass + 1)); else
        fail=$((fail + 1)); printf 'FAIL: %s: no [%s] in [%s]\n' "$1" "$3" "$2" >&2
    fi
}
lacks() {
    if grep -qF -- "$3" <<<"$2"; then
        fail=$((fail + 1)); printf 'FAIL: %s: found [%s] in [%s]\n' "$1" "$3" "$2" >&2
    else pass=$((pass + 1)); fi
}
# run <command...>: sets out (stdout), err (stderr) and st (exit status)
run() { out="$("$@" 2>"$work/err")"; st=$?; err="$(cat "$work/err")"; }

# A stub docker: logs every call; "running" makes `compose ps` list the broker, "broken" fails.
mkdir -p "$work/bin"
cat >"$work/bin/docker" <<'EOS'
#!/bin/sh
echo "$*" >>"$DOCKER_LOG"
mode="$(cat "$DOCKER_MODE" 2>/dev/null || echo stopped)"
[ "$mode" != broken ] || exit 1
if [ "$1 $2" = "compose ps" ] && [ "$mode" = running ]; then echo broker; fi
exit 0
EOS
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" DOCKER_LOG="$work/docker.log" DOCKER_MODE="$work/docker.mode"
set_docker() { echo "$1" >"$DOCKER_MODE"; : >"$DOCKER_LOG"; }

# A fake repo root: the two scripts under detection/central, and a .env.
root="$work/repo"
mkdir -p "$root/detection/central"
cp "$detection/central/node-logins.sh" "$detection/central/rf-node-logins.sh" \
    "$detection/central/node-secrets.sh" "$detection/central/rf-node-secrets.sh" "$root/detection/central/"
cat >"$root/.env" <<'EOS'
BIND_ADDRESS=172.20.100.150
BROKER_CAMERA_USER=relay
BROKER_CAMERA_PASSWORD=shortpw1234
EOS
chmod 600 "$root/.env"
nl="$root/detection/central/node-logins.sh"
rfnl="$root/detection/central/rf-node-logins.sh"
env_file="$root/.env"
value() { sed -n "s/^$1=//p" "$env_file" | tail -n1; }
pw_of() { tr ' ' '\n' <<<"$(value "$1")" | sed -n "s/^$2://p" | sed -n 1p; }

echo "== node-logins.sh"
set_docker stopped

# Camera logins from nothing.
run "$nl" camera cam-1 cam-2
check "camera add: exit" 0 "$st"
check "camera add: message" "added broker logins: cam-node-cam-1 cam-node-cam-2" "$out"
lacks "camera add: no restart while the broker is stopped" "$(cat "$DOCKER_LOG")" "compose up"
check "camera add: .env stays 0600" 600 "$(stat -c %a "$env_file")"
check "camera add: two entries" 2 "$(wc -w <<<"$(value BROKER_CAMERA_NODE_LOGINS)")"
for n in cam-1 cam-2; do
    pw="$(pw_of BROKER_CAMERA_NODE_LOGINS $n)"
    check "camera add: $n has a 40-character alphanumeric password" 1 \
        "$([[ "$pw" =~ ^[A-Za-z0-9]{40}$ ]] && echo 1 || echo 0)"
done
has "camera add: other .env lines kept" "$(cat "$env_file")" "BROKER_CAMERA_PASSWORD=shortpw1234"
has "camera add: BIND_ADDRESS kept" "$(cat "$env_file")" "BIND_ADDRESS=172.20.100.150"
check "camera add: no RF key created" "" "$(value BROKER_RF_NODE_LOGINS)"

# Re-running keeps the passwords.
cam1="$(pw_of BROKER_CAMERA_NODE_LOGINS cam-1)"; cam2="$(pw_of BROKER_CAMERA_NODE_LOGINS cam-2)"
run "$nl" camera cam-1 cam-2
check "camera keep: exit" 0 "$st"
check "camera keep: message" "unchanged: every node has a login" "$out"
check "camera keep: cam-1 password kept" "$cam1" "$(pw_of BROKER_CAMERA_NODE_LOGINS cam-1)"
check "camera keep: cam-2 password kept" "$cam2" "$(pw_of BROKER_CAMERA_NODE_LOGINS cam-2)"

# A new node next to the existing ones keeps them.
run "$nl" camera cam-3
check "camera add one more: message" "added broker logins: cam-node-cam-3" "$out"
check "camera add one more: cam-1 kept" "$cam1" "$(pw_of BROKER_CAMERA_NODE_LOGINS cam-1)"
check "camera add one more: three entries" 3 "$(wc -w <<<"$(value BROKER_CAMERA_NODE_LOGINS)")"
check "camera key appears once" 1 "$(grep -c '^BROKER_CAMERA_NODE_LOGINS=' "$env_file")"

# RF logins, through the wrapper and through the new script: both kinds coexist.
run "$rfnl" wbt-1
check "rf wrapper add: exit" 0 "$st"
check "rf wrapper add: message" "added broker logins: rf-node-wbt-1" "$out"
run "$nl" rf wbt-2
check "rf add via node-logins: message" "added broker logins: rf-node-wbt-2" "$out"
run "$nl" rf wbt-1 wbt-2
check "rf re-run: unchanged" "unchanged: every node has a login" "$out"
check "rf and camera coexist: camera password kept" "$cam1" "$(pw_of BROKER_CAMERA_NODE_LOGINS cam-1)"
check "rf key appears once" 1 "$(grep -c '^BROKER_RF_NODE_LOGINS=' "$env_file")"
check "rf key has no camera entries" "" "$(pw_of BROKER_RF_NODE_LOGINS cam-1)"
check "camera key has no rf entries" "" "$(pw_of BROKER_CAMERA_NODE_LOGINS wbt-1)"
check "rf key has two entries" 2 "$(wc -w <<<"$(value BROKER_RF_NODE_LOGINS)")"

# --get prints "<node> <password>" lines and changes nothing.
before="$(cksum <"$env_file")"
run "$nl" camera --get cam-2
check "--get camera: exit" 0 "$st"
check "--get camera: line" "cam-2 $cam2" "$out"
run "$nl" camera --get cam-1 cam-2
check "--get camera: two lines" "cam-1 $cam1
cam-2 $cam2" "$out"
run "$rfnl" --get wbt-1
check "--get rf wrapper: line" "wbt-1 $(pw_of BROKER_RF_NODE_LOGINS wbt-1)" "$out"
run "$nl" camera --get cam-9
check "--get missing: exit 1" 1 "$st"
has "--get missing: message" "$err" "no broker login for cam-9"
run "$nl" rf --get cam-1
check "--get camera login under the rf kind: exit 1" 1 "$st"
check "--get leaves .env alone" "$before" "$(cksum <"$env_file")"

# Bad input is refused before anything is written.
before="$(cksum <"$env_file")"
run "$nl" camera Cam_1
check "bad name: exit 2" 2 "$st"
has "bad name: message" "$err" "node names are lowercase letters, digits and dashes: Cam_1"
run "$nl" camera cam-7 Cam_1
check "bad name among good ones: exit 2" 2 "$st"
run "$nl" camera -x cam-7
check "unknown option: exit 2" 2 "$st"
has "unknown option: message" "$err" "unknown option -x"
run "$nl" camera
check "no node names: exit 2" 2 "$st"
has "no node names: usage" "$out" "Gives each WBT (RF) node or camera node"
run "$nl" bogus cam-1
check "bad kind: exit 2" 2 "$st"
has "bad kind: message" "$err" "first argument must be rf or camera, not 'bogus'"
run "$nl"
check "no arguments: exit 2" 2 "$st"
run "$nl" --get cam-1
check "--get without a kind: exit 2" 2 "$st"
check "bad input leaves .env alone" "$before" "$(cksum <"$env_file")"

# No .env at all.
mv "$env_file" "$work/env.hold"
run "$nl" camera cam-8
check "missing .env: exit 1" 1 "$st"
has "missing .env: message" "$err" "no $env_file: run detection/central/make-env.sh first"
mv "$work/env.hold" "$env_file"

# Restart: only when a login was added and the broker is running.
set_docker running
run "$nl" camera cam-4
check "restart: exit" 0 "$st"
has "restart: broker re-created" "$out" "broker re-created with the new logins"
has "restart: compose up -d broker called" "$(cat "$DOCKER_LOG")" "compose up -d broker"

set_docker running
run "$nl" camera --no-restart cam-5
check "--no-restart: exit" 0 "$st"
check "--no-restart: message" "added broker logins: cam-node-cam-5" "$out"
lacks "--no-restart: no compose up" "$(cat "$DOCKER_LOG")" "compose up"

set_docker running
run "$nl" camera cam-1 cam-5
check "nothing added while the broker runs: no compose up" "unchanged: every node has a login" "$out"
lacks "nothing added: no compose up" "$(cat "$DOCKER_LOG")" "compose up"

set_docker broken
run "$nl" camera cam-6
check "docker failing: login still added, exit 0" 0 "$st"
check "docker failing: message" "added broker logins: cam-node-cam-6" "$out"
lacks "docker failing: no compose up" "$(cat "$DOCKER_LOG")" "compose up"
set_docker stopped

# The wrapper, from another directory.
cd / || exit 1
run "$rfnl"
check "rf wrapper, no nodes: exit 2" 2 "$st"
has "rf wrapper, no nodes: usage" "$out" "Gives each WBT (RF) node or camera node"
run "$rfnl" --no-restart wbt-3
check "rf wrapper from / : message" "added broker logins: rf-node-wbt-3" "$out"
cd "$work" || exit 1

echo "== node-secrets.sh"
ns="$root/detection/central/node-secrets.sh"
rfns="$root/detection/central/rf-node-secrets.sh"
rf_env="$root/detection/rf-subsystem/central/.env"
mkdir -p "$(dirname "$rf_env")"
set_docker stopped

# camera: shared kit login first, then one line per node; a new node gets a login on the way.
run "$ns" camera cam-1 cam-9
check "secrets camera: exit" 0 "$st"
check "secrets camera: output" "relay_user relay
relay_password shortpw1234
cam-1 $cam1
cam-9 $(pw_of BROKER_CAMERA_NODE_LOGINS cam-9)" "$out"
has "secrets camera: login made, reported on stderr" "$err" "added broker logins: cam-node-cam-9"
check "secrets camera: no console token made" 0 "$([ -e "$rf_env" ] && echo 1 || echo 0)"
run "$ns" camera cam-1
check "secrets camera re-run: same password" "cam-1 $cam1" "$(sed -n '$p' <<<"$out")"

# camera without the shared password: refused, nothing printed.
cp "$env_file" "$work/env.bak"
sed -i '/^BROKER_CAMERA_PASSWORD=/d' "$env_file"
run "$ns" camera cam-1
check "secrets camera, no BROKER_CAMERA_PASSWORD: exit 1" 1 "$st"
check "secrets camera, no BROKER_CAMERA_PASSWORD: no output" "" "$out"
has "secrets camera, no BROKER_CAMERA_PASSWORD: says why" "$err" "BROKER_CAMERA_PASSWORD is not set"
cp "$work/env.bak" "$env_file"
sed -i '/^BROKER_CAMERA_USER=/d' "$env_file"
run "$ns" camera cam-1
check "secrets camera, no BROKER_CAMERA_USER: defaults to relay" "relay_user relay" "$(sed -n 1p <<<"$out")"
cp "$work/env.bak" "$env_file"

# rf: a token is made once and kept, then one line per node; through the old name too.
run "$rfns" wbt-1
check "secrets rf wrapper: exit" 0 "$st"
token="$(sed -n 's/^CONSOLE_NODE_TOKEN=//p' "$rf_env")"
check "secrets rf: 48-character token stored" 1 "$([[ "$token" =~ ^[a-z0-9]{48}$ ]] && echo 1 || echo 0)"
check "secrets rf: token env is 0600" 600 "$(stat -c %a "$rf_env")"
check "secrets rf: output" "token $token
wbt-1 $(pw_of BROKER_RF_NODE_LOGINS wbt-1)" "$out"
has "secrets rf: new token reported" "$err" "new console heartbeat token"
run "$ns" rf wbt-1
check "secrets rf re-run: token kept" "token $token" "$(sed -n 1p <<<"$out")"
lacks "secrets rf re-run: no new token" "$err" "new console heartbeat token"
lacks "secrets rf: no camera lines" "$out" "relay_"

# One file for both kinds, as the fleet playbook reads it.
{ "$ns" rf wbt-1; "$ns" camera cam-1; } >"$work/node-secrets" 2>/dev/null
check "secrets file: five lines, keys unique" "5 5" "$(wc -l <"$work/node-secrets" | tr -d ' ') $(cut -d' ' -f1 "$work/node-secrets" | sort -u | wc -l | tr -d ' ')"

run "$ns"
check "secrets, no kind: exit 2" 2 "$st"
has "secrets, no kind: usage" "$err" "without SSH"
run "$ns" camera
check "secrets camera, no nodes: exit 2" 2 "$st"
run "$rfns"
check "secrets rf wrapper, no nodes: exit 2" 2 "$st"
run "$ns" camera Bad_Name
check "secrets camera, bad name: exit 2" 2 "$st"
check "secrets camera, bad name: no output" "" "$out"

echo "== entrypoint.sh: login and ACL generation (mosquitto stubbed, paths redirected into a temp dir)"
ep="$work/entrypoint"
mkdir -p "$ep/bin" "$ep/broker" "$ep/auth" "$ep/data"
cp "$detection/central-broker/aclfile" "$ep/broker/aclfile"
sed -e "s|/run/mosquitto-auth|$ep/auth|g" -e "s|/broker/|$ep/broker/|g" \
    -e "s|/tls/|$ep/tls/|g" -e "s|/mosquitto/data|$ep/data|g" \
    "$detection/central-broker/entrypoint.sh" >"$ep/entrypoint.sh"
cat >"$ep/bin/mosquitto_passwd" <<'EOS'
#!/bin/sh
# Stub: the user is the last argument; the password comes on stdin, twice.
for a; do user="$a"; done
read -r pw1; read -r pw2
printf '%s %s %s\n' "$user" "$pw1" "$([ "$pw1" = "$pw2" ] && echo same || echo differ)" >>"$PASSWD_LOG"
printf '%s\n' "$*" >>"$ARGV_LOG"
EOS
cat >"$ep/bin/mosquitto" <<'EOS'
#!/bin/sh
env >"$MOSQ_ENV"
EOS
printf '#!/bin/sh\nexit 0\n' >"$ep/bin/chown"
chmod +x "$ep/bin/"*

# ep_run [VAR=value ...]: runs the entrypoint with a clean environment plus the given variables.
ep_run() {
    rm -rf "$ep/auth" && mkdir -p "$ep/auth"
    : >"$ep/passwd.log"; : >"$ep/argv.log"; rm -f "$ep/env.log"
    env -i PATH="$ep/bin:$PATH" PASSWD_LOG="$ep/passwd.log" ARGV_LOG="$ep/argv.log" MOSQ_ENV="$ep/env.log" \
        BROKER_FUSION_USER=fusion BROKER_FUSION_PASSWORD=fusionpw0000000 \
        BROKER_PHYSICAL_USER=physical-subsystem BROKER_PHYSICAL_PASSWORD=physicalpw00000 \
        BROKER_RF_USER=rf-console BROKER_RF_PASSWORD=rfconsolepw0000 \
        BROKER_CAMERA_USER=relay BROKER_CAMERA_PASSWORD=camerarelaypw00 \
        "$@" sh "$ep/entrypoint.sh" >"$ep/out" 2>"$ep/err"
    st=$?
}
# block <user line>: that user's ACL lines, up to the next blank line or user line
block() { awk -v u="$1" '$0 == u { p = 1; print; next } p && (/^$/ || /^user /) { exit } p { print }' "$ep/auth/aclfile"; }

ep_run BROKER_RF_NODE_LOGINS="wbt-1:rfpw0000000001 wbt-2:rfpw0000000002" \
    BROKER_CAMERA_NODE_LOGINS="cam-1:campw00000001 cam-2:campw00000002"
check "both kinds: exit" 0 "$st"
has "both kinds: rf-node-wbt-1 password" "$(cat "$ep/passwd.log")" "rf-node-wbt-1 rfpw0000000001 same"
has "both kinds: rf-node-wbt-2 password" "$(cat "$ep/passwd.log")" "rf-node-wbt-2 rfpw0000000002 same"
has "both kinds: cam-node-cam-1 password" "$(cat "$ep/passwd.log")" "cam-node-cam-1 campw00000001 same"
has "both kinds: cam-node-cam-2 password" "$(cat "$ep/passwd.log")" "cam-node-cam-2 campw00000002 same"
lacks "passwords never on the command line" "$(cat "$ep/argv.log")" "rfpw0000000001"
lacks "passwords never on the command line" "$(cat "$ep/argv.log")" "campw00000001"
check "rf-node-wbt-1 ACL" "user rf-node-wbt-1
topic write detection/rf/nodes/wbt-1/#" "$(block "user rf-node-wbt-1")"
check "cam-node-cam-1 ACL" "user cam-node-cam-1
topic write detection/camera/#
topic read detection/rf/cues/camera" "$(block "user cam-node-cam-1")"
check "cam-node-cam-2 ACL" "user cam-node-cam-2
topic write detection/camera/#
topic read detection/rf/cues/camera" "$(block "user cam-node-cam-2")"
check "shared relay ACL unchanged" "user relay
topic write detection/camera/#
topic read detection/rf/cues/camera" "$(block "user relay")"
lacks "rf-analytics block dropped when unset" "$(cat "$ep/auth/aclfile")" "user rf-analytics"
check "mosquitto starts with no broker variables" 0 "$(grep -c '^BROKER_' "$ep/env.log")"
lacks "passwords not on the entrypoint's stdout or stderr" "$(cat "$ep/out" "$ep/err")" "campw00000001"

ep_run BROKER_RF_NODE_LOGINS="wbt-1:rfpw0000000001"
check "camera variable unset: exit" 0 "$st"
lacks "camera variable unset: no cam-node login" "$(cat "$ep/passwd.log")" "cam-node-"
lacks "camera variable unset: no cam-node ACL" "$(cat "$ep/auth/aclfile")" "user cam-node-"
has "camera variable unset: rf login still made" "$(cat "$ep/passwd.log")" "rf-node-wbt-1"

ep_run
check "no per-node logins: exit" 0 "$st"
lacks "no per-node logins: no node logins" "$(cat "$ep/passwd.log")" "node-"

ep_run BROKER_CAMERA_NODE_LOGINS="Cam_1:campw00000001"
check "bad camera name: exit 1" 1 "$st"
has "bad camera name: message" "$(cat "$ep/err")" "BROKER_CAMERA_NODE_LOGINS: bad node name 'Cam_1'"
ep_run BROKER_CAMERA_NODE_LOGINS="cam-1"
check "camera entry without a password: exit 1" 1 "$st"
has "camera entry without a password: message" "$(cat "$ep/err")" "BROKER_CAMERA_NODE_LOGINS: cam-1 has no password"
ep_run BROKER_RF_NODE_LOGINS="-x:rfpw0000000001"
check "bad rf name: exit 1" 1 "$st"
has "bad rf name: message" "$(cat "$ep/err")" "BROKER_RF_NODE_LOGINS: bad node name '-x'"

# The camera relay API's optional login: present with its read-only ACL when set, dropped when unset.
ep_run BROKER_RELAY_API_USER=relay-api BROKER_RELAY_API_PASSWORD=relayapipw00000
check "relay-api: exit" 0 "$st"
has "relay-api: password" "$(cat "$ep/passwd.log")" "relay-api relayapipw00000 same"
check "relay-api: read-only ACL on the three camera topics" "user relay-api
topic read detection/camera/detections
topic read detection/camera/cue-acks
topic read detection/camera/status/nodes" "$(block "user relay-api")"
lacks "relay-api: rf-analytics block still dropped" "$(cat "$ep/auth/aclfile")" "@RF_ANALYTICS_USER@"
check "relay-api: no broker variables reach mosquitto" 0 "$(grep -c '^BROKER_' "$ep/env.log")"
ep_run
lacks "relay-api unset: block dropped" "$(grep -v '^#' "$ep/auth/aclfile")" "RELAY_API_USER"
lacks "relay-api unset: no camera-topic read lines" "$(cat "$ep/auth/aclfile")" "topic read detection/camera/detections"
ep_run BROKER_RF_ANALYTICS_USER=rf-analytics BROKER_RF_ANALYTICS_PASSWORD=analyticspw0000 \
    BROKER_RELAY_API_USER=relay-api BROKER_RELAY_API_PASSWORD=relayapipw00000
check "both optional logins: rf-analytics ACL kept" "user rf-analytics
topic write detection/rf/#" "$(block "user rf-analytics")"
has "both optional logins: relay-api ACL kept" "$(block "user relay-api")" "topic read detection/camera/cue-acks"
lacks "both optional logins: no marker left" "$(grep -v '^#' "$ep/auth/aclfile")" "@"
ep_run BROKER_RELAY_API_USER=relay-api
check "relay-api without a password: exit 1" 1 "$st"
has "relay-api without a password: message" "$(cat "$ep/err")" "BROKER_RELAY_API_USER is set without BROKER_RELAY_API_PASSWORD"

echo "== make-env.sh: root .env, camera relay settings and fleet node logins"
me="$work/me"
mkdir -p "$me/detection/central" "$me/detection/rf-subsystem/node/fleet/ansible"
cp "$detection/central/make-env.sh" "$detection/central/node-logins.sh" "$detection/central/node-secrets.sh" "$me/detection/central/"
cp "$detection/rf-subsystem/node/fleet/ansible/inventory.yml" "$me/detection/rf-subsystem/node/fleet/ansible/"
me_env="$me/.env"
mval() { sed -n "s/^$1=//p" "$me_env" | tail -n1; }
set_docker stopped
run env -u RELAY_SITE_SUBNET -u RF_NODES -u CAMERA_NODES "$me/detection/central/make-env.sh" 172.20.100.150
check "make-env: exit" 0 "$st"
has "make-env: RF node logins from the inventory" "$out" "added broker logins: rf-node-wbt-1 rf-node-wbt-2 rf-node-wbt-3 rf-node-wbt-4"
has "make-env: camera node logins from the inventory" "$out" "added broker logins: cam-node-cam-1 cam-node-cam-2 cam-node-cam-3 cam-node-cam-4"
lacks "make-env: no broker restart" "$(cat "$DOCKER_LOG")" "compose up"
check "make-env: .env is 0600" 600 "$(stat -c %a "$me_env")"
check "make-env: kit login user" relay "$(mval BROKER_CAMERA_USER)"
check "make-env: kit password, 16 characters" 1 "$([[ "$(mval BROKER_CAMERA_PASSWORD)" =~ ^[A-Za-z0-9]{16}$ ]] && echo 1 || echo 0)"
check "make-env: relay-api login" relay-api "$(mval BROKER_RELAY_API_USER)"
check "make-env: site subnet from the address" 172.20.100.0/24 "$(mval RELAY_SITE_SUBNET)"
check "make-env: relay kit login is a reference" '${BROKER_CAMERA_USER} ${BROKER_CAMERA_PASSWORD}' "$(mval RELAY_BROKER_USER) $(mval RELAY_BROKER_PASSWORD)"
check "make-env: relay API broker login is a reference" '${BROKER_RELAY_API_USER} ${BROKER_RELAY_API_PASSWORD}' "$(mval RELAY_API_BROKER_USER) $(mval RELAY_API_BROKER_PASSWORD)"
check "make-env: relay addresses follow BIND_ADDRESS" '${BIND_ADDRESS} ${BIND_ADDRESS} ${BIND_ADDRESS} ${BIND_ADDRESS}' \
    "$(mval RELAY_CENTRAL_IP) $(mval RELAY_BIND_ADDRESS) $(mval RELAY_WEBRTC_HOSTS) $(mval RELAY_BROKER_HOST)"
check "make-env: relay API off the console's 8080" 8091 "$(mval RELAY_API_PORT)"
check "make-env: viewer login" viewer "$(mval RELAY_READ_USER)"
for k in RELAY_API_TOKEN RELAY_READ_PASSWORD RELAY_MEDIAMTX_API_PASSWORD RELAY_MQTT_CLIENT_ID BROKER_RELAY_API_PASSWORD; do
    check "make-env: $k generated" 1 "$(v="$(mval "$k")"; [[ "$v" =~ ^[A-Za-z0-9-]{16,}$ ]] && echo 1 || echo 0)"
done
cp "$me_env" "$work/me.env.1"
run env -u RELAY_SITE_SUBNET -u RF_NODES -u CAMERA_NODES "$me/detection/central/make-env.sh" 172.20.100.150
check "make-env re-run: exit" 0 "$st"
check "make-env re-run: .env unchanged" "$(cksum <"$work/me.env.1")" "$(cksum <"$me_env")"
has "make-env re-run: logins kept" "$out" "unchanged: every node has a login"
run env CAMERA_NODES="cam-5" RF_NODES="" RELAY_SITE_SUBNET=10.9.9.0/24 "$me/detection/central/make-env.sh" 172.20.100.150
has "make-env: CAMERA_NODES adds a node" "$out" "added broker logins: cam-node-cam-5"
check "make-env: an existing site subnet is kept" 172.20.100.0/24 "$(mval RELAY_SITE_SUBNET)"
run "$me/detection/central/node-secrets.sh" camera cam-1
check "node-secrets camera right after make-env: exit" 0 "$st"
check "node-secrets camera right after make-env: kit login" "relay_user relay
relay_password $(mval BROKER_CAMERA_PASSWORD)" "$(sed -n 1,2p <<<"$out")"
lacks "node-secrets camera right after make-env: no new login" "$err" "added"

echo "== $pass passed, $fail failed"
[ "$fail" = 0 ]
