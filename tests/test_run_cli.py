import json

from brigade import aboyeur
from brigade import cli


def test_run_cli_missing_roster_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "do something"])
    assert rc == 2
    assert "roster not found" in capsys.readouterr().err


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
    ):
        seen["task"] = task
        seen["orchestrator"] = loaded_roster.orchestrator
        seen["dry_run"] = dry_run
        seen["show_plan"] = show_plan
        seen["verbose"] = verbose
        seen["cwd"] = cwd
        seen["output_dir"] = output_dir
        seen["handoff_inbox"] = handoff_inbox
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
    }


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
        lambda task, loaded_roster, dry_run=False, show_plan=False, verbose=False, cwd=None, output_dir=None, handoff_inbox=None: 0,
    )
    assert cli.main(["run", json.dumps({"task": "x"}), "--dry-run"]) == 0


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
    ):
        seen["output_dir"] = output_dir
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "x", "--no-artifacts"]) == 0
    assert seen["output_dir"] is None
