from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import handoff_cmd
from brigade.untrusted import scan_handoff_injection_heuristics

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "handoff_lint" / "injection"

EVIL_FIXTURES = sorted(FIXTURE_DIR.glob("evil-*.md"))
BENIGN_FIXTURES = sorted(FIXTURE_DIR.glob("benign-*.md"))


def _clean_egress_scan(*args, **kwargs):
    return {
        "available": True,
        "status": "ok",
        "exit_code": 0,
        "detail": "clean",
        "stdout": "",
        "stderr": "",
    }


def _leaky_egress_scan(*args, **kwargs):
    return {
        "available": True,
        "status": "blocked",
        "exit_code": 1,
        "detail": "content-guard reported findings",
        "stdout": "finding in handoff",
        "stderr": "",
    }


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


def test_handoff_lint_content_guard_fails_on_injection_laden_egress_clean(tmp_path, capsys, monkeypatch):
    evil = FIXTURE_DIR / "evil-ignore-previous.md"
    path = tmp_path / "evil.md"
    path.write_text(evil.read_text())

    monkeypatch.setattr("brigade.scrub.run_scan", _clean_egress_scan)
    assert handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["content_guard_egress"] == "ok"
    assert payload["content_guard_injection"] == "fail"
    heuristics = payload["results"][0]["injection_heuristics"]
    assert heuristics
    assert any(item["severity"] == "warning" for item in heuristics)
    assert all("line" in item and item["line"] >= 1 for item in heuristics)
    guard = payload["content_guard"][0]
    assert guard["egress_verdict"] == "ok"
    assert guard["injection_verdict"] == "fail"
    assert guard["injection_warning_count"] >= 1
    assert guard["injection_heuristics"]


def test_handoff_lint_content_guard_fails_on_egress_leaky_injection_free(tmp_path, capsys, monkeypatch):
    benign = FIXTURE_DIR / "benign-about-injection.md"
    path = tmp_path / "benign.md"
    path.write_text(benign.read_text())

    monkeypatch.setattr("brigade.scrub.run_scan", _leaky_egress_scan)
    assert handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["content_guard_egress"] == "fail"
    assert payload["content_guard_injection"] == "ok"
    guard = payload["content_guard"][0]
    assert guard["egress_verdict"] == "fail"
    assert guard["injection_verdict"] == "ok"
    assert guard["exit_code"] == 1
    assert guard["injection_warning_count"] == 0


def test_handoff_lint_content_guard_clean_passes_with_both_axes_named(tmp_path, capsys, monkeypatch):
    benign = FIXTURE_DIR / "benign-about-injection.md"
    path = tmp_path / "benign.md"
    path.write_text(benign.read_text())

    monkeypatch.setattr("brigade.scrub.run_scan", _clean_egress_scan)
    assert handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["content_guard_egress"] == "ok"
    assert payload["content_guard_injection"] == "ok"
    guard = payload["content_guard"][0]
    assert guard["egress_verdict"] == "ok"
    assert guard["injection_verdict"] == "ok"

    handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True)
    out = capsys.readouterr().out
    assert "content_guard egress:" in out
    assert "content_guard injection:" in out
    assert "[ok] content_guard egress:" in out
    assert "[ok] content_guard injection:" in out


def test_handoff_lint_content_guard_info_only_injection_does_not_fail(tmp_path, capsys, monkeypatch):
    benign = FIXTURE_DIR / "benign-quoted-payload.md"
    path = tmp_path / "benign.md"
    path.write_text(benign.read_text())

    monkeypatch.setattr("brigade.scrub.run_scan", _clean_egress_scan)
    assert handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["content_guard_injection"] == "ok"
    guard = payload["content_guard"][0]
    assert guard["injection_verdict"] == "ok"
    assert guard["injection_warning_count"] == 0
    assert any(hit["severity"] == "info" for hit in guard["injection_heuristics"])


def test_handoff_lint_content_guard_prints_injection_scope(tmp_path, capsys, monkeypatch):
    evil = FIXTURE_DIR / "evil-disregard-system.md"
    path = tmp_path / "evil.md"
    path.write_text(evil.read_text())

    monkeypatch.setattr("brigade.scrub.run_scan", _clean_egress_scan)
    handoff_cmd.lint(target=tmp_path, paths=[path], content_guard=True)
    out = capsys.readouterr().out
    assert "egress leak scan + injection heuristics" in out
    assert "[ok] content_guard egress:" in out
    assert "[fail] content_guard injection:" in out
    assert "line " in out
    assert "disregard-system-prompt" in out or "classic-injection" in out
