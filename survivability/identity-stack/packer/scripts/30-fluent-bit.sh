#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

if [[ -z "${FLUENT_BIT_PACKAGE_VERSION:-}" || "${FLUENT_BIT_PACKAGE_VERSION}" == REPLACE_WITH_* ]]; then
  printf 'missing exact package pin: FLUENT_BIT_PACKAGE_VERSION\n' >&2
  exit 1
fi

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://packages.fluentbit.io/fluentbit.key -o /tmp/fluentbit.asc
actual_fingerprint="$(gpg --show-keys --with-colons /tmp/fluentbit.asc | awk -F: '/^fpr:/ {print $10; exit}')"
expected_fingerprint="C3C0A28534B9293EAF51FABD9F9DDC083888C1CD"
if [[ "${actual_fingerprint}" != "${expected_fingerprint}" ]]; then
  printf 'unexpected Fluent Bit archive key fingerprint: %s\n' "${actual_fingerprint}" >&2
  exit 1
fi
gpg --dearmor -o /etc/apt/keyrings/fluentbit.gpg /tmp/fluentbit.asc
chmod a+r /etc/apt/keyrings/fluentbit.gpg

# shellcheck disable=SC1091
. /etc/os-release
printf 'deb [signed-by=/etc/apt/keyrings/fluentbit.gpg] https://packages.fluentbit.io/debian/%s %s main\n' \
  "${VERSION_CODENAME}" "${VERSION_CODENAME}" >/etc/apt/sources.list.d/fluent-bit.list

apt-get update
apt-get install -y --no-install-recommends "fluent-bit=${FLUENT_BIT_PACKAGE_VERSION}"
apt-mark hold fluent-bit

systemctl disable --now fluent-bit || true
