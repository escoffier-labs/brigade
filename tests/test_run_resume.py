"""Tests for brigade runs resume."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import agents, codex_appserver, runguard, run_resume


def _write_run_dir(tmp_path: Path, *, results: list[dict]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "task": "big task",
                "cwd": str(tmp_path),
                "orchestrator": "chef",
                "read_only": False,
                "status": "failed",
                "started_at": "2026-07-03T00:00:00+00:00",
            }
        )
    )
    (run_dir / "roster.json").write_text(
        json.dumps(
            {
                "orchestrator": "chef",
                "max_workers": 4,
                "timeout_seconds": 600.0,
                "allow_models": [],
                "sandbox": None,
                "agents": {
                    "chef": {"cli": "claude", "model": None, "role": "plan", "timeout_seconds": None},
                    "cook": {"cli": "codex", "model": None, "role": "code", "timeout_seconds": None},
                },
            }
        )
    )
    (run_dir / "plan.json").write_text(
        json.dumps({"assignments": [{"stage": 1, "worker": "cook", "task": "write code"}]})
    )
    (run_dir / "worker-results.json").write_text(json.dumps({"results": results, "ground_truth": {}}))
    return run_dir


class _StubThread:
    def __init__(self, thread_id):
        self.thread_id = thread_id

    def run_turn(self, prompt, *, timeout, on_event=None):
        assert "big task" not in prompt  # continuation carries the sub-task, not the run task
        assert "write code" in prompt
        return codex_appserver.TurnResult(text="finished now", ok=True, status="complete", thread_id=self.thread_id)


class _StubServer:
    def __init__(self, *a, **k):
        self.resumed = []

    def start(self):
        pass

    def close(self):
        pass

    def resume_thread(self, thread_id, *, cwd, model=None, sandbox=None):
        self.resumed.append(thread_id)
        return _StubThread(thread_id)


def test_roster_snapshot_preserves_invalid_final_fallback():
    snapshot = {
        "orchestrator": "chef",
        "agents": {
            "chef": {"cli": "codex", "role": "plan"},
            "grok-review": {
                "cli": "grok",
                "role": "review",
                "invalid_final_fallback": "cursor-grok",
            },
            "cursor-grok": {
                "cli": "cursor",
                "role": "fallback review",
                "transport": "acpx",
                "transport_version": "0.12.0",
            },
        },
    }

    roster = run_resume._roster_from_snapshot(snapshot)

    assert roster.agents["grok-review"].invalid_final_fallback == "cursor-grok"


def test_roster_snapshot_rejects_inline_secret_env():
    snapshot = {
        "orchestrator": "chef",
        "agents": {
            "chef": {
                "cli": "claude",
                "role": "plan",
                "env": {"ANTHROPIC_AUTH_TOKEN": "not-for-storage"},
            }
        },
    }

    with pytest.raises(ValueError, match="pass it by reference"):
        run_resume._roster_from_snapshot(snapshot)


def test_roster_snapshot_rejects_colliding_env_targets():
    snapshot = {
        "orchestrator": "chef",
        "agents": {
            "chef": {
                "cli": "claude",
                "role": "plan",
                "env": {"LANE_MODE": "direct", "LANE_MODE_REF": "PARENT_LANE_MODE"},
            }
        },
    }

    with pytest.raises(ValueError, match="both resolve to LANE_MODE"):
        run_resume._roster_from_snapshot(snapshot)


def test_resume_reattaches_and_resynthesizes(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "part",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    recovered = json.loads((run_dir / "run.json").read_text())
    recovered.update(
        {
            "error": "run owner process 99999999 is no longer active",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "owner_pid": 99999999,
            },
        }
    )
    (run_dir / "run.json").write_text(json.dumps(recovered))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    rc = run_resume.resume(run_dir)
    assert rc == 0
    results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert results[0]["ok"] is True and results[0]["text"] == "finished now"
    assert results[0]["status"] == "complete"
    assert (run_dir / "final.txt").read_text().strip() == "final synthesis"
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["status"] == "ok"
    assert run_json["resumed_at"]
    assert run_json["recovery_history"] == [recovered["failure"]]
    assert "failure_phase" not in run_json
    assert "failure" not in run_json


def test_resume_with_nothing_resumable_reports_and_exits_2(tmp_path, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[{"worker": "cook", "task": "write code", "ok": False, "detail": "exec timeout", "text": ""}],
    )
    rc = run_resume.resume(run_dir)
    assert rc == 2
    err = capsys.readouterr().err
    assert "no resumable workers" in err
    assert "cook" in err  # names the non-resumable failure


def test_resume_missing_artifacts_errors(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_resume.resume(empty) == 2
    assert "missing" in capsys.readouterr().err


def test_resume_refuses_nonterminal_run(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "thread_id": "t-1",
                "status": "interrupted",
            }
        ],
    )
    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider must not start")),
    )

    for status in ("dispatching", "result-processing", "artifact-collection"):
        run_meta = json.loads((run_dir / "run.json").read_text())
        run_meta["status"] = status
        (run_dir / "run.json").write_text(json.dumps(run_meta))

        assert run_resume.resume(run_dir) == 2
        assert "run is not terminal" in capsys.readouterr().err


def test_resume_refuses_matching_live_owner(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "thread_id": "t-1",
                "status": "interrupted",
            }
        ],
    )
    lock = runguard.lock_path(tmp_path)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "owner.json").write_text(
        json.dumps({"owner_token": "live", "pid": os.getpid(), "run_dir": str(run_dir.resolve())})
    )
    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider must not start")),
    )

    assert run_resume.resume(run_dir) == 2
    assert "run owner process is still active" in capsys.readouterr().err
    assert lock.is_dir()


def test_resume_refuses_foreign_live_owner(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "thread_id": "t-1",
                "status": "interrupted",
            }
        ],
    )
    foreign_run = tmp_path / "foreign-run"
    lock = runguard.lock_path(tmp_path)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "owner.json").write_text(
        json.dumps({"owner_token": "live", "pid": os.getpid(), "run_dir": str(foreign_run.resolve())})
    )
    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider must not start")),
    )

    assert run_resume.resume(run_dir) == 2
    assert "another brigade run appears active" in capsys.readouterr().err
    assert lock.is_dir()


def test_resume_infers_lock_workspace_for_legacy_worktree_run(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_run_dir = _write_run_dir(
        workspace,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "thread_id": "t-1",
                "status": "interrupted",
            }
        ],
    )
    run_dir = workspace / ".brigade" / "runs" / "legacy"
    run_dir.parent.mkdir(parents=True)
    original_run_dir.rename(run_dir)
    detached = tmp_path / "detached"
    detached.mkdir()
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["cwd"] = str(detached)
    (run_dir / "run.json").write_text(json.dumps(run_meta))
    foreign_run = workspace / ".brigade" / "runs" / "foreign"
    lock = runguard.lock_path(workspace)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "owner.json").write_text(
        json.dumps({"owner_token": "live", "pid": os.getpid(), "run_dir": str(foreign_run.resolve())})
    )
    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider must not start")),
    )

    assert run_resume.resume(run_dir) == 2
    assert "another brigade run appears active" in capsys.readouterr().err
    assert lock.is_dir()


def test_runs_resume_cli_dispatches(tmp_path, monkeypatch):
    from brigade import cli

    seen = {}
    monkeypatch.setattr(run_resume, "resume", lambda run_dir: seen.update(run_dir=run_dir) or 0)
    rc = cli.main(["runs", "resume", str(tmp_path)])
    assert rc == 0
    assert seen["run_dir"] == tmp_path


def test_resume_orchestrator_without_cli_errors(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    roster = json.loads((run_dir / "roster.json").read_text())
    roster["agents"]["chef"]["cli"] = None
    (run_dir / "roster.json").write_text(json.dumps(roster))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    rc = run_resume.resume(run_dir)
    assert rc == 2
    assert "no CLI" in capsys.readouterr().err
    # Resumed worker progress is still persisted even though synthesis was skipped.
    results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert results[0]["text"] == "finished now"


def test_resume_synthesis_carries_orchestrator_env(tmp_path, monkeypatch):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "part",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    roster_snapshot = json.loads((run_dir / "roster.json").read_text())
    roster_snapshot["agents"]["chef"]["env"] = {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic"}
    (run_dir / "roster.json").write_text(json.dumps(roster_snapshot))
    recovered = json.loads((run_dir / "run.json").read_text())
    recovered.update(
        {
            "error": "run owner process 99999999 is no longer active",
            "failure_phase": "stale-lock-recovery",
            "failure": {
                "phase": "stale-lock-recovery",
                "kind": "owner-process-exited",
                "owner_pid": 99999999,
            },
        }
    )
    (run_dir / "run.json").write_text(json.dumps(recovered))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    captured = {}

    def fake_run_agent(cli, prompt, **kwargs):
        captured["env"] = kwargs.get("env")
        return agents.AgentResult(text="final synthesis", ok=True)

    monkeypatch.setattr(run_resume.agents, "run_agent", fake_run_agent)
    assert run_resume.resume(run_dir) == 0
    assert captured["env"] == {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic"}


def test_resume_rejects_invalid_snapshot_env_before_dispatch(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "part",
                "thread_id": "t-1",
                "status": "interrupted",
            }
        ],
    )
    roster_snapshot = json.loads((run_dir / "roster.json").read_text())
    roster_snapshot["agents"]["chef"]["env"] = {"ANTHROPIC_AUTH_TOKEN": "not-for-storage"}
    (run_dir / "roster.json").write_text(json.dumps(roster_snapshot))
    monkeypatch.setattr(
        run_resume.codex_appserver,
        "AppServer",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch must not start")),
    )

    assert run_resume._resume_locked(run_dir) == 2
    assert "invalid roster snapshot" in capsys.readouterr().err
