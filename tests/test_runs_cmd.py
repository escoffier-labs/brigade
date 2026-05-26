import json

from brigade import cli
from brigade import runs_cmd


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_run_artifacts(run_dir):
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "task": "build feature",
            "cwd": "/repo",
            "orchestrator": "chef",
            "dry_run": False,
            "read_only": True,
            "status": "ok",
            "started_at": "2026-05-26T14:00:00Z",
            "finished_at": "2026-05-26T14:00:02Z",
            "duration_seconds": 2.0,
            "artifacts": str(run_dir),
            "handoff": str(run_dir / "handoff.md"),
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "max_workers": 1,
            "timeout_seconds": 180.0,
            "allow_models": ["codex"],
            "agents": {
                "chef": {"cli": "codex", "role": "plan", "timeout_seconds": 180.0},
                "coder": {"cli": "codex", "role": "code", "timeout_seconds": None},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"worker": "coder", "task": "implement it"}]},
    )
    _write_json(
        run_dir / "worker-results.json",
        {"results": [{"worker": "coder", "task": "implement it", "ok": True, "detail": "", "text": "done"}]},
    )
    _write_json(
        run_dir / "synthesis.json",
        {"orchestrator": "chef", "result": {"ok": True, "detail": "", "text": "final answer"}},
    )
    (run_dir / "final.txt").write_text("final answer\n")


def _write_minimal_run(run_dir, *, task, status, started_at, duration=1.0, read_only=False, dry_run=False):
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "task": task,
            "cwd": "/repo",
            "orchestrator": "chef",
            "dry_run": dry_run,
            "read_only": read_only,
            "status": status,
            "started_at": started_at,
            "duration_seconds": duration,
        },
    )


def test_runs_show_prints_summary(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert f"run: {run_dir}" in out
    assert "status: ok" in out
    assert "mode: read-only" in out
    assert "duration: 2s" in out
    assert "handoff:" in out
    assert "roster:" in out
    assert "  - chef: codex (orchestrator); timeout=180s" in out
    assert "plan:" in out
    assert "  -> coder: implement it" in out
    assert "workers:" in out
    assert "  [ok] coder" in out
    assert "synthesis:" in out
    assert "  [ok] chef" in out
    assert "final:" in out
    assert "  final answer" in out


def test_runs_show_reports_missing_run_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert runs_cmd.show(run_dir) == 2
    assert "run.json not found" in capsys.readouterr().err


def test_runs_show_reports_invalid_json(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("not json")

    assert runs_cmd.show(run_dir) == 2
    assert "run.json is not valid JSON" in capsys.readouterr().err


def test_runs_show_cli(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert cli.main(["runs", "show", str(run_dir)]) == 0
    assert "status: ok" in capsys.readouterr().out


def test_runs_list_prints_recent_runs(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "older",
        task="older task",
        status="failed",
        started_at="2026-05-26T13:00:00Z",
    )
    _write_minimal_run(
        runs_root / "newer",
        task="newer task",
        status="ok",
        started_at="2026-05-26T14:00:00Z",
        duration=2.5,
        read_only=True,
    )

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0
    out = capsys.readouterr().out
    first = out.index("newer task")
    second = out.index("older task")
    assert first < second
    assert "[ok] 2.5s read-only" in out
    assert str(runs_root / "newer") in out


def test_runs_list_respects_limit(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "one",
        task="one task",
        status="ok",
        started_at="2026-05-26T13:00:00Z",
    )
    _write_minimal_run(
        runs_root / "two",
        task="two task",
        status="ok",
        started_at="2026-05-26T14:00:00Z",
    )

    assert runs_cmd.list_runs(cwd=tmp_path, limit=1) == 0
    out = capsys.readouterr().out
    assert "two task" in out
    assert "one task" not in out


def test_runs_list_reports_missing_runs_dir(tmp_path, capsys):
    assert runs_cmd.list_runs(cwd=tmp_path) == 2
    assert "runs directory not found" in capsys.readouterr().err


def test_runs_list_rejects_bad_limit(tmp_path, capsys):
    assert runs_cmd.list_runs(cwd=tmp_path, limit=0) == 2
    assert "--limit must be a positive integer" in capsys.readouterr().err


def test_runs_list_cli_with_explicit_runs_dir(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    _write_minimal_run(
        runs_root / "one",
        task="cli task",
        status="dry-run",
        started_at="2026-05-26T14:00:00Z",
        dry_run=True,
    )

    assert cli.main(["runs", "list", "--cwd", str(tmp_path), "--runs-dir", str(runs_root)]) == 0
    out = capsys.readouterr().out
    assert "cli task" in out
    assert "dry-run" in out


def test_runs_latest_shows_newest_run(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    _write_minimal_run(
        runs_root / "older",
        task="older task",
        status="failed",
        started_at="2026-05-26T13:00:00Z",
    )
    newest = runs_root / "newer"
    _write_run_artifacts(newest)

    assert runs_cmd.show_latest(cwd=tmp_path) == 0
    out = capsys.readouterr().out
    assert f"run: {newest}" in out
    assert "task: build feature" in out
    assert "final:" in out


def test_runs_latest_reports_no_runs(tmp_path, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    runs_root.mkdir(parents=True)

    assert runs_cmd.show_latest(cwd=tmp_path) == 1
    assert "no runs found" in capsys.readouterr().err


def test_runs_latest_cli_with_explicit_runs_dir(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    run_dir = runs_root / "one"
    _write_run_artifacts(run_dir)

    assert cli.main(["runs", "latest", "--cwd", str(tmp_path), "--runs-dir", str(runs_root)]) == 0
    assert f"run: {run_dir}" in capsys.readouterr().out
