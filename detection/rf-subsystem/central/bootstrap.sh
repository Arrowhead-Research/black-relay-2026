#!/bin/sh
# One-shot first-start setup for the RF central services, so `docker compose up` (from the repo root
# or from here) needs nothing else: generates every RF password once into the rf-secrets volume and
# gives each file to the one container user that reads it. Later starts find them and exit at once.
# A value set in .env (KISMET_ADMIN_PASSWORD, CONSOLE_OPERATOR_PASSWORD, CONSOLE_ADMIN_PASSWORD)
# still wins over the generated file.
#
#   kismet-admin-password       Kismet web/REST admin (user KISMET_ADMIN_USER, default admin)
#   console-operator-password   WBT Console sign-in, operator
#   console-admin-password      WBT Console sign-in, admin
#   analytics-broker-password   rf-analytics' login (rf-analytics) on the central broker
#
# Read one:  docker compose exec rf-console cat /run/rf-secrets/console-admin-password
set -eu
umask 077
cd /run/rf-secrets
gen() { # gen <file> <length>
    if [ ! -s "$1" ]; then
        LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$2" >"$1.tmp"
        mv "$1.tmp" "$1"
        echo "rf-bootstrap: generated $1"
    fi
}
gen kismet-admin-password 24
gen console-operator-password 16
gen console-admin-password 20
gen analytics-broker-password 40
# rf-analytics runs as uid 10020 (analytics/Dockerfile) and reads its broker and Kismet passwords;
# Kismet, the console and the broker read theirs as root.
chown 10020 analytics-broker-password kismet-admin-password
chmod 0400 ./*-password
chmod 0755 .
# rf-analytics' outbox and events (a volume an older root-run image may have created).
chown -R 10020 /run/rf-analytics-data
