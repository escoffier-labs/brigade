import json
from pathlib import Path

import pytest

from brigade import aboyeur
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
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox=None,
    ):
        seen["cwd"] = cwd
        seen["output_dir"] = output_dir
        assert cwd != repo
        assert (cwd / "tracked.txt").read_text() == "base\n"
        assert proc.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=cwd).code == 1
        (cwd / "tracked.txt").write_text("changed in worktree\n")
        (cwd / "created.txt").write_text("created\n")
        output_dir.mkdir(parents=True)
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
    assert not expected_checkout.exists()
    assert (repo / "tracked.txt").read_text() == "base\n"
    patch = (output_dir / "changes.patch").read_text()
    assert "tracked.txt" in patch
    assert "created.txt" in patch
    assert "+changed in worktree" in patch
    assert "+created" in patch
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
    assert "changes: none" in captured.err
    assert "changes.patch" in captured.err
    assert "warning: suspected no-op run" in captured.err


def test_run_cli_worktree_keeps_checkout_when_patch_invalid(tmp_path, monkeypatch, capsys):
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        cwd = kwargs["cwd"]
        (cwd / "tracked.txt").write_text("changed in worktree\n")
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


def test_run_cli_worktree_kept_when_patch_collection_raises(tmp_path, monkeypatch, capsys):
    # If patch collection itself dies after agents edited the worktree, the
    # worktree is the only copy of the work and must survive cleanup.
    repo = _git_repo_with_roster(tmp_path)
    output_dir = tmp_path / "run"

    def fake_run(task, loaded_roster, **kwargs):
        (kwargs["cwd"] / "tracked.txt").write_text("changed in worktree\n")
        return 0

    def raising_collect(cwd, patch_path):
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
