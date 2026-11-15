"""Integration tests run through the shared conformance harness fixtures (real mosquitto, simulated IQ panel on a
real TLS MQTT broker, simulated Control4 TLS panel with audit of every byte the bridge sends the panel).

The harness is found ONLY through the environment variable BR_HARNESS_DIR (a checkout of the conformance harness that
contains conformance/, sim/, lib/ and a .venv). Unset or not a directory: every integration test is skipped."""
import os, sys, pathlib

_h = os.environ.get("BR_HARNESS_DIR")
HARNESS = pathlib.Path(_h) if _h else None
IMPL_DIR = pathlib.Path(__file__).resolve().parents[2]
os.environ.setdefault("BR_IMPL", str(IMPL_DIR))
if HARNESS is not None and HARNESS.is_dir():
    sys.path.insert(0, str(HARNESS))
    from conformance.conftest import *  # noqa: F401,F403  (fixtures: env, env_nostart, pki, api_key_hash + violation hook)
else:  # pragma: no cover
    import pytest
    collect_ignore_glob = ["test_*.py"]
    print("integration tests skipped: set BR_HARNESS_DIR to the conformance harness directory", file=sys.stderr)
