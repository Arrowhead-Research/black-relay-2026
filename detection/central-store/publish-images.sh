#!/usr/bin/env bash
# Push images from this machine's Docker to the central store, so hosts pull them instead of
# loading a tarball. Run on the build machine after detection/deploy/build.sh.
#
#   docker login <server>:5001 -u publisher        once (password from ./server.sh init)
#   ./publish-images.sh <server> [image ...]       default: the sf-dev bundle's images + the store toolbox
#
# Images are pushed to <server>:5001 (publisher only) and hosts pull the same images from
# <server>:5000 with their own read-only login, e.g. <server>:5000/black-relay/fusion-service:sfdev.
# Images must never contain keys or passwords: those go in the secrets store (secrets.sh).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
server="${1:?usage: $0 <server> [image ...]}"; shift
images=("$@")
if [ ${#images[@]} -eq 0 ]; then
  # Keep in step with detection/deploy/build.sh (its `docker save` list).
  images=(black-relay/fusion-service:sfdev black-relay/physical-sim:sfdev black-relay/rf-analytics:sfdev
          black-relay/rfsim:sfdev eclipse-mosquitto:2.1.2-alpine camera-relay-netguard:0.1.0
          camera-relay-chrony:4.8 bluenviron/mediamtx:1.21.3 black-relay/central-tools:0.1.0)
  docker image inspect black-relay/central-tools:0.1.0 >/dev/null 2>&1 \
    || docker build -q -t black-relay/central-tools:0.1.0 "$here/tools" >/dev/null
fi
for img in "${images[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "missing locally: $img (run build.sh or docker pull it)" >&2; exit 1; }
  docker tag "$img" "$server:5001/$img"
  docker push -q "$server:5001/$img"
  docker rmi "$server:5001/$img" >/dev/null
  echo "pull as: $server:5000/$img"
done
