"""Yolo mode follows the verification target, not every observed root (#2235)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from brigade import cli
from brigade.claude_hooks import runtime, verification_mode
from brigade.install import install_selection
from brigade.selection import Selection
from tests._home import set_home


def _wired(path: Path, mode: str, capsys) -> Path:
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(path, selection) == 0
    if mode != "enforced":
        assert cli.main(["work", "mode", "set", mode, "--target", str(path)]) == 0
    capsys.readouterr()
    return path.resolve()


@pytest.fixture
def repos(tmp_path: Path, monkeypatch, capsys) -> dict[str, Path]:
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(runtime.heartbeat, "on_event", lambda *args: None)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *args: None)
    monkeypatch.setattr(runtime, "_run_recall", lambda *args, **kwargs: "")
    monkeypatch.setattr(runtime, "_run_brief", lambda *args: "work brief")
    return {
        "enforced": _wired(tmp_path / "enforced", "enforced", capsys),
        "other": _wired(tmp_path / "other", "enforced", capsys),
        "yolo": _wired(tmp_path / "yolo", "yolo", capsys),
        "plain": tmp_path / "plain",
    }


def _sh(path: Path) -> str:
    """A path as a bash command would carry it: forward slashes, shell-quoted (Windows paths too)."""
    return shlex.quote(path.as_posix())


def _bash(event: str, start: Path, command: str):
    return runtime.handle_payload(
        event,
        {
            "session_id": "vt",
            "cwd": str(start),
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
    )


def _denied(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def test_enforced_workspace_cd_into_yolo_repo_is_allowed(repos) -> None:
    command = f"cd {_sh(repos['yolo'])} && pytest -q"
    assert not _denied(_bash("PreToolUse", repos["enforced"], command))
    assert _bash("PostToolUseFailure", repos["enforced"], command) is None


def test_yolo_repo_cd_into_enforced_repo_is_denied(repos) -> None:
    result = _bash("PreToolUse", repos["yolo"], f"cd {_sh(repos['enforced'])} && pytest -q")
    assert _denied(result)


def test_enforced_workspace_cd_into_enforced_repo_is_denied(repos) -> None:
    assert _denied(_bash("PreToolUse", repos["enforced"], f"cd {_sh(repos['other'])} && pytest -q"))


def test_plain_check_follows_the_starting_directory(repos) -> None:
    assert not _denied(_bash("PreToolUse", repos["yolo"], "pytest -q"))
    assert _denied(_bash("PreToolUse", repos["enforced"], "pytest -q"))


def test_unresolvable_or_unwired_target_keeps_the_starting_mode(repos) -> None:
    for command in (
        "cd $REPO_DIR && pytest -q",
        f"cd {_sh(repos['plain'])} && pytest -q",
        f"(cd {_sh(repos['yolo'])} && pytest -q)",
        f"cd {_sh(repos['yolo'])} || true; pytest -q",
    ):
        # An enforced start stays enforced, and a yolo start is not newly denied.
        assert _denied(_bash("PreToolUse", repos["enforced"], command)), command
        assert not _denied(_bash("PreToolUse", repos["yolo"], command)), command


@pytest.mark.parametrize(
    "template",
    [
        "false && cd {yolo}; pytest -q",
        "cd {yolo}/missing; pytest -q",
        "cd {yolo} && true & pytest -q",
        "sleep 1 & cd {yolo} && pytest -q",
        "echo `pytest -q`; cd {yolo} && ruff check .",
        "if false; then cd {yolo}; fi; bash -c 'pytest -q'",
        "cd {yolo} && pushd {enforced} && pytest -q",
        "cd {yolo} && popd && pytest -q",
        "cd {yolo} && env -C {enforced} pytest -q",
        "cd {yolo} && env --chdir={enforced} pytest -q",
        "cd {yolo} && make -C {enforced} test",
        "cd {yolo} && env -C {enforced} bash -c 'pytest -q'",
        "cd {yolo} && cd .. && pytest -q",
        "cd {yolo}/.. && pytest -q",
    ],
)
def test_a_cd_outside_a_plain_and_chain_never_promotes_an_enforced_start(repos, template: str) -> None:
    # Each shape can run a check outside the cd destination: a skipped or failed cd, a
    # backgrounded list, a substitution, or a control structure. So none may count as yolo.
    command = template.format(yolo=_sh(repos["yolo"]), enforced=_sh(repos["enforced"]))
    assert not verification_mode.verification_in_yolo(command, str(repos["enforced"]), repos["enforced"])
    assert _denied(_bash("PreToolUse", repos["enforced"], command))


def test_post_failure_follows_the_verification_target(repos) -> None:
    command = f"cd {_sh(repos['enforced'])} && pytest -q"
    result = _bash("PostToolUseFailure", repos["yolo"], command)
    assert result is not None and "brigade work verify run" in result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("pytest -q", ["start"]),
        ("cd {yolo} && pytest -q", ["yolo"]),
        ("cd {yolo}; cd {other} && pytest -q", [None]),
        ("cd {yolo} && pytest -q && cd {other} && ruff check .", [None]),
        ("cd {yolo} && pytest -q && bash -c 'cd {other} && ruff check .'", ["yolo", "other"]),
        ("cd {yolo}/.. && pytest -q", [None]),
        ("cd {yolo} && cd .. && pytest -q", [None]),
        ("bash -c 'cd {yolo} && pytest -q'", ["yolo"]),
        ("cd {yolo} | cat; pytest -q", [None]),
        ("cd {yolo} & pytest -q", [None]),
        ("sleep 1 & cd {yolo} && pytest -q", [None]),
        ("false && cd {yolo}; pytest -q", [None]),
        ("false || cd {yolo}; pytest -q", [None]),
        ("true && cd {yolo} && pytest -q", [None]),
        ("true && cd {yolo} && pytest -q; ruff check .", [None]),
        ("cd {yolo} && bash -c 'pytest -q'", ["yolo"]),
        ("cd {yolo} && echo $(pytest -q)", [None]),
        ("cd {yolo} || exit 1; pytest -q", [None]),
        ("cd $DIR && pytest -q", [None]),
        ("cd - && pytest -q", [None]),
        ("cd && pytest -q", [None]),
        ("{{ cd {yolo}; pytest -q; }}", [None]),
        ("echo hello", []),
    ],
)
def test_verifier_cwds(repos, template: str, expected: list) -> None:
    names = {"yolo": repos["yolo"], "other": repos["other"], "start": repos["enforced"]}
    command = template.format(yolo=_sh(repos["yolo"]), other=_sh(repos["other"]))
    want = [names[item] if item else None for item in expected]
    assert verification_mode.verifier_cwds(command, repos["enforced"]) == want


def test_every_verification_in_a_command_must_be_yolo(repos) -> None:
    mixed = f"cd {_sh(repos['enforced'])} && pytest -q && bash -c 'cd {_sh(repos['yolo'])} && ruff check .'"
    assert not verification_mode.verification_in_yolo(mixed, str(repos["yolo"]), repos["yolo"])
    both = f"cd {_sh(repos['yolo'])} && pytest -q && bash -c 'ruff check .'"
    assert verification_mode.verification_in_yolo(both, str(repos["enforced"]), repos["enforced"])


def test_missing_cwd_falls_back_to_the_handled_root(repos) -> None:
    assert verification_mode.verification_in_yolo("pytest -q", None, repos["yolo"])
    assert not verification_mode.verification_in_yolo("pytest -q", None, repos["enforced"])
