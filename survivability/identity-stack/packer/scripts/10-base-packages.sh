#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get -y dist-upgrade
apt-get install -y --no-install-recommends \
  apt-transport-https \
  ca-certificates \
  curl \
  gnupg \
  jq \
  lsb-release \
  needrestart \
  openssh-server \
  unattended-upgrades \
  vim-tiny

systemctl enable ssh
systemctl enable systemd-timesyncd
systemctl restart systemd-timesyncd
