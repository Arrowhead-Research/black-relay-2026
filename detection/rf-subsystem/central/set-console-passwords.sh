#!/usr/bin/env bash
# Set the WBT Console sign-in passwords in .env and restart the console.
# Run it on the host that runs the stack, in a terminal (it prompts):
#   ./set-console-passwords.sh
# Passwords are never echoed or passed on a command line.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env here: cp .env.example .env first." >&2; exit 1; }

ask() {
  local a b
  while :; do
    read -rsp "$1: " a; echo >&2
    if [ ${#a} -lt 10 ]; then echo "Use at least 10 characters." >&2; continue; fi
    if [[ $a == *"'"* ]]; then echo "No single quotes, please." >&2; continue; fi
    read -rsp "Again: " b; echo >&2
    [ "$a" = "$b" ] && break
    echo "They did not match." >&2
  done
  printf '%s' "$a"
}

op=$(ask "Operator password")
ad=$(ask "Admin password")
[ "$op" != "$ad" ] || { echo "Use different passwords for operator and admin." >&2; exit 1; }

# Single quotes keep compose from expanding $ in the values.
tmp=$(mktemp .env.XXXXXX)
grep -vE '^#? *CONSOLE_(OPERATOR|ADMIN)_PASSWORD=' .env > "$tmp" || true
printf "CONSOLE_OPERATOR_PASSWORD='%s'\nCONSOLE_ADMIN_PASSWORD='%s'\n" "$op" "$ad" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" .env

docker compose --profile console up -d --build rf-console
echo "Sign-in is on. Reload the console and sign in."
