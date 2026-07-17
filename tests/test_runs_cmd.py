import json
import os
from pathlib import Path

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


def _write_lock_owner(workspace, run_dir, *, pid=99999999, owner_token="owner"):
    lock_path = workspace / ".brigade" / "run.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    _write_json(
        lock_path / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": owner_token,
            "pid": pid,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": "2026-07-16T00:00:00+00:00",
        },
    )
    return lock_path


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


def test_runs_recover_cli_dispatches_resolved_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}
    monkeypatch.setattr(
        runs_cmd,
        "recover",
        lambda run, **kwargs: seen.update(run=run, **kwargs) or 0,
        raising=False,
    )

    rc = cli.main(["runs", "recover", str(run_dir), "--cwd", str(tmp_path)])

    assert rc == 0
    assert seen == {"run": str(run_dir), "cwd": tmp_path, "runs_dir": None}


def test_runs_recover_marks_dead_owner_run_terminal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert not lock_path.exists()
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    assert "resume: unavailable" in out


def test_runs_recover_reconstructs_missing_run_json_from_matching_dead_lock(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    run_dir.mkdir(parents=True)
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure"]["prior_status"] == "artifact-unavailable"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_and_reconstructs_corrupt_run_json(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("not json")
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    preserved = Path(recovered["recovery_preserved_artifact"])
    assert preserved.read_text() == "not json"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_refuses_live_owner_without_changing_artifacts(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "active"
    _write_minimal_run(
        run_dir,
        task="active task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir, pid=os.getpid())

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"
    assert lock_path.is_dir()
    assert "run owner process is still active" in capsys.readouterr().err


def test_runs_recover_refuses_lock_for_different_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    requested = workspace / ".brigade" / "runs" / "requested"
    recorded = workspace / ".brigade" / "runs" / "recorded"
    _write_minimal_run(
        requested,
        task="requested task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((requested / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(requested / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, recorded)

    rc = runs_cmd.recover(str(requested), cwd=workspace)

    assert rc == 2
    assert json.loads((requested / "run.json").read_text())["status"] == "dispatching"
    assert lock_path.is_dir()
    assert "run lock belongs to a different run" in capsys.readouterr().err


def test_runs_recover_is_idempotent_for_terminal_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"already terminal: {run_dir} [failed]" in out
    assert "resume: unavailable" in out


def test_runs_recover_treats_lost_concurrent_claim_as_already_terminal(tmp_path, monkeypatch, capsys):
    from brigade import runguard

    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(run_dir, task="orphaned task", status="dispatching", started_at="2026-07-16T00:00:00Z")
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)

    def lose_claim(cwd, requested_run, *, required=True):
        terminal = json.loads((run_dir / "run.json").read_text())
        terminal.update({"status": "failed", "failure_phase": "stale-lock-recovery"})
        _write_json(run_dir / "run.json", terminal)
        raise runguard.RunLockError(f"run lock not found for run: {requested_run}")

    monkeypatch.setattr(runguard, "recover_stale_run", lose_claim)

    assert runs_cmd.recover(str(run_dir), cwd=workspace) == 0
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_terminal_run_ignores_foreign_workspace_lock(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    foreign_run = workspace / ".brigade" / "runs" / "other"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, foreign_run)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert lock_path.is_dir()
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_clears_matching_dead_lock_after_artifact_was_already_terminal(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert not lock_path.exists()
    assert f"already terminal: {run_dir} [failed]" in capsys.readouterr().out


def test_runs_recover_reports_app_server_resume_when_available(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "failed"
    _write_minimal_run(
        run_dir,
        task="failed task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    _write_json(
        run_dir / "worker-results.json",
        {
            "results": [
                {
                    "worker": "coder",
                    "ok": False,
                    "status": "failed",
                    "thread_id": "thread-123",
                }
            ]
        },
    )

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert f"resume: brigade runs resume {run_dir}" in capsys.readouterr().out


def test_runs_show_surfaces_stale_lock_recovery_and_returns_nonzero(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "cwd": str(tmp_path),
            "finished_at": "2026-07-16T00:01:00Z",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.show(run_dir)

    assert rc == 1
    out = capsys.readouterr().out
    assert "failure phase: stale-lock-recovery" in out
    assert "failure kind: owner-process-exited" in out
    assert f"inspect: brigade runs show {run_dir}" in out
    assert "recover: completed (stale lock cleared)" in out
    assert "resume: unavailable" in out


def test_runs_watch_surfaces_stale_lock_recovery_and_returns_nonzero(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "cwd": str(tmp_path),
            "finished_at": "2026-07-16T00:01:00Z",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.watch(run_dir, cwd=tmp_path, interval=0.0)

    assert rc == 1
    out = capsys.readouterr().out
    assert "failure phase: stale-lock-recovery" in out
    assert f"inspect: brigade runs show {run_dir}" in out
    assert "recover: completed (stale lock cleared)" in out
    assert "resume: unavailable" in out


def test_runs_show_does_not_claim_stale_lock_was_cleared_while_matching_lock_remains(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    _write_lock_owner(workspace, run_dir)

    assert runs_cmd.show(run_dir) == 1
    out = capsys.readouterr().out
    assert "recover: required (stale lock remains)" in out
    assert "recover: completed (stale lock cleared)" not in out


def test_runs_watch_does_not_claim_stale_lock_was_cleared_while_matching_claim_remains(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned task",
        status="failed",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(workspace), "failure_phase": "stale-lock-recovery"})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)
    claimed = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(claimed)

    assert runs_cmd.watch(run_dir, cwd=workspace, interval=0.0) == 1
    out = capsys.readouterr().out
    assert "recover: required (stale lock remains)" in out
    assert "recover: completed (stale lock cleared)" not in out


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


def test_runs_show_prints_ground_truth(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "worker-results.json").read_text())
    payload["ground_truth"] = {
        "available": True,
        "diffstat": " a.txt | 1 +\n 1 file changed, 1 insertion(+)",
        "changed_files": ["a.txt"],
        "untracked_files": ["notes.md"],
        "patch_ref": "changes.patch",
        "verify_receipts": [
            {
                "run_id": "20260703-000000-work-verify-abc",
                "status": "completed",
                "commands": [{"command": "pytest -q", "status": "completed", "exit_code": 0}],
            }
        ],
    }
    _write_json(run_dir / "worker-results.json", payload)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "ground truth:" in out
    assert "changed_files: 1 (a.txt)" in out
    assert "untracked_files: 1 (notes.md)" in out
    assert "1 file changed, 1 insertion(+)" in out
    assert "patch_ref: changes.patch" in out
    assert "verify: 20260703-000000-work-verify-abc completed" in out
    assert "pytest -q" in out and "exit=0" in out


def test_runs_show_prints_unavailable_ground_truth_reason(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)
    payload = json.loads((run_dir / "worker-results.json").read_text())
    payload["ground_truth"] = {"available": False, "reason": "not a git worktree"}
    _write_json(run_dir / "worker-results.json", payload)

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "ground truth: unavailable (not a git worktree)" in out


def test_runs_show_without_ground_truth_stays_quiet(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _write_run_artifacts(run_dir)

    assert runs_cmd.show(run_dir) == 0
    assert "ground truth" not in capsys.readouterr().out
