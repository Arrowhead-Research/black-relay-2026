#!/bin/sh
# Builds the password file from .env on a tmpfs. The password goes in on stdin, so it never
# appears in a process list. Not "mosquitto_passwd -U": that cuts a password at its first colon,
# and a node's portal allows one.
set -eu
umask 077
auth=/run/mosquitto-auth

case "$RELAY_BROKER_USER" in
*:*)
    echo "RELAY_BROKER_USER cannot hold a colon" >&2
    exit 1
    ;;
esac

printf '%s\n%s\n' "$RELAY_BROKER_PASSWORD" "$RELAY_BROKER_PASSWORD" |
    mosquitto_passwd -c "$auth/passwd" "$RELAY_BROKER_USER" >/dev/null
cp /relay/aclfile "$auth/aclfile"
chown -R mosquitto:mosquitto "$auth" /mosquitto/data
unset RELAY_BROKER_USER RELAY_BROKER_PASSWORD

exec mosquitto -c /relay/mosquitto.conf
