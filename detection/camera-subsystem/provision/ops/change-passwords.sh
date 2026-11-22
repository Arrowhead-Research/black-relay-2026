#!/usr/bin/env bash
# Change one of a kit's passwords. Every kit starts with the default that seed-kit.sh gives it,
# and this is how its owner replaces that. Run on the kit, at its console or over SSH.
#
# Usage: sudo change-passwords.sh console | setup-wifi | broker
#   console      the password of the console user running sudo (kit-N).
#   setup-wifi   the passphrase of this kit's setup WiFi. It prints the new label.
#   broker       the broker password this kit logs in to the central node with. The central node
#                must be given the same one, with relay/change-passwords.sh broker, or this kit
#                goes quiet: its events queue here until the two match again.
#
# The new password is asked for twice, is never echoed, and never appears in an argument or a
# log. Until the two entries match and pass the checks, nothing is changed.
#
# CONSOLE_USER, NM_DIR, PROVISION_PYTHON and CHANGE_ALLOW_NON_ROOT can be overridden for testing.
set -euo pipefail
set +x # the password must never reach a trace
# Character ranges below mean ASCII. In a UTF-8 locale they follow the dictionary instead, and
# kit 1, in en_GB.UTF-8, refused its own default until this was set.
export LC_ALL=C

nm_dir=${NM_DIR:-/etc/NetworkManager/system-connections}
provision_python=${PROVISION_PYTHON:-/opt/black-relay/provision/bin/python}
console_user=${CONSOLE_USER:-${SUDO_USER:-}}
profile="$nm_dir/black-relay-setup-ap.nmconnection"

die() {
    printf 'change-passwords: %s\n' "$1" >&2
    exit 1
}

usage="usage: change-passwords.sh console | setup-wifi | broker"
[[ $# -eq 1 ]] || die "$usage"
what=$1
case "$what" in
    console | setup-wifi | broker) ;;
    *) die "$usage" ;;
esac
[[ $EUID -eq 0 || -n ${CHANGE_ALLOW_NON_ROOT:-} ]] || die "run as root"

# One rule for all three, so a password that works in one place can be typed in every other:
# the setup WiFi takes 8 to 63 characters, the setup page's form and the central node's settings
# file take no space, quote, backtick, backslash or dollar sign.
acceptable() {
    local value=$1
    [[ ${#value} -ge 8 && ${#value} -le 63 ]] || return 1
    [[ $value =~ ^[!-~]+$ ]] || return 1
    case "$value" in
        *\'* | *\"* | *\`* | *\\* | *\$*) return 1 ;;
    esac
}

# Sets $password, or dies with nothing changed.
ask() {
    local label=$1 first second
    password=
    for _ in 1 2 3; do
        printf 'New %s: ' "$label" >&2
        IFS= read -rs first || die "no password given, and nothing was changed"
        printf '\nAgain: ' >&2
        IFS= read -rs second || die "no password given, and nothing was changed"
        printf '\n' >&2
        if ! acceptable "$first"; then
            printf 'change-passwords: 8 to 63 characters, with no space, quote, backtick, backslash or dollar sign.\n' >&2
        elif [[ $first != "$second" ]]; then
            printf 'change-passwords: the two did not match.\n' >&2
        else
            password=$first
            return 0
        fi
    done
    die "no password set, and nothing was changed"
}

change_console() {
    [[ -n $console_user && $console_user != root ]] || die "no console user: run through sudo, or set CONSOLE_USER"
    id -u -- "$console_user" >/dev/null 2>&1 || die "no such console user: $console_user"
    ask "console password for $console_user"
    printf '%s:%s\n' "$console_user" "$password" | chpasswd || die "setting the console password failed"
    unset password
    printf 'change-passwords: the console password of %s is changed.\n' "$console_user" >&2
}

change_setup_wifi() {
    [[ -r $profile ]] || die "this kit has no setup WiFi profile. Seed it first, with seed-kit.sh"
    local line ssid='' staged found=no
    ask "setup WiFi passphrase"
    umask 077
    # Hidden and ending in .tmp, so NetworkManager's keyfile plugin never loads it.
    staged="$nm_dir/.black-relay-setup-ap.change.tmp"
    trap 'rm -f -- "$staged"' EXIT
    # Line by line in the shell, so the passphrase is in no argv, and nothing else in the profile moves.
    while IFS= read -r line || [[ -n $line ]]; do
        case "$line" in
            psk=*)
                printf 'psk=%s\n' "$password"
                found=yes
                ;;
            ssid=*)
                ssid=${line#ssid=}
                printf '%s\n' "$line"
                ;;
            *) printf '%s\n' "$line" ;;
        esac
    done <"$profile" >"$staged"
    [[ $found == yes && -n $ssid ]] || die "the setup WiFi profile has no passphrase line, and nothing was changed"
    nmcli --offline connection modify connection.autoconnect no <"$staged" >/dev/null ||
        die "NetworkManager rejected the new profile, and nothing was changed"
    mv -f -- "$staged" "$profile"
    trap - EXIT

    # The label, on stdout, as seed-kit.sh prints it. Before the reload, so a failed reload still shows it.
    printf 'BLACK RELAY CAMERA KIT\n'
    printf 'Setup WiFi:  %s\n' "$ssid"
    printf 'Passphrase:  %s\n' "$password"
    printf 'Setup page:  http://10.42.0.1/\n'
    unset password
    printf 'change-passwords: put the new label on the kit. The old passphrase no longer works.\n' >&2
    nmcli connection reload
}

change_broker() {
    [[ -x $provision_python ]] || die "the provisioning code is not installed: $provision_python"
    ask "broker password"
    # The kit's own store does the write, as the setup page does: all of the saved settings are
    # replaced together or not at all. The password goes in on stdin.
    printf '%s\n' "$password" | "$provision_python" -c '
import configparser, shutil, sys
from dataclasses import replace
from cameraprovision.store import NodeStore, Paths

password = sys.stdin.readline().rstrip("\n")
paths = Paths()
store = NodeStore(paths, chown=lambda path: shutil.chown(path, "camerabridge"))
config = store.load()
if config is None:
    sys.exit("this kit has no saved settings yet: use its setup page first")
keyfile = configparser.ConfigParser(interpolation=None)
keyfile.read(paths.nm_keyfile)
store.save(
    replace(
        config,
        broker=replace(config.broker, password=password),
        wifi_psk=keyfile.get("wifi-security", "psk", fallback=""),
        broker_anonymous=False,
    )
)
' || die "saving the broker password failed, and nothing was changed"
    unset password
    printf 'change-passwords: this kit now logs in to the broker with the new password.\n' >&2
    printf 'change-passwords: give the central node the same one, in its relay directory: ./change-passwords.sh broker\n' >&2
}

case "$what" in
    console) change_console ;;
    setup-wifi) change_setup_wifi ;;
    broker) change_broker ;;
esac
