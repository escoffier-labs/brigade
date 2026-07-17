import argparse
import json
from pathlib import Path

from brigade import aboyeur, cli
from brigade import roster as roster_mod
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


def test_run_detach_child_records_artifact_identity_in_lock(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write_roster(home)
    workspace_roster = tmp_path / ".brigade" / "roster.toml"
    user_roster = home / ".brigade" / "roster.toml"
    seen = {}

    def fake_run(*args, output_dir=None, **kwargs):
        resolution = args[1].resolution
        seen["roster_path"] = str(resolution.path)
        seen["roster_source"] = resolution.source
        seen["roster_shadowed"] = [str(path) for path in resolution.shadowed]
        owner_path = tmp_path / ".brigade" / "run.lock" / "owner.json"
        seen.update(json.loads(owner_path.read_text()))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run.json").write_text(json.dumps({"status": "ok"}) + "\n")
        return 0

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            assert cli.main(argv[3:]) == 0

        def poll(self):
            return None

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(run_cli, "Popen", FakeProcess)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach"]) == 0

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    assert seen["run_dir"] == str(run_dir.resolve())
    assert isinstance(seen["owner_token"], str) and seen["owner_token"]
    assert seen["roster_path"] == str(workspace_roster.resolve())
    assert seen["roster_source"] == "workspace"
    assert seen["roster_shadowed"] == [str(user_roster.resolve())]
    assert not (tmp_path / ".brigade" / "run.lock").exists()


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
    roster_resolution = roster_mod.RosterResolution(
        path=(tmp_path / "roster.toml").resolve(),
        source="workspace",
        shadowed=((tmp_path / "user-roster.toml").resolve(),),
    )
    output_dir = tmp_path / "run"

    argv = run_cli._detached_child_argv(
        args,
        run_cwd=tmp_path,
        roster_resolution=roster_resolution,
        output_dir=output_dir,
    )

    assert "--worker" in argv
    assert argv[argv.index("--worker") + 1] == "coder"
    assert argv[argv.index("--wait") + 1] == "2.5"
    assert argv[argv.index("--resolved-roster-source") + 1] == "workspace"
    assert argv[argv.index("--resolved-roster-shadowed") + 1] == str(roster_resolution.shadowed[0])
