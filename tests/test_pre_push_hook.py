"""The seeded pre-push hook must not mislabel scanner errors as leaks (issue #82)."""

from __future__ import annotations

from pathlib import Path

import brigade


def _hook_text() -> str:
    hook = Path(brigade.__file__).resolve().parent / "templates" / "hooks" / "pre-push"
    return hook.read_text()


def test_pre_push_hook_captures_exit_code():
    text = _hook_text()
    assert "|| rc=$?" in text


def test_pre_push_hook_only_blocks_on_findings_exit_code():
    text = _hook_text()
    # The "found violations" message is gated on exit code 1 specifically.
    assert '"$rc" -eq 1' in text
    assert "BLOCKED. content-guard found violations." in text


def test_pre_push_hook_reports_scanner_errors_separately():
    text = _hook_text()
    assert "failed to run" in text
    assert "not a leak verdict" in text
