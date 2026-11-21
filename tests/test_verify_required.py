from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli
from brigade.work_cmd import verify_required


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def write_config(repo: Path, *, planner: list[str] | None = None) -> None:
    config = {
        "schema": "brigade.verify_required_config.v1",
        "planner_argv": planner
        or [sys.executable, "planner.py", "--base", "{base}", "--head", "{head}", "--output", "{output}"],
        "mode_key": "mode",
        "selected_key": "selected",
        "digest_key": "selector_digest",
        "run_argv": ["focused", "{tests}"],
    }
    path = repo / "verify" / "required-tests.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def planner(repo: Path, body: str) -> None:
    (repo / "planner.py").write_text(
        "import json, pathlib, sys, time\nout = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n" + body + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test User")
    write_config(tmp_path)
    planner(
        tmp_path,
        "out.write_text(json.dumps({'mode': 'selective', 'selected': ['tests/z.py', 'tests/a.py'], "
        "'selector_digest': 'digest'}))",
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "change.py").write_text("changed = True\n", encoding="utf-8")
    git(tmp_path, "add", "change.py")
    git(tmp_path, "commit", "-qm", "head")
    return tmp_path, base


def test_selective_plan_sorts_tests_prints_command_and_writes_record(repo, capsys):
    target, base = repo
    assert cli.main(["work", "verify", "required", "--target", str(target), "--base", base]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "focused tests/a.py tests/z.py"
    head = git(target, "rev-parse", "HEAD")
    record = json.loads((target / ".brigade/work/verify-required" / f"{head}.json").read_text())
    assert record["tests"] == ["tests/a.py", "tests/z.py"]
    assert record["local_requirement"] == "tests"
    assert record["covers_worktree"] is True
    assert cli.main(["work", "verify", "required", "--target", str(target), "--base", base, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["covers_worktree"] is True


def test_full_plan_requires_ci_and_never_proposes_local_suite(repo, capsys):
    target, base = repo
    planner(target, "out.write_text(json.dumps({'mode': 'full', 'selected': ['tests/a.py'], 'selector_digest': 'd'}))")
    assert cli.main(["work", "verify", "required", "-t", str(target), "--base", base]) == 0
    output = capsys.readouterr().out
    assert "Required CI on head commit" in output
    assert "Do not run the full suite locally" in output
    assert "focused" not in output
    record = json.loads(next((target / ".brigade/work/verify-required").glob("*.json")).read_text())
    assert record["local_requirement"] == "ci"


def test_worktree_config_change_cannot_replace_base_config(repo, capsys):
    target, base = repo
    write_config(target, planner=[sys.executable, "missing-planner.py", "{output}"])
    assert cli.main(["work", "verify", "required", "-t", str(target), "--base", base]) == 0
    assert "focused tests/a.py tests/z.py" in capsys.readouterr().out


def test_missing_base_config_is_unconfigured(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "tracked").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    write_config(tmp_path)
    planner(tmp_path, "out.write_text('{}')")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "head")
    assert cli.main(["work", "verify", "required", "-t", str(tmp_path), "--base", base, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "unconfigured"


@pytest.mark.parametrize(
    ("body", "timeout"),
    [
        ("raise SystemExit(7)", False),
        ("time.sleep(1)", True),
        ("out.write_text('not json')", False),
    ],
)
def test_planner_failures_are_errors(repo, capsys, monkeypatch, body, timeout):
    target, base = repo
    planner(target, body)
    if timeout:
        monkeypatch.setattr(verify_required, "PLANNER_TIMEOUT_SECONDS", 0.01)
    assert cli.main(["work", "verify", "required", "-t", str(target), "--base", base, "--json"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "error"
    assert payload["local_requirement"] == "ci"


def test_dirty_worktree_is_not_covered(repo, capsys):
    target, base = repo
    (target / "change.py").write_text("dirty = True\n")
    assert cli.main(["work", "verify", "required", "-t", str(target), "--base", base, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["covers_worktree"] is False


def test_test_placeholder_expands_to_distinct_argv_without_shell(repo, capsys):
    target, base = repo
    planner(
        target,
        "out.write_text(json.dumps({'mode': 'selective', 'selected': ['tests/a file.py', 'tests/x;echo.py'], "
        "'selector_digest': 'd'}))",
    )
    assert cli.main(["work", "verify", "required", "-t", str(target), "--base", base]) == 0
    output = capsys.readouterr().out
    argv = ["focused", "tests/a file.py", "tests/x;echo.py"]
    # The hint is quoted for the host shell family, so the expected text differs on Windows.
    expected = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    assert output.splitlines()[0] == expected
