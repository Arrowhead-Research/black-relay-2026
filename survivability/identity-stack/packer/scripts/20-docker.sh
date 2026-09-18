#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

required_vars=(
  DOCKER_PACKAGE_VERSION
  DOCKER_CLI_PACKAGE_VERSION
  CONTAINERD_PACKAGE_VERSION
  DOCKER_BUILDX_PACKAGE_VERSION
  DOCKER_COMPOSE_PACKAGE_VERSION
)
for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" || "${!var_name}" == REPLACE_WITH_* ]]; then
    printf 'missing exact package pin: %s\n' "${var_name}" >&2
    exit 1
  fi
done

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /tmp/docker.asc
actual_fingerprint="$(gpg --show-keys --with-colons /tmp/docker.asc | awk -F: '/^fpr:/ {print $10; exit}')"
expected_fingerprint="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
if [[ "${actual_fingerprint}" != "${expected_fingerprint}" ]]; then
  printf 'unexpected Docker archive key fingerprint: %s\n' "${actual_fingerprint}" >&2
  exit 1
fi
gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.asc
chmod a+r /etc/apt/keyrings/docker.gpg

# shellcheck disable=SC1091
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian %s stable\n' \
  "$(dpkg --print-architecture)" "${VERSION_CODENAME}" >/etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y --no-install-recommends \
  "containerd.io=${CONTAINERD_PACKAGE_VERSION}" \
  "docker-ce=${DOCKER_PACKAGE_VERSION}" \
  "docker-ce-cli=${DOCKER_CLI_PACKAGE_VERSION}" \
  "docker-buildx-plugin=${DOCKER_BUILDX_PACKAGE_VERSION}" \
  "docker-compose-plugin=${DOCKER_COMPOSE_PACKAGE_VERSION}"

apt-mark hold \
  containerd.io \
  docker-ce \
  docker-ce-cli \
  docker-buildx-plugin \
  docker-compose-plugin

install -d -m 0755 /etc/docker
cat >/etc/docker/daemon.json <<'JSON'
{
  "icc": false,
  "log-driver": "journald",
  "live-restore": true,
  "userland-proxy": false
}
JSON

systemctl enable docker
systemctl enable containerd
