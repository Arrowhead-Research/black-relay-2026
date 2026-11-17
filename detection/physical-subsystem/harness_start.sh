#!/usr/bin/env bash
# Harness entry: harness_start.sh <config.yaml>  -> runs all four services (separate OS processes) in the foreground.
set -euo pipefail
cd "$(dirname "$0")"
CFG="${1:?usage: harness_start.sh <config.yaml>}"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import br2, aiohttp, aiomqtt, argon2, yaml" 2>/dev/null; then
  python3 -m venv .venv
  .venv/bin/pip install -q -e . >&2
fi
export PYTHONUNBUFFERED=1
# The conformance harness rotates the simulated panel's TLS certificate on purpose (test_iq_cert_rotation_trusted_ca_recovers).
# By default the bridge REFUSES to connect without a recorded pin (`iq_remote.pin_mode: required`; record one with
# `br2 pair` / `br2 repin --yes`) and REFUSES a changed panel certificate until `br2 repin --yes` (leaf pin, see
# src/br2/pin.py). The harness has no pairing step, so it opts into trust-on-first-use plus rotation-following. These two
# variables, set ONLY here, are the only place either is enabled; they are off in every shipped config/compose file.
export BR2_IQ_PIN_MODE=tofu
export BR2_IQ_PIN_ACCEPT_ROTATION=1
# The harness fixes its API key (br_test_key_0123456789) and writes only an argon2 hash. The bridge REQUIRES a key_id on
# every api.keys record (it refuses to start otherwise), so for the harness ONLY this entry point adds the key_id of that
# known key to a record whose hash verifies it, in a temporary copy of the config. Shipped configs are never rewritten.
RUNCFG="${TMPDIR:-/tmp}/br2-harness-$(printf %s "$CFG" | cksum | cut -d" " -f1).yaml"     # holds no secret (a hash only)
.venv/bin/python - "$CFG" "$RUNCFG" <<'PY'
import sys, yaml
from argon2 import PasswordHasher
from br2.outbound.api import key_id
KEY = "br_test_key_0123456789"
cfg = yaml.safe_load(open(sys.argv[1])) or {}
for k in (cfg.get("api") or {}).get("keys") or []:
    if isinstance(k, dict) and not k.get("key_id"):
        try:
            PasswordHasher().verify(k.get("argon2_hash", ""), KEY)
            k["key_id"] = key_id(KEY)
        except Exception:
            pass
open(sys.argv[2], "w").write(yaml.safe_dump(cfg))
PY
exec .venv/bin/python -m br2 run --config "$RUNCFG"
