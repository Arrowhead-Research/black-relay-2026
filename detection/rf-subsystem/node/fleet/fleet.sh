#!/usr/bin/env bash
# Flash and provision the Raspberry Pi nodes: RF (wbt-N) and camera (cam-N). Run from an admin Mac or Linux machine.
#
#   ./fleet.sh flash <node> <device>    write Raspberry Pi OS Lite to an SD card / USB disk and seed its
#                                       first boot (hostname, SSH key, Wi-Fi). Asks before erasing.
#   ./fleet.sh seed <node> <boot dir>   only seed a card that is already flashed (e.g. with Raspberry Pi
#                                       Imager, customisation off): <boot dir> is its mounted bootfs
#   ./fleet.sh provision [target]       install the node stack and point it at the central server (Ansible)
#   ./fleet.sh ping [target]            check Ansible can reach the nodes
#   ./fleet.sh wifi <target> <ssid>     set (or change) running nodes' backhaul Wi-Fi with Ansible; each
#                                       joins now if in range, else whenever the network appears
#   target: a node (wbt-1, cam-2), rf (the RF nodes), camera (the camera nodes) or all (every node)
#   Camera nodes (names starting cam-) use eth0 for the camera, so their card needs Wi-Fi:
#   set SETUP_SSID or WIFI_SSID before seeding one.
#
# Settings come from fleet.env next to this script (copy fleet.env.example; it is gitignored). The SSH
# key pair named by SSH_KEY is created on first use; only its public half goes on the cards.
# Node names are wbt-1 ... wbt-4 (the Kismet source prefix and the hostname) and cam-1 ... cam-4
# (the hostname, bridge node_id and Frigate camera name); see ansible/inventory.yml.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,14p' "$0"; exit 2; }

FLEET_ENV="${FLEET_ENV:-$here/fleet.env}"
# shellcheck source=/dev/null
[ ! -f "$FLEET_ENV" ] || . "$FLEET_ENV"
ADMIN_USER="${ADMIN_USER:-pi}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/blackrelay_fleet}"
TIMEZONE="${TIMEZONE:-America/Chicago}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
WIFI_SSID="${WIFI_SSID:-}"
SETUP_SSID="${SETUP_SSID:-}"     # staging network the hub hosts; empty: no hub, no rescue hotspot
SETUP_HUB="${SETUP_HUB:-wbt-1}"
IMAGE_URL="${IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
CACHE_DIR="${CACHE_DIR:-$HOME/.cache/blackrelay-fleet}"
KNOWN_HOSTS="${KNOWN_HOSTS:-$HOME/.ssh/blackrelay_fleet_known_hosts}"   # node host keys, by node name

valid_node() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "node names are lowercase letters, digits and dashes: $1"; }
is_camera() { [[ "$1" == cam-* ]]; }
json() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

ensure_key() {
  [ -f "$SSH_KEY.pub" ] && return
  [ ! -f "$SSH_KEY" ] || die "$SSH_KEY exists without $SSH_KEY.pub (ssh-keygen -y -f $SSH_KEY > $SSH_KEY.pub)"
  echo "Creating the fleet SSH key $SSH_KEY (keep the private half on this machine only)."
  mkdir -p "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -C "blackrelay-fleet" -f "$SSH_KEY"
}

# ask_pass <label> <var>: a WPA passphrase from $<var> or the terminal (8 to 63 characters).
ask_pass() {
  local v="${!2:-}"
  if [ -z "$v" ]; then
    [ -t 0 ] || [ -r /dev/tty ] || die "set $2 or run interactively"
    read -r -s -p "$1: " v </dev/tty; echo >&2
  fi
  [ "${#v}" -ge 8 ] && [ "${#v}" -le 63 ] || die "a WPA passphrase is 8 to 63 characters"
  printf -v "$2" '%s' "$v"; export "${2?}"
}
# psk_of <ssid> <passphrase>: the WPA key (64 hex digits), so cards and nodes never hold the passphrase.
psk_of() {
  printf '%s' "$2" | python3 -c 'import hashlib,sys; print(hashlib.pbkdf2_hmac("sha1", sys.stdin.buffer.read(), sys.argv[1].encode(), 4096, 32).hex())' "$1"
}
wifi_psk() { ask_pass "Wi-Fi passphrase for $WIFI_SSID" WIFI_PASSWORD; psk_of "$WIFI_SSID" "$WIFI_PASSWORD"; }

# nm_profile <id> <ssid> <psk> <ap|client> <autoconnect yes|no> <priority>: a NetworkManager keyfile.
# Hotspots: WPA2-CCMP only and PMF off (pmf=1). The Pi's brcmfmac radio advertises PMF in AP mode but
# does not complete it, so iPhones and Macs see the network and then fail to join.
nm_profile() {
  local auto=false; [ "$5" != yes ] || auto=true
  printf '[connection]\nid=%s\ntype=wifi\ninterface-name=wlan0\nautoconnect=%s\nautoconnect-priority=%s\n\n' "$1" "$auto" "$6"
  if [ "$4" = ap ]; then
    printf '[wifi]\nmode=ap\nband=bg\nssid=%s\n\n[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn\npairwise=ccmp\ngroup=ccmp\npmf=1\npsk=%s\n\n[ipv4]\nmethod=shared\naddress1=10.42.0.1/24\n\n[ipv6]\nmethod=disabled\n' "$2" "$3"
  else
    printf '[wifi]\nmode=infrastructure\nssid=%s\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n' "$2" "$3"
  fi
}
# write_file <path> <mode> <content>: one cloud-init write_files entry (base64, so any content is safe).
write_file() {
  printf '  - path: %s\n    permissions: "%s"\n    encoding: b64\n    content: %s\n' "$1" "$2" "$(printf '%s' "$3" | base64 | tr -d '\n')"
}

# seed <node> <dir>: the cloud-init NoCloud files Raspberry Pi OS (trixie and later) reads from bootfs.
cmd_seed() {
  local node="${1:-}" dir="${2:-}" pubkey
  [ -n "$node" ] && [ -n "$dir" ] || usage
  valid_node "$node"
  [ -d "$dir" ] || die "not a directory: $dir"
  [ -f "$dir/config.txt" ] || [ -n "${FLEET_SEED_ANY_DIR:-}" ] || die "$dir does not look like a Raspberry Pi boot partition (no config.txt)"
  # A camera node's eth0 is the camera link: the card must not configure it, so it needs Wi-Fi.
  ! is_camera "$node" || [ -n "$SETUP_SSID" ] || [ -n "$WIFI_SSID" ] || die "$node is a camera node: eth0 is the camera link, so set SETUP_SSID or WIFI_SSID (fleet.env) to give it Wi-Fi"
  ensure_key
  pubkey="$(cat "$SSH_KEY.pub")"
  local psk="" setup_files="" setup_cmds="" diag_files="" diag_cmds=""
  [ -z "$WIFI_SSID" ] || psk="$(wifi_psk)"
  if [ -n "$SETUP_SSID" ]; then
    # Hub: hosts SETUP_SSID while staging. Others: join it, and keep <node>-setup as their rescue hotspot.
    ask_pass "Passphrase for the setup network $SETUP_SSID (also the rescue hotspots')" SETUP_PASSWORD
    local nm=/etc/NetworkManager/system-connections f name dest mode
    if [ "$node" = "$SETUP_HUB" ]; then
      setup_files="$(write_file "$nm/setup-ap.nmconnection" 0600 "$(nm_profile setup-ap "$SETUP_SSID" "$(psk_of "$SETUP_SSID" "$SETUP_PASSWORD")" ap yes -10)")"
    else
      setup_files="$(write_file "$nm/setup-wifi.nmconnection" 0600 "$(nm_profile setup-wifi "$SETUP_SSID" "$(psk_of "$SETUP_SSID" "$SETUP_PASSWORD")" client yes -10)")
$(write_file "$nm/setup-ap.nmconnection" 0600 "$(nm_profile setup-ap "$node-setup" "$(psk_of "$node-setup" "$SETUP_PASSWORD")" ap no -20)")"
    fi
    for f in rf-wifi-fallback:/usr/local/sbin:0755 rf-wifi-fallback.service:/etc/systemd/system:0644 rf-wifi-fallback.timer:/etc/systemd/system:0644; do
      IFS=: read -r name dest mode <<<"$f"
      setup_files="$setup_files
$(write_file "$dest/$name" "$mode" "$(cat "$here/files/$name")")"
    done
    setup_cmds='  - [nmcli, connection, reload]
  - [systemctl, enable, --now, rf-wifi-fallback.timer]'
    [ "$node" != "$SETUP_HUB" ] || setup_cmds="$setup_cmds
  - [nmcli, connection, up, setup-ap]"
  fi
  if is_camera "$node"; then
    # NetworkManager must not make a DHCP profile for eth0 (the camera link, set up by provision).
    setup_files="$setup_files${setup_files:+
}$(write_file /etc/NetworkManager/conf.d/10-black-relay-camera.conf 0644 $'[main]\nno-auto-default=eth0\n')"
  fi
  if [ -n "${FLEET_DIAG:-}" ]; then
    # Wi-Fi snapshots to rf-diag.txt on the boot partition for 6 minutes (read it on the admin machine).
    diag_files="$(write_file /usr/local/sbin/rf-diag 0755 "$(cat "$here/files/rf-diag")")"
    diag_cmds='  - [systemd-run, --collect, --unit=rf-diag, /usr/local/sbin/rf-diag]'
  fi

  # A new instance-id on every seed: cloud-init runs first boot again even on a card that has booted.
  printf 'instance-id: %s\nlocal-hostname: %s\n' "$node-$(date -u +%Y%m%dT%H%M%SZ)" "$node" >"$dir/meta-data"
  # That also gives the node new SSH host keys: forget its old one.
  [ ! -f "$KNOWN_HOSTS" ] || ssh-keygen -R "$node" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true

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
${diag_cmds}
  - [raspi-config, nonint, do_wifi_country, $(json "$WIFI_COUNTRY")]
  - [systemctl, enable, --now, ssh]
${setup_cmds}
EOF
  setup_files="$setup_files${setup_files:+${diag_files:+
}}$diag_files"
  [ -z "$setup_files" ] || printf 'write_files:\n%s\n' "$setup_files" >>"$dir/user-data"

  local eth_cfg='  ethernets:
    eth0:
      dhcp4: true
      optional: true
'
  is_camera "$node" && eth_cfg=""    # eth0 is the camera link: not configured here
  if [ -n "$WIFI_SSID" ]; then
    cat >"$dir/network-config" <<EOF
network:
  version: 2
${eth_cfg}  wifis:
    wlan0:
      dhcp4: true
      optional: true
      regulatory-domain: $(json "$WIFI_COUNTRY")
      access-points:
        $(json "$WIFI_SSID"):
          password: $(json "$psk")
EOF
  else
    printf 'network:\n  version: 2\n%s' "$eth_cfg" >"$dir/network-config"
  fi
  : >"$dir/ssh"   # older images: enable sshd on first boot
  local role=""
  if [ -n "$SETUP_SSID" ]; then
    if [ "$node" = "$SETUP_HUB" ]; then role="hosts setup network \"$SETUP_SSID\" (10.42.0.1), "
    else role="joins setup network \"$SETUP_SSID\", rescue hotspot \"$node-setup\", "; fi
  fi
  echo "Seeded $dir for $node: user $ADMIN_USER, key $(ssh-keygen -lf "$SSH_KEY.pub" | awk '{print $2}'), ${role}${WIFI_SSID:+Wi-Fi \"$WIFI_SSID\", }$(is_camera "$node" && echo "eth0 left for the camera" || echo "Ethernet DHCP")."
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
  # ask now, not after the long write
  [ -z "$WIFI_SSID" ] || ask_pass "Wi-Fi passphrase for $WIFI_SSID" WIFI_PASSWORD
  [ -z "$SETUP_SSID" ] || ask_pass "Passphrase for the setup network $SETUP_SSID (also the rescue hotspots')" SETUP_PASSWORD
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

# target_pattern <node|all|rf|camera>: the Ansible pattern for a target (all is every node, not central).
target_pattern() {
  case "$1" in
    all) echo fleet_nodes ;;
    rf) echo rf_nodes ;;
    camera) echo camera_nodes ;;
    *) valid_node "$1"; echo "$1" ;;
  esac
}

ansible_run() { # ansible_run <site.yml|wifi.yml|ping> [target] [ansible args...]
  command -v ansible-playbook >/dev/null || die "Ansible is not installed (pipx install ansible-core, or brew install ansible)"
  ensure_key
  local pattern; pattern="$(target_pattern "${2:-all}")"
  case "${CENTRAL_SECRETS:-}" in ""|/*) ;; *) CENTRAL_SECRETS="$PWD/$CENTRAL_SECRETS" ;; esac
  cd "$here/ansible"
  export FLEET_SSH_KEY="$SSH_KEY" FLEET_ADMIN_USER="$ADMIN_USER" FLEET_KNOWN_HOSTS="$KNOWN_HOSTS"
  export FLEET_CAMERA_HEF="${CAMERA_HEF:-}"
  if [ "$1" = ping ]; then
    ansible fleet_nodes -m ansible.builtin.ping --limit "$pattern"
  else
    if [ "$1" = site.yml ] && [ -n "${CENTRAL_SECRETS:-}" ]; then
      # No SSH to the central server: the values come from node-secrets.sh's output, run there by hand.
      [ -r "$CENTRAL_SECRETS" ] || die "CENTRAL_SECRETS: cannot read $CENTRAL_SECRETS"
      ansible-playbook "$1" --limit "$pattern" -e "central_secrets_file=$CENTRAL_SECRETS" "${@:3}"
    else
      # The central host is in the play: it makes the broker logins and holds the console's token.
      ansible-playbook "$1" --limit "central,$pattern" "${@:3}"
    fi
  fi
}

# wifi <node|all> <ssid>: ansible/wifi.yml with the derived WPA key in the environment (never argv).
cmd_wifi() {
  local limit="${1:-}" ssid="${2:-}"
  [ -n "$limit" ] && [ -n "$ssid" ] || usage
  [[ "$ssid" != *$'\n'* ]] && [ "${#ssid}" -le 32 ] || die "bad SSID"
  WIFI_SSID="$ssid"
  FLEET_WIFI_PSK="$(wifi_psk)" FLEET_WIFI_SSID="$ssid" FLEET_WIFI_COUNTRY="$WIFI_COUNTRY" \
    ansible_run wifi.yml "$limit" ${SETUP_SSID:+-e "setup_hub=$SETUP_HUB"} "${@:3}"
}

case "${1:-}" in
  flash) shift; cmd_flash "$@" ;;
  seed) shift; cmd_seed "$@" ;;
  provision) shift; ansible_run site.yml "${1:-all}" "${@:2}" ;;
  ping) shift; ansible_run ping "${1:-all}" ;;
  wifi) shift; cmd_wifi "$@" ;;
  *) usage ;;
esac
