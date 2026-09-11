#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

install -d -m 0755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/10-gold-image-baseline.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding yes
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
sshd -t

install -d -m 0755 /etc/sysctl.d
cat >/etc/sysctl.d/99-survivability-gold.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.tcp_syncookies = 1
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
EOF

install -d -m 0755 /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/10-bounds.conf <<'EOF'
[Journal]
SystemMaxUse=512M
RuntimeMaxUse=128M
MaxRetentionSec=14day
Compress=yes
EOF

cat >/etc/apt/apt.conf.d/51survivability-unattended-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
EOF

systemctl enable unattended-upgrades
