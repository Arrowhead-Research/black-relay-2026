#!/usr/bin/env bash
# Push images from this machine's Docker to the central store, so hosts pull them instead of
# loading a tarball. Run on the build machine after detection/deploy/build.sh.
#
#   docker login <server>:5001 -u publisher        once (password from ./server.sh init)
#   ./publish-images.sh <server> [image ...]       default: the sf-dev bundle's images + the store toolbox
#   ./publish-images.sh <server> --bridge          the physical bridge, built for amd64 (see below)
#
# An image can be given as LOCAL=REMOTE to push a local tag under another name, e.g.
#   blackrelay-f2/outbound:amd64=blackrelay-f2/outbound:latest
# so an amd64 build for the server can sit next to your Mac's arm64 :latest without replacing it.
#
# Images are pushed to <server>:5001 (publisher only) and hosts pull the same images from
# <server>:5000 with their own read-only login, e.g. <server>:5000/black-relay/fusion-service:sfdev.
# Images must never contain keys or passwords: those go in the secrets store (secrets.sh).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
server="${1:?usage: $0 <server> [image ...]}"; shift
images=("$@")
if [ "${images[0]:-}" = "--bridge" ]; then
  # Build the four bridge services for amd64 under :amd64 tags (your local :latest stays as it is).
  phys="$here/../physical-subsystem"; images=()
  for s in normalizer iq-remote control4 outbound; do
    docker buildx build --platform linux/amd64 --load -q -t "blackrelay-f2/$s:amd64" -f "$phys/docker/Dockerfile.$s" "$phys" >/dev/null
    images+=("blackrelay-f2/$s:amd64=blackrelay-f2/$s:latest")
  done
elif [ ${#images[@]} -eq 0 ]; then
  # Keep in step with detection/deploy/build.sh (its `docker save` list).
  images=(black-relay/fusion-service:sfdev black-relay/physical-sim:sfdev black-relay/rf-analytics:sfdev
          black-relay/rfsim:sfdev eclipse-mosquitto:2.1.2-alpine camera-relay-netguard:0.1.0
          camera-relay-chrony:4.8 bluenviron/mediamtx:1.21.3 black-relay/central-tools:0.1.0)
  docker image inspect black-relay/central-tools:0.1.0 >/dev/null 2>&1 \
    || docker build -q -t black-relay/central-tools:0.1.0 "$here/tools" >/dev/null
fi
for spec in "${images[@]}"; do
  img="${spec%%=*}"; remote="${spec#*=}"
  docker image inspect "$img" >/dev/null 2>&1 || { echo "missing locally: $img (run build.sh or docker pull it)" >&2; exit 1; }
  docker tag "$img" "$server:5001/$remote"
  docker push -q "$server:5001/$remote"
  docker rmi "$server:5001/$remote" >/dev/null
  echo "pull as: $server:5000/$remote"
done
