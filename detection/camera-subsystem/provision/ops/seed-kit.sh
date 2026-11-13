#!/usr/bin/env bash
# Seed one kit: give this clone of the image its own identity, and print the kit label on stdout.
# It replaces the SSH host keys and machine ID, makes the kit's own console user, kit-N, and
# renders the setup-AP profile with this Pi's serial, root-only. Run once per kit, on the kit's own
# Pi, at its console. See README, "Seed each kit".
#
# Usage: sudo seed-kit.sh --kit N [--force]
#   --kit N   this kit's number, 1 to 99. Its console user is kit-N.
#   --force   seeds a kit again. Its SSH host keys and machine ID change, and its console password
#             and setup passphrase go back to the default.
#
# Every kit starts with one default password, for its console user and its setup WiFi, so that
# nobody handed a kit has to guess. change-passwords.sh, beside this script, changes either.
# BLACK_RELAY_DEFAULT_PASSWORD names another default for a whole build.
#
# The image's user is the one running sudo. CONSOLE_USER names another. NM_DIR, SERIAL_FILE,
# SSH_DIR, MACHINE_ID_FILE, DBUS_MACHINE_ID_FILE, SEED_MARKER, SUDOERS_DIR, AUTOLOGIN_DROPIN and
# SEED_ALLOW_NON_ROOT can be overridden for testing.
set -euo pipefail
set +x # the password must never reach a trace
# Character ranges below mean ASCII. In a UTF-8 locale they follow the dictionary instead.
export LC_ALL=C

default_password=${BLACK_RELAY_DEFAULT_PASSWORD:-BlackRelay2026}

nm_dir=${NM_DIR:-/etc/NetworkManager/system-connections}
serial_file=${SERIAL_FILE:-/sys/firmware/devicetree/base/serial-number}
ssh_dir=${SSH_DIR:-/etc/ssh}
machine_id_file=${MACHINE_ID_FILE:-/etc/machine-id}
dbus_machine_id_file=${DBUS_MACHINE_ID_FILE:-/var/lib/dbus/machine-id}
seed_marker=${SEED_MARKER:-/etc/black-relay/kit-seeded}
sudoers_dir=${SUDOERS_DIR:-/etc/sudoers.d}
autologin_dropin=${AUTOLOGIN_DROPIN:-/etc/systemd/system/getty@tty1.service.d/autologin.conf}
image_user=${CONSOLE_USER:-${SUDO_USER:-}}
template="$(dirname -- "$(readlink -f -- "$0")")/black-relay-setup-ap.nmconnection.tmpl"
profile="$nm_dir/black-relay-setup-ap.nmconnection"
sudoers_file="$sudoers_dir/010_black-relay-kit"

die() {
    printf 'seed-kit: %s\n' "$1" >&2
    exit 1
}

usage="usage: seed-kit.sh --kit N [--force], with N from 1 to 99"
force=no
kit=
while (($#)); do
    case "$1" in
        --force) force=yes ;;
        --kit)
            [[ $# -ge 2 ]] || die "$usage"
            kit=$2
            shift
            ;;
        *) die "$usage" ;;
    esac
    shift
done
[[ $kit =~ ^[1-9][0-9]?$ ]] || die "$usage"
kit_user="kit-$kit"

[[ $EUID -eq 0 || -n ${SEED_ALLOW_NON_ROOT:-} ]] || die "run as root"
[[ -r $template ]] || die "template not found: $template"
if [[ (-e $profile || -e $seed_marker) && $force != yes ]]; then
    die "this kit is already seeded. --force seeds it again, and puts its passwords back to the default"
fi
for tool in ssh-keygen chpasswd systemd-machine-id-setup nmcli useradd usermod visudo getent; do
    command -v "$tool" >/dev/null || die "$tool is missing"
done
[[ -n $image_user && $image_user != root ]] || die "no console user: run through sudo, or set CONSOLE_USER"
id -u -- "$image_user" >/dev/null 2>&1 || die "no such console user: $image_user"

# The device tree value ends in a NUL. An SSID holds 32 bytes, 15 of them the prefix.
serial=$(tr -d '\0' <"$serial_file")
[[ $serial =~ ^[0-9a-fA-F]{8,16}$ ]] || die "unexpected serial format in $serial_file"

# WPA2 takes 8 to 63 characters. The rest is what change-passwords.sh also holds a password to,
# so that the default can be typed anywhere a changed one can.
passphrase=$default_password
[[ ${#passphrase} -ge 8 && ${#passphrase} -le 63 && $passphrase =~ ^[A-Za-z0-9._@%+=:,/-]+$ ]] ||
    die "the default password must be 8 to 63 letters, digits or ._@%+=:,/-"

rendered=$(<"$template")
rendered=${rendered//@SERIAL@/$serial}
rendered=${rendered//@PASSPHRASE@/$passphrase}

umask 077
# Hidden and ending in .tmp, so NetworkManager's keyfile plugin never loads it.
staged="$nm_dir/.black-relay-setup-ap.seed.tmp"
staged_sudoers="$sudoers_dir/.black-relay-kit.seed.tmp"
new_keys=$(mktemp -d "$ssh_dir/.seed-keys.XXXXXX")
trap 'rm -f -- "$staged" "$staged_sudoers"; rm -rf -- "$new_keys"' EXIT
printf '%s\n' "$rendered" >"$staged"
nmcli --offline connection modify connection.autoconnect no <"$staged" >/dev/null ||
    die "NetworkManager rejected the rendered profile"

# Generated before the old ones go, so that a failure here leaves the kit as it was. A crash during
# what follows leaves no marker and no profile, so running this again finishes the job.
install -d -m 0755 -- "$new_keys/etc/ssh"
ssh-keygen -A -f "$new_keys" >/dev/null
[[ -s $new_keys/etc/ssh/ssh_host_ed25519_key ]] || die "ssh-keygen made no host keys, and nothing was changed"

# The kit's own user. The image's user is logged in and running this, so it cannot be renamed:
# the kit's user is made beside it, with the same groups, the same SSH key and the same sudo
# rule, and the image's user is closed further down, once the kit's user is known to be good.
home_of() {
    getent passwd "$1" | cut -d: -f6
}
if ! id -u -- "$kit_user" >/dev/null 2>&1; then
    # grep finds nothing, and so fails, for a user in no group but its own.
    groups=$(id -Gn -- "$image_user" | tr ' ' '\n' | { grep -vx -- "$image_user" || true; } | paste -sd, -)
    useradd --create-home --shell /bin/bash --comment "Black Relay kit $kit" \
        ${groups:+--groups "$groups"} "$kit_user" || die "making the user $kit_user failed, and nothing else was changed"
fi
kit_home=$(home_of "$kit_user")
image_home=$(home_of "$image_user")
[[ -n $kit_home ]] || die "the user $kit_user has no home directory"
if [[ $kit_user != "$image_user" && -s $image_home/.ssh/authorized_keys ]]; then
    install -d -m 0700 -- "$kit_home/.ssh"
    install -m 0600 -- "$image_home/.ssh/authorized_keys" "$kit_home/.ssh/authorized_keys"
    chown -R -- "$kit_user:" "$kit_home/.ssh" || die "giving $kit_user its SSH key failed"
fi
# Checked before it is installed: a sudoers file that does not parse locks everyone out of sudo.
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$kit_user" >"$staged_sudoers"
visudo -cf "$staged_sudoers" >/dev/null || die "the sudo rule for $kit_user does not parse, and it was not installed"
chmod 0440 -- "$staged_sudoers"
mv -f -- "$staged_sudoers" "$sudoers_file"

# By stdin, so the password never appears in any argv, and it is never echoed or logged.
printf '%s:%s\n' "$kit_user" "$default_password" | chpasswd || die "setting the console password failed"

rm -f -- "$ssh_dir"/ssh_host_*
chmod 0644 -- "$new_keys"/etc/ssh/ssh_host_*.pub
mv -f -- "$new_keys"/etc/ssh/ssh_host_* "$ssh_dir"/

old_machine_id=$(cat -- "$machine_id_file" 2>/dev/null || true)
rm -f -- "$machine_id_file" "$dbus_machine_id_file"
(umask 022 && systemd-machine-id-setup >/dev/null 2>&1) || die "systemd-machine-id-setup failed. Run this again"
new_machine_id=$(cat -- "$machine_id_file" 2>/dev/null || true)
[[ $new_machine_id =~ ^[0-9a-f]{32}$ ]] || die "the new machine ID looks wrong. Run this again"
[[ $new_machine_id != "$old_machine_id" ]] || die "the machine ID did not change. Run this again"

# The console asks for a login from now on. The image logs its own user in by itself, which
# would leave a kit's console open to anyone, and would fail once that user is closed.
rm -f -- "$autologin_dropin"
if [[ $kit_user != "$image_user" ]]; then
    usermod --lock --expiredate 1 "$image_user" || die "closing the image's user $image_user failed. Run this again"
fi

mv -f -- "$staged" "$profile"
install -d -m 0755 -- "$(dirname -- "$seed_marker")"
: >"$seed_marker"
rm -rf -- "$new_keys"
trap - EXIT

# Print first: the profile holds this passphrase now, even if the reload fails.
printf 'BLACK RELAY CAMERA KIT\n'
printf 'Setup WiFi:  BlackRelay-CAM-%s\n' "$serial"
printf 'Passphrase:  %s\n' "$passphrase"
printf 'Setup page:  http://10.42.0.1/\n'

# On stderr, as stdout is the label. Before the reload, so that a failed reload still shows it.
printf 'seed-kit: the console login is %s, with the default password, the one on the label. Change either with: sudo %s/change-passwords.sh\n' \
    "$kit_user" "$(dirname -- "$(readlink -f -- "$0")")" >&2
printf 'seed-kit: reboot now. The new machine ID takes full effect only at a reboot: until then journalctl shows nothing, and journald keeps writing under the old ID. After it, log in as %s.\n' "$kit_user" >&2

nmcli connection reload
