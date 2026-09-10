#!/usr/bin/env bash
set -euo pipefail

required_commands=(
  age
  age-keygen
  ansible
  ansible-lint
  docker
  jq
  packer
  pi
  pi-web-server
  rg
  shellcheck
  sops
  tofu
  yamllint
  yq
)

for command_name in "${required_commands[@]}"; do
  command -v "${command_name}" >/dev/null
done

test "$(id -u)" -ne 0
test -w /workspace
test -z "${SSH_AUTH_SOCK:-}"
test ! -S /var/run/docker.sock
test ! -e /home/pi/.config/sops/age/keys.txt
test ! -e /run/secrets/sops-age-key

printf '%s\n' \
  "user=$(id -un) uid=$(id -u)" \
  "workspace=/workspace writable=yes" \
  "pi=$(pi --version)" \
  "ansible=$(ansible --version | sed -n '1p')" \
  "ansible-lint=$(ansible-lint --version)" \
  "packer=$(packer version | sed -n '1p')" \
  "opentofu=$(tofu version | sed -n '1p')" \
  "sops=$(sops --version 2>/dev/null | sed -n '1p')" \
  "age=$(age --version)" \
  "docker=$(docker --version)" \
  "compose=$(docker compose version)" \
  "ssh-agent=absent" \
  "age-private-key=absent" \
  "docker-socket=absent" \
  "credential isolation checks passed"
