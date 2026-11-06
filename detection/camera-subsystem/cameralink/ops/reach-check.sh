#!/usr/bin/env bash
# Answers the first field question: can each daemon still reach docker and systemd from inside its
# own hardening? Runs the same commands in a transient unit with the sandbox settings read from the
# installed unit file, so this cannot drift from it. Run as root on the node, with the camera out.
# See provision/ops/field-run-kit1.md, check C53. Exit status: the number of failed checks.
set -uo pipefail

units=${BLACK_RELAY_UNITS:-/etc/systemd/system}
failures=0

sandbox_of() {
    grep -E '^(CapabilityBoundingSet|NoNewPrivileges|Protect[A-Za-z]*|Private[A-Za-z]*|LockPersonality|MemoryDenyWriteExecute|Restrict[A-Za-z]*|SystemCall[A-Za-z]*|Device[A-Za-z]*|UMask)=' "$1"
}

in_sandbox() {
    local unit=$1 what=$2 line output status
    shift 2
    local properties=()
    while IFS= read -r line; do
        properties+=(-p "$line")
    done < <(sandbox_of "$units/$unit")
    output=$(systemd-run --wait --pipe --collect --quiet "${properties[@]}" "$@" 2>&1)
    status=$?
    if ((status == 0)); then
        printf 'ok   %s: %s\n' "$what" "${output:-(no output)}"
    else
        printf 'FAIL %s: exit %d: %s\n' "$what" "$status" "$output"
        failures=$((failures + 1))
    fi
}

in_sandbox camera-link.service 'camera-link reaches docker' \
    /usr/bin/docker version --format '{{.Server.Version}}'
in_sandbox camera-link.service 'camera-link reads container state from docker' \
    /usr/bin/docker ps --all --format '{{.Names}}'
in_sandbox camera-link.service 'camera-link asks systemd a question' \
    /usr/bin/systemctl show -p LoadState --value frigate.service
in_sandbox camera-link.service 'camera-link gives systemd a job, which is what resets DHCP' \
    /usr/bin/systemctl --no-block restart camera-link-dhcp.service
in_sandbox camera-provision.service 'camera-provision reloads the firewall through systemd' \
    /usr/bin/systemctl reload nftables.service
in_sandbox camera-provision.service 'camera-provision restarts timesyncd through systemd' \
    /usr/bin/systemctl --no-block try-restart systemd-timesyncd.service

sleep 3
printf '\nThe DHCP unit, as the daemon starts it:\n'
systemctl show -p ActiveState,SubState,NRestarts camera-link-dhcp.service
pid=$(systemctl show -p MainPID --value camera-link-dhcp.service)
if [[ ${pid:-0} != 0 ]]; then
    printf 'user %s\n' "$(ps -o user= -p "$pid")"
    grep -E '^Cap(Eff|Bnd)' "/proc/$pid/status"
elif [[ $(cat /sys/class/net/eth0/carrier 2>/dev/null) == 1 ]]; then
    printf 'FAIL the link is up and the DHCP unit has no process.\n'
    failures=$((failures + 1))
else
    printf 'No process, as eth0 has no link and so no address: it starts when the link does.\n'
fi
exit "$failures"
