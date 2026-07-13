import argparse
import json
from pathlib import Path

from brigade import cli
from brigade.cli import run as run_cli


def _write_roster(target: Path) -> None:
    config_dir = target / ".brigade"
    config_dir.mkdir()
    (config_dir / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"
"""
    )


def test_run_detach_rejects_fixed_incompatibilities(tmp_path, capsys):
    _write_roster(tmp_path)

    for flag in ("--dry-run", "--no-artifacts", "--inspect"):
        rc = cli.main(["run", "x", "--cwd", str(tmp_path), "--detach", flag])
        assert rc == 2
        assert f"--detach cannot be used with {flag}" in capsys.readouterr().err


def test_run_detach_spawns_child_with_output_dir_and_log(tmp_path, monkeypatch, capsys):
    _write_roster(tmp_path)
    calls = []

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            output_dir = Path(argv[argv.index("--output-dir") + 1])
            assert output_dir.is_dir()
            assert "--detach" not in argv
            assert argv.count("--output-dir") == 1
            assert kwargs["cwd"] == tmp_path
            kwargs["stdout"].write("child started\n")
            kwargs["stdout"].flush()
            (output_dir / "run.json").write_text(json.dumps({"status": "started"}) + "\n")

        def poll(self):
            return None

    monkeypatch.setattr(run_cli, "Popen", FakeProcess)

    rc = cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach"])

    captured = capsys.readouterr()
    assert rc == 0
    assert len(calls) == 1
    run_dirs = list((tmp_path / ".brigade" / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "detached.log").read_text() == "child started\n"
    assert f"run: {run_dir.name}" in captured.out
    assert "detached: pid 4321" in captured.err
    assert f"artifacts: {run_dir}" in captured.err
    assert f"log: {run_dir / 'detached.log'}" in captured.err


def test_run_detach_reports_early_child_exit(tmp_path, monkeypatch, capsys):
    _write_roster(tmp_path)

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            kwargs["stdout"].write("boom\n")
            kwargs["stdout"].flush()

        def poll(self):
            return 7

    monkeypatch.setattr(run_cli, "Popen", FakeProcess)

    rc = cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach"])

    captured = capsys.readouterr()
    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    assert rc == 2
    assert "detached child exited before run metadata was written: exit 7" in captured.err
    assert f"log: {run_dir / 'detached.log'}" in captured.err
    assert (run_dir / "detached.log").read_text() == "boom\n"


def test_run_detach_child_argv_preserves_worker(tmp_path):
    args = argparse.Namespace(
        task="do work",
        allow_dirty=False,
        worktree=False,
        show_plan=False,
        verbose=False,
        read_only=False,
        no_code_graph=False,
        no_evidence=False,
        sandbox=None,
        codex_transport=None,
        handoff=False,
        handoff_inbox=None,
        worker="coder",
        wait=2.5,
    )
    roster_path = tmp_path / "roster.toml"
    output_dir = tmp_path / "run"

    argv = run_cli._detached_child_argv(
        args,
        run_cwd=tmp_path,
        roster_path=roster_path,
        output_dir=output_dir,
    )

    assert "--worker" in argv
    assert argv[argv.index("--worker") + 1] == "coder"
    assert argv[argv.index("--wait") + 1] == "2.5"
