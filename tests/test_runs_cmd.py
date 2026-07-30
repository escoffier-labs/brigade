import errno
import json
import os
import shutil
import time as system_time
from pathlib import Path

import pytest

from brigade import cli
from brigade import run_checkpoint
from brigade import run_events
from brigade import run_journal
from brigade import run_lifecycle
from brigade import runguard
from brigade import runs_cmd


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_artifact_collection_status_is_nonterminal():
    assert runs_cmd._is_terminal({"status": "artifact-collection"}) is False
    assert runs_cmd._is_terminal({"status": "result-processing"}) is False


def test_legacy_handoff_failed_status_remains_terminal():
    assert runs_cmd._is_terminal({"status": "handoff-failed"}) is True


def test_watch_reports_unrecoverable_artifact_collection_lock_error(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "status": "artifact-collection",
            "cwd": str(tmp_path),
            "lock_workspace": str(tmp_path),
            "started_at": "2026-07-17T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        runguard,
        "recover_stale_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(runguard.RunLockError("run lock has no owner metadata")),
    )

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 2
    assert "artifact-collection recovery failed: run lock has no owner metadata" in capsys.readouterr().err


def test_watch_reports_artifact_collection_without_matching_lock(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "status": "artifact-collection",
            "cwd": str(tmp_path),
            "lock_workspace": str(tmp_path),
            "started_at": "2026-07-17T00:00:00Z",
        },
    )

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 2
    assert "artifact-collection run has no matching recoverable lock" in capsys.readouterr().err


def test_watch_handles_artifact_collection_completion_race(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_path = run_dir / "run.json"
    payload = {
        "status": "artifact-collection",
        "cwd": str(tmp_path),
        "lock_workspace": str(tmp_path),
        "started_at": "2026-07-17T00:00:00Z",
    }
    _write_json(run_path, payload)

    def finish_without_recovery(*args, **kwargs):
        payload["status"] = "ok"
        payload["finished_at"] = "2026-07-17T00:00:01Z"
        _write_json(run_path, payload)
        return False

    monkeypatch.setattr(runguard, "recover_stale_run", finish_without_recovery)

    assert runs_cmd.watch(run_dir, cwd=tmp_path, interval=0) == 0


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
    assert not (run_dir / "revisions").exists()


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


def test_runs_recover_uses_recorded_lock_workspace_for_worktree_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    detached = tmp_path / "detached"
    detached.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _write_minimal_run(
        run_dir,
        task="orphaned worktree task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update({"cwd": str(detached), "lock_workspace": str(workspace)})
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=detached)

    assert rc == 0
    assert not lock_path.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_infers_lock_workspace_for_legacy_worktree_run(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    detached = tmp_path / "detached"
    detached.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "legacy"
    _write_minimal_run(
        run_dir,
        task="legacy worktree task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(detached)
    _write_json(run_dir / "run.json", run_meta)
    lock_path = _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=tmp_path)

    assert rc == 0
    assert not lock_path.exists()
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


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


def test_runs_recover_lost_claim_with_invalid_utf8_run_json_is_bounded(tmp_path, capsys):
    """Invalid UTF-8 in run.json must not escape the concurrent-terminal
    fallback as a raw ``UnicodeDecodeError``; recovery stays bounded at rc=2."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _activate_journal_with_checkpoint(workspace, run_dir, {"status": "planning", "task": "demo", "cwd": str(workspace)})
    # A concurrent recovery already cleared the matching lock.
    shutil.rmtree(workspace / ".brigade" / "run.lock")
    # run.json is corrupt: invalid UTF-8 bytes.
    (run_dir / "run.json").write_bytes(b"\xff\xfe invalid utf8 \x00")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_runs_recover_lost_claim_with_deeply_nested_run_json_is_bounded(tmp_path, capsys):
    """A ``RecursionError`` from deeply nested run.json must not escape the
    concurrent-terminal fallback; recovery stays bounded at rc=2."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "orphan"
    _activate_journal_with_checkpoint(workspace, run_dir, {"status": "planning", "task": "demo", "cwd": str(workspace)})
    shutil.rmtree(workspace / ".brigade" / "run.lock")
    # Valid but deeply nested JSON: json.loads recurses and raises RecursionError.
    nesting = 10000
    (run_dir / "run.json").write_text('{"a":' * nesting + "1" + "}" * nesting)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err


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


def test_runs_list_flags_over_timeout_nonterminal_run_without_mutating_it(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "stale"
    _write_minimal_run(
        run_dir,
        task="stalled task",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": None},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "inspect"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )
    before = (run_dir / "run.json").read_bytes()
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[dispatching; stale > 90s timeout]" in out
    assert (run_dir / "run.json").read_bytes() == before


def test_runs_list_uses_current_synthesis_phase_age(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "synthesizing"
    _write_minimal_run(
        run_dir,
        task="synthesize result",
        status="synthesizing",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["status_started_at"] = "2026-05-26T14:01:10Z"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {"chef": {"timeout_seconds": 30.0}},
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[synthesizing]" in out
    assert "stale" not in out


def test_handoff_timeout_uses_orchestrator_default_not_planned_workers(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "handoff"
    _write_minimal_run(
        run_dir,
        task="write handoff",
        status="handoff",
        started_at="2026-05-26T14:00:00Z",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": None},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "implement"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )

    run_meta = json.loads((run_dir / "run.json").read_text())
    assert runs_cmd._run_timeout_seconds(run_dir, run_meta) == 180.0


def test_result_processing_timeout_uses_orchestrator_not_direct_worker(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "result-processing"
    _write_minimal_run(
        run_dir,
        task="process worker result",
        status="result-processing",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["worker"] = "coder"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 90.0},
                "coder": {"timeout_seconds": 30.0},
            },
        },
    )

    assert runs_cmd._run_timeout_seconds(run_dir, run_meta) == 90.0


def test_runs_list_uses_current_later_stage_age(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "stage-two"
    _write_minimal_run(
        run_dir,
        task="review implementation",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "status_started_at": "2026-05-26T14:01:10Z",
            "active_stage": 2,
            "active_seats": ["reviewer"],
        }
    )
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "coder": {"timeout_seconds": 30.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"stage": 1, "worker": "coder", "task": "implement"},
                {"stage": 2, "worker": "reviewer", "task": "review"},
            ]
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    out = capsys.readouterr().out
    assert "[dispatching]" in out
    assert "stale" not in out


def test_runs_list_detects_stale_active_phase_with_corrupt_optional_plan(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "active-corrupt-plan"
    _write_minimal_run(
        run_dir,
        task="review implementation",
        status="dispatching",
        started_at="2026-05-26T14:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["active_seats"] = ["reviewer"]
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "reviewer": {"timeout_seconds": 90.0},
            },
        },
    )
    (run_dir / "plan.json").write_text("not json")
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    assert "[dispatching; stale > 90s timeout]" in capsys.readouterr().out


def test_runs_list_uses_direct_worker_timeout_while_status_is_started(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "direct-worker"
    _write_minimal_run(
        run_dir,
        task="direct task",
        status="started",
        started_at="2026-05-26T14:01:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["worker"] = "coder"
    _write_json(run_dir / "run.json", run_meta)
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 180.0,
            "agents": {
                "chef": {"timeout_seconds": 180.0},
                "coder": {"timeout_seconds": 30.0},
            },
        },
    )
    monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804091.0)

    assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0

    assert "[started; stale > 30s timeout]" in capsys.readouterr().out


def test_runs_list_treats_naive_legacy_timestamp_as_utc(tmp_path, monkeypatch, capsys):
    runs_root = tmp_path / ".brigade" / "runs"
    run_dir = runs_root / "legacy-naive"
    _write_minimal_run(
        run_dir,
        task="legacy task",
        status="started",
        started_at="2026-05-26T14:00:00",
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "timeout_seconds": 30.0,
            "agents": {"chef": {"timeout_seconds": None}},
        },
    )
    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        system_time.tzset()
        monkeypatch.setattr(runs_cmd.time, "time", lambda: 1779804031.0)

        assert runs_cmd.list_runs(cwd=tmp_path, limit=10) == 0
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        system_time.tzset()

    assert "[started; stale > 30s timeout]" in capsys.readouterr().out


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


# -- Issue #568 slice 5 Task 5: checkpoint-backed runs recover integration --


_RUN_ID = "20260727-153045-a1b2c3d4"
_RECORDED_AT = "2026-07-27T15:30:45.123456Z"


def _writer_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _activate_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
) -> None:
    """Build an activated lifecycle journal ending in a checkpoint event.

    Bootstraps run.json with the durable request, then under a live run lock
    activates the journal and appends one ``run.snapshot.checkpointed``
    event (the recoverable state: crash after the checkpoint publish+append
    but before the paired status append). Finally replaces the lock owner
    with a dead pid so ``runs_cmd.recover`` sees a stale matching lock.
    """
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    localio = _localio()
    localio.write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type=paired_event_type,
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)


def _localio():
    from brigade import localio

    return localio


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _events(run_dir: Path) -> list:
    return run_journal.read_journal(_journal_path(run_dir)).events


def _overwrite_lock_owner(workspace, run_dir, *, pid, owner_token="owner"):
    """Overwrite an existing run.lock's owner metadata in place (no mkdir)."""
    lock_path = workspace / ".brigade" / "run.lock"
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


def test_runs_recover_restores_missing_run_json_from_latest_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "planning",
        "task": "demo task",
        "cwd": str(workspace),
        "orchestrator": "chef",
    }
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    assert not (run_dir / "run.json").exists()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    # Restored receipt fields are preserved through terminalization.
    assert recovered["task"] == "demo task"
    assert recovered["orchestrator"] == "chef"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_corrupt_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_text("not json")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not json"


def test_runs_recover_terminalization_preserves_restored_receipt_fields(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "dispatching",
        "task": "inspect",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "active_seats": ["coder"],
        "duration_seconds": 5,
    }
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "inspect"
    assert recovered["orchestrator"] == "chef"
    assert recovered["duration_seconds"] == 5
    assert recovered["failure"]["prior_status"] == "dispatching"


def test_runs_recover_quarantines_partial_journal_tail_then_verifies(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)
    complete_bytes = journal.read_bytes()[: -len(partial)]

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert journal.read_bytes() == complete_bytes
    quarantine = list((run_dir / "events" / "quarantine").iterdir())
    assert quarantine, "partial tail was not quarantined"
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"


def test_runs_recover_fails_closed_on_invalid_latest_checkpoint(tmp_path, capsys):
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    sha = hashlib.sha256(_writer_bytes(run_json_obj)).hexdigest()
    cp_file = run_checkpoint.checkpoint_path(run_dir, sha)
    cp_file.write_bytes(b"x" * len(_writer_bytes(run_json_obj)))
    os.chmod(cp_file, 0o600)
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    # The dead lock is restored (callback failed; lock restored by runguard).
    assert lock_path.is_dir()


def test_runs_recover_accepts_covered_paired_status_event_and_preserves_fields(tmp_path, capsys):
    """CLI recovery accepts a checkpoint N plus matching status N+1 and preserves fields.

    The checkpoint at N carries ``paired_event_type`` for the checkpointed
    status, and the N+1 event is the matching paired status event whose
    derived status equals the checkpoint status. Per the slice-5 coverage
    semantics this is a covered tail, so recovery restores the checkpoint
    bytes and terminalizes, preserving the restored receipt fields
    (``task``, ``orchestrator``, ``cwd``, ``active_seats``,
    ``duration_seconds``) and stamping the stale-lock-recovery failure.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {
        "status": "dispatching",
        "task": "inspect",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "active_seats": ["coder"],
        "duration_seconds": 5,
    }
    # Build the journal with a checkpoint AND its matching paired status event
    # (covered tail) under a live lock, then replace the owner with a dead pid.
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.dispatch.requested",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-req-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    (run_dir / "run.json").unlink()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    # Restoration and terminalization field preservation.
    assert recovered["task"] == "inspect"
    assert recovered["orchestrator"] == "chef"
    assert recovered["cwd"] == str(workspace)
    assert recovered["active_seats"] == ["coder"]
    assert recovered["duration_seconds"] == 5
    assert recovered["failure"]["prior_status"] == "dispatching"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_fails_closed_on_uncovered_tail_wrong_event_type(tmp_path, capsys):
    """CLI recovery fails closed when the N+1 event_type is not the checkpoint's pair.

    A checkpoint at N with ``paired_event_type`` set, followed by an N+1
    event whose ``event_type`` is not the paired type, is an uncovered tail.
    Recovery exits 2, restores no run.json, and the runguard claim is
    restored (lock dir remains).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    (run_dir / "run.json").unlink()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    assert lock_path.is_dir()


def test_runs_recover_fails_closed_on_uncovered_tail_with_parseable_run_json(tmp_path, capsys):
    """An uncovered tail fails closed even when run.json is parseable; run.json is untouched."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    runs_dir = run_dir.parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    _localio().write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={"detail": "dispatching"},
            idempotency_key="dispatch-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    _write_lock_owner(workspace, run_dir, pid=99999999)
    parseable = {"schema": "brigade.run.v1", "status": "planning", "task": "different", "cwd": str(workspace)}
    _write_json(run_dir / "run.json", parseable)
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert not list(run_dir.glob("run.json.corrupt-*"))


def test_runs_recover_fails_closed_on_chain_break(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["sequence"] = 3
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()


def test_runs_recover_fails_closed_on_envelope_run_id_mismatch(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["run_id"] = "a-different-run-id"
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()


def test_runs_recover_refuses_live_owner_without_mutation_activated(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Replace the dead owner with the current live pid.
    _overwrite_lock_owner(workspace, run_dir, pid=os.getpid())
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert "run owner process is still active" in capsys.readouterr().err


def test_runs_recover_refuses_foreign_pid_without_mutation_activated(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    other_run = workspace / ".brigade" / "runs" / "other-run"
    _overwrite_lock_owner(workspace, other_run, pid=99999999)
    before = (run_dir / "run.json").read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()
    assert "run lock belongs to a different run" in capsys.readouterr().err


def test_runs_recover_refuses_invalid_lock_without_mutation(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Corrupt the lock: remove owner.json so run_lock_state returns "invalid".
    lock_path = workspace / ".brigade" / "run.lock"
    (lock_path / "owner.json").unlink()
    before = (run_dir / "run.json").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (run_dir / "run.json").read_bytes() == before
    assert lock_path.is_dir()


def test_runs_recover_terminal_run_with_activated_journal_unchanged(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Mark the run terminal with a stale-lock-recovery failure phase.
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta.update(
        {
            "status": "failed",
            "failure_phase": "stale-lock-recovery",
            "finished_at": "2026-07-27T15:31:00Z",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "detail": "run owner process 99999999 is no longer active",
            },
        }
    )
    _write_json(run_dir / "run.json", run_meta)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"already terminal: {run_dir} [failed]" in out


def test_runs_recover_no_journal_legacy_dead_owner_marks_terminal(tmp_path, capsys):
    # A nonterminal run with a dead matching lock but NO activated journal
    # keeps the legacy recovery path (no checkpoint callback).
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "legacy"
    _write_minimal_run(
        run_dir,
        task="legacy task",
        status="dispatching",
        started_at="2026-07-16T00:00:00Z",
    )
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(workspace)
    _write_json(run_dir / "run.json", run_meta)
    _write_lock_owner(workspace, run_dir)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"
    assert f"recovered: {run_dir}" in capsys.readouterr().out


# -- Issue #568 slice 5 Task 5 sendback: bounded checkpoint reason surfacing --


def test_runs_recover_invalid_checkpoint_surfaces_bounded_reason_and_preserves_lock(tmp_path, capsys):
    """Invalid checkpoint recovery surfaces the bounded checkpoint reason.

    runguard wraps every ``before_terminalize`` callback exception into a
    ``RunLockError`` after restoring the claimed lock, so
    ``_recover_from_checkpoint`` cannot catch ``CheckpointError`` directly.
    It must preserve the original bounded ``CheckpointError`` in a local holder,
    catch the ``RunLockError``, and print the stored checkpoint diagnostic
    while still returning exit 2 and leaving the lock restored.
    """
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    sha = hashlib.sha256(_writer_bytes(run_json_obj)).hexdigest()
    cp_file = run_checkpoint.checkpoint_path(run_dir, sha)
    cp_file.write_bytes(b"x" * len(_writer_bytes(run_json_obj)))
    os.chmod(cp_file, 0o600)
    lock_path = workspace / ".brigade" / "run.lock"

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert not (run_dir / "run.json").exists()
    # The dead lock is restored (callback failed; lock restored by runguard).
    assert lock_path.is_dir()
    # The specific bounded checkpoint reason is surfaced to stderr, not the
    # generic runguard "before_terminalize callback failed" wrapper.
    err = capsys.readouterr().err
    assert "checkpoint digest mismatch" in err
    assert "before_terminalize callback failed" not in err


# -- Issue #568 slice 5 Task 5 second sendback: run.json input edges --


def test_runs_recover_preserves_non_utf8_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    corrupt_bytes = b"\xff\xfe not utf-8"
    (run_dir / "run.json").write_bytes(corrupt_bytes)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == corrupt_bytes
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_preserves_recursion_error_run_json_and_restores_from_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    depth = 20000
    deep_json = ("[" * depth) + ("]" * depth)
    (run_dir / "run.json").write_text(deep_json)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["task"] == "demo task"
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == deep_json
    assert f"recovered: {run_dir}" in capsys.readouterr().out


def test_runs_recover_run_json_read_oserror_exits_without_mutation(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    run_json = run_dir / "run.json"
    before_run_json = run_json.read_bytes()
    lock_path = workspace / ".brigade" / "run.lock"
    lock_pid_before = (lock_path / "pid").read_text()
    lock_owner_before = (lock_path / "owner.json").read_text()

    real_read_text = Path.read_text

    def fail_read_text(self, *args, **kwargs):
        if self == run_json:
            raise OSError(errno.EACCES, "read denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert run_json.read_bytes() == before_run_json
    assert lock_path.is_dir()
    assert (lock_path / "pid").read_text() == lock_pid_before
    assert (lock_path / "owner.json").read_text() == lock_owner_before
    assert "could not read run.json" in capsys.readouterr().err


def test_runs_recover_proceeds_under_foreign_lock_with_matching_stale_claim(tmp_path, capsys):
    """CLI recovery proceeds under a foreign current lock when a matching persistent
    ``.stale`` claim exists, recovering only the matching claim via the checkpoint
    path and leaving the foreign lock byte-for-byte and path-state unchanged.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    # Relocate the matching dead lock to a persistent .stale claim.
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    # Install a FOREIGN dead lock at the lock path (different run_dir).
    foreign_run = workspace / ".brigade" / "runs" / "foreign-run"
    foreign_run.mkdir(parents=True)
    _write_lock_owner(workspace, foreign_run, pid=99999999, owner_token="foreign")
    foreign_owner_bytes = (lock_path / "owner.json").read_bytes()
    foreign_pid_bytes = (lock_path / "pid").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    # The foreign lock is byte-for-byte and path-state unchanged.
    assert lock_path.is_dir()
    assert (lock_path / "owner.json").read_bytes() == foreign_owner_bytes
    assert (lock_path / "pid").read_bytes() == foreign_pid_bytes
    # The matching .stale claim was cleared.
    assert not stale.exists()
    # The run was terminalized via the checkpoint recovery path.
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"


def test_runs_recover_proceeds_under_invalid_lock_with_matching_stale_claim(tmp_path, capsys):
    """CLI recovery proceeds under an invalid current lock when a matching persistent
    ``.stale`` claim exists, recovering only the matching claim and leaving the invalid
    lock path-state unchanged (still a directory with no owner.json).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    # Install an INVALID lock at the lock path: directory with no owner.json.
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{os.getpid()}\n")
    invalid_pid_bytes = (lock_path / "pid").read_bytes()

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    # The invalid lock is path-state unchanged.
    assert lock_path.is_dir()
    assert not (lock_path / "owner.json").exists()
    assert (lock_path / "pid").read_bytes() == invalid_pid_bytes
    assert not stale.exists()
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"


def test_runs_recover_lost_matching_claim_under_foreign_lock_is_bounded(tmp_path, monkeypatch, capsys):
    """When the matching ``.stale`` claim disappears between preflight and recovery,
    CLI recovery returns bounded rc=2 without mutating the foreign lock or run.json.
    """
    import shutil

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "task": "demo task", "cwd": str(workspace)}
    _activate_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    lock_path = workspace / ".brigade" / "run.lock"
    stale = lock_path.with_name(f".{lock_path.name}.crashed.stale")
    lock_path.rename(stale)
    foreign_run = workspace / ".brigade" / "runs" / "foreign-run"
    foreign_run.mkdir(parents=True)
    _write_lock_owner(workspace, foreign_run, pid=99999999, owner_token="foreign")
    foreign_owner_bytes = (lock_path / "owner.json").read_bytes()
    run_json_before = (run_dir / "run.json").read_bytes()

    real_recover = runguard.recover_stale_run

    def lose_claim(cwd, requested_run, **kwargs):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
        return real_recover(cwd, requested_run, **kwargs)

    monkeypatch.setattr(runguard, "recover_stale_run", lose_claim)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 2
    assert (lock_path / "owner.json").read_bytes() == foreign_owner_bytes
    assert (run_dir / "run.json").read_bytes() == run_json_before
    err = capsys.readouterr().err
    assert "run lock not found for run:" in err
    assert "Traceback" not in err


def test_runs_recover_restores_from_retained_lock_after_initial_run_json_write_failure(tmp_path, monkeypatch, capsys):
    """``runs_cmd.recover`` restores a run whose first ``run.json`` write failed
    after checkpoint publication, leaving the lock retained with durable
    journal/checkpoint state but no ``run.json``.

    This is the end-to-end recovery assertion for the
    ``RetainRunLockError`` translation in ``aboyeur.record_run_start``: the lock
    is retained (not orphaned), the lifecycle journal and recovery checkpoint
    survive, and ``runs_cmd.recover`` terminalizes the run from the retained
    state.
    """
    from brigade import aboyeur
    from brigade.roster import Agent, Roster

    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)

    real_write_text_atomic = aboyeur.localio.write_text_atomic

    def fail_run_json_write(path, text):
        if Path(path).name == "run.json":
            raise OSError("receipt disk full")
        return real_write_text_atomic(path, text)

    monkeypatch.setattr(aboyeur.localio, "write_text_atomic", fail_run_json_write)

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=1,
    )

    with pytest.raises(runguard.RetainRunLockError):
        with runguard.run_lock(workspace, run_dir=run_dir):
            aboyeur.record_run_start(
                run_dir,
                task="demo task",
                cwd=workspace,
                roster=roster,
                read_only=False,
                lock_workspace=workspace,
            )

    # Pre-conditions: lock retained, run.json absent, durable state present.
    lock_path = workspace / ".brigade" / "run.lock"
    assert lock_path.is_dir()
    assert not (run_dir / "run.json").is_file()
    assert run_lifecycle._journal_path(run_dir).is_file()
    assert any(run_checkpoint.checkpoint_dir(run_dir).glob("*.json"))

    # Mark the retained lock owner dead so recovery can claim it.
    _overwrite_lock_owner(workspace, run_dir, pid=99999999)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    # Stop failing the write so recovery can write the terminal receipt.
    monkeypatch.setattr(aboyeur.localio, "write_text_atomic", real_write_text_atomic)

    rc = runs_cmd.recover(str(run_dir), cwd=workspace)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"recovered: {run_dir}" in out
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"
    assert not lock_path.exists()
