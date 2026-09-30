"""Unexpandable ``~name`` tokens must not crash hook target resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from brigade import wiring
from brigade.claude_hooks import paths, runtime
from brigade.install import install_selection
from brigade.selection import Selection

_NO_USER = "~nosuchuser_zz"


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    return target


@pytest.fixture(autouse=True)
def _require_unknown_user():
    with pytest.raises(RuntimeError):
        Path(f"{_NO_USER}/x").expanduser()


def test_expand_user_path_keeps_unknown_user_literal():
    assert wiring.expand_user_path(Path(f"{_NO_USER}/x")) == Path(f"{_NO_USER}/x")


def test_resolve_command_path_treats_unknown_user_as_literal(tmp_path: Path):
    resolved = runtime._resolve_command_path(f"{_NO_USER}/x", tmp_path)

    assert resolved == (tmp_path / _NO_USER / "x").resolve()


def test_effective_hook_target_survives_unknown_user_token(tmp_path: Path):
    target = _wired_claude(tmp_path)
    payload = {
        "session_id": "x",
        "cwd": str(target),
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {_NO_USER}/x"},
    }

    assert runtime.effective_hook_target(payload) == target.resolve()


def test_file_path_with_unknown_user_resolves_under_cwd(tmp_path: Path):
    target = _wired_claude(tmp_path)
    payload = {
        "session_id": "x",
        "cwd": str(target),
        "tool_name": "Write",
        "tool_input": {"file_path": f"{_NO_USER}/f.txt"},
    }

    assert runtime.wired_target_from_payload(payload) == target.resolve()


def test_wired_target_from_payload_with_unknown_user_cwd(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    (target / _NO_USER).mkdir()
    monkeypatch.chdir(target)
    payload = {"session_id": "x", "cwd": _NO_USER, "tool_name": "Read", "tool_input": {}}

    assert runtime.wired_target_from_payload(payload) == target.resolve()


def test_resolve_wired_target_with_unknown_user(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    (target / _NO_USER).mkdir()
    monkeypatch.chdir(target)

    assert wiring.resolve_wired_target(_NO_USER) == target.resolve()
    assert wiring.resolve_wired_target(f"{_NO_USER}/missing") == target.resolve()


def test_resolved_path_with_unknown_user(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert paths.resolved_path(Path(_NO_USER)) == (tmp_path / _NO_USER).resolve()
    assert paths.is_operator_home(Path(_NO_USER)) is False


def test_effective_hook_target_with_pin_and_unknown_user_cwd(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    (target / _NO_USER).mkdir()
    monkeypatch.chdir(target)
    payload = {
        "session_id": "x",
        "cwd": _NO_USER,
        "tool_name": "Bash",
        "tool_input": {"command": f"ls {_NO_USER}/y"},
    }

    assert runtime.effective_hook_target(payload, pin=target) == target.resolve()
