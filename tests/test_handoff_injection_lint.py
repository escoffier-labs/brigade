from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import handoff_cmd
from brigade.untrusted import scan_handoff_injection_heuristics

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "handoff_lint" / "injection"

EVIL_FIXTURES = sorted(FIXTURE_DIR.glob("evil-*.md"))
BENIGN_FIXTURES = sorted(FIXTURE_DIR.glob("benign-*.md"))


@pytest.mark.parametrize("fixture_path", EVIL_FIXTURES, ids=lambda path: path.name)
def test_injection_fixture_flags_warning(fixture_path: Path):
    text = fixture_path.read_text()
    hits = scan_handoff_injection_heuristics(text)
    warnings = [hit for hit in hits if hit.severity == "warning"]
    assert warnings, f"expected warning-level injection hits in {fixture_path.name}"
    assert all(hit.line >= 1 for hit in warnings)


@pytest.mark.parametrize("fixture_path", BENIGN_FIXTURES, ids=lambda path: path.name)
def test_benign_injection_fixture_has_no_warnings(fixture_path: Path):
    text = fixture_path.read_text()
    hits = scan_handoff_injection_heuristics(text)
    warnings = [hit for hit in hits if hit.severity == "warning"]
    assert not warnings, f"benign fixture should not warn: {warnings}"


def test_handoff_lint_content_guard_reports_injection_with_line_numbers(tmp_path, capsys, monkeypatch):
    evil = FIXTURE_DIR / "evil-ignore-previous.md"
    path = tmp_path / "evil.md"
    path.write_text(evil.read_text())

    def fake_run_scan(scan_target, *, repo_target=None, policy="public-repo"):
        return {
            "available": True,
            "status": "ok",
            "exit_code": 0,
            "detail": "clean",
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr("brigade.scrub.run_scan", fake_run_scan)
    assert handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    heuristics = payload["results"][0]["injection_heuristics"]
    assert heuristics
    assert any(item["severity"] == "warning" for item in heuristics)
    assert all("line" in item and item["line"] >= 1 for item in heuristics)
    guard = payload["content_guard"][0]
    assert guard["injection_warning_count"] >= 1
    assert guard["injection_heuristics"]


def test_handoff_lint_content_guard_prints_injection_scope(tmp_path, capsys, monkeypatch):
    evil = FIXTURE_DIR / "evil-disregard-system.md"
    path = tmp_path / "evil.md"
    path.write_text(evil.read_text())

    monkeypatch.setattr(
        "brigade.scrub.run_scan",
        lambda *args, **kwargs: {
            "available": True,
            "status": "ok",
            "exit_code": 0,
            "detail": "clean",
            "stdout": "",
            "stderr": "",
        },
    )
    handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True)
    out = capsys.readouterr().out
    assert "leak scan + injection heuristics" in out
    assert "line " in out
    assert "disregard-system-prompt" in out or "classic-injection" in out
