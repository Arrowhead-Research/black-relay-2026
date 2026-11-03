#!/bin/sh
# Seeds the Kismet web admin credentials from the environment on first start,
# then runs Kismet in the foreground.
set -eu

HTTPD_CONF=/root/.kismet/kismet_httpd.conf
mkdir -p /root/.kismet /var/lib/kismet/logs

if [ ! -f "$HTTPD_CONF" ]; then
    if [ -n "${KISMET_ADMIN_USER:-}" ] && [ -n "${KISMET_ADMIN_PASSWORD:-}" ]; then
        umask 077
        printf 'httpd_username=%s\nhttpd_password=%s\n' \
            "$KISMET_ADMIN_USER" "$KISMET_ADMIN_PASSWORD" > "$HTTPD_CONF"
        echo "entrypoint: seeded Kismet admin credentials for '$KISMET_ADMIN_USER'"
    else
        echo "entrypoint: KISMET_ADMIN_USER/KISMET_ADMIN_PASSWORD not set;" \
             "set credentials in the web UI on first login" >&2
    fi
fi

exec kismet --no-line-wrap "$@"
