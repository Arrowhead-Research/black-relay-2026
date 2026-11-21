#!/usr/bin/env bash
# Makes the team publisher's broker password private to it and checks that it can read it.
#   deploy/scripts/fusion-secret.sh            fix owner/modes (sudo if needed), then check
#   deploy/scripts/fusion-secret.sh --check    check only
# secrets/fusion/ is mounted whole at /run/secrets in the `fusion` container, which runs as uid 10003:
# the directory must be 10003:10000 0700 and fusion_password 10003:10000 0600. Docker passes the host's
# owner and mode through (Docker Desktop too), so a file owned by you with mode 0600 is unreadable there
# and the publisher logs "cannot read password_file" and never connects.
set -euo pipefail
deploy="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$deploy"
d=secrets/fusion f=secrets/fusion/fusion_password
fix="sudo chown -R 10003:10000 $deploy/$d && sudo chmod 700 $deploy/$d && sudo chmod 600 $deploy/$f"
as_root() { if [ "$(id -u)" = 0 ] || [ -n "${NO_SUDO:-}" ]; then "$@"; else sudo "$@"; fi; }

if [ "${1:-}" != "--check" ]; then
  as_root test -s "$f" || { echo "$deploy/$f is missing or empty: put BROKER_PHYSICAL_PASSWORD from the central .env in it" >&2; exit 1; }
  [ "$(id -u)" = 0 ] || [ -n "${NO_SUDO:-}" ] || echo "   sudo: handing $d to the publisher's uid 10003 (mode 0700, password 0600)"
  as_root chown 10003:10000 "$d" "$f"
  as_root chmod 700 "$d"
  as_root chmod 600 "$f"
fi

# The real test: read it as the container user, through the same mount (needs the publisher's image locally).
result="$(docker compose --profile fusion run --rm --no-deps --pull never --entrypoint sh fusion \
            -c 'test -r /run/secrets/fusion_password && echo READABLE || echo UNREADABLE' 2>/dev/null || true)"
case "$result" in
  *UNREADABLE*)
    echo "ERROR: the fusion publisher (uid 10003) cannot read /run/secrets/fusion_password. Fix with:" >&2
    echo "   $fix" >&2
    exit 1 ;;
  *READABLE*) echo "   fusion publisher can read its broker password" ;;
  *) echo "   (could not start the publisher's image here, e.g. not built or pulled yet: readability check skipped)" ;;
esac
