#!/usr/bin/env bash
set -euo pipefail

failures=0
check() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'ok - %s\n' "${description}"
  else
    printf 'not ok - %s\n' "${description}" >&2
    "$@" >&2 || true
    failures=$((failures + 1))
  fi
}

service_is_disabled() {
  local state
  state="$(systemctl is-enabled "$1" 2>/dev/null || true)"
  [[ "${state}" == "disabled" ]] || {
    printf '  expected unit-file state disabled, got: %s\n' "${state:-unknown}"
    return 1
  }
}

service_is_inactive() {
  local state
  state="$(systemctl show --property=ActiveState --value "$1" 2>/dev/null || true)"
  [[ "${state}" == "inactive" ]] || {
    printf '  expected ActiveState=inactive, got: %s\n' "${state:-unknown}"
    return 1
  }
}

check 'Debian 13 is booted' grep -q '^VERSION_ID="13"' /etc/os-release
check 'x86-64 architecture matches the production CPX32' test "$(uname -m)" = "x86_64"
check 'cloud-init completed' cloud-init status --wait
check 'sshd configuration parses' sshd -t
check 'password SSH authentication is disabled' grep -R '^PasswordAuthentication no$' /etc/ssh/sshd_config /etc/ssh/sshd_config.d
check 'Docker is installed' docker --version
check 'Compose plugin is installed' docker compose version
check 'Docker service is enabled' systemctl is-enabled docker
check 'IPv6 is disabled by sysctl config' grep -R '^net.ipv6.conf.all.disable_ipv6 = 1$' /etc/sysctl.d
check 'journald has bounded persistent retention' grep -R '^SystemMaxUse=512M$' /etc/systemd/journald.conf.d
check 'unattended upgrades will not reboot automatically' grep -R 'Automatic-Reboot "false"' /etc/apt/apt.conf.d

if (( failures > 0 )); then
  printf '%s validation check(s) failed\n' "${failures}" >&2
  exit 1
fi
