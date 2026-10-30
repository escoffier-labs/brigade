"""Suggested verification commands retain the selected checks and target."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from brigade import cli
from brigade.work_cmd import verify_ranking

_SRC = Path(__file__).resolve().parents[1] / "src"


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(_SRC), env.get("PYTHONPATH"))))
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def _command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "target's workspace"
    caller = tmp_path / "caller"
    target.mkdir()
    caller.mkdir()
    (target / "check script.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "with Path('executed.jsonl').open('a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    (target / "tests").mkdir()
    (target / "tests/test_selected.py").write_text(
        "from pathlib import Path\ndef test_selected():\n    Path('default-executed').write_text('ran')\n",
        encoding="utf-8",
    )
    return target, caller


def _plan(
    target: Path, caller: Path, options: list[str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brigade",
            "work",
            "verify",
            "plan",
            "--target",
            str(target),
            "--json",
            *(options or []),
        ],
        cwd=caller,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, json.loads(result.stdout)


def _run_hint(hint: str, caller: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    # The hint explicitly identifies its POSIX syntax. Decode that transport,
    # then exercise the real CLI process on every platform.
    argv = shlex.split(hint)
    assert argv[:4] == ["brigade", "work", "verify", "run"]
    result = subprocess.run(
        [sys.executable, "-m", "brigade", *argv[1:], "--json"],
        cwd=caller,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.stdout, result.stderr
    return result, json.loads(result.stdout)


def test_default_plan_hint_runs_selected_default_in_the_target(tmp_path):
    target, caller = _project(tmp_path)

    result, plan = _plan(target, caller)

    assert result.returncode == 0, result.stdout + result.stderr
    assert plan["commands"] == ["python3 -m pytest -q"]
    assert not (target / "default-executed").exists()
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 0, run.stdout + run.stderr
    assert receipt["target"] == str(target)
    assert receipt["planned_commands"] == plan["commands"]
    assert (target / "default-executed").read_text() == "ran"
    assert not (caller / "default-executed").exists()


def test_explicit_repeated_commands_are_preserved_in_order(tmp_path):
    target, caller = _project(tmp_path)
    arguments = [["one value", "it's quoted", r"back\slash"], ["second", "--literal"]]
    commands = [_command([sys.executable, "check script.py", *args]) for args in arguments]
    options = [option for command in commands for option in [f"--command={command}"]]

    result, plan = _plan(target, caller, options)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (target / "executed.jsonl").exists()
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 0, run.stdout + run.stderr
    assert receipt["planned_commands"] == commands
    assert [json.loads(line) for line in (target / "executed.jsonl").read_text().splitlines()] == arguments
    assert not (target / "default-executed").exists()
    assert "--argv-json" not in shlex.split(plan["suggested_command"])


@pytest.mark.parametrize("command", ["", "-missing-verifier"])
def test_blocked_command_hint_preserves_empty_or_option_like_command(tmp_path, command):
    target, caller = _project(tmp_path)

    result, plan = _plan(target, caller, [f"--command={command}"])

    assert result.returncode == 1
    assert plan["blockers"]
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 2
    assert receipt["status"] == "rejected"
    assert receipt["planned_commands"] == [command]
    assert "requires exactly one" not in run.stderr
    assert not (target / "executed.jsonl").exists()


def test_hint_keeps_string_command_metacharacter_rejection(tmp_path):
    target, caller = _project(tmp_path)
    command = _command([sys.executable, "check script.py", "literal;argument"])

    result, plan = _plan(target, caller, [f"--command={command}"])

    assert result.returncode == 1
    assert "shell metacharacters" in plan["blockers"][0]
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 2
    assert receipt["commands"][0]["status"] == "rejected"
    assert "shell metacharacters" in receipt["commands"][0]["stderr_summary"]
    assert not (target / "executed.jsonl").exists()


@pytest.mark.parametrize("selector_kind", ["id", "path"])
def test_manifest_hint_preserves_selector_and_target(tmp_path, selector_kind):
    target, caller = _project(tmp_path)
    relative = "verify/manifests/local fixture.json"
    manifest = target / relative
    manifest.parent.mkdir(parents=True)
    manifest_id = "-quoted ' manifest"
    manifest.write_text(
        json.dumps(
            {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": manifest_id,
                "binding_mode": "patch_backed",
                "verifier_id": "brigade.verify.test",
                "subject": {
                    "artifact_kind": "skill",
                    "artifact_id": "brigade-work",
                    "subject_path": "check script.py",
                },
                "checks": [
                    {
                        "check_id": "check",
                        "check_role": "effectiveness",
                        "command": _command([sys.executable, "check script.py", "manifest"]),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        cwd=target,
        check=True,
    )
    selector = manifest_id if selector_kind == "id" else relative

    result, plan = _plan(target, caller, [f"--manifest={selector}"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"--manifest={selector}" in shlex.split(plan["suggested_command"])
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 0, run.stdout + run.stderr
    assert receipt["verify_manifest_id"] == manifest_id
    assert [json.loads(line) for line in (target / "executed.jsonl").read_text().splitlines()] == [["manifest"]]


@pytest.mark.parametrize("explicit", [False, True])
def test_graph_hint_priority_preserves_explicit_choice(tmp_path, monkeypatch, capsys, explicit):
    target, caller = _project(tmp_path)
    ranked = "python3 -m pytest -q tests/test_selected.py"
    command = _command([sys.executable, "check script.py", "explicit"])
    monkeypatch.setattr(
        verify_ranking,
        "rank_verification_candidates",
        lambda *_a, **_k: {"suggested_command": ranked, "candidates": [], "degraded": False},
    )
    args = ["work", "verify", "plan", "--target", str(target), "--json"]
    if explicit:
        args.append(f"--command={command}")

    assert cli.main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    run, receipt = _run_hint(plan["suggested_command"], caller)
    assert run.returncode == 0, run.stdout + run.stderr
    assert receipt["planned_commands"] == [command if explicit else ranked]
    assert bool(plan.get("suggested_from_graph_impact")) is not explicit
    assert plan["commands"] == ([command] if explicit else ["python3 -m pytest -q"])


@pytest.mark.skipif(os.name == "nt", reason="executes the explicitly labelled POSIX shell hint")
def test_posix_shell_hint_quotes_target_without_expanding_it(tmp_path):
    shell = shutil.which("sh")
    assert shell is not None
    target, caller = _project(tmp_path)
    destination = tmp_path / "target $(touch injected) `touch injected2` & 'quoted'"
    target.rename(destination)
    command = _command([sys.executable, "check script.py", "literal"])
    result, plan = _plan(destination, caller, [f"--command={command}"])
    assert result.returncode == 0
    assert plan["suggested_command_shell"] == "posix"

    run = subprocess.run(
        [shell, "-c", plan["suggested_command"] + " --json"],
        cwd=caller,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["target"] == str(destination)
    assert not (caller / "injected").exists()
    assert not (caller / "injected2").exists()
    assert (destination / "executed.jsonl").is_file()


def test_empty_plan_and_missing_manifest_keep_actionable_selector(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    caller = tmp_path
    result, plan = _plan(target, caller)
    assert result.returncode == 1
    assert "--command=..." in shlex.split(plan["suggested_command"])
    assert f"--target={target}" in shlex.split(plan["suggested_command"])

    result, plan = _plan(target, caller, ["--manifest=-missing ' manifest"])
    assert result.returncode == 1
    assert "--manifest=-missing ' manifest" in shlex.split(plan["suggested_command"])
    assert f"--target={target}" in shlex.split(plan["suggested_command"])


def test_text_plan_labels_a_runnable_posix_hint(tmp_path):
    target, caller = _project(tmp_path)
    command = _command([sys.executable, "check script.py", "text"])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brigade",
            "work",
            "verify",
            "plan",
            f"--target={target}",
            f"--command={command}",
        ],
        cwd=caller,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "run (POSIX shell): "
    hint = next(line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix))
    run, receipt = _run_hint(hint, caller)
    assert run.returncode == 0, run.stdout + run.stderr
    assert receipt["planned_commands"] == [command]
    assert (target / "executed.jsonl").is_file()
