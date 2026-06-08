import io
import pytest
from brigade.prompt import prompt_for_selection, NonInteractiveError


def _run_with_input(text, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)


def test_prompt_returns_defaults_on_empty_input(monkeypatch, capsys):
    """All defaults: claude harness, repo depth, no includes."""
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\n\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    sel = prompt_for_selection()
    assert sel.harnesses == ["claude"]
    assert sel.depth == "repo"
    assert sel.includes == []


def test_prompt_toggles_codex_and_openclaw(monkeypatch):
    """User types '2 6' to toggle codex and openclaw on (claude was default on)."""
    # Toggle loop re-prompts after each non-empty input; blank line confirms.
    # Harness: '2 6' toggles codex+openclaw on, blank line confirms.
    # (order is claude, codex, opencode, antigravity, pi, openclaw, hermes)
    # Depth: '2' picks workspace.
    # Includes: blank line confirms empty selection.
    monkeypatch.setattr("sys.stdin", io.StringIO("2 6\n\n2\n\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    sel = prompt_for_selection()
    assert set(sel.harnesses) == {"claude", "codex", "openclaw"}
    assert sel.depth == "workspace"  # depth choice 2
    assert sel.owner == "openclaw"


def test_prompt_errors_when_not_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(NonInteractiveError):
        prompt_for_selection()
