import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from brigade import aboyeur
from brigade import agents
from brigade import cli
from brigade import proc
from brigade import runguard
from brigade import runs_cmd


def test_run_cli_missing_roster_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    rc = cli.main(["run", "do something"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "roster not found" in err
    assert str(tmp_path / ".brigade" / "roster.toml") in err
    assert str(tmp_path / "home" / ".brigade" / "roster.toml") in err


def test_run_cli_rejects_missing_cwd(tmp_path, capsys):
    rc = cli.main(["run", "do something", "--cwd", str(tmp_path / "missing")])
    assert rc == 2
    assert "--cwd is not a directory" in capsys.readouterr().err


def test_run_cli_loads_roster_and_dispatches(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "ollama:llama3.3"
role = "code"
"""
    )
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["task"] = task
        seen["orchestrator"] = loaded_roster.orchestrator
        seen["dry_run"] = dry_run
        seen["show_plan"] = show_plan
        seen["verbose"] = verbose
        seen["cwd"] = cwd
        seen["output_dir"] = output_dir
        seen["handoff_inbox"] = handoff_inbox
        seen["read_only"] = read_only
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    rc = cli.main(
        [
            "run",
            "do something",
            "--roster",
            str(roster_path),
            "--show-plan",
            "--verbose",
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "runs" / "one"),
            "--handoff",
            "--handoff-inbox",
            str(tmp_path / "handoffs"),
            "--read-only",
        ]
    )
    assert rc == 0
    assert seen == {
        "task": "do something",
        "orchestrator": "chef",
        "dry_run": False,
        "show_plan": True,
        "verbose": True,
        "cwd": tmp_path,
        "output_dir": tmp_path / "runs" / "one",
        "handoff_inbox": tmp_path / "handoffs",
        "read_only": True,
        "sandbox": None,
    }


def test_run_cli_passes_no_code_graph_to_aboyeur(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
        code_graph_enabled=True,
    ):
        seen["code_graph_enabled"] = code_graph_enabled
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)

    assert cli.main(["run", "x", "--no-artifacts", "--no-code-graph"]) == 0
    assert seen["code_graph_enabled"] is False


def test_run_cli_passes_no_evidence_to_aboyeur(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
        evidence_enabled=True,
    ):
        seen["evidence_enabled"] = evidence_enabled
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)

    assert cli.main(["run", "x", "--no-artifacts", "--no-evidence"]) == 0
    assert seen["evidence_enabled"] is False


def test_run_cli_default_sandbox_is_none(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox="unset",
    ):
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--no-artifacts"]) == 0
    assert seen["sandbox"] is None


def test_run_cli_passes_sandbox_to_aboyeur(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--sandbox", "danger-full-access", "--no-artifacts"]) == 0
    assert seen["sandbox"] == "danger-full-access"


def test_run_cli_uses_roster_sandbox_when_flag_absent(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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

[limits]
sandbox = "workspace-write"
"""
    )
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--no-artifacts"]) == 0
    assert seen["sandbox"] == "workspace-write"


def test_run_cli_sandbox_flag_overrides_roster_sandbox(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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

[limits]
sandbox = "workspace-write"
"""
    )
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--sandbox", "read-only", "--no-artifacts"]) == 0
    assert seen["sandbox"] == "read-only"


def test_run_cli_rejects_invalid_sandbox(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "x", "--sandbox", "none"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_run_cli_default_roster_path(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        aboyeur,
        "run",
        lambda task, loaded_roster, dry_run=False, show_plan=False, verbose=False, cwd=None, output_dir=None, handoff_inbox=None, read_only=False, sandbox=None: (
            0
        ),
    )
    assert cli.main(["run", json.dumps({"task": "x"}), "--dry-run"]) == 0


def test_run_cli_falls_back_to_home_roster_when_cwd_roster_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".brigade"
    config_dir.mkdir(parents=True)
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["orchestrator"] = loaded_roster.orchestrator
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--no-artifacts"]) == 0
    assert seen["orchestrator"] == "chef"


def test_run_cli_records_workspace_roster_provenance_and_shadow_warning(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace_roster = workspace / ".brigade" / "roster.toml"
    home = tmp_path / "home"
    user_roster = home / ".brigade" / "roster.toml"
    workspace_roster.parent.mkdir(parents=True)
    user_roster.parent.mkdir(parents=True)
    roster_text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "code"\n'
    )
    workspace_roster.write_text(roster_text)
    user_roster.write_text(roster_text)
    output_dir = tmp_path / "run"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        aboyeur.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect"}]}),
            ok=True,
        ),
    )

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(workspace),
            "--output-dir",
            str(output_dir),
            "--dry-run",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
    )

    assert rc == 0
    err = capsys.readouterr().err
    assert f"roster: {workspace_roster.resolve()} (workspace)" in err
    assert f"workspace roster {workspace_roster.resolve()} shadows user roster {user_roster.resolve()}" in err
    assert "--roster" in err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["roster"] == {
        "path": str(workspace_roster.resolve()),
        "source": "workspace",
        "shadowed": [str(user_roster.resolve())],
    }
    roster_meta = json.loads((output_dir / "roster.json").read_text())
    assert roster_meta["resolution"] == {
        "path": str(workspace_roster.resolve()),
        "source": "workspace",
        "shadowed": [str(user_roster.resolve())],
    }


def test_run_cli_explicit_direct_worker_reports_choice_without_shadow_warning(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    user_roster = home / ".brigade" / "roster.toml"
    workspace_roster = tmp_path / ".brigade" / "roster.toml"
    explicit_roster = tmp_path / "chosen.toml"
    user_roster.parent.mkdir(parents=True)
    workspace_roster.parent.mkdir(parents=True)
    roster_text = (
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.coder]\ncli = "codex"\nrole = "code"\n'
    )
    for path in (user_roster, workspace_roster, explicit_roster):
        path.write_text(roster_text)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(aboyeur, "run", lambda *args, **kwargs: 0)

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(tmp_path),
            "--roster",
            str(explicit_roster),
            "--worker",
            "coder",
            "--no-artifacts",
            "--read-only",
        ]
    )

    assert rc == 0
    err = capsys.readouterr().err
    assert f"roster: {explicit_roster.resolve()} (explicit)" in err
    assert "shadows user roster" not in err


def test_run_cli_identifies_fallback_roster_in_validation_error(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    roster_path = home / ".brigade" / "roster.toml"
    roster_path.parent.mkdir(parents=True)
    roster_path.write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "claude"
role = "plan"

[limits]
allow_models = ["codex"]
"""
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home)

    rc = cli.main(["run", "x", "--dry-run", "--no-artifacts"])

    assert rc == 2
    err = capsys.readouterr().err
    assert f"invalid roster at {roster_path}" in err
    assert "agents.chef.cli is not allowed by limits.allow_models" in err


def test_run_cli_explicit_roster_does_not_fall_back_to_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    config_dir = home / ".brigade"
    config_dir.mkdir(parents=True)
    (config_dir / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"
"""
    )
    missing = tmp_path / "missing.toml"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: home)
    rc = cli.main(["run", "x", "--roster", str(missing), "--no-artifacts"])
    assert rc == 2
    err = capsys.readouterr().err
    assert str(missing) in err
    assert str(config_dir / "roster.toml") not in err


def test_run_cli_rejects_handoff_with_dry_run(tmp_path, capsys, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "x", "--dry-run", "--handoff"])
    assert rc == 2
    assert "--handoff cannot be used with --dry-run" in capsys.readouterr().err


def test_run_cli_can_disable_artifacts(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
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
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["output_dir"] = output_dir
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--no-artifacts"]) == 0
    assert seen["output_dir"] is None


def test_run_cli_rejects_inspect_without_artifacts(tmp_path, capsys):
    rc = cli.main(["run", "x", "--cwd", str(tmp_path), "--inspect", "--no-artifacts"])
    assert rc == 2
    assert "--inspect cannot be used with --no-artifacts" in capsys.readouterr().err


def test_run_cli_inspect_shows_artifacts_and_preserves_run_code(tmp_path, monkeypatch, capsys):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
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
    output_dir = tmp_path / "run"
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["output_dir"] = output_dir
        return 2

    def fake_show(run_dir):
        seen["inspect_dir"] = run_dir
        print(f"summary for {run_dir}")
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runs_cmd, "show", fake_show)

    rc = cli.main(
        [
            "run",
            "x",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--inspect",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert seen == {"output_dir": output_dir, "inspect_dir": output_dir}
    assert f"summary for {output_dir}" in captured.out
    assert f"artifacts: {output_dir}" in captured.err


def test_run_cli_warns_on_suspected_noop_in_stderr_and_inspect_output(tmp_path, monkeypatch, capsys):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
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
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        output_dir.mkdir(parents=True)
        (output_dir / "run.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "task": task,
                    "cwd": str(tmp_path),
                    "read_only": False,
                    "dry_run": False,
                    "started_at": "2026-07-09T12:00:00Z",
                    "finished_at": "2026-07-09T12:00:01Z",
                    "duration_seconds": 1,
                    "artifacts": str(output_dir),
                    "suspected_noop": True,
                }
            )
            + "\n"
        )
        (output_dir / "roster.json").write_text(json.dumps({"orchestrator": "chef", "agents": {}}) + "\n")
        (output_dir / "plan.json").write_text(
            json.dumps({"assignments": [{"stage": 1, "worker": "coder", "task": "implement it"}]}) + "\n"
        )
        (output_dir / "worker-results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "worker": "coder",
                            "task": "implement it",
                            "ok": True,
                            "detail": "no-op",
                            "text": "worker output",
                        }
                    ],
                    "ground_truth": {
                        "available": True,
                        "changed_files": [],
                        "untracked_files": [],
                        "diffstat": "",
                        "patch_ref": None,
                        "suspected_noop": True,
                    },
                }
            )
            + "\n"
        )
        (output_dir / "synthesis.json").write_text(
            json.dumps({"orchestrator": "chef", "result": {"ok": True}, "ground_truth": {}}) + "\n"
        )
        (output_dir / "final.txt").write_text("final answer\n")
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)

    rc = cli.main(
        [
            "run",
            "x",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--inspect",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "warning: suspected no-op run" in captured.err
    assert "warning: suspected no-op run" in captured.out
    assert "[ok] coder: no-op" in captured.out


def _git(repo, *args):
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _git_repo_with_roster(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(
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
    _git(repo, "add", "tracked.txt", ".brigade/roster.toml")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_run_cli_dirty_guard_blocks_by_default(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n")

    def fail_run(*args, **kwargs):
        raise AssertionError("aboyeur.run should not be called")

    monkeypatch.setattr(aboyeur, "run", fail_run)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--no-artifacts"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "dirty worktree" in err
    assert "tracked.txt" in err
    assert "--allow-dirty" in err


def test_run_cli_allow_dirty_passes_dirty_guard(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n")
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["cwd"] = cwd
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)

    assert cli.main(["run", "x", "--cwd", str(repo), "--allow-dirty", "--no-artifacts"]) == 0
    assert seen["cwd"] == repo


def test_run_cli_lock_conflict_errors(tmp_path, monkeypatch, capsys):
    import os

    repo = _git_repo_with_roster(tmp_path)
    (repo / ".brigade" / "run.lock").mkdir()
    (repo / ".brigade" / "run.lock" / "pid").write_text(f"{os.getpid()}\n")

    def fail_run(*args, **kwargs):
        raise AssertionError("aboyeur.run should not be called")

    monkeypatch.setattr(aboyeur, "run", fail_run)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--no-artifacts"])

    assert rc == 2
    assert "another brigade run appears active" in capsys.readouterr().err


@pytest.mark.parametrize(("wait_arg", "expected"), [("--wait=0.25", 0.25), ("--wait", 600.0)])
def test_run_cli_passes_bounded_wait_to_run_lock(tmp_path, monkeypatch, wait_arg, expected):
    repo = _git_repo_with_roster(tmp_path)
    seen = {}

    @contextmanager
    def fake_lock(cwd, *, run_dir=None, wait_seconds=0.0):
        seen["cwd"] = cwd
        seen["run_dir"] = run_dir
        seen["wait_seconds"] = wait_seconds
        yield

    monkeypatch.setattr(runguard, "run_lock", fake_lock)
    monkeypatch.setattr(aboyeur, "run", lambda *args, **kwargs: 0)

    rc = cli.main(["run", "x", "--cwd", str(repo), wait_arg, "--no-artifacts"])

    assert rc == 0
    assert seen == {"cwd": repo, "run_dir": None, "wait_seconds": expected}


def test_run_cli_records_output_dir_in_run_lock(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run-artifacts"
    seen = {}

    @contextmanager
    def fake_lock(cwd, *, run_dir=None, wait_seconds=0.0):
        seen["cwd"] = cwd
        seen["run_dir"] = run_dir
        seen["wait_seconds"] = wait_seconds
        yield

    monkeypatch.setattr(runguard, "run_lock", fake_lock)
    monkeypatch.setattr(aboyeur, "run", lambda *args, **kwargs: 0)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir)])

    assert rc == 0
    assert seen == {"cwd": repo, "run_dir": output_dir, "wait_seconds": 0.0}


@pytest.mark.parametrize("lane", ["direct", "acpx"])
def test_direct_worker_run_records_dispatching_before_provider_call(tmp_path, monkeypatch, lane):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run-artifacts"
    seen = {}
    worker = "coder"
    if lane == "acpx":
        worker = "composer"
        (repo / ".brigade" / "roster.toml").write_text(
            """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.composer]
cli = "cursor"
model = "composer-2.5"
transport = "acpx"
transport_version = "0.12.0"
role = "code"
"""
        )

    def fake_dispatch(*args, **kwargs):
        seen.update(json.loads((output_dir / "run.json").read_text()))
        return [
            aboyeur.WorkerResult(
                worker=worker,
                task="inspect",
                text="done",
                ok=True,
            )
        ]

    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(repo),
            "--output-dir",
            str(output_dir),
            "--worker",
            worker,
            "--allow-dirty",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
    )

    assert rc == 0
    assert seen["status"] == "dispatching"


def test_orchestrated_run_records_planning_and_synthesizing_phases(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run-artifacts"
    seen = {}

    def fake_plan(*args, **kwargs):
        seen["plan"] = json.loads((output_dir / "run.json").read_text())["status"]
        return [aboyeur.Assignment(worker="coder", task="inspect")]

    def fake_dispatch(*args, **kwargs):
        return [aboyeur.WorkerResult(worker="coder", task="inspect", text="done", ok=True)]

    def fake_orchestrator(*args, **kwargs):
        seen["synthesis"] = json.loads((output_dir / "run.json").read_text())["status"]
        return agents.AgentResult(text="final", ok=True)

    monkeypatch.setattr(aboyeur, "plan", fake_plan)
    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)
    monkeypatch.setattr(aboyeur, "_run_orchestrator", fake_orchestrator)

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(repo),
            "--output-dir",
            str(output_dir),
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
    )

    assert rc == 0
    assert seen == {"plan": "planning", "synthesis": "synthesizing"}


def test_app_server_and_control_start_after_dispatching_is_recorded(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run-artifacts"
    seen = {}

    class StubAppServer:
        def __init__(self, *, cwd):
            self.cwd = cwd

        def start(self):
            seen["app_server"] = json.loads((output_dir / "run.json").read_text())["status"]

        def close(self):
            pass

    class StubControlServer:
        def __init__(self, socket_path, registry):
            self.socket_path = socket_path
            self.registry = registry

        def start(self):
            seen["control_server"] = json.loads((output_dir / "run.json").read_text())["status"]

        def close(self):
            pass

    def fake_dispatch(*args, **kwargs):
        return [aboyeur.WorkerResult(worker="coder", task="inspect", text="done", ok=True)]

    monkeypatch.setattr(aboyeur.codex_appserver, "AppServer", StubAppServer)
    monkeypatch.setattr(aboyeur.run_control, "ControlServer", StubControlServer)
    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(repo),
            "--output-dir",
            str(output_dir),
            "--worker",
            "coder",
            "--codex-transport",
            "app-server",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
    )

    assert rc == 0
    assert seen == {"app_server": "dispatching", "control_server": "dispatching"}


def test_app_server_fallback_clears_uncreated_control_socket(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run-artifacts"

    class UnavailableAppServer:
        def __init__(self, *, cwd):
            self.cwd = cwd

        def start(self):
            raise aboyeur.codex_appserver.AppServerError("unavailable")

    def fake_dispatch(*args, **kwargs):
        return [aboyeur.WorkerResult(worker="coder", task="inspect", text="done", ok=True)]

    monkeypatch.setattr(aboyeur.codex_appserver, "AppServer", UnavailableAppServer)
    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)

    rc = cli.main(
        [
            "run",
            "inspect",
            "--cwd",
            str(repo),
            "--output-dir",
            str(output_dir),
            "--worker",
            "coder",
            "--codex-transport",
            "app-server",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
    )

    run_meta = json.loads((output_dir / "run.json").read_text())
    assert rc == 0
    assert run_meta["codex_transport"] == "exec"
    assert "control_socket" not in run_meta
    assert "control_transport" not in run_meta


def _write_successful_worktree_run(output_dir: Path, cwd: Path, *, final: str = "done") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run.v1",
                "task": "x",
                "cwd": str(cwd),
                "status": "ok",
                "started_at": "2026-07-09T12:00:00Z",
                "finished_at": "2026-07-09T12:00:01Z",
                "duration_seconds": 1,
                "artifacts": str(output_dir),
            }
        )
        + "\n"
    )
    (output_dir / "final.txt").write_text(final + "\n")


def test_run_cli_worktree_passes_detached_cwd_and_writes_changes_patch(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"
    seen = {}

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        lock_workspace=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
        defer_artifact_collection=False,
    ):
        seen["cwd"] = cwd
        seen["lock_workspace"] = lock_workspace
        seen["output_dir"] = output_dir
        seen["defer_artifact_collection"] = defer_artifact_collection
        assert cwd != repo
        assert (cwd / "tracked.txt").read_text() == "base\n"
        assert proc.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=cwd).code == 1
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        (cwd / "created.txt").write_text("created\n")
        _write_successful_worktree_run(output_dir, cwd)
        (output_dir / "worker-results.json").write_text(
            json.dumps({"results": [], "ground_truth": {"available": True, "patch_ref": None}}) + "\n"
        )
        (output_dir / "synthesis.json").write_text(
            json.dumps({"orchestrator": "chef", "result": {"ok": True}, "ground_truth": {"patch_ref": None}}) + "\n"
        )
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    assert rc == 0
    assert seen["output_dir"] == output_dir
    expected_checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert seen["cwd"] == expected_checkout
    assert seen["lock_workspace"] == repo.resolve()
    assert seen["defer_artifact_collection"] is True
    assert not expected_checkout.exists()
    assert (repo / "tracked.txt").read_text() == "base\n"
    patch = (output_dir / "changes.patch").read_text()
    assert "tracked.txt" in patch
    assert "created.txt" in patch
    assert "+changed in worktree" in patch
    assert "+created" in patch
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    assert run_meta["artifact_collection"] == {
        "status": "ok",
        "patch_ref": "changes.patch",
        "changed": True,
        "tracked_count": 1,
        "untracked_count": 1,
    }
    assert json.loads((output_dir / "worker-results.json").read_text())["ground_truth"]["patch_ref"] == "changes.patch"
    assert json.loads((output_dir / "synthesis.json").read_text())["ground_truth"]["patch_ref"] == "changes.patch"


def test_run_cli_worktree_warns_on_empty_changes_patch_noop(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        output = kwargs["output_dir"]
        output.mkdir(parents=True)
        (output / "run.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "task": task,
                    "cwd": str(cwd),
                    "read_only": False,
                    "dry_run": False,
                    "started_at": "2026-07-09T12:00:00Z",
                    "finished_at": "2026-07-09T12:00:01Z",
                    "duration_seconds": 1,
                    "artifacts": str(output),
                    "suspected_noop": True,
                }
            )
            + "\n"
        )
        (output / "worker-results.json").write_text(
            json.dumps(
                {
                    "results": [{"worker": "coder", "task": "implement it", "ok": True, "detail": "no-op", "text": ""}],
                    "ground_truth": {
                        "available": True,
                        "changed_files": [],
                        "untracked_files": [],
                        "diffstat": "",
                        "patch_ref": None,
                        "suspected_noop": True,
                    },
                }
            )
            + "\n"
        )
        (output / "synthesis.json").write_text(
            json.dumps({"orchestrator": "chef", "result": {"ok": True}, "ground_truth": {"patch_ref": None}}) + "\n"
        )
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    captured = capsys.readouterr()
    assert rc == 0
    assert (output_dir / "changes.patch").read_text() == ""
    assert json.loads((output_dir / "worker-results.json").read_text())["ground_truth"]["patch_ref"] == "changes.patch"
    assert json.loads((output_dir / "run.json").read_text())["artifact_collection"] == {
        "status": "ok",
        "patch_ref": "changes.patch",
        "changed": False,
        "tracked_count": 0,
        "untracked_count": 0,
    }
    assert "changes: none" in captured.err
    assert "changes.patch" in captured.err
    assert "warning: suspected no-op run" in captured.err


def test_run_cli_worktree_keeps_checkout_when_patch_invalid(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        output = kwargs["output_dir"]
        _write_successful_worktree_run(output, cwd, final="implementation complete")
        run_path = output / "run.json"
        run_meta = json.loads(run_path.read_text())
        run_meta["status"] = "artifact-collection"
        run_meta.pop("finished_at")
        run_meta.pop("duration_seconds")
        run_path.write_text(json.dumps(run_meta) + "\n")
        (output / "worker-results.json").write_text(
            json.dumps({"results": [], "ground_truth": {"patch_ref": None}}) + "\n"
        )
        (output / "synthesis.json").write_text(
            json.dumps({"result": {"ok": True}, "ground_truth": {"patch_ref": None}}) + "\n"
        )
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runguard, "verify_changes_patch", lambda cwd, patch_path: False)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    err = capsys.readouterr().err
    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert "changes.patch failed validation" in err
    assert str(checkout) in err
    assert checkout.exists()
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "artifact-validation"
    assert run_meta["failure"] == {
        "phase": "artifact-validation",
        "kind": "invalid-patch",
        "detail": "changes.patch failed validation",
    }
    assert run_meta["artifact_collection"] == {
        "status": "failed",
        "patch_ref": "changes.patch",
        "changed": True,
        "tracked_count": 1,
        "untracked_count": 0,
        "worktree": str(checkout),
        "failure": {
            "phase": "artifact-validation",
            "kind": "invalid-patch",
            "detail": "changes.patch failed validation",
        },
    }
    assert (output_dir / "final.txt").read_text() == "implementation complete\n"
    assert json.loads((output_dir / "worker-results.json").read_text())["ground_truth"]["patch_ref"] is None
    assert json.loads((output_dir / "synthesis.json").read_text())["ground_truth"]["patch_ref"] is None
    assert runs_cmd.show(output_dir) == 1
    show_output = capsys.readouterr().out
    assert "status: failed" in show_output
    assert "failure phase: artifact-validation" in show_output
    assert "final:\n  implementation complete" in show_output
    assert runs_cmd.watch(output_dir, cwd=repo, interval=0) == 1
    watch_output = capsys.readouterr().out
    assert "status: failed" in watch_output
    assert "failure phase: artifact-validation" in watch_output


def test_run_cli_worktree_artifact_failure_preserves_model_failure(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        _write_successful_worktree_run(kwargs["output_dir"], cwd, final="provider diagnostic")
        run_path = kwargs["output_dir"] / "run.json"
        run_meta = json.loads(run_path.read_text())
        run_meta.update(
            {
                "status": "failed",
                "error": "provider inference failed",
                "failure_phase": "inference",
                "failure": {
                    "phase": "inference",
                    "kind": "provider-error",
                    "detail": "provider inference failed",
                },
            }
        )
        run_path.write_text(json.dumps(run_meta) + "\n")
        return 2

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runguard, "verify_changes_patch", lambda cwd, patch_path: False)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    assert rc == 2
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "inference"
    assert run_meta["failure"] == {
        "phase": "inference",
        "kind": "provider-error",
        "detail": "provider inference failed",
    }
    assert run_meta["artifact_collection"]["failure"] == {
        "phase": "artifact-validation",
        "kind": "invalid-patch",
        "detail": "changes.patch failed validation",
    }


def test_run_cli_worktree_kept_when_patch_collection_raises(tmp_path, monkeypatch, capsys):
    # If patch collection itself dies after agents edited the worktree, the
    # worktree is the only copy of the work and must survive cleanup.
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        _write_successful_worktree_run(kwargs["output_dir"], cwd, final="implementation complete")
        return 0

    def raising_collect(cwd, patch_path):
        patch_path.write_text("partial patch\n")
        raise runguard.RunGuardError("failed to collect tracked diff: boom")

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runguard, "collect_changes_patch", raising_collect)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    err = capsys.readouterr().err
    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert checkout.exists()
    assert (checkout / "tracked.txt").read_text() == "changed in worktree\n"
    assert str(checkout) in err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "artifact-collection"
    assert run_meta["failure"] == {
        "phase": "artifact-collection",
        "kind": "collection-error",
        "detail": "failed to collect tracked diff: boom",
    }
    assert run_meta["artifact_collection"] == {
        "status": "failed",
        "patch_ref": "changes.patch",
        "worktree": str(checkout),
        "failure": {
            "phase": "artifact-collection",
            "kind": "collection-error",
            "detail": "failed to collect tracked diff: boom",
        },
    }
    assert (output_dir / "final.txt").read_text() == "implementation complete\n"


def test_run_cli_worktree_records_patch_write_error(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        _write_successful_worktree_run(kwargs["output_dir"], cwd, final="implementation complete")
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(
        runguard,
        "collect_changes_patch",
        lambda cwd, patch_path: (_ for _ in ()).throw(OSError("disk full")),
    )

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert checkout.exists()
    assert "failed to write changes.patch: disk full" in capsys.readouterr().err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "artifact-collection"
    assert run_meta["failure"]["kind"] == "collection-error"
    assert run_meta["failure"]["detail"] == "failed to write changes.patch: disk full"
    assert run_meta["artifact_collection"] == {
        "status": "failed",
        "worktree": str(checkout),
        "failure": {
            "phase": "artifact-collection",
            "kind": "collection-error",
            "detail": "failed to write changes.patch: disk full",
        },
    }


def test_run_cli_worktree_keeps_checkout_when_receipt_finalization_fails(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        output = kwargs["output_dir"]
        _write_successful_worktree_run(output, cwd, final="implementation complete")
        run_path = output / "run.json"
        run_meta = json.loads(run_path.read_text())
        run_meta["status"] = "artifact-collection"
        run_meta.pop("finished_at")
        run_meta.pop("duration_seconds")
        run_path.write_text(json.dumps(run_meta) + "\n")
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(
        aboyeur,
        "_write_json",
        lambda path, payload: (_ for _ in ()).throw(OSError("receipt disk full")),
    )

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert checkout.exists()
    assert "failed to update run receipt after artifact collection: receipt disk full" in capsys.readouterr().err
    assert json.loads((output_dir / "run.json").read_text())["status"] == "artifact-collection"
    assert runguard.lock_path(repo).is_dir()

    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert runs_cmd.watch(output_dir, cwd=repo, interval=0) == 1
    recovered = json.loads((output_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["failure"]["prior_status"] == "artifact-collection"
    assert not runguard.lock_path(repo).exists()


def test_run_cli_worktree_records_patch_reference_failure(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        output = kwargs["output_dir"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        _write_successful_worktree_run(output, cwd, final="implementation complete")
        (output / "worker-results.json").write_text(
            json.dumps({"results": [], "ground_truth": {"patch_ref": None}}) + "\n"
        )
        return 0

    original_write_json = aboyeur._write_json

    def fail_worker_results(path, payload):
        if path.name == "worker-results.json":
            raise OSError("worker receipt disk full")
        return original_write_json(path, payload)

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(aboyeur, "_write_json", fail_worker_results)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert checkout.exists()
    detail = "failed to record artifact patch reference in worker-results.json: worker receipt disk full"
    assert detail in capsys.readouterr().err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "artifact-collection"
    assert run_meta["failure"]["kind"] == "receipt-update-error"
    assert run_meta["failure"]["detail"] == detail


def test_run_cli_worktree_rejects_corrupt_worker_receipt_during_patch_reference(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        output = kwargs["output_dir"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        _write_successful_worktree_run(output, cwd, final="implementation complete")
        (output / "worker-results.json").write_text("{not json\n")
        return 0

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(aboyeur, "run", fake_run)

    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

    checkout = tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}"
    assert rc == 2
    assert checkout.exists()
    assert "failed to parse worker-results.json while recording artifact patch reference" in capsys.readouterr().err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure_phase"] == "artifact-collection"
    assert run_meta["failure"]["kind"] == "receipt-update-error"


def test_run_cli_rejects_worktree_with_no_artifacts(tmp_path, capsys):
    rc = cli.main(["run", "x", "--cwd", str(tmp_path), "--worktree", "--no-artifacts"])
    assert rc == 2
    assert "--worktree cannot be used with --no-artifacts" in capsys.readouterr().err


def test_run_cli_dirty_guard_skips_dry_and_read_only_runs(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n")
    calls = []

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        calls.append({"dry_run": dry_run, "read_only": read_only})
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)

    assert cli.main(["run", "x", "--cwd", str(repo), "--dry-run", "--no-artifacts"]) == 0
    assert cli.main(["run", "x", "--cwd", str(repo), "--read-only", "--no-artifacts"]) == 0
    assert cli.main(["run", "x", "--cwd", str(repo), "--sandbox", "read-only", "--no-artifacts"]) == 0
    assert len(calls) == 3


def test_run_cli_normal_runs_write_no_changes_patch(tmp_path, monkeypatch):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(
        task,
        loaded_roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)

    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir)]) == 0
    assert not (output_dir / "changes.patch").exists()


def test_run_cli_passes_codex_transport_to_aboyeur(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    seen = {}

    def fake_run(task, loaded_roster, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    rc = cli.main(
        [
            "run",
            "t",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--codex-transport",
            "app-server",
            "--no-artifacts",
        ]
    )
    assert rc == 0
    assert seen["codex_transport"] == "app-server"


def test_run_cli_codex_transport_default_is_none(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
    seen = {}

    def fake_run(task, loaded_roster, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    rc = cli.main(["run", "t", "--roster", str(roster_path), "--cwd", str(tmp_path), "--no-artifacts"])
    assert rc == 0
    assert "codex_transport" not in seen


def _worker_roster_toml() -> str:
    return """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"
"""


def test_run_cli_forwards_worker_to_aboyeur(tmp_path, monkeypatch):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(_worker_roster_toml())
    seen = {}

    def fake_run(task, loaded_roster, **kwargs):
        seen["task"] = task
        seen["worker"] = kwargs.get("worker")
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    rc = cli.main(
        [
            "run",
            "do something",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--worker",
            "coder",
            "--no-artifacts",
        ]
    )
    assert rc == 0
    assert seen == {"task": "do something", "worker": "coder"}


def test_run_cli_rejects_unknown_worker(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / ".brigade"
    config_dir.mkdir()
    (config_dir / "roster.toml").write_text(_worker_roster_toml())

    def fail_run(*args, **kwargs):
        raise AssertionError("aboyeur.run should not be called")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fail_run)

    rc = cli.main(["run", "x", "--worker", "missing", "--no-artifacts"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "missing" in err
    assert "worker" in err.lower()


def test_run_cli_rejects_orchestrator_as_worker(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / ".brigade"
    config_dir.mkdir()
    (config_dir / "roster.toml").write_text(_worker_roster_toml())

    def fail_run(*args, **kwargs):
        raise AssertionError("aboyeur.run should not be called")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fail_run)

    rc = cli.main(["run", "x", "--worker", "chef", "--no-artifacts"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "chef" in err
    assert "orchestrator" in err.lower()
