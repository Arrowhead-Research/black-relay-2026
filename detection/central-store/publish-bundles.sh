#!/usr/bin/env bash
# Pushes text bundles (made by `secrets.sh seal ... --out FILE`) to an OCI registry as the same
# artifacts `secrets.sh fetch` reads. Used by .github/workflows/publish-secrets.yml on a GitHub-hosted
# runner; the bundles are age-encrypted, so the runner never sees a key or a pairing file.
#   publish-bundles.sh <base> <file>...      e.g. ghcr.io/arrowhead-research/blackrelay-secrets
# Needs oras, already logged in to the registry (ORAS_FLAGS adds flags, e.g. --ca-file for a test store).
set -euo pipefail
base="${1:?usage: $0 <base> <bundle file>...}"; shift
[ $# -gt 0 ] || { echo "no bundles given" >&2; exit 1; }
TYPE=application/vnd.blackrelay.secrets.v1
valid() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; }
umask 077
for f in "$@"; do
  work="$(mktemp -d)"
  device="" bundle="" recipient="" paths=() armored=0
  while IFS= read -r line; do
    if [ "$armored" = 1 ]; then printf '%s\n' "$line" >>"$work/bundle.age"; continue; fi
    case "$line" in
      "blackrelay-bundle: v1") ;;
      "device: "*) device="${line#device: }" ;;
      "bundle: "*|"bundle:") bundle="${line#bundle:}"; bundle="${bundle# }" ;;
      "recipient: "*) recipient="${line#recipient: }" ;;
      "path: "*) paths+=("${line#path: }") ;;
      "-----BEGIN AGE ENCRYPTED FILE-----") armored=1; printf '%s\n' "$line" >"$work/bundle.age" ;;
      *) echo "$(basename "$f"): unexpected line in header, skipped" >&2 ;;
    esac
  done <"$f"
  name="$(basename "$f")"
  valid "$device" || { echo "$name: bad or missing device name" >&2; exit 1; }
  [ -z "$bundle" ] || valid "$bundle" || { echo "$name: bad bundle name" >&2; exit 1; }
  [[ "$recipient" =~ ^age1[0-9a-z]{58}$ ]] || { echo "$name: bad recipient" >&2; exit 1; }
  [ ${#paths[@]} -gt 0 ] && [ "$armored" = 1 ] || { echo "$name: no paths or no encrypted payload" >&2; exit 1; }
  grep -q -- "-----END AGE ENCRYPTED FILE-----" "$work/bundle.age" || { echo "$name: truncated payload" >&2; exit 1; }
  for p in "${paths[@]}"; do case "$p" in ""|/*|*..*) echo "$name: bad path $p" >&2; exit 1 ;; esac; done
  printf '%s\n' "$recipient" >"$work/recipients.txt"
  printf '%s\n' "${paths[@]}" >"$work/paths.txt"
  ref="$base/$device${bundle:+/$bundle}"
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  (cd "$work" && oras push ${ORAS_FLAGS:-} --artifact-type "$TYPE" \
     -a "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" -a "blackrelay.device=$device" \
     "$ref:latest,$ts" bundle.age:application/vnd.blackrelay.secrets.bundle.v1+age \
     recipients.txt:text/plain paths.txt:text/plain >/dev/null)
  rm -rf "$work"
  echo "published $device${bundle:+ ($bundle)}: ${#paths[@]} path(s) -> $ref:latest (also :$ts)"
done
