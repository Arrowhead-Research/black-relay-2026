#!/usr/bin/env bash
# Takes a Pi from provision/ops/README.md's package steps to ready: the three Python packages, all
# ops files and units, the model, a persistent journal. Run on the Pi as root, from the copied
# source. See README, "Build the image", step 9. Safe to run again.
#
# Usage: sudo install-node.sh [--hef PATH] [--purge-tailscale] [--start]
#   --hef PATH          where to copy yolov6n.hef from. Needed until a good copy is installed.
#   --purge-tailscale   remove the package, its login and its files, from a node on an old image.
#   --start             start the services at the end. Refuses unless the firewall is already live.
#
# It never reboots, never loads the firewall, never restarts a running service, never installs
# hailo-all, and never replaces a saved central-hosts.nft, so an open SSH session stays up and a
# re-run keeps the broker address. A reboot is what applies the firewall and the network changes.
#
# Exit status: 0 ready; 1 a step failed, and it is named; 2 bad usage; 3 installed, but a check it
# cannot fix itself failed, and each is listed.
#
# BLACK_RELAY_ROOT and BLACK_RELAY_HEF_SHA256 can be overridden for testing.
set -Eeuo pipefail

root=${BLACK_RELAY_ROOT:-}
hef_sha=${BLACK_RELAY_HEF_SHA256:-6a20a3952732c7ea5f3f94a586f004015d3ae7d8f6ec5f7b2fcaa4126237124e}
self=$(readlink -f -- "$0")
src=$(dirname -- "$(dirname -- "$(dirname -- "$self")")")
cameralink_ops=$src/cameralink/ops
provision_ops=$src/provision/ops
unit_dir=$root/etc/systemd/system
nm_dir=$root/etc/NetworkManager/system-connections
central_hosts=$root/etc/black-relay/central-hosts.nft
# systemd's escape of /run/frigate-cache. The backslash is part of the file's real name.
cache_mount='run-frigate\x2dcache.mount'
wired_profile='Wired connection 1'

hef_source=
purge_tailscale=no
start_services=no

step_number=0
step_name='checking arguments'
last_command=
finished=no
changed=()
not_ready=()
notes=()

begin() {
    step_number=$((step_number + 1))
    step_name=$1
    printf '\n== %d. %s\n' "$step_number" "$step_name"
}

say_failed() {
    local status=$?
    if [[ $finished == no ]]; then
        printf '\nINSTALL FAILED at step %d: %s\n' "$step_number" "$step_name" >&2
        printf '  last command: %s\n  exit status: %d\n' "$last_command" "$status" >&2
        printf '  Fix that and run this again: finished steps are safe to repeat.\n' >&2
        exit 1
    fi
}
trap 'last_command=$BASH_COMMAND' ERR
trap say_failed EXIT

die() {
    printf 'install-node: %s\n' "$1" >&2
    last_command="(the check above)"
    exit 1
}

usage() {
    sed -n '/^# Usage:/,/^# *--start/p' "$self" | sed 's/^# \{0,1\}//' >&2
    finished=usage
    exit 2
}

while (($#)); do
    case "$1" in
        --hef)
            [[ $# -ge 2 ]] || usage
            hef_source=$2
            shift
            ;;
        --purge-tailscale) purge_tailscale=yes ;;
        --start) start_services=yes ;;
        *) usage ;;
    esac
    shift
done

# Replaces a file in one rename, so that a power cut leaves the old file or the new one. The staging
# name is hidden and matches no glob or suffix that anything here loads.
put() {
    local source=$1 target=$2 mode=$3 staged
    install -d -m 0755 -- "$(dirname -- "$target")"
    if [[ -e $target ]] && cmp -s -- "$source" "$target" && [[ $(stat -c %a -- "$target") == "${mode#0}" ]]; then
        return 0
    fi
    staged="$(dirname -- "$target")/.$(basename -- "$target").install-tmp"
    install -m "$mode" -- "$source" "$staged"
    mv -f -- "$staged" "$target"
    changed+=("$target")
}

sha256_of() {
    local line
    line=$(sha256sum -- "$1")
    printf '%s' "${line%% *}"
}

# True if an established SSH session arrived on an address held by this interface. If that cannot
# be read, true: the caller is about to do something that would cut one.
ssh_session_on() {
    local addresses locals address
    addresses=$(ip -o addr show dev "$1" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)
    locals=$(ss -Htn state established '( sport = :22 )' | awk '{print $3}' |
        sed -e 's/:22$//' -e 's/^\[//' -e 's/\]$//' -e 's/^::ffff://' -e 's/%.*$//') || return 0
    for address in $addresses; do
        if grep -Fxq -- "$address" <<<"$locals"; then
            return 0
        fi
    done
    return 1
}

wired_keyfiles() {
    local file
    for file in "$nm_dir"/*.nmconnection; do
        if [[ -f $file ]] && grep -Fxq "id=$wired_profile" "$file"; then
            printf '%s\n' "$file"
        fi
    done
}

remove_wired_keyfiles() {
    local files file
    files=$(wired_keyfiles)
    while IFS= read -r file; do
        if [[ -n $file ]]; then
            rm -f -- "$file"
        fi
    done <<<"$files"
}

remove_wired_profile() {
    local running profiles
    running=$(nmcli -t -f RUNNING general 2>/dev/null || true)
    if [[ $running != running ]]; then
        remove_wired_keyfiles
        printf '%s: NetworkManager is not running, so its keyfile was removed, if there was one.\n' "$wired_profile"
        return 0
    fi
    profiles=$(nmcli -t -f NAME connection show)
    if ! grep -Fxq "$wired_profile" <<<"$profiles"; then
        printf '%s: already gone.\n' "$wired_profile"
        return 0
    fi
    if ssh_session_on eth0; then
        # Deleting it now would take eth0 down and cut this session. NetworkManager keeps the
        # profile in memory until it is told to reload, so only a reboot notices the file is gone.
        # A generated profile has no file here, and the camera profile stops it being generated again.
        remove_wired_keyfiles
        notes+=("An SSH session is on eth0, so '$wired_profile' stays live until the reboot, which removes it. Then eth0 is the camera link, with no SSH.")
        printf '%s: an SSH session is on eth0, so this waits for the reboot.\n' "$wired_profile"
        return 0
    fi
    nmcli connection delete "$wired_profile"
}

install_model() {
    local directory=$root/var/lib/frigate/config/model_cache target
    target=$directory/yolov6n.hef
    if [[ -n $hef_source ]]; then
        [[ -f $hef_source ]] || die "the model is not at $hef_source"
        [[ $(sha256_of "$hef_source") == "$hef_sha" ]] ||
            die "yolov6n.hef at $hef_source has the wrong SHA-256. Expected $hef_sha"
    elif [[ ! -f $target ]]; then
        die "no model installed yet. Give --hef PATH, with PATH the file yolov6n.hef"
    fi
    install -d -m 0750 -- "$root/var/lib/frigate" "$root/var/lib/frigate/config" \
        "$root/var/lib/frigate/media" "$directory"
    if [[ -f $target ]] && [[ $(sha256_of "$target") == "$hef_sha" ]]; then
        printf 'yolov6n.hef: already installed, and its SHA-256 matches.\n'
        return 0
    fi
    [[ -n $hef_source ]] || die "the installed yolov6n.hef has the wrong SHA-256. Give --hef PATH"
    put "$hef_source" "$target" 0640
    [[ $(sha256_of "$target") == "$hef_sha" ]] || die "yolov6n.hef was damaged while copying"
    printf 'yolov6n.hef: copied, and its SHA-256 matches.\n'
}

install_package() {
    local name=$1 venv=$root/opt/black-relay/$1
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --disable-pip-version-check "$src/$name"
}

install_files() {
    local file
    for file in camera-link.service camera-link-dhcp.service frigate.service "$cache_mount"; do
        put "$cameralink_ops/$file" "$unit_dir/$file" 0644
    done
    put "$cameralink_ops/daemon.json" "$root/etc/docker/daemon.json" 0644
    put "$cameralink_ops/hailo_pci.conf" "$root/etc/modprobe.d/hailo_pci.conf" 0644
    put "$cameralink_ops/black-relay-camera.nmconnection" "$nm_dir/black-relay-camera.nmconnection" 0600

    for file in camera-provision.service camerabridge.service camerabridge-config.path \
        camerabridge-config-changed.service; do
        put "$provision_ops/$file" "$unit_dir/$file" 0644
    done
    put "$provision_ops/ssh-needs-firewall.conf" "$unit_dir/ssh.service.d/black-relay.conf" 0644
    put "$provision_ops/mosquitto-service-override.conf" "$unit_dir/mosquitto.service.d/black-relay.conf" 0644
    put "$provision_ops/mosquitto-black-relay.conf" "$root/etc/mosquitto/conf.d/black-relay.conf" 0644
    put "$provision_ops/black-relay-captive.conf" \
        "$root/etc/NetworkManager/dnsmasq-shared.d/black-relay-captive.conf" 0644

    for file in README.md black-relay-setup-ap.nmconnection.tmpl central-hosts.nft; do
        put "$provision_ops/$file" "$root/opt/black-relay/ops/$file" 0644
    done
    put "$provision_ops/seed-kit.sh" "$root/opt/black-relay/ops/seed-kit.sh" 0755
}

install_firewall() {
    # Only if absent: the portal saves the broker address here, and a re-run must not erase it.
    if [[ ! -e $central_hosts ]]; then
        put "$provision_ops/central-hosts.nft" "$central_hosts" 0644
    fi
    # Checked first, and against the live central-hosts file, because a file that does not parse
    # stops the firewall at boot and takes SSH and detection with it.
    if ! nft -c -f "$provision_ops/black-relay.nft"; then
        die "the firewall rules do not parse with $central_hosts, so they were not installed. See README, 'If nftables fails at boot'"
    fi
    put "$provision_ops/black-relay.nft" "$root/etc/nftables.conf" 0644
}

install_journal() {
    put "$provision_ops/journald-persistent.conf" "$root/etc/systemd/journald.conf.d/90-black-relay.conf" 0644
    install -d -m 0755 -- "$root/var/log/journal"
    systemd-tmpfiles --create --prefix /var/log/journal
}

purge_tailscale_package() {
    if dpkg -s tailscale >/dev/null 2>&1; then
        # Never logged in on kit 1, so a logout that fails or hangs changes nothing that matters.
        timeout 30 tailscale logout || printf 'tailscale logout did not succeed: carrying on.\n'
        systemctl disable --now tailscaled.service || printf 'tailscaled was not disabled: carrying on.\n'
        DEBIAN_FRONTEND=noninteractive apt-get purge -y tailscale
    fi
    rm -rf -- "$root/var/lib/tailscale" "$root/etc/apt/sources.list.d/tailscale.list" \
        "$root/usr/share/keyrings/tailscale-archive-keyring.gpg" \
        "$root/var/lib/camera-provision/tailscale.key"
    printf 'Tailscale: gone. Also remove the node from the tailnet admin console, if it ever joined.\n'
}

enable_units() {
    systemctl daemon-reload
    # frigate.service is left out on purpose: camera-link starts it once the camera is adopted.
    systemctl enable nftables.service NetworkManager.service mosquitto.service \
        systemd-timesyncd.service docker.service camera-provision.service \
        camerabridge.service camerabridge-config.path camera-link-dhcp.service camera-link.service
    # After the enable, which turns wait-online back on through Also=.
    systemctl mask NetworkManager-wait-online.service
}

check() {
    local what=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf '  ok       %s\n' "$what"
    else
        printf '  MISSING  %s\n' "$what"
        not_ready+=("$what")
    fi
}

driver_is_pinned() {
    [[ $(modinfo -F version hailo_pci) == 4.21.0 ]]
}

hailo_all_absent() {
    ! dpkg -s hailo-all
}

ssh_is_key_only() {
    local settings
    settings=$(sshd -T)
    grep -qx 'passwordauthentication no' <<<"$settings" && grep -qx 'permitrootlogin no' <<<"$settings"
}

frigate_image() {
    grep -Eo 'ghcr\.io/[^ ]+' "$unit_dir/frigate.service"
}

verify_units() {
    local output
    if ! command -v systemd-analyze >/dev/null; then
        printf 'systemd-analyze is not here, so the units were not verified.\n'
        return 0
    fi
    if output=$(systemd-analyze verify --man=no "$unit_dir/camera-link.service" \
        "$unit_dir/camera-link-dhcp.service" "$unit_dir/frigate.service" "$unit_dir/$cache_mount" \
        "$unit_dir/camera-provision.service" "$unit_dir/camerabridge.service" \
        "$unit_dir/camerabridge-config.path" "$unit_dir/camerabridge-config-changed.service" 2>&1); then
        printf 'systemd-analyze verify: passed.\n'
    else
        not_ready+=("systemd-analyze verify")
        printf 'systemd-analyze verify: FAILED.\n'
    fi
    if [[ -n $output ]]; then
        printf '%s\n' "$output"
    fi
}

start_units() {
    nft list table inet black_relay >/dev/null 2>&1 ||
        die "the firewall table is not live, and nothing may start without it. A reboot applies the firewall and starts these"
    systemctl start docker.service mosquitto.service camera-link-dhcp.service camera-link.service \
        camera-provision.service camerabridge-config.path
}

summarize() {
    local admitted file
    printf '\n== Summary\n'
    printf 'Files changed: %d\n' "${#changed[@]}"
    for file in "${changed[@]+"${changed[@]}"}"; do
        printf '  %s\n' "${file#"$root"}"
    done
    printf 'Enabled for the next boot. frigate.service is not: camera-link starts it.\n'
    if [[ $start_units_done == yes ]]; then
        printf 'Started the services, as --start asked.\n'
    else
        printf 'Started nothing. Running services keep their old code until the reboot.\n'
    fi
    printf '\nChecks this script cannot fix by itself:\n'
    check 'hailo_pci driver 4.21.0 (Frigate script at tag v0.18.2, README step 9)' driver_is_pinned
    check 'hailo-all is not installed (remove it if it is: Frigate needs driver 4.21.0)' hailo_all_absent
    check 'docker answers' docker version --format '{{.Server.Version}}'
    check "Frigate image $(frigate_image) is pulled (0.18.2, by digest)" docker image inspect "$(frigate_image)"
    check 'sshd is key only with no root login (README step 7)' ssh_is_key_only

    printf '\nNOT REBOOTED. A reboot applies the firewall and the camera link on eth0.\n'
    admitted=$(grep -v '^[[:space:]]*#' "$central_hosts" | grep -Eo '[0-9]+(\.[0-9]+){3}' || true)
    if [[ -n $admitted ]]; then
        printf 'After it, SSH is admitted only from %s, on wlan0.\n' "${admitted//$'\n'/ }"
    else
        printf 'After it, NOBODY is admitted over the network until a portal save names the broker.\n'
        printf 'Before that reboot, have a console ready. See field-run-kit1.md, part A.\n'
    fi
    printf 'Open sessions survive until they close. Do not run systemctl restart nftables.\n'
    for file in "${notes[@]+"${notes[@]}"}"; do
        printf 'NOTE: %s\n' "$file"
    done

    finished=yes
    if ((${#not_ready[@]})); then
        printf '\nINSTALLED, BUT NOT READY: %d check(s) failed:\n' "${#not_ready[@]}"
        printf '  %s\n' "${not_ready[@]}"
        exit 3
    fi
    printf '\nREADY.\n'
}

begin 'Checking what this needs'
[[ $EUID -eq 0 || -n $root ]] || die "run as root"
[[ -f $cameralink_ops/$cache_mount ]] || die "this is not a whole source tree: no $cache_mount in $cameralink_ops"
[[ -d $src/bridge && -d $src/provision && -d $src/cameralink ]] || die "this is not a whole source tree: $src"
for tool in python3 nft nmcli systemctl docker dnsmasq sha256sum ss ip modinfo dpkg useradd \
    systemd-tmpfiles; do
    command -v "$tool" >/dev/null || die "$tool is missing. Do README steps 2 and 9 first"
done
if [[ $purge_tailscale == yes ]] && ssh_session_on tailscale0; then
    die "an SSH session is on tailscale0, and --purge-tailscale would cut it. Reconnect another way"
fi

begin 'Model yolov6n.hef'
install_model

begin 'Python packages, into their own venvs'
install -d -m 0755 -- "$root/opt/black-relay"
for package in provision bridge cameralink; do
    install_package "$package"
done
"$root/opt/black-relay/provision/bin/camera-provision" run --help >/dev/null
"$root/opt/black-relay/bridge/bin/camerabridge" run --help >/dev/null
"$root/opt/black-relay/cameralink/bin/camera-link" --help >/dev/null

begin 'Service user camerabridge'
if ! id -u camerabridge >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/camerabridge --shell /usr/sbin/nologin camerabridge
fi

begin 'Firewall, not loaded: a reboot applies it'
install_firewall

begin 'Units and config files'
install_files

begin 'Persistent journal, which a reboot applies'
install_journal

if [[ $purge_tailscale == yes ]]; then
    begin 'Removing Tailscale'
    purge_tailscale_package
fi

begin 'Enabling units, and starting none'
enable_units

begin "Removing '$wired_profile'"
remove_wired_profile

begin 'Verifying the units'
verify_units

start_units_done=no
if [[ $start_services == yes ]]; then
    begin 'Starting services, as --start asked'
    start_units
    start_units_done=yes
fi

summarize
