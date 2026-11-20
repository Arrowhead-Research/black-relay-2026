#!/usr/bin/env bash
# Builds the four images for the target host's architecture, pulls mosquitto, and writes
# out/sf-dev-bundle.tgz: compose.yaml, configs, make-env.sh and images.tar. Run from anywhere.
#   PLATFORM=linux/amd64 ./build.sh      (default; use linux/arm64 for an arm64 host)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
det="$(cd "$here/.." && pwd)"
PLATFORM="${PLATFORM:-linux/amd64}"
TAG=sfdev
build() { docker buildx build --platform "$PLATFORM" --load "$@"; }

build -t black-relay/fusion-service:$TAG -f "$det/fusion-service/Dockerfile" "$det"
build -t black-relay/physical-sim:$TAG  -f "$here/physical-sim/Dockerfile" "$det"
build -t black-relay/rf-analytics:$TAG  "$det/rf-subsystem/analytics"
build -t black-relay/rfsim:$TAG         -f "$here/rfsim/Dockerfile" "$det/rf-subsystem/simulator"
# The camera video service (its compose names these images; loaded, no build is needed there).
build -t camera-relay-netguard:0.1.0    "$det/camera-relay-service/netguard"
build -t camera-relay-chrony:4.8        "$det/camera-relay-service/chrony"
docker pull --platform "$PLATFORM" eclipse-mosquitto:2.1.2-alpine
docker pull --platform "$PLATFORM" bluenviron/mediamtx:1.21.3

stage="$here/out/bundle"
rm -rf "$stage"; mkdir -p "$stage/broker" "$stage/rf"
cp "$here/compose.yaml" "$here/fusion.config.yaml" "$here/make-env.sh" "$here/README.md" "$stage/"
cp "$det/central-broker/mosquitto.conf" "$det/central-broker/aclfile" "$det/central-broker/entrypoint.sh" "$stage/broker/"
cp "$det/rf-subsystem/simulator/sites/dev-site.yaml" "$stage/rf/"
mkdir -p "$stage/camera-relay-service"
cp -R "$det/camera-relay-service/compose.yaml" "$det/camera-relay-service/mediamtx.yml" "$det/camera-relay-service/render.sh" \
  "$det/camera-relay-service/kits.example" "$det/camera-relay-service/.env.example" "$det/camera-relay-service/README.md" \
  "$det/camera-relay-service/netguard" "$det/camera-relay-service/chrony" "$stage/camera-relay-service/"
chmod +x "$stage/make-env.sh" "$stage/broker/entrypoint.sh" "$stage/camera-relay-service/render.sh"
docker save -o "$stage/images.tar" \
  black-relay/fusion-service:$TAG black-relay/physical-sim:$TAG black-relay/rf-analytics:$TAG black-relay/rfsim:$TAG \
  eclipse-mosquitto:2.1.2-alpine camera-relay-netguard:0.1.0 camera-relay-chrony:4.8 bluenviron/mediamtx:1.21.3
# COPYFILE_DISABLE: no macOS extended attributes in the archive (they only produce warnings on Linux).
COPYFILE_DISABLE=1 tar -C "$here/out" --no-xattrs -czf "$here/out/sf-dev-bundle.tgz" bundle
ls -lh "$here/out/sf-dev-bundle.tgz" "$stage/images.tar"
