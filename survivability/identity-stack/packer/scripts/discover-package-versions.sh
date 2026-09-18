#!/usr/bin/env bash
# Run only as root on a disposable Debian 13 discovery server.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

log() {
  printf '[package-discovery] %s\n' "$*" >&2
}

if command -v cloud-init >/dev/null 2>&1; then
  log 'Waiting for cloud-init to finish...'
  cloud-init status --wait >&2
fi
log 'Updating Debian package metadata...'
apt-get update >/dev/null
log 'Installing repository verification tools...'
apt-get install -y --no-install-recommends ca-certificates curl gnupg >/dev/null
install -m 0755 -d /etc/apt/keyrings

log 'Downloading and verifying the Docker repository key...'
curl -fsSL https://download.docker.com/linux/debian/gpg -o /tmp/docker.asc
docker_fingerprint="$(gpg --show-keys --with-colons /tmp/docker.asc | awk -F: '/^fpr:/ {print $10; exit}')"
if [[ "${docker_fingerprint}" != "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" ]]; then
  printf 'unexpected Docker archive key fingerprint: %s\n' "${docker_fingerprint}" >&2
  exit 1
fi
gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.asc
chmod a+r /etc/apt/keyrings/docker.gpg

# shellcheck disable=SC1091
. /etc/os-release
architecture="$(dpkg --print-architecture)"
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian %s stable\n' \
  "${architecture}" "${VERSION_CODENAME}" >/etc/apt/sources.list.d/docker.list
log 'Updating metadata from the verified Docker repository...'
apt-get update >/dev/null

candidate_version() {
  local package="$1"
  local version
  version="$(apt-cache madison "${package}" | awk 'NR == 1 {print $3}')"
  if [[ -z "${version}" ]]; then
    printf 'no candidate version found for %s\n' "${package}" >&2
    exit 1
  fi
  printf '%s' "${version}"
}

log 'Resolving exact package candidates...'
cat <<EOF
snapshot_epoch = "$(date -u +%Y%m%d%H%M)"
docker_package_version = "$(candidate_version docker-ce)"
docker_cli_package_version = "$(candidate_version docker-ce-cli)"
containerd_package_version = "$(candidate_version containerd.io)"
docker_buildx_package_version = "$(candidate_version docker-buildx-plugin)"
docker_compose_package_version = "$(candidate_version docker-compose-plugin)"
EOF
