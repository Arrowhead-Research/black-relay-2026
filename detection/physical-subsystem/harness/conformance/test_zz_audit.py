"""One-way audit (static + whole-run). Keep this file last (alphabetical)."""
import sys
from pathlib import Path
import pytest
from audit.static_audit import scan
from conformance.conftest import IMPL, SESSION_VIOLATIONS, SESSION_STATE


def test_static_grep_audit_no_panel_write_paths():
    if IMPL is None: pytest.skip("BR_IMPL not set")
    files, findings = scan(IMPL)
    assert files > 0, f"no source files found under {IMPL}"
    assert not findings, "static audit found panel-write / mutating-route code:\n" + "\n".join("  %s:%d [%s] %s" % f for f in findings)


def test_whole_run_zero_state_changing_messages_to_panel():
    if IMPL is None: pytest.skip("BR_IMPL not set")
    assert SESSION_STATE["tests"] > 0, "no bridge test ran in this session; whole-run audit is vacuous"
    assert not SESSION_VIOLATIONS, f"{len(SESSION_VIOLATIONS)} ONE-WAY VIOLATION(S) across the run:\n" + \
        "\n".join(f"  [{v['side']}] {v['reason']} raw={v['raw'][:160]!r}" for v in SESSION_VIOLATIONS[:20])
