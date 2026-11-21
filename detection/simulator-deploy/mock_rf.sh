#!/usr/bin/env bash
# One fake RF event on this stack's broker, in the shape RF analytics publishes.
# Run from this folder, after ./deploy-simulator-api.sh has started the stack.
#
#   ./mock_rf.sh             # an rf.alert: a phone loitering at north-entrance
#   ./mock_rf.sh position    # an rf.position: where two nodes place that phone
#   DRY=1 ./mock_rf.sh       # print topic and JSON, publish nothing
set -euo pipefail
kind="${1:-alert}"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen | tr 'A-Z' 'a-z'; }
phone="0a:ec:f3:7d:6b:94"

case "$kind" in
alert)
    topic=detection/rf/alerts
    payload="$(printf '{"schema":"rf.alert.v0","id":"%s","timestamp":"%s","rule_id":"loiter-exterior","rule_type":"loiter","severity":"medium","emitter":{"id":"%s","type":"ble","randomized_address":true},"location":{"zone":"north-entrance","confidence":1.0},"context":{"dwell_s":605}}' \
        "$(uuid)" "$(now)" "$phone")"
    ;;
position)
    topic=detection/rf/positions
    payload="$(printf '{"schema":"rf.position.v0","id":"%s","timestamp":"%s","site":{"id":"dev-site","name":"dev-site"},"emitter":{"id":"%s","type":"ble","band":"bt","randomized":true},"position":{"x_ft":66.0,"y_ft":18.5,"calibrated":false},"heard_by":["rf-node-01","rf-node-02"]}' \
        "$(uuid)" "$(now)" "$phone")"
    ;;
*)
    echo "usage: $0 [alert|position]" >&2
    exit 2
    ;;
esac

if [[ -n "${DRY:-}" ]]; then
    printf '%s\t%s\n' "$topic" "$payload"
else
    set -a; . ./.env; set +a
    docker compose exec -T broker mosquitto_pub -q 1 \
        -u "$BROKER_RF_USER" -P "$BROKER_RF_PASSWORD" -t "$topic" -m "$payload"
    echo "published $topic"
fi
