"""Yolo mode follows the verification target, not every observed root (#2235)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from brigade import cli
from brigade.claude_hooks import runtime, observed_verifiers
from brigade.install import install_selection
from brigade.selection import Selection
from tests._home import set_home


def _wired(path: Path, mode: str, capsys) -> Path:
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(path, selection) == 0
    if mode != "engineer":
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
        "enforced": _wired(tmp_path / "enforced", "engineer", capsys),
        "other": _wired(tmp_path / "other", "engineer", capsys),
        "yolo": _wired(tmp_path / "yolo", "yolo", capsys),
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
        "cd {yolo} && eval 'cd {enforced}' && pytest -q",
        "cd {yolo} && source ./env.sh && pytest -q",
        "cd yolo && pytest -q",
        "cd {yolo} && uv run bash -c 'cd {enforced} && pytest -q'",
        "cd {yolo} && npx sh -c 'pytest -q'",
        "cd {yolo} && npm test --workspace=packages/enforced",
        "cd {yolo} && npm test -w packages/enforced",
        "cd {yolo} && pnpm --filter enforced test",
        "cd {yolo} && python -c 'import os; os.chdir(\"/\")' && pytest -q",
    ],
)
def test_uncertain_cd_keeps_the_handled_root(repos, template: str) -> None:
    # Each shape can run a check outside the cd destination: a skipped or failed cd, a
    # backgrounded list, a substitution, or a control structure.
    command = template.format(yolo=_sh(repos["yolo"]), enforced=_sh(repos["enforced"]))
    start = repos["enforced"]
    assert observed_verifiers.governing_modes(command, str(start), start) == [(start, "strict")]


def test_post_failure_follows_the_verification_target(repos) -> None:
    command = f"cd {_sh(repos['enforced'])} && pytest -q"
    result = _bash("PostToolUseFailure", repos["yolo"], command)
    assert result is not None and "brigade work verify run" in result["hookSpecificOutput"]["additionalContext"]
    command = f"cd {_sh(repos['yolo'])} && pytest -q"
    assert _bash("PostToolUseFailure", repos["enforced"], command) is None


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("pytest -q", ["start"]),
        ("cd {yolo} && pytest -q", ["yolo"]),
        ("cd {yolo}; cd {other} && pytest -q", [None]),
        ("cd {yolo} && pytest -q && cd {other} && ruff check .", [None]),
        ("cd {yolo} && pytest -q && ruff check .", ["yolo", "yolo"]),
        ("cd {yolo} && uv run pytest -q", ["yolo"]),
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
        ("cd {yolo} && bash -c 'pytest -q'", [None]),
        ("cd {yolo} && echo hi && pytest -q", [None]),
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
    assert observed_verifiers.verifier_cwds(command, repos["enforced"]) == want


def test_a_dot_slash_cd_resolves_the_child(repos, capsys) -> None:
    # A bare `cd child` may go through CDPATH, but `cd ./child` never does.
    start = repos["enforced"]
    child = _wired(start / "child", "yolo", capsys)
    assert observed_verifiers.governing_modes("cd ./child && pytest -q", str(start), start) == [(child, "off")]
    assert observed_verifiers.governing_modes("cd child && pytest -q", str(start), start) == [(start, "strict")]


def test_a_workspace_subcommand_is_a_directory_override() -> None:
    # `yarn workspace <name> test` runs in a member package, wherever the shell is.
    assert observed_verifiers._overrides_cwd(["yarn", "workspace", "enforced", "test"])
    assert not observed_verifiers._overrides_cwd(["pytest", "-q"])


def test_missing_cwd_falls_back_to_the_handled_root(repos) -> None:
    root = repos["yolo"]
    assert observed_verifiers.governing_modes("pytest -q", None, root) == [(root, "off")]


def test_governing_modes_resolves_observe_repo(repos, capsys):
    observed = _wired(repos["enforced"] / "observed", "observe", capsys)
    command = "pytest && cd ./observed && pytest"
    assert observed_verifiers.governing_modes(command, str(repos["enforced"]), repos["enforced"]) == [
        (repos["enforced"], "strict"),
    ]
    assert observed_verifiers.governing_modes("cd ./observed && pytest", str(repos["enforced"]), repos["enforced"]) == [
        (observed, "observe")
    ]
    assert observed_verifiers.governing_modes("echo done", str(repos["enforced"]), repos["enforced"]) == []


def test_recognizes_focused_verify_by_exact_script_name():
    assert observed_verifiers.is_raw_verification("./scripts/verify-focused")
    assert not observed_verifiers.is_raw_verification("./scripts/verify-focused-extra")
