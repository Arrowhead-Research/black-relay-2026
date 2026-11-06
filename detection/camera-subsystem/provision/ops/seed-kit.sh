#!/usr/bin/env bash
# Seed one kit: give this clone of the image its own identity, and print the kit label on stdout.
# It replaces the SSH host keys and machine ID, sets a new console password that it asks for, and
# renders the setup-AP profile with this Pi's serial and a new random passphrase, root-only. Run
# once per kit, on the kit's own Pi, at its console. See README, "Seed each kit".
#
# Usage: sudo seed-kit.sh [--force]
#   --force seeds a kit again. Its label stops working, and its SSH host keys, machine ID and
#   console password all change.
#
# The console user is the one running sudo. CONSOLE_USER names another. NM_DIR, SERIAL_FILE,
# PROVISION_PYTHON, SSH_DIR, MACHINE_ID_FILE, DBUS_MACHINE_ID_FILE, SEED_MARKER and
# SEED_ALLOW_NON_ROOT can be overridden for testing.
set -euo pipefail
set +x # the password must never reach a trace

nm_dir=${NM_DIR:-/etc/NetworkManager/system-connections}
serial_file=${SERIAL_FILE:-/sys/firmware/devicetree/base/serial-number}
provision_python=${PROVISION_PYTHON:-/opt/black-relay/provision/bin/python}
ssh_dir=${SSH_DIR:-/etc/ssh}
machine_id_file=${MACHINE_ID_FILE:-/etc/machine-id}
dbus_machine_id_file=${DBUS_MACHINE_ID_FILE:-/var/lib/dbus/machine-id}
seed_marker=${SEED_MARKER:-/etc/black-relay/kit-seeded}
console_user=${CONSOLE_USER:-${SUDO_USER:-}}
template="$(dirname -- "$(readlink -f -- "$0")")/black-relay-setup-ap.nmconnection.tmpl"
profile="$nm_dir/black-relay-setup-ap.nmconnection"
min_password=8

die() {
    printf 'seed-kit: %s\n' "$1" >&2
    exit 1
}

force=no
case "${1:-}" in
    "") ;;
    --force) force=yes ;;
    *) die "usage: seed-kit.sh [--force]" ;;
esac

[[ $EUID -eq 0 || -n ${SEED_ALLOW_NON_ROOT:-} ]] || die "run as root"
[[ -r $template ]] || die "template not found: $template"
if [[ (-e $profile || -e $seed_marker) && $force != yes ]]; then
    die "this kit is already seeded. --force replaces its identity and passphrase, and its label stops working"
fi
for tool in ssh-keygen chpasswd systemd-machine-id-setup nmcli; do
    command -v "$tool" >/dev/null || die "$tool is missing"
done
[[ -n $console_user && $console_user != root ]] || die "no console user: run through sudo, or set CONSOLE_USER"
id -u -- "$console_user" >/dev/null 2>&1 || die "no such console user: $console_user"

# The device tree value ends in a NUL. An SSID holds 32 bytes, 15 of them the prefix.
serial=$(tr -d '\0' <"$serial_file")
[[ $serial =~ ^[0-9a-fA-F]{8,16}$ ]] || die "unexpected serial format in $serial_file"

# Secrets stay in shell builtins, so the passphrase never appears in any argv.
passphrase=$("$provision_python" -c \
    'from cameraprovision.netprofile import generate_ap_passphrase as g; print(g())')
[[ $passphrase =~ ^[A-Za-z0-9]{16,63}$ ]] || die "the generated passphrase has an unexpected format"

rendered=$(<"$template")
rendered=${rendered//@SERIAL@/$serial}
rendered=${rendered//@PASSPHRASE@/$passphrase}

umask 077
# Hidden and ending in .tmp, so NetworkManager's keyfile plugin never loads it.
staged="$nm_dir/.black-relay-setup-ap.seed.tmp"
new_keys=$(mktemp -d "$ssh_dir/.seed-keys.XXXXXX")
trap 'rm -f -- "$staged"; rm -rf -- "$new_keys"' EXIT
printf '%s\n' "$rendered" >"$staged"
nmcli --offline connection modify connection.autoconnect no <"$staged" >/dev/null ||
    die "NetworkManager rejected the rendered profile"

# Every clone has the image's console password, so each kit gets its own. Read by a shell builtin
# and passed on stdin, so it never appears in any argv, and it is never echoed or logged.
password=
for _ in 1 2 3; do
    printf 'New console password for %s, for this kit only: ' "$console_user" >&2
    IFS= read -rs first || die "no password given, and nothing was changed"
    printf '\nAgain: ' >&2
    IFS= read -rs second || die "no password given, and nothing was changed"
    printf '\n' >&2
    if [[ ${#first} -lt $min_password ]]; then
        printf 'seed-kit: at least %d characters.\n' "$min_password" >&2
    elif [[ $first != "$second" ]]; then
        printf 'seed-kit: the two did not match.\n' >&2
    else
        password=$first
        break
    fi
done
unset first second
[[ -n $password ]] || die "no password set, and nothing was changed"

# Generated before the old ones go, so that a failure here leaves the kit as it was. A crash during
# what follows leaves no marker and no profile, so running this again finishes the job.
install -d -m 0755 -- "$new_keys/etc/ssh"
ssh-keygen -A -f "$new_keys" >/dev/null
[[ -s $new_keys/etc/ssh/ssh_host_ed25519_key ]] || die "ssh-keygen made no host keys, and nothing was changed"

printf '%s:%s\n' "$console_user" "$password" | chpasswd || die "setting the console password failed"
unset password

rm -f -- "$ssh_dir"/ssh_host_*
chmod 0644 -- "$new_keys"/etc/ssh/ssh_host_*.pub
mv -f -- "$new_keys"/etc/ssh/ssh_host_* "$ssh_dir"/

old_machine_id=$(cat -- "$machine_id_file" 2>/dev/null || true)
rm -f -- "$machine_id_file" "$dbus_machine_id_file"
(umask 022 && systemd-machine-id-setup >/dev/null 2>&1) || die "systemd-machine-id-setup failed. Run this again"
new_machine_id=$(cat -- "$machine_id_file" 2>/dev/null || true)
[[ $new_machine_id =~ ^[0-9a-f]{32}$ ]] || die "the new machine ID looks wrong. Run this again"
[[ $new_machine_id != "$old_machine_id" ]] || die "the machine ID did not change. Run this again"

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

nmcli connection reload
