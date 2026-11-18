#!/usr/bin/env bash
# Writes mediamtx.generated.yml: mediamtx.yml plus one on-demand RTSP path per kit, pulled from
# the kit with the camera login in .env. MediaMTX dials a kit only while someone is watching and
# hangs up 10 s after the last viewer leaves. Re-run after changing kits or the camera password,
# then: docker compose up -d
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kits="$here/kits"
out="$here/mediamtx.generated.yml"
[[ -f "$here/.env" ]] || { echo "no .env: cp .env.example .env and fill it in" >&2; exit 1; }
[[ -f "$kits" ]] || { echo "no kits file: cp kits.example kits and list the kits" >&2; exit 1; }
set -a; . "$here/.env"; set +a
: "${RELAY_CAMERA_USER:?}" "${RELAY_CAMERA_PASSWORD:?}"
umask 077
{
    cat "$here/mediamtx.yml"
    n=0
    while read -r node_id address _; do
        [[ -z "$node_id" || "$node_id" == \#* ]] && continue
        [[ "$node_id" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "bad node id: $node_id" >&2; exit 1; }
        [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad address for $node_id" >&2; exit 1; }
        printf '  %s:\n    source: rtsp://%s:%s@%s:8554/%s\n    sourceOnDemand: yes\n    sourceOnDemandCloseAfter: 10s\n    rtspTransport: tcp\n' \
            "$node_id" "$RELAY_CAMERA_USER" "$RELAY_CAMERA_PASSWORD" "$address" "$node_id"
        n=$((n + 1))
    done <"$kits"
    [[ $n -gt 0 ]] || printf '  {}\n'
    echo "rendered $n path(s) into $out (0600)" >&2
} >"$out"
