from brigade import aboyeur
from brigade import cli
from brigade import dogfood_cmd
from brigade import runs_cmd


def test_dogfood_runs_default_codex_workflow(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(
        task,
        roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox_read_only=None,
        sandbox=None,
    ):
        seen["task"] = task
        seen["roster"] = roster
        seen["show_plan"] = show_plan
        seen["cwd"] = cwd
        seen["output_dir"] = output_dir
        seen["handoff_inbox"] = handoff_inbox
        seen["read_only"] = read_only
        seen["sandbox_read_only"] = sandbox_read_only
        seen["sandbox"] = sandbox
        return 0

    def fake_show(run_dir):
        seen["inspect_dir"] = run_dir
        print(f"summary for {run_dir}")
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runs_cmd, "show", fake_show)

    assert dogfood_cmd.run(None, target=tmp_path) == 0
    roster = seen["roster"]
    assert seen["task"] == dogfood_cmd.DEFAULT_TASK
    assert seen["show_plan"] is True
    assert seen["cwd"] == tmp_path.resolve()
    assert seen["output_dir"].parent == tmp_path / ".brigade" / "runs"
    assert seen["handoff_inbox"] == tmp_path / ".claude" / "memory-handoffs"
    assert seen["read_only"] is True
    assert seen["sandbox_read_only"] is None
    assert seen["sandbox"] == "danger-full-access"
    assert roster.orchestrator == "chef"
    assert roster.max_workers == 1
    assert roster.allow_models == ("codex",)
    assert {agent.cli for agent in roster.agents.values()} == {"codex"}
    assert seen["inspect_dir"] == seen["output_dir"]
    captured = capsys.readouterr()
    assert "summary for" in captured.out
    assert "artifacts:" in captured.err


def test_dogfood_can_disable_handoff_and_inspect(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(
        task,
        roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox_read_only=None,
        sandbox=None,
    ):
        seen["handoff_inbox"] = handoff_inbox
        return 2

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runs_cmd, "show", lambda run_dir: seen.setdefault("inspect", run_dir))

    assert (
        dogfood_cmd.run(
            "custom task",
            target=tmp_path,
            output_dir=tmp_path / "run",
            handoff=False,
            inspect=False,
        )
        == 2
    )
    assert seen["handoff_inbox"] is None
    assert "inspect" not in seen
    assert "artifacts:" in capsys.readouterr().err


def test_dogfood_init_writes_local_config(tmp_path, capsys):
    assert (
        dogfood_cmd.init(
            target=tmp_path,
            artifacts_dir=tmp_path / "artifacts",
            handoff_inbox=tmp_path / "handoffs",
            handoff=False,
            inspect=False,
            timeout_seconds=12,
        )
        == 0
    )

    config = tmp_path / ".brigade" / "dogfood.toml"
    assert config.is_file()
    loaded = dogfood_cmd.load_config(tmp_path)
    assert loaded is not None
    assert loaded.target == tmp_path.resolve()
    assert loaded.artifacts_dir == tmp_path / "artifacts"
    assert loaded.handoff is False
    assert loaded.handoff_inbox == tmp_path / "handoffs"
    assert loaded.inspect is False
    assert loaded.timeout_seconds == 12
    assert f"wrote {config}" in capsys.readouterr().out


def test_dogfood_init_refuses_existing_without_force(tmp_path, capsys):
    assert dogfood_cmd.init(target=tmp_path) == 0
    assert dogfood_cmd.init(target=tmp_path) == 2
    assert "already exists" in capsys.readouterr().err


def test_dogfood_loads_config_defaults(tmp_path, monkeypatch):
    seen = {}
    artifacts_dir = tmp_path / "configured-runs"
    handoff_inbox = tmp_path / "configured-handoffs"
    dogfood_cmd.init(
        target=tmp_path,
        artifacts_dir=artifacts_dir,
        handoff_inbox=handoff_inbox,
        inspect=False,
        native_read_only_sandbox=True,
        timeout_seconds=33,
    )

    def fake_run(
        task,
        roster,
        dry_run=False,
        show_plan=False,
        verbose=False,
        cwd=None,
        output_dir=None,
        handoff_inbox=None,
        read_only=False,
        sandbox_read_only=None,
        sandbox=None,
    ):
        seen["roster"] = roster
        seen["cwd"] = cwd
        seen["output_dir"] = output_dir
        seen["handoff_inbox"] = handoff_inbox
        seen["sandbox"] = sandbox
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(runs_cmd, "show", lambda run_dir: seen.setdefault("inspect", run_dir))

    assert dogfood_cmd.run(None, target=tmp_path) == 0
    assert seen["cwd"] == tmp_path.resolve()
    assert seen["output_dir"].parent == artifacts_dir
    assert seen["handoff_inbox"] == handoff_inbox
    assert seen["sandbox"] == "read-only"
    assert seen["roster"].timeout_seconds == 33
    assert "inspect" not in seen


def test_dogfood_status_reports_config_and_latest_run(tmp_path, monkeypatch, capsys):
    dogfood_cmd.init(target=tmp_path, timeout_seconds=33)
    run_dir = tmp_path / ".brigade" / "runs" / "20260526-120000-test"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"started_at":"2026-05-26T12:00:00Z","status":"ok","task":"review the repo"}'
    )

    monkeypatch.setattr(dogfood_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "yes")

    assert dogfood_cmd.status(target=tmp_path) == 0
    captured = capsys.readouterr()
    assert "dogfood: ready" in captured.out
    assert f"config: {tmp_path / '.brigade' / 'dogfood.toml'}" in captured.out
    assert f"target: {tmp_path.resolve()}" in captured.out
    assert "artifacts_ignored: yes" in captured.out
    assert "codex: /usr/bin/codex" in captured.out
    assert "brigade: /usr/bin/brigade" in captured.out
    assert "timeout_seconds: 33" in captured.out
    assert "latest_run: 2026-05-26T12:00:00Z [ok]" in captured.out
    assert "latest_task: review the repo" in captured.out


def test_dogfood_status_reports_missing_codex(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dogfood_cmd.shutil, "which", lambda name: None)
    monkeypatch.setattr(dogfood_cmd, "_check_git_ignored", lambda repo, path: "no")

    assert dogfood_cmd.status(target=tmp_path) == 1
    captured = capsys.readouterr()
    assert "dogfood: not ready" in captured.out
    assert "config:" in captured.out
    assert "(missing)" in captured.out
    assert "codex: missing" in captured.out
    assert "warning: config missing" in captured.err
    assert "error: codex CLI not found on PATH" in captured.err


def test_dogfood_rejects_missing_target(tmp_path, capsys):
    assert dogfood_cmd.run(None, target=tmp_path / "missing") == 2
    assert "--target is not a directory" in capsys.readouterr().err


def test_dogfood_rejects_bad_timeout(tmp_path, capsys):
    assert dogfood_cmd.run(None, target=tmp_path, timeout_seconds=0) == 2
    assert "--timeout-seconds must be positive" in capsys.readouterr().err


def test_dogfood_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_run(task, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_run)

    assert (
        cli.main(
            [
                "dogfood",
                "review this",
                "--target",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "run"),
                "--handoff-inbox",
                str(tmp_path / "handoffs"),
                "--no-handoff",
                "--no-inspect",
                "--native-read-only-sandbox",
                "--timeout-seconds",
                "12",
            ]
        )
        == 0
    )
    assert seen == {
        "task": "review this",
        "target": tmp_path,
        "output_dir": tmp_path / "run",
        "handoff": False,
        "handoff_inbox": tmp_path / "handoffs",
        "inspect": False,
        "native_read_only_sandbox": True,
        "timeout_seconds": 12.0,
    }


def test_dogfood_cli_init(tmp_path, monkeypatch):
    seen = {}

    def fake_init(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(dogfood_cmd, "init", fake_init)

    assert (
        cli.main(
            [
                "dogfood",
                "init",
                "--target",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "runs"),
                "--handoff-inbox",
                str(tmp_path / "handoffs"),
                "--no-handoff",
                "--no-inspect",
                "--native-read-only-sandbox",
                "--timeout-seconds",
                "12",
                "--force",
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "artifacts_dir": tmp_path / "runs",
        "handoff_inbox": tmp_path / "handoffs",
        "force": True,
        "handoff": False,
        "inspect": False,
        "native_read_only_sandbox": True,
        "timeout_seconds": 12.0,
    }


def test_dogfood_cli_status(tmp_path, monkeypatch):
    seen = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(dogfood_cmd, "status", fake_status)

    assert cli.main(["dogfood", "status", "--target", str(tmp_path)]) == 0
    assert seen == {"target": tmp_path}
