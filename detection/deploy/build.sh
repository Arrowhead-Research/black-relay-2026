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
docker pull --platform "$PLATFORM" eclipse-mosquitto:2.1.2-alpine

stage="$here/out/bundle"
rm -rf "$stage"; mkdir -p "$stage/broker" "$stage/rf"
cp "$here/compose.yaml" "$here/fusion.config.yaml" "$here/make-env.sh" "$here/README.md" "$stage/"
cp "$det/central-broker/mosquitto.conf" "$det/central-broker/aclfile" "$det/central-broker/entrypoint.sh" "$stage/broker/"
cp "$det/rf-subsystem/simulator/sites/dev-site.yaml" "$stage/rf/"
chmod +x "$stage/make-env.sh" "$stage/broker/entrypoint.sh"
docker save -o "$stage/images.tar" \
  black-relay/fusion-service:$TAG black-relay/physical-sim:$TAG black-relay/rf-analytics:$TAG black-relay/rfsim:$TAG \
  eclipse-mosquitto:2.1.2-alpine
# COPYFILE_DISABLE: no macOS extended attributes in the archive (they only produce warnings on Linux).
COPYFILE_DISABLE=1 tar -C "$here/out" --no-xattrs -czf "$here/out/sf-dev-bundle.tgz" bundle
ls -lh "$here/out/sf-dev-bundle.tgz" "$stage/images.tar"
