#!/usr/bin/env bash
# Seed this kit on its first boot, with no screen or keyboard. Run by
# black-relay-first-boot.service, only while the boot partition holds a file with the kit's
# number, which whoever wrote the card put there. See README, "Seed each kit".
#
# It grows the root filesystem to the card, runs seed-kit.sh with the number, leaves the label
# text beside where the number was, removes the number so that this never runs again, and
# reboots, which the seed needs. If anything fails it leaves the number where it is, writes why
# to a log on the boot partition, where the card's writer can read it, and does not reboot.
#
# BOOT_DIR, OPS_DIR, SEED_MARKER and FIRST_BOOT_IMAGE_UID can be overridden for testing.
set -uo pipefail
export LC_ALL=C

boot_dir=${BOOT_DIR:-/boot/firmware}
ops_dir=${OPS_DIR:-/opt/black-relay/ops}
image_uid=${FIRST_BOOT_IMAGE_UID:-1000}
seed_marker=${SEED_MARKER:-/etc/black-relay/kit-seeded}
number_file="$boot_dir/black-relay-kit"
label_file="$boot_dir/black-relay-label.txt"
log_file="$boot_dir/black-relay-first-boot.log"

fail() {
    printf 'first-boot: %s\n' "$1" >&2
    printf '%s\n' "$1" >"$log_file" 2>/dev/null || true
    exit 1
}

[[ -r $number_file ]] || fail "no kit number at $number_file"
# A seed that finished, with power lost before the number was removed: there is nothing left to do,
# and seeding again would be refused on every boot from now on.
if [[ -e $seed_marker ]]; then
    rm -f -- "$number_file"
    printf 'first-boot: this kit is already seeded, so the kit number was only removed.
' >&2
    exit 0
fi
# Whatever editor wrote it may have added blank lines, spaces around it or a carriage return.
# Nothing inside it is dropped: "2 3" is not kit 23.
kit=$(tr -d '\r' <"$number_file" | awk 'NF { print; exit }' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
[[ $kit =~ ^[1-9][0-9]?$ ]] || fail "the kit number in $number_file must be 1 to 99, and nothing was changed"

# The image's user: seed-kit.sh copies its groups, SSH key and sudo rule to the kit's own user.
image_user=$(getent passwd "$image_uid" | cut -d: -f1)
[[ -n $image_user ]] || fail "the image has no user with uid $image_uid, and nothing was changed"

# The image is cut down to its data. This grows the partition now and the filesystem at the reboot.
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_expand_rootfs >/dev/null 2>&1 ||
        printf 'first-boot: the root filesystem was not expanded. Run raspi-config nonint do_expand_rootfs later.\n' >&2
fi

# The label goes to a file only once the seed has worked. Its errors go to the log.
umask 077
staged=$(mktemp "$boot_dir/.black-relay-label.XXXXXX") || fail "the boot partition cannot be written"
if ! CONSOLE_USER=$image_user "$ops_dir/seed-kit.sh" --kit "$kit" >"$staged" 2>"$log_file"; then
    # Its last act is to have NetworkManager reload, after the seed is whole and the label is
    # printed. A failure there alone is mended by the reboot below, which loads the profile.
    if [[ ! -e $seed_marker || ! -s $staged ]]; then
        rm -f -- "$staged"
        printf 'first-boot: seed-kit.sh failed. Its message is in %s\n' "$log_file" >&2
        exit 1
    fi
fi
mv -f -- "$staged" "$label_file"
rm -f -- "$number_file" "$log_file"
sync
printf 'first-boot: kit %s is seeded. Rebooting.\n' "$kit" >&2
systemctl --no-block reboot
