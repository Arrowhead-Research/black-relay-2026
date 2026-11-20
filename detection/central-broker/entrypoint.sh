#!/bin/sh
# Builds the password and ACL files from .env on a tmpfs, then starts mosquitto. Passwords go in
# on stdin so they never appear in a process list. Same approach as the camera relay's broker.
set -eu
umask 077
auth=/run/mosquitto-auth

for user in "$BROKER_FUSION_USER" "$BROKER_PHYSICAL_USER" "$BROKER_RF_USER" \
    "$BROKER_CAMERA_USER"; do
    case "$user" in
    *:* | *\&* | *\|* | *\\* | *[[:space:]]*)
        echo "a broker user cannot hold a colon, &, |, backslash or space" >&2
        exit 1
        ;;
    esac
done

printf '%s\n%s\n' "$BROKER_FUSION_PASSWORD" "$BROKER_FUSION_PASSWORD" |
    mosquitto_passwd -c "$auth/passwd" "$BROKER_FUSION_USER" >/dev/null
printf '%s\n%s\n' "$BROKER_PHYSICAL_PASSWORD" "$BROKER_PHYSICAL_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$BROKER_PHYSICAL_USER" >/dev/null
printf '%s\n%s\n' "$BROKER_RF_PASSWORD" "$BROKER_RF_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$BROKER_RF_USER" >/dev/null
printf '%s\n%s\n' "$BROKER_CAMERA_PASSWORD" "$BROKER_CAMERA_PASSWORD" |
    mosquitto_passwd "$auth/passwd" "$BROKER_CAMERA_USER" >/dev/null
sed -e "s|@FUSION_USER@|$BROKER_FUSION_USER|" \
    -e "s|@PHYSICAL_USER@|$BROKER_PHYSICAL_USER|" \
    -e "s|@RF_USER@|$BROKER_RF_USER|" \
    -e "s|@CAMERA_USER@|$BROKER_CAMERA_USER|" \
    /broker/aclfile >"$auth/aclfile"
mkdir -p "$auth/conf.d"
# TLS listener on 8883 when a certificate is mounted (read-only) at /tls. Copied so that only the
# mosquitto user can read the key; the source can stay private to the store's owner.
if [ -r /tls/server.crt ] && [ -r /tls/server.key ]; then
    cp /tls/server.crt "$auth/tls.crt"
    cp /tls/server.key "$auth/tls.key"
    printf 'listener 8883\ncertfile %s\nkeyfile %s\ntls_version tlsv1.2\n' \
        "$auth/tls.crt" "$auth/tls.key" >"$auth/conf.d/tls.conf"
fi
chown -R mosquitto:mosquitto "$auth" /mosquitto/data
unset BROKER_FUSION_USER BROKER_FUSION_PASSWORD BROKER_PHYSICAL_USER BROKER_PHYSICAL_PASSWORD \
    BROKER_RF_USER BROKER_RF_PASSWORD BROKER_CAMERA_USER BROKER_CAMERA_PASSWORD

exec mosquitto -c /broker/mosquitto.conf
