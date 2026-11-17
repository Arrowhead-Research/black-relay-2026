#!/usr/bin/env bash
# Generates the bundle's secrets into .env, mode 0600. Prints no secret, and refuses to overwrite
# an existing file without --force.
set -euo pipefail

# Fleet default for the two passwords people type: the kits' broker login and the video viewer's.
# Change either one later with ./change-passwords.sh broker or ./change-passwords.sh viewer.
readonly DEFAULT_PASSWORD='BlackRelay2026'

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
output="$here/.env"
force=0

usage() {
    echo "usage: $0 [--force] [--output PATH]"
}

while (($#)); do
    case "$1" in
    --force) force=1 ;;
    --output)
        [[ $# -ge 2 ]] || { usage >&2; exit 2; }
        output="$2"
        shift
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
    esac
    shift
done

if [[ -e "$output" && "$force" != 1 ]]; then
    echo "$output already exists; not overwriting it. Use --force to make new secrets." >&2
    exit 1
fi

random() {
    local value
    # Letters and digits only: nothing an env file, a URL or a node's portal treats specially.
    value="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c "$1" || true)"
    if ((${#value} != $1)); then
        echo "could not read random bytes" >&2
        return 1
    fi
    printf '%s' "$value"
}

# Each in its own assignment, as set -e does not see a failure inside an argument's $(...).
api_token="$(random 48)"
api_broker_password="$(random 40)"
client_id="relay-api-$(random 32)"
control_password="$(random 32)"

umask 077
temporary="$(mktemp "$output.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
# With --force, every setting but the random secrets stays as it was in the existing file. That
# includes the two passwords people type: every kit and viewer holds them, so only
# ./change-passwords.sh changes them. A missing or empty one gets the default.
existing=""
[[ -e "$output" ]] && existing="$(cat "$output")"
while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
    RELAY_API_TOKEN=* | RELAY_API_BROKER_PASSWORD=* | RELAY_MQTT_CLIENT_ID=* | \
        RELAY_MEDIAMTX_API_PASSWORD=*) ;;
    RELAY_BROKER_PASSWORD=* | RELAY_READ_PASSWORD=*)
        kept="$(grep -m1 "^${line%%=*}=" <<<"$existing" || true)"
        if [[ -n "${kept#*=}" && "${kept#*=}" != "''" ]]; then
            printf '%s\n' "$kept"
            continue
        fi
        ;;
    *=*)
        kept="$(grep -m1 "^${line%%=*}=" <<<"$existing" || true)"
        if [[ -n "$kept" ]]; then
            printf '%s\n' "$kept"
            continue
        fi
        ;;
    esac
    case "$line" in
    RELAY_API_TOKEN=*) printf "RELAY_API_TOKEN='%s'\n" "$api_token" ;;
    RELAY_BROKER_USER=*) printf "RELAY_BROKER_USER='relay'\n" ;;
    RELAY_BROKER_PASSWORD=*) printf "RELAY_BROKER_PASSWORD='%s'\n" "$DEFAULT_PASSWORD" ;;
    RELAY_API_BROKER_USER=*) printf "RELAY_API_BROKER_USER='relay-api'\n" ;;
    RELAY_API_BROKER_PASSWORD=*) printf "RELAY_API_BROKER_PASSWORD='%s'\n" "$api_broker_password" ;;
    RELAY_MQTT_CLIENT_ID=*) printf "RELAY_MQTT_CLIENT_ID='%s'\n" "$client_id" ;;
    RELAY_READ_USER=*) printf "RELAY_READ_USER='viewer'\n" ;;
    RELAY_READ_PASSWORD=*) printf "RELAY_READ_PASSWORD='%s'\n" "$DEFAULT_PASSWORD" ;;
    RELAY_MEDIAMTX_API_PASSWORD=*) printf "RELAY_MEDIAMTX_API_PASSWORD='%s'\n" "$control_password" ;;
    *) printf '%s\n' "$line" ;;
    esac
done <"$here/.env.example" >"$temporary"
chmod 600 "$temporary"
mv -f "$temporary" "$output"
trap - EXIT

echo "Wrote $output, mode 0600."
if [[ "$force" == 1 ]]; then
    echo "Random secrets replaced. The broker and viewer passwords and all other settings are kept,"
    echo "so kits need no change. API clients need the new RELAY_API_TOKEN."
else
    echo "The broker and viewer passwords are the fleet default in README.md. To change one, run"
    echo "./change-passwords.sh broker or ./change-passwords.sh viewer."
fi
if ! grep -qE "^RELAY_SITE_SUBNET=.+" "$output"; then
    echo "Next: set RELAY_SITE_SUBNET and RELAY_WEBRTC_HOSTS in it, then see README.md."
fi
