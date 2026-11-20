#!/usr/bin/env bash
# Flash and provision the RF (WBT) Raspberry Pi nodes. Run from an admin Mac or Linux machine.
#
#   ./fleet.sh flash <node> <device>    write Raspberry Pi OS Lite to an SD card / USB disk and seed its
#                                       first boot (hostname, SSH key, Wi-Fi). Asks before erasing.
#   ./fleet.sh seed <node> <boot dir>   only seed a card that is already flashed (e.g. with Raspberry Pi
#                                       Imager, customisation off): <boot dir> is its mounted bootfs
#   ./fleet.sh provision [node|all]     install the node stack and point it at the central server (Ansible)
#   ./fleet.sh ping [node|all]          check Ansible can reach the nodes
#
# Settings come from fleet.env next to this script (copy fleet.env.example; it is gitignored). The SSH
# key pair named by SSH_KEY is created on first use; only its public half goes on the cards.
# Node names are wbt-1 ... wbt-8 (the Kismet source prefix and the hostname; see ansible/inventory.yml).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0"; exit 2; }

FLEET_ENV="${FLEET_ENV:-$here/fleet.env}"
# shellcheck source=/dev/null
[ ! -f "$FLEET_ENV" ] || . "$FLEET_ENV"
ADMIN_USER="${ADMIN_USER:-pi}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/blackrelay_fleet}"
TIMEZONE="${TIMEZONE:-America/Chicago}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
WIFI_SSID="${WIFI_SSID:-}"
IMAGE_URL="${IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
CACHE_DIR="${CACHE_DIR:-$HOME/.cache/blackrelay-fleet}"

valid_node() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "node names are lowercase letters, digits and dashes: $1"; }
json() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

ensure_key() {
  [ -f "$SSH_KEY.pub" ] && return
  [ ! -f "$SSH_KEY" ] || die "$SSH_KEY exists without $SSH_KEY.pub (ssh-keygen -y -f $SSH_KEY > $SSH_KEY.pub)"
  echo "Creating the fleet SSH key $SSH_KEY (keep the private half on this machine only)."
  mkdir -p "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -C "blackrelay-fleet" -f "$SSH_KEY"
}

# The WPA PSK (64 hex digits) derived from the passphrase, so the card never holds the passphrase itself.
wifi_psk() {
  local pass="${WIFI_PASSWORD:-}"
  if [ -z "$pass" ]; then
    [ -t 0 ] || die "set WIFI_PASSWORD or run interactively"
    read -r -s -p "Wi-Fi passphrase for $WIFI_SSID: " pass </dev/tty; echo >&2
  fi
  [ "${#pass}" -ge 8 ] && [ "${#pass}" -le 63 ] || die "a WPA passphrase is 8 to 63 characters"
  printf '%s' "$pass" | python3 -c 'import hashlib,sys; print(hashlib.pbkdf2_hmac("sha1", sys.stdin.buffer.read(), sys.argv[1].encode(), 4096, 32).hex())' "$WIFI_SSID"
}

# seed <node> <dir>: the cloud-init NoCloud files Raspberry Pi OS (trixie and later) reads from bootfs.
cmd_seed() {
  local node="${1:-}" dir="${2:-}" pubkey
  [ -n "$node" ] && [ -n "$dir" ] || usage
  valid_node "$node"
  [ -d "$dir" ] || die "not a directory: $dir"
  [ -f "$dir/config.txt" ] || [ -n "${FLEET_SEED_ANY_DIR:-}" ] || die "$dir does not look like a Raspberry Pi boot partition (no config.txt)"
  ensure_key
  pubkey="$(cat "$SSH_KEY.pub")"
  local psk=""
  [ -z "$WIFI_SSID" ] || psk="$(wifi_psk)"

  printf 'instance-id: %s\nlocal-hostname: %s\n' "$node" "$node" >"$dir/meta-data"

  cat >"$dir/user-data" <<EOF
#cloud-config
hostname: $node
manage_etc_hosts: true
timezone: $(json "$TIMEZONE")
users:
  - name: $(json "$ADMIN_USER")
    groups: [adm, dialout, plugdev, netdev, video, gpio, i2c, spi, sudo]
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - $(json "$pubkey")
ssh_pwauth: false
disable_root: true
runcmd:
  - [raspi-config, nonint, do_wifi_country, $(json "$WIFI_COUNTRY")]
  - [systemctl, enable, --now, ssh]
EOF

  if [ -n "$WIFI_SSID" ]; then
    cat >"$dir/network-config" <<EOF
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      optional: true
  wifis:
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: $(json "$WIFI_COUNTRY")
      access-points:
        $(json "$WIFI_SSID"):
          password: $(json "$psk")
EOF
  else
    printf 'network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n      optional: true\n' >"$dir/network-config"
  fi
  : >"$dir/ssh"   # older images: enable sshd on first boot
  echo "Seeded $dir for $node: user $ADMIN_USER, key $(ssh-keygen -lf "$SSH_KEY.pub" | awk '{print $2}'), ${WIFI_SSID:+Wi-Fi \"$WIFI_SSID\", }Ethernet DHCP."
}

# fetch_image -> path of the verified, cached .img.xz
fetch_image() {
  local url name sum
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$IMAGE_URL")" || die "cannot reach $IMAGE_URL"
  name="$(basename "$url")"
  [[ "$name" == *.img.xz ]] || die "expected an .img.xz from $IMAGE_URL, got $name"
  mkdir -p "$CACHE_DIR"
  if [ ! -f "$CACHE_DIR/$name" ]; then
    echo "Downloading $name ..." >&2
    curl -fL --progress-bar -o "$CACHE_DIR/$name.part" "$url"
    mv "$CACHE_DIR/$name.part" "$CACHE_DIR/$name"
  fi
  sum="$(curl -fsSL "$url.sha256" | awk '{print $1}')"
  [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || die "no checksum at $url.sha256"
  if command -v sha256sum >/dev/null; then got="$(sha256sum "$CACHE_DIR/$name" | awk '{print $1}')"
  else got="$(shasum -a 256 "$CACHE_DIR/$name" | awk '{print $1}')"; fi
  [ "$got" = "$sum" ] || { rm -f "$CACHE_DIR/$name"; die "checksum mismatch for $name (deleted; run again)"; }
  echo "$CACHE_DIR/$name"
}

confirm_erase() { # confirm_erase <device> <description> <node>
  echo "About to ERASE $1 ($2) and write $3's card." >&2
  [ -n "${FLEET_YES:-}" ] && return
  local answer; read -r -p "Type the node name ($3) to continue: " answer </dev/tty
  [ "$answer" = "$3" ] || die "not confirmed"
}

cmd_flash() {
  local node="${1:-}" dev="${2:-}" image
  [ -n "$node" ] && [ -n "$dev" ] || usage
  valid_node "$node"
  command -v xz >/dev/null || die "xz is not installed"
  if [ -n "$WIFI_SSID" ] && [ -z "${WIFI_PASSWORD:-}" ]; then   # ask now, not after the long write
    read -r -s -p "Wi-Fi passphrase for $WIFI_SSID: " WIFI_PASSWORD </dev/tty; echo >&2; export WIFI_PASSWORD
  fi
  ensure_key
  case "$(uname -s)" in
    Darwin)
      [[ "$dev" =~ ^/dev/disk[0-9]+$ ]] || die "give the whole disk, e.g. /dev/disk4 (diskutil list external)"
      local info; info="$(diskutil info "$dev")" || die "no such disk: $dev"
      grep -Eq 'Device Location: +External|Removable Media: +(Removable|Yes)' <<<"$info" || die "$dev is not an external/removable disk; refusing"
      ! grep -Eq 'Part of Whole: +disk0$' <<<"$info" || die "refusing disk0"
      image="$(fetch_image)"
      confirm_erase "$dev" "$(sed -n 's/^ *Device \/ Media Name: *//p' <<<"$info") $(sed -n 's/^ *Disk Size: *\([^(]*\).*/\1/p' <<<"$info")" "$node"
      diskutil unmountDisk "$dev" >/dev/null
      echo "Writing (sudo) ..." >&2
      xz -dc "$image" | sudo dd of="${dev/disk/rdisk}" bs=4m
      sync; sleep 2
      diskutil mountDisk "$dev" >/dev/null || true
      local boot=""
      for _ in $(seq 1 15); do [ -f /Volumes/bootfs/config.txt ] && { boot=/Volumes/bootfs; break; }; sleep 1; done
      [ -n "$boot" ] || die "image written but /Volumes/bootfs did not mount; mount it and run: $0 seed $node /Volumes/bootfs"
      cmd_seed "$node" "$boot"
      diskutil eject "$dev" >/dev/null
      ;;
    Linux)
      [ -b "$dev" ] || die "not a block device: $dev"
      [ "$(lsblk -dno TYPE "$dev")" = disk ] || die "give the whole disk, e.g. /dev/sdb or /dev/mmcblk0"
      local rm tran; rm="$(lsblk -dno RM "$dev" | tr -d ' ')"; tran="$(lsblk -dno TRAN "$dev" | tr -d ' ')"
      [ "$rm" = 1 ] || [ "$tran" = usb ] || [[ "$dev" == /dev/mmcblk* ]] || die "$dev is not removable/USB/SD; refusing"
      ! lsblk -no MOUNTPOINT "$dev" | grep -Eqx '/|/boot|/boot/efi|/home' || die "$dev holds this system's filesystems; refusing"
      image="$(fetch_image)"
      confirm_erase "$dev" "$(lsblk -dno MODEL,SIZE "$dev" | xargs)" "$node"
      lsblk -lnpo NAME,MOUNTPOINT "$dev" | awk '$2!=""{print $1}' | xargs -r -n1 sudo umount
      echo "Writing (sudo) ..." >&2
      xz -dc "$image" | sudo dd of="$dev" bs=4M conv=fsync status=progress
      sudo partprobe "$dev" 2>/dev/null || sudo blockdev --rereadpt "$dev" || true
      sleep 2
      local part="${dev}1"; [[ "$dev" =~ [0-9]$ ]] && part="${dev}p1"
      local mnt; mnt="$(mktemp -d)"
      sudo mount -o "uid=$(id -u),gid=$(id -g)" "$part" "$mnt"
      ( cmd_seed "$node" "$mnt" ) || { sudo umount "$mnt"; rmdir "$mnt"; exit 1; }
      sync; sudo umount "$mnt"; rmdir "$mnt"
      ;;
    *) die "flash runs on macOS or Linux" ;;
  esac
  echo "Done. Put the card in $node and power it on, then: $0 provision $node"
}

ansible_run() { # ansible_run <playbook|ping> [limit]
  command -v ansible-playbook >/dev/null || die "Ansible is not installed (pipx install ansible-core, or brew install ansible)"
  ensure_key
  local limit="${2:-all}"
  [ "$limit" = all ] || valid_node "$limit"
  cd "$here/ansible"
  export FLEET_SSH_KEY="$SSH_KEY" FLEET_ADMIN_USER="$ADMIN_USER"
  if [ "$1" = ping ]; then
    ansible rf_nodes -m ansible.builtin.ping ${limit:+--limit "$limit"}
  else
    # The central host is always in the play: it holds the console's heartbeat token.
    ansible-playbook site.yml --limit "central,$( [ "$limit" = all ] && echo rf_nodes || echo "$limit")" "${@:3}"
  fi
}

case "${1:-}" in
  flash) shift; cmd_flash "$@" ;;
  seed) shift; cmd_seed "$@" ;;
  provision) shift; ansible_run site "${1:-all}" "${@:2}" ;;
  ping) shift; ansible_run ping "${1:-all}" ;;
  *) usage ;;
esac
