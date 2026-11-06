#!/bin/sh
# Builds the password and ACL files from .env on a tmpfs. Passwords go in on stdin, so they never
# appear in a process list. Not "mosquitto_passwd -U": that cuts a password at its first colon,
# and a node's portal allows one.
set -eu
umask 077
auth=/run/mosquitto-auth

for user in "$RELAY_BROKER_USER" "$RELAY_API_BROKER_USER"; do
    case "$user" in
    *:* | *\&* | *\|* | *\\* | *[[:space:]]*)
        echo "a broker user cannot hold a colon, &, |, backslash or space" >&2
        exit 1
        ;;
    esac
done

printf '%s\n%s\n' "$RELAY_BROKER_PASSWORD" "$RELAY_BROKER_PASSWORD" |
    mosquitto_passwd -c "$auth/passwd" "$RELAY_BROKER_USER" >/dev/null
printf '%s\n%s\n' "$RELAY_API_BROKER_PASSWORD" "$RELAY_API_BROKER_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$RELAY_API_BROKER_USER" >/dev/null
sed -e "s|@API_USER@|$RELAY_API_BROKER_USER|" -e "s|@NODE_USER@|$RELAY_BROKER_USER|" \
    /relay/aclfile >"$auth/aclfile"
chown -R mosquitto:mosquitto "$auth" /mosquitto/data
unset RELAY_BROKER_USER RELAY_BROKER_PASSWORD RELAY_API_BROKER_USER RELAY_API_BROKER_PASSWORD

exec mosquitto -c /relay/mosquitto.conf
