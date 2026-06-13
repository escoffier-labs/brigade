import json
from pathlib import Path

import pytest

from brigade import aboyeur
from brigade import cli
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
