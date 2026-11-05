#!/usr/bin/env bash
# Seed one kit: render the setup-AP profile with this Pi's serial and a new random
# passphrase, install it root-only, and print the kit label on stdout. Run once per kit,
# on the kit's own Pi. See README, "Seed each kit".
#
# Usage: sudo seed-kit.sh [--force]
#   --force replaces an existing profile and passphrase, so the old label stops working.
#
# NM_DIR, SERIAL_FILE and PROVISION_PYTHON can be overridden for testing.
set -euo pipefail

nm_dir=${NM_DIR:-/etc/NetworkManager/system-connections}
serial_file=${SERIAL_FILE:-/sys/firmware/devicetree/base/serial-number}
provision_python=${PROVISION_PYTHON:-/opt/black-relay/provision/bin/python}
template="$(dirname -- "$(readlink -f -- "$0")")/black-relay-setup-ap.nmconnection.tmpl"
profile="$nm_dir/black-relay-setup-ap.nmconnection"

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

[[ $EUID -eq 0 ]] || die "run as root"
[[ -r $template ]] || die "template not found: $template"
if [[ -e $profile && $force != yes ]]; then
    die "this kit is already seeded. --force replaces its passphrase, and its label stops working"
fi

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
trap 'rm -f -- "$staged"' EXIT
printf '%s\n' "$rendered" >"$staged"
nmcli --offline connection modify connection.autoconnect no <"$staged" >/dev/null ||
    die "NetworkManager rejected the rendered profile"
mv -f -- "$staged" "$profile"
trap - EXIT

# Print first: the profile holds this passphrase now, even if the reload fails.
printf 'BLACK RELAY CAMERA KIT\n'
printf 'Setup WiFi:  BlackRelay-CAM-%s\n' "$serial"
printf 'Passphrase:  %s\n' "$passphrase"
printf 'Setup page:  http://10.42.0.1/\n'

nmcli connection reload
