"""Verification planning accepts the same manifest command formats as running."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from tests.work_cmd_test_helpers import _init_git_repo

_SRC = Path(__file__).resolve().parents[1] / "src"


def _write_manifest(target: Path, commands: list[str | list[str]]) -> None:
    _init_git_repo(target)
    manifest = target / "verify" / "manifests" / "argv-plan.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": "argv-plan",
                "binding_mode": "patch_backed",
                "verifier_id": "brigade.verify.test",
                "subject": {
                    "artifact_kind": "skill",
                    "artifact_id": "brigade-work",
                    "subject_path": "check command.py",
                },
                "checks": [
                    {"check_id": f"check-{index}", "check_role": "effectiveness", "command": command}
                    for index, command in enumerate(commands)
                ],
            }
        ),
        encoding="utf-8",
    )
    (target / "check command.py").write_text(
        "from pathlib import Path\nPath('verifier-executed').write_text('ran')\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "verify/manifests/argv-plan.json"], cwd=target, check=True, capture_output=True)


def _verify(target: Path, *, operation: str = "plan", json_output: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(_SRC), env.get("PYTHONPATH"))))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "brigade",
            "work",
            "verify",
            operation,
            "--target",
            str(target),
            "--manifest",
            "argv-plan",
            *(["--json"] if json_output else []),
        ],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _target_files(target: Path) -> dict[str, bytes]:
    return {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(target).parts
    }


def _string_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("mixed_commands", [False, True])
def test_verify_plan_accepts_manifest_argv_without_executing_or_writing(tmp_path, json_output, mixed_commands):
    argv = [sys.executable, "check command.py", "space value", "literal;|&<>$", "quote'\"", r"literal\backslash"]
    commands: list[str | list[str]] = [argv]
    if mixed_commands:
        commands = [_string_command([sys.executable, "check command.py"]), *commands]
    _write_manifest(tmp_path, commands)
    before = _target_files(tmp_path)

    result = _verify(tmp_path, json_output=json_output)

    assert result.returncode == 0, result.stdout + result.stderr
    if json_output:
        plan = json.loads(result.stdout)
        assert plan["commands"] == commands
        assert plan["blockers"] == []
        assert plan["manifest_id"] == "argv-plan"
    else:
        assert f"commands: {len(commands)}" in result.stdout
        hint = next(
            line.removeprefix("run (POSIX shell): ")
            for line in result.stdout.splitlines()
            if line.startswith("run (POSIX shell): ")
        )
        assert shlex.split(hint) == [
            "brigade",
            "work",
            "verify",
            "run",
            f"--target={tmp_path.resolve()}",
            "--manifest=argv-plan",
        ]
    assert _target_files(tmp_path) == before


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (["./missing-verifier"], "verification command is not resolvable: ./missing-verifier"),
        (["bash", "check command.py"], "high-risk verification command: bash"),
        ([sys.executable, "check command.py", "bad\x00argument"], "verification command contains NUL characters"),
        (["bad\x00executable"], "verification command contains NUL characters"),
    ],
)
def test_verify_plan_reports_manifest_argv_blockers_as_json(tmp_path, command, error):
    _write_manifest(tmp_path, [command])
    before = _target_files(tmp_path)

    result = _verify(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["commands"] == [command]
    assert len(plan["blockers"]) == 1
    assert error in plan["blockers"][0]
    assert "Traceback" not in result.stderr
    assert _target_files(tmp_path) == before


def test_verify_plan_preserves_string_command_metacharacter_guard_in_mixed_manifest(tmp_path):
    argv = [sys.executable, "check command.py", "literal;argument"]
    command = _string_command(argv)
    _write_manifest(tmp_path, [argv, command])
    before = _target_files(tmp_path)

    result = _verify(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["commands"] == [argv, command]
    assert len(plan["blockers"]) == 1
    assert "high-risk verification command contains shell metacharacters" in plan["blockers"][0]
    assert _target_files(tmp_path) == before


@pytest.mark.parametrize("command", [[sys.executable, "check command.py", "bad\x00argument"], ["bad\x00executable"]])
def test_verify_run_rejects_manifest_argv_with_nul_without_traceback(tmp_path, command):
    _write_manifest(tmp_path, [command])

    result = _verify(tmp_path, operation="run")

    assert result.returncode == 2, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "rejected"
    assert receipt["commands"][0]["status"] == "rejected"
    assert receipt["commands"][0]["stderr_summary"] == "verification command contains NUL characters"
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "verifier-executed").exists()
