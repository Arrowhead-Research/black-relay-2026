#!/bin/sh
# Sets the Kismet web admin credentials from the environment,
# then runs Kismet in the foreground.
set -eu

HTTPD_CONF=/root/.kismet/kismet_httpd.conf
mkdir -p /root/.kismet /var/lib/kismet/logs

# KISMET_ADMIN_PASSWORD_FILE: the compose stack's generated password (bootstrap.sh).
if [ -z "${KISMET_ADMIN_PASSWORD:-}" ] && [ -n "${KISMET_ADMIN_PASSWORD_FILE:-}" ]; then
    KISMET_ADMIN_PASSWORD="$(cat "$KISMET_ADMIN_PASSWORD_FILE")"
fi

# The configured login is written on every start, so Kismet always matches what rf-analytics and the
# console use (a password changed in the web UI is replaced; set KISMET_ADMIN_PASSWORD to choose it).
if [ -n "${KISMET_ADMIN_USER:-}" ] && [ -n "${KISMET_ADMIN_PASSWORD:-}" ]; then
    umask 077
    printf 'httpd_username=%s\nhttpd_password=%s\n' \
        "$KISMET_ADMIN_USER" "$KISMET_ADMIN_PASSWORD" > "$HTTPD_CONF"
    echo "entrypoint: Kismet admin login set for '$KISMET_ADMIN_USER'"
elif [ ! -f "$HTTPD_CONF" ]; then
    echo "entrypoint: KISMET_ADMIN_USER/KISMET_ADMIN_PASSWORD not set;" \
         "set credentials in the web UI on first login" >&2
fi

exec kismet --no-line-wrap "$@"
