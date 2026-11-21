#!/usr/bin/env bash
# One fake camera kit on this stack's broker: a heartbeat, then one person walking through
# (object_start, object_update, object_end). Same shapes camerabridge publishes from a real kit.
# Run from this folder, after ./deploy-simulator-api.sh has started the stack.
#
#   ./mock-camera.sh               # node id cam-mock01
#   ./mock-camera.sh cam-mock02    # another fake kit
#   DRY=1 ./mock-camera.sh         # print topic and JSON, publish nothing
set -euo pipefail
node="${1:-cam-mock01}"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
uuid() { cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen | tr 'A-Z' 'a-z'; }

if [[ -z "${DRY:-}" ]]; then
    set -a; . ./.env; set +a
fi
pub() {
    if [[ -n "${DRY:-}" ]]; then
        printf '%s\t%s\n' "$1" "$2"
    else
        docker compose exec -T broker mosquitto_pub -q 1 \
            -u "$BROKER_CAMERA_USER" -P "$BROKER_CAMERA_PASSWORD" -t "$1" -m "$2"
        echo "published $1"
    fi
}

heartbeat() {
    printf '{"schema":"camera.node-status.v0","event_id":"%s","timestamp":"%s","node_id":"%s","camera_online":true,"link_online":true,"recording_ok":true,"detector_ok":true,"attributes":{"camera_link_state":"adopted"}}' \
        "$(uuid)" "$(now)" "$node"
}

# detection <event> <object id> <confidence> <x1,y1,x2,y2> <area> <zones as JSON>
detection() {
    printf '{"schema":"camera.detection.v0","event_id":"%s","timestamp":"%s","node_id":"%s","camera_id":"%s","event":"%s","object_id":"%s","label":"person","confidence":%s,"zones":%s,"attributes":{"bbox":[%s],"area_px":%s,"stationary":false,"snapshot_url":"https://%s.local:8971/api/events/%s/snapshot.jpg"}}' \
        "$(uuid)" "$(now)" "$node" "$node" "$1" "$2" "$3" "$6" "$4" "$5" "$node" "$2"
}

# Frigate's track id: start time, a dash, six random characters.
track="$(date +%s).$(printf '%06d' $((RANDOM * 30 % 1000000)))-$(uuid | cut -c1-6)"

pub detection/camera/status/nodes "$(heartbeat)"
pub detection/camera/detections "$(detection object_start "$track" 0.71 "212,140,298,372" 19952 '[]')"
sleep 2
pub detection/camera/detections "$(detection object_update "$track" 0.88 "250,132,348,380" 24304 '["gate"]')"
sleep 2
pub detection/camera/detections "$(detection object_end "$track" 0.91 "301,128,402,384" 25856 '["gate"]')"
