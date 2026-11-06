#!/usr/bin/env bash
# Runs ops/install-node.sh on a real Debian Trixie with systemd as PID 1, in a throwaway container
# with its own network namespace. Not part of pytest: it needs Docker and the internet, and takes
# some minutes. Uses its own container and image names, touches no other, and always removes both.
#
# What this cannot show is in provision/ops/field-run-kit1.md, part A: no Pi, no arm64, no Hailo, no
# WiFi, no real Frigate image, and a container's root is not a real host's root.
# shellcheck disable=SC2016  # Commands in single quotes run in the container.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
name="camera-install-node-test-$$"
image="$name:image"
good_sha=$(printf model | sha256sum | cut -d' ' -f1)
failures=0

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker rmi -f "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT

on() { docker exec "$name" "$@"; }
sh_on() { docker exec "$name" bash -c "$1"; }
ok() { printf 'ok   - %s\n' "$1"; }
bad() { printf 'FAIL - %s\n' "$1"; failures=$((failures + 1)); }
check() {
    local what=$1
    shift
    if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi
}
matches() { grep -q -- "$1" <<<"$2"; }
installer() {
    docker exec -e BLACK_RELAY_HEF_SHA256="$good_sha" "$name" \
        /opt/black-relay/src/cameralink/ops/install-node.sh "$@"
}

docker build -q -t "$image" - >/dev/null <<'DOCKERFILE'
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus \
    network-manager dnsmasq-base nftables gpiod python3-venv mosquitto mosquitto-clients \
    systemd-timesyncd avahi-daemon iw rsync curl docker.io docker-cli openssh-server kmod iproute2 \
    ca-certificates >/dev/null
# Nested Docker is not what is under test, and nothing here may wait on it.
RUN systemctl disable docker.service docker.socket containerd.service >/dev/null 2>&1
# Docker's own address is the way out for pip. The test uses a dummy link for the wired profile.
RUN printf '[keyfile]\nunmanaged-devices=interface-name:eth0\n[main]\nno-auto-default=*\n' \
    > /etc/NetworkManager/conf.d/test.conf
CMD ["/sbin/init"]
DOCKERFILE

docker run -d --name "$name" --hostname "$name" --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock "$image" >/dev/null

for _ in $(seq 1 60); do
    state=$(on systemctl is-system-running 2>/dev/null || true)
    if [[ $state == running || $state == degraded ]]; then break; fi
    sleep 1
done
ok "systemd is PID 1 ($state)"

on install -d /opt/black-relay/src
tar -C "$repo" --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
    -c bridge cameralink provision | docker exec -i "$name" tar -C /opt/black-relay/src -x
sh_on 'printf model > /tmp/yolov6n.hef; printf wrong > /tmp/wrong.hef'

printf '\n--- a wrong model stops it, and it names the step\n'
if output=$(installer --hef /tmp/wrong.hef 2>&1); then
    bad "a wrong hash must fail"
else
    check "wrong hash fails at the model step" matches 'INSTALL FAILED at step 2' "$output"
fi

printf '\n--- first run, NetworkManager stopped, so the offline path removes the keyfile\n'
sh_on "printf '[connection]\nid=Wired connection 1\ntype=ethernet\ninterface-name=dummy0\n[ipv4]\nmethod=disabled\n[ipv6]\nmethod=disabled\n' > '/etc/NetworkManager/system-connections/Wired connection 1.nmconnection'; chmod 600 /etc/NetworkManager/system-connections/*"
on systemctl stop NetworkManager
status=0
installer --hef /tmp/yolov6n.hef >/tmp/install-first.$$ 2>&1 || status=$?
tail -n 22 /tmp/install-first.$$
check "first run says NOT READY, as no driver or image exists here (exit $status)" test "$status" = 3
rm -f /tmp/install-first.$$
check "the offline path removed the keyfile" sh_on "! ls '/etc/NetworkManager/system-connections/Wired connection 1.nmconnection'"
output=$(installer 2>&1 || true)
check "systemd-analyze verify passes on systemd 257 with all units" matches 'systemd-analyze verify: passed' "$output"

printf '\n--- real package installs\n'
check "camera-link runs from its venv" on /opt/black-relay/cameralink/bin/camera-link --help
check "camera-provision runs from its venv" on /opt/black-relay/provision/bin/camera-provision run --help
check "camerabridge runs from its venv" on /opt/black-relay/bridge/bin/camerabridge run --help
check "the Frigate template is in the package" on /opt/black-relay/cameralink/bin/python -c \
    'from cameralink.frigate_config import _template; assert "go2rtc" in _template().template'
check "reolink_aio loads on demand" on /opt/black-relay/cameralink/bin/python -c 'import reolink_aio'

printf '\n--- nothing started, frigate not enabled\n'
for unit in camera-link camera-link-dhcp camera-provision frigate camerabridge; do
    check "$unit is not running" sh_on "! systemctl is-active --quiet $unit"
done
check "firewall is not loaded" sh_on '! nft list table inet black_relay'
for unit in camera-link camera-link-dhcp camera-provision nftables docker mosquitto; do
    check "$unit is enabled" on systemctl is-enabled "$unit.service"
done
check "frigate is not enabled" sh_on '[ "$(systemctl is-enabled frigate.service)" = static ]'
check "wait-online is masked" sh_on '[ "$(systemctl is-enabled NetworkManager-wait-online.service)" = masked ]'

printf '\n--- real services read the installed files\n'
check "literal backslash file" sh_on "ls -- '/etc/systemd/system/run-frigate\\x2dcache.mount'"
check "the mount unit starts under its own name" on systemctl start 'run-frigate\x2dcache.mount'
check "a 256 MB tmpfs, mode 0700" sh_on '[ "$(findmnt -no FSTYPE /run/frigate-cache)" = tmpfs ] && [ "$(stat -c %a /run/frigate-cache)" = 700 ] && findmnt -no OPTIONS /run/frigate-cache | grep -q size=262144k'
check "camera-link requires that mount" sh_on "systemctl show -p RequiresMountsFor --value camera-link | grep -q /run/frigate-cache"
check "dockerd accepts daemon.json" on dockerd --validate --config-file /etc/docker/daemon.json
on systemctl start NetworkManager
sleep 2
on nmcli connection reload
check "NetworkManager parses the camera profile" sh_on "nmcli -t -f NAME connection show | grep -qx black-relay-camera"
check "the camera profile is manual, no default route" sh_on "[ \"\$(nmcli -g ipv4.method,ipv4.never-default connection show black-relay-camera | tr '\n' ' ')\" = 'manual yes ' ]"
check "journald config lands" sh_on "systemd-analyze cat-config systemd/journald.conf | grep -q '^Storage=persistent'"
on systemctl restart systemd-journald
check "the journal is on disk after a restart" sh_on 'find /var/log/journal -name "*.journal" | grep -q .'

printf '\n--- real nmcli deletes the real profile, on a link with no SSH\n'
on ip link add dummy0 type dummy
sh_on "printf '[connection]\nid=Wired connection 1\ntype=ethernet\ninterface-name=dummy0\n[ethernet]\n[ipv4]\nmethod=disabled\n[ipv6]\nmethod=disabled\n' > '/etc/NetworkManager/system-connections/Wired connection 1.nmconnection'; chmod 600 /etc/NetworkManager/system-connections/*"
on nmcli connection reload
check "the profile is there before" sh_on "nmcli -t -f NAME connection show | grep -qx 'Wired connection 1'"
installer >/dev/null 2>&1 || true
check "the installer deleted it" sh_on "! nmcli -t -f NAME connection show | grep -qx 'Wired connection 1'"

printf '\n--- a saved broker survives, and a damaged one is refused\n'
sh_on "printf 'add element inet black_relay central_hosts { 192.168.1.22 }\n' > /etc/black-relay/central-hosts.nft"
output=$(installer 2>&1 || true)
check "a re-run changes no file" matches 'Files changed: 0' "$output"
check "the summary names the saved address" matches 'only from 192.168.1.22' "$output"
check "the saved broker is still there" sh_on "grep -q 192.168.1.22 /etc/black-relay/central-hosts.nft"
on nft -f /etc/nftables.conf
check "the real rules load in this namespace, with the address" sh_on "nft list set inet black_relay central_hosts | grep -q 192.168.1.22"
on nft delete table inet black_relay
sh_on "cp /etc/black-relay/central-hosts.nft /tmp/saved.nft; echo 'this is not nft' > /etc/black-relay/central-hosts.nft"
output=$(installer 2>&1 || true)
check "a damaged central-hosts.nft fails loudly" matches 'the firewall rules do not parse' "$output"
sh_on "cp /tmp/saved.nft /etc/black-relay/central-hosts.nft"

printf '\n--- --start refuses with no firewall, then starts with one\n'
output=$(installer --start 2>&1 || true)
check "--start refuses with no firewall" matches 'firewall table is not live' "$output"
check "camera-link did not start" sh_on '! systemctl is-active --quiet camera-link'
on nft -f /etc/nftables.conf
installer --start >/dev/null 2>&1 || true
sleep 8
for unit in camera-link camera-link-dhcp camera-provision mosquitto; do
    printf '     %s: %s, NRestarts %s\n' "$unit" "$(on systemctl is-active "$unit" || true)" \
        "$(on systemctl show -p NRestarts --value "$unit" || true)"
done
sh_on "cat /run/camera-link/state.json 2>/dev/null || echo 'no state file'" | sed 's/^/     state.json: /'
sh_on "journalctl -u camera-link --no-pager -n 8 2>/dev/null" | sed 's/^/     /'

printf '\n--- reach-check.sh, under a container root, so this proves the script and little else\n'
on systemctl start docker.service || printf '     nested dockerd did not start\n'
sleep 3
on /opt/black-relay/src/cameralink/ops/reach-check.sh | sed 's/^/     /' || true

printf '\n--- a real SSH session on an address of eth0 keeps the wired profile until a reboot\n'
sh_on "printf '[connection]\nid=Wired connection 1\ntype=ethernet\ninterface-name=dummy0\n[ethernet]\n[ipv4]\nmethod=disabled\n[ipv6]\nmethod=disabled\n' > '/etc/NetworkManager/system-connections/Wired connection 1.nmconnection'; chmod 600 /etc/NetworkManager/system-connections/*"
on nmcli connection reload
sh_on 'mkdir -p -m 700 /root/.ssh; ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519; cp /root/.ssh/id_ed25519.pub /root/.ssh/authorized_keys; systemctl start ssh.service'
address=$(on sh -c "ip -o -4 addr show dev eth0 | awk '{print \$4}' | cut -d/ -f1")
docker exec -d "$name" ssh -o StrictHostKeyChecking=no "root@$address" sleep 120
sleep 3
on ss -Htn state established '( sport = :22 )' | sed 's/^/     /'
output=$(installer 2>&1 || true)
check "the installer says it waits for the reboot" matches 'an SSH session is on eth0' "$output"
check "the live profile was not deleted" sh_on "nmcli -t -f NAME connection show | grep -qx 'Wired connection 1'"
check "its file is gone, so a reboot removes it" sh_on "! ls '/etc/NetworkManager/system-connections/Wired connection 1.nmconnection'"
check "the session is still up" sh_on "ss -Htn state established '( sport = :22 )' | grep -q ."

printf '\n--- --purge-tailscale removes a real package\n'
sh_on 'set -e; d=/tmp/ts; mkdir -p $d/DEBIAN $d/usr/bin $d/usr/lib/systemd/system
printf "Package: tailscale\nVersion: 1.0\nArchitecture: all\nMaintainer: t <t@t>\nDescription: stand-in\n" > $d/DEBIAN/control
printf "#!/bin/sh\nexit 0\n" > $d/usr/bin/tailscale; chmod 755 $d/usr/bin/tailscale
printf "[Service]\nExecStart=/bin/sleep 1000\n[Install]\nWantedBy=multi-user.target\n" > $d/usr/lib/systemd/system/tailscaled.service
dpkg-deb -b $d /tmp/ts.deb >/dev/null; dpkg -i /tmp/ts.deb >/dev/null; systemctl daemon-reload; systemctl enable --now tailscaled.service >/dev/null 2>&1
mkdir -p /var/lib/tailscale; touch /var/lib/tailscale/state'
installer --purge-tailscale >/dev/null 2>&1 || true
check "the package is gone" sh_on '! dpkg -s tailscale'
check "its unit is gone" sh_on '! systemctl cat tailscaled.service'
check "its files are gone" sh_on '! ls /var/lib/tailscale'
installer --purge-tailscale >/dev/null 2>&1 || true
check "a second purge is harmless" sh_on '! dpkg -s tailscale'

printf '\n'
if ((failures)); then
    printf '%d check(s) failed\n' "$failures"
    exit 1
fi
printf 'all checks passed\n'
