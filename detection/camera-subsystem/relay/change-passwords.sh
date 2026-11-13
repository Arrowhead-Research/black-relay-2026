#!/usr/bin/env bash
# Changes one of the two passwords people type, in .env, then runs "docker compose up -d" so the
# bundle uses it. "broker" is the login every kit holds. "viewer" is the login for watching video.
# The password is read from the terminal, twice, with no echo. It never goes into an argument, a
# log or a trace, and a bad or mismatched entry changes nothing.
#
# RELAY_ENV_FILE names another env file, for tests. Compose then gets it with --env-file.
set +x
set -euo pipefail
export LC_ALL=C

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${RELAY_ENV_FILE:-$here/.env}"

usage() {
    echo "usage: $0 broker|viewer"
    echo "  broker  the MQTT login every camera kit holds"
    echo "  viewer  the login for watching video over RTSP or WebRTC"
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
case "$1" in
broker) key=RELAY_BROKER_PASSWORD ;;
viewer) key=RELAY_READ_PASSWORD ;;
-h | --help) usage; exit 0 ;;
*) usage >&2; exit 2 ;;
esac
who="$1"

if [[ ! -f "$env_file" ]]; then
    echo "$env_file does not exist. Run ./setup-credentials.sh first." >&2
    exit 1
fi

# Exactly one line to replace, or nothing is touched.
count=0
while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$key="* ]] && count=$((count + 1))
done <"$env_file"
if ((count != 1)); then
    echo "$env_file has $count $key lines, not one. Nothing changed." >&2
    exit 1
fi

# Builtins only from here on, so that the password reaches no other process.
printf 'New %s password: ' "$who" >&2
IFS= read -r -s first || { echo >&2; echo "No password given. Nothing changed." >&2; exit 1; }
printf '\nAgain: ' >&2
IFS= read -r -s second || second=""
echo >&2

if [[ "$first" != "$second" ]]; then
    echo "The two entries differ. Nothing changed." >&2
    exit 1
fi
# The value is single-quoted in .env and typed into a kit's web portal, which refuses the same
# characters. A backtick is refused there too.
if ((${#first} < 8 || ${#first} > 63)); then
    echo "The password must be 8 to 63 characters. Nothing changed." >&2
    exit 1
fi
if [[ "$first" =~ [^[:graph:]] || "$first" == *[\'\"\\\$\`]* ]]; then
    echo "The password must be printable ASCII, with no space, quote, backslash, \$ or backtick." >&2
    echo "Nothing changed." >&2
    exit 1
fi
# MediaMTX reads a password with one of these prefixes as a hash, not as the password.
if [[ "$who" == viewer && ("$first" == sha256:* || "$first" == argon2:*) ]]; then
    echo "The viewer password cannot start with sha256: or argon2:. Nothing changed." >&2
    exit 1
fi

# Every other line is copied byte for byte, including a last line with no newline.
umask 077
temporary="$(mktemp "$env_file.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
while true; do
    ended=1
    IFS= read -r line || ended=0
    ((ended)) || [[ -n "$line" ]] || break
    if [[ "$line" == "$key="* ]]; then
        printf "%s='%s'" "$key" "$first"
    else
        printf '%s' "$line"
    fi
    ((ended)) || break
    printf '\n'
done <"$env_file" >"$temporary"
unset first second
chmod 600 "$temporary"
chown --reference="$env_file" "$temporary" 2>/dev/null || true
mv -f "$temporary" "$env_file"
trap - EXIT
echo "Changed the $who password in $env_file, mode 0600."

echo "Running docker compose up -d, so the bundle uses it."
compose=(docker compose)
[[ -n "${RELAY_ENV_FILE:-}" ]] && compose+=(--env-file "$env_file")
if ! (cd "$here" && "${compose[@]}" up -d); then
    echo "docker compose up -d failed. The new password is in .env, but the bundle does not use it" >&2
    echo "yet. Fix the error, then run docker compose up -d in $here." >&2
    exit 1
fi

if [[ "$who" == broker ]]; then
    echo
    echo "Every kit must now get the same password, or it stops reporting and its video stops."
    echo "On each kit, run:"
    echo "  sudo /opt/black-relay/ops/change-passwords.sh broker"
    echo "and type the same new password there."
else
    echo "Give the new password to everyone who watches video."
fi
