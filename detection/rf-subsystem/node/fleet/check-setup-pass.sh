#!/usr/bin/env bash
# check-setup-pass.sh [bootfs]: does the passphrase you type match the setup-network key on this card?
# Reads the card's user-data, never prints the passphrase or the key.
set -euo pipefail
boot="${1:-/Volumes/bootfs}"
ud="$boot/user-data"
[ -f "$ud" ] || { echo "no $ud (is the card in?)" >&2; exit 1; }
if [ -t 0 ]; then read -r -s -p "Setup passphrase to test: " pass; echo >&2; else read -r pass; fi
PASS="$pass" python3 - "$ud" <<'PY'
import base64, hashlib, os, re, sys
text = open(sys.argv[1]).read()
host = re.search(r'^hostname:\s*(\S+)', text, re.M)
print("card:", host.group(1) if host else "?")
found = False
for m in re.finditer(r'- path: (\S+\.nmconnection)\n(?:.*\n)*?\s+content: (\S+)', text):
    path, body = m.group(1), base64.b64decode(m.group(2)).decode()
    ssid = re.search(r'^ssid=(.*)$', body, re.M).group(1)
    key = re.search(r'^psk=(.*)$', body, re.M).group(1)
    mode = re.search(r'^mode=(.*)$', body, re.M).group(1)
    want = hashlib.pbkdf2_hmac("sha1", os.environ["PASS"].encode(), ssid.encode(), 4096, 32).hex()
    print(f"  {os.path.basename(path):28} {mode:15} ssid={ssid!r:24} {'MATCH' if want == key else 'no match'}")
    found = True
if not found:
    print("  no setup-network profiles on this card")
PY
