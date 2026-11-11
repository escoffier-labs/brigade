"""Reuse must bind file arguments that Git's tree fingerprint cannot see."""

import json
import os
import subprocess
import sys

import pytest

from brigade.work_cmd import verification


@pytest.fixture
def verify_target(tmp_path, monkeypatch):
    target = tmp_path / "checkout"
    target.mkdir()
    (target / ".gitignore").write_text(".brigade/\n")
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
    )
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_BIN", str(tmp_path / "missing-graph"))
    return target


def run_script(target, script):
    assert verification.verify_run(target=target, commands=[[sys.executable, str(script)]], timeout=30) == 0
    return verification._verify_receipts(target)[0]


@pytest.mark.parametrize("ignored", [False, True], ids=["out_of_tree", "gitignored"])
def test_changed_script_executes_again(verify_target, tmp_path, ignored):
    script = (verify_target if ignored else tmp_path) / "verify.py"
    if ignored:
        (verify_target / ".gitignore").write_text(".brigade/\nverify.py\n")
    script.write_text("print('before')\n")
    first = run_script(verify_target, script)
    script.write_text("print('after')\n")
    second = run_script(verify_target, script)
    assert first["tree_fingerprint"] == second["tree_fingerprint"]
    assert "reused_from" not in second
    assert second["commands"][0]["stdout_summary"] == "after"


def test_unchanged_out_of_tree_script_reuses(verify_target, tmp_path, capsys):
    script = tmp_path / "verify.py"
    script.write_text("print('unchanged')\n")
    first = run_script(verify_target, script)
    second = run_script(verify_target, script)
    third = run_script(verify_target, script)
    assert second["reused_from"] == first["run_id"]
    assert third["reused_from"] == second["run_id"]
    assert "--no-reuse" in capsys.readouterr().out


def test_legacy_receipt_cannot_reuse_external_file(verify_target, tmp_path):
    script = tmp_path / "verify.py"
    script.write_text("print('ok')\n")
    first = run_script(verify_target, script)
    receipt_path = verification.helpers._verify_runs_root(verify_target) / first["run_id"] / "receipt.json"
    first.pop("argv_file_hashes", None)
    receipt_path.write_text(json.dumps(first))
    assert "reused_from" not in run_script(verify_target, script)


@pytest.mark.parametrize("kind", ["directory", "oversized", "unreadable"])
def test_unhashable_argument_disables_reuse(verify_target, tmp_path, monkeypatch, kind):
    from pathlib import Path

    from brigade.work_cmd import verify_argv_files

    script = tmp_path / "verify.py"
    script.write_text("print('ok')\n")
    argument = tmp_path / "input"
    if kind == "directory":
        argument.mkdir()
    else:
        argument.write_bytes(b"abc")
    if kind == "oversized":
        monkeypatch.setattr(verify_argv_files, "MAX_ARGV_FILE_BYTES", 2)
    if kind == "unreadable":
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == argument:
                raise PermissionError("test unreadable input")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    commands = [[sys.executable, str(script), str(argument)]]
    assert verification.verify_run(target=verify_target, commands=commands) == 0
    assert verification.verify_run(target=verify_target, commands=commands) == 0
    assert "reused_from" not in verification._verify_receipts(verify_target)[0]


@pytest.mark.skipif(sys.platform == "win32", reason="executable shebang requires POSIX")
def test_changed_script_on_path_executes_again(verify_target, tmp_path, monkeypatch):
    script = tmp_path / "verify-probe"
    script.write_text(f"#!{sys.executable}\nprint('before')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    commands = [[script.name]]
    assert verification.verify_run(target=verify_target, commands=commands) == 0
    script.write_text(f"#!{sys.executable}\nprint('after')\n")
    assert verification.verify_run(target=verify_target, commands=commands) == 0
    receipt = verification._verify_receipts(verify_target)[0]
    assert "reused_from" not in receipt
    assert receipt["commands"][0]["stdout_summary"] == "after"
