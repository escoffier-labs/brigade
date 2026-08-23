"""Tests for brigade runs resume."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from brigade import agents, codex_appserver, message_envelope, provenance, runguard, run_resume, runs_cmd


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
    (run_dir / "synthesis.json").write_text(
        json.dumps(
            {
                "orchestrator": "chef",
                "result": {"ok": False, "text": "stale synthesis"},
                "ground_truth": {},
            }
        )
    )
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


def test_roster_snapshot_preserves_read_only_capability_with_legacy_default():
    snapshot = {
        "orchestrator": "chef",
        "agents": {
            "chef": {"cli": "codex", "role": "plan"},
            "composer": {
                "cli": "cursor",
                "role": "implement",
                "read_only_capable": False,
            },
        },
    }

    roster = run_resume._roster_from_snapshot(snapshot)

    assert roster.agents["chef"].read_only_capable is True
    assert roster.agents["composer"].read_only_capable is False


def test_roster_snapshot_preserves_explicit_capabilities():
    snapshot = {
        "orchestrator": "chef",
        "agents": {
            "chef": {"cli": "codex", "role": "plan"},
            "luna": {
                "cli": "codex",
                "role": "researcher",
                "capabilities": ["research.plan", "research.extract", "research.review"],
            },
        },
    }

    roster = run_resume._roster_from_snapshot(snapshot)

    assert roster.agents["luna"].capabilities == (
        "research.plan",
        "research.extract",
        "research.review",
    )
    assert roster.agents["chef"].capabilities == ()


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
    legacy_worker_results = json.loads((run_dir / "worker-results.json").read_text())
    legacy_synthesis = json.loads((run_dir / "synthesis.json").read_text())
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
            "finished_at": "2026-07-03T00:05:00+00:00",
            "duration_seconds": 300.0,
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
    assert run_json["finished_at"] != recovered["finished_at"]
    assert run_json["duration_seconds"] != recovered["duration_seconds"]
    worker_revisions = sorted((run_dir / "revisions" / "worker-results").glob("*.json"))
    synthesis_revisions = sorted((run_dir / "revisions" / "synthesis").glob("*.json"))
    assert [path.name for path in worker_revisions] == ["000001.json", "000002.json"]
    assert [path.name for path in synthesis_revisions] == ["000001.json", "000002.json"]
    assert json.loads(worker_revisions[0].read_text()) == legacy_worker_results
    assert json.loads(synthesis_revisions[0].read_text()) == legacy_synthesis
    assert json.loads(worker_revisions[1].read_text()) == json.loads((run_dir / "worker-results.json").read_text())
    assert json.loads(synthesis_revisions[1].read_text()) == json.loads((run_dir / "synthesis.json").read_text())


def test_resume_re_envelopes_output_and_drops_transplanted_identity(tmp_path, monkeypatch):
    from brigade import aboyeur, message_envelope

    stale = message_envelope.emit(
        "part",
        kind="worker-result",
        producer="run_transport.dispatch",
        from_seat="cook",
        to_seat="chef",
        run_id="other-run",
        assignment_id=message_envelope.assignment_id_for("cook", "write code"),
    )
    assert stale.delivered, stale.reason
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
                "provenance": stale.envelope,
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
            "finished_at": "2026-07-03T00:05:00+00:00",
            "duration_seconds": 300.0,
        }
    )
    (run_dir / "run.json").write_text(json.dumps(recovered))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    assert run_resume.resume(run_dir) == 0
    results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    env = results[0]["provenance"]
    assert results[0]["text"] == "finished now"
    assert env["message"]["run_id"] == run_dir.name
    assert env["message"]["from_seat"] == "cook"
    assert env["message"]["to_seat"] == "chef"
    assert env["message"]["run_id"] != "other-run"
    assert env["hashes"]["content"] != stale.envelope["hashes"]["content"]
    admitted = aboyeur.build_synth_prompt(
        "big task",
        [
            aboyeur.WorkerResult(
                worker=results[0]["worker"],
                task=results[0]["task"],
                text=results[0]["text"],
                ok=True,
                provenance=env,
            )
        ],
        run_id=run_dir.name,
        to_seat="chef",
    )
    assert "finished now" in admitted
    assert "part" not in admitted


def test_resume_truncates_worker_text_before_envelope_hash(tmp_path, monkeypatch):
    oversized = "W" * (message_envelope.MESSAGE_WRAP_MAX_BYTES + 4096)
    bounded = message_envelope.truncate_utf8(oversized)

    class HugeThread(_StubThread):
        def run_turn(self, prompt, *, timeout, on_event=None):  # noqa: ARG002
            return codex_appserver.TurnResult(
                text=oversized,
                ok=True,
                status="complete",
                thread_id=self.thread_id,
            )

    class HugeServer(_StubServer):
        def resume_thread(self, thread_id, *, cwd, model=None, sandbox=None):  # noqa: ARG002
            self.resumed.append(thread_id)
            return HugeThread(thread_id)

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
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", HugeServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )

    assert run_resume.resume(run_dir) == 0
    results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert results[0]["text"] == bounded
    assert len(results[0]["text"].encode("utf-8")) == message_envelope.MESSAGE_WRAP_MAX_BYTES
    assert results[0]["provenance"]["hashes"]["content"] == provenance.content_sha256(bounded)
    assert results[0]["provenance"]["hashes"]["content"] != provenance.content_sha256(oversized)


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


def test_resume_recovers_appserver_thread_from_interrupted_dispatch_events(tmp_path, monkeypatch):
    run_dir = _write_run_dir(tmp_path, results=[])
    (run_dir / "worker-results.json").unlink()
    meta = json.loads((run_dir / "run.json").read_text())
    meta.update({"status": "dispatching", "codex_transport": "app-server"})
    (run_dir / "run.json").write_text(json.dumps(meta))
    events = run_dir / "events"
    events.mkdir()
    (events / "cook.jsonl").write_text(
        json.dumps({"method": "turn/started", "params": {"threadId": "t-recovered", "turnId": "turn-1"}}) + "\n"
    )

    def recover_stale(workspace, candidate, *, required):
        assert candidate == run_dir
        assert required is False
        return False

    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", recover_stale)
    monkeypatch.setattr(run_resume.runguard, "run_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(text="recovered synthesis", ok=True),
    )

    assert run_resume.resume(run_dir) == 0
    results = json.loads((run_dir / "worker-results.json").read_text())["results"]
    assert results[0]["thread_id"] == "t-recovered"
    assert results[0]["ok"] is True
    final_meta = json.loads((run_dir / "run.json").read_text())
    assert final_meta["recovery_history"][0]["kind"] == "owner-process-exited"


def test_resume_refuses_interrupted_multistage_appserver_salvage(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(tmp_path, results=[])
    (run_dir / "worker-results.json").unlink()
    meta = json.loads((run_dir / "run.json").read_text())
    meta.update({"status": "dispatching", "codex_transport": "app-server", "active_stage": 2})
    (run_dir / "run.json").write_text(json.dumps(meta))
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "assignments": [
                    {"stage": 1, "worker": "cook", "task": "stage one"},
                    {"stage": 2, "worker": "cook", "task": "stage two"},
                ]
            }
        )
    )
    events = run_dir / "events"
    events.mkdir()
    (events / "cook.jsonl").write_text(
        json.dumps({"method": "turn/started", "params": {"threadId": "t-stage-two", "turnId": "turn-2"}}) + "\n"
    )
    provider_calls: list[str] = []

    class GuardServer:
        def __init__(self, *args, **kwargs):
            provider_calls.append("constructed")

        def start(self):
            provider_calls.append("started")

    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", lambda *args, **kwargs: False)
    monkeypatch.setattr(run_resume.runguard, "run_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", GuardServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synthesis must not run")),
    )

    assert run_resume.resume(run_dir) == 2
    assert provider_calls == []
    err = capsys.readouterr().err
    assert "stage 2" in err
    assert "earlier-stage worker results were not persisted" in err


@pytest.mark.parametrize("malformed_results", [None, {"worker": "cook"}])
def test_resume_refuses_multistage_salvage_with_malformed_worker_results(
    tmp_path, monkeypatch, capsys, malformed_results
):
    run_dir = _write_run_dir(tmp_path, results=[])
    (run_dir / "worker-results.json").write_text(json.dumps({"results": malformed_results, "ground_truth": {}}))
    meta = json.loads((run_dir / "run.json").read_text())
    meta.update({"status": "dispatching", "codex_transport": "app-server", "active_stage": 2})
    (run_dir / "run.json").write_text(json.dumps(meta))
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "assignments": [
                    {"stage": 1, "worker": "cook", "task": "stage one"},
                    {"stage": 2, "worker": "cook", "task": "stage two"},
                ]
            }
        )
    )
    events = run_dir / "events"
    events.mkdir()
    (events / "cook.jsonl").write_text(
        json.dumps({"method": "turn/started", "params": {"threadId": "t-stage-two", "turnId": "turn-2"}}) + "\n"
    )
    provider_calls: list[str] = []

    class GuardServer:
        def __init__(self, *args, **kwargs):
            provider_calls.append("constructed")

        def start(self):
            provider_calls.append("started")

    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", lambda *args, **kwargs: False)
    monkeypatch.setattr(run_resume.runguard, "run_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", GuardServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synthesis must not run")),
    )

    assert run_resume.resume(run_dir) == 2
    assert provider_calls == []
    err = capsys.readouterr().err
    assert "stage 2" in err
    assert "earlier-stage worker results were not persisted" in err


def test_resume_synthesis_failure_replaces_owner_exit_attribution(tmp_path, monkeypatch):
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
            "status": "failed",
            "error": "run owner exited before recording a terminal state",
            "failure_phase": "dispatching",
            "failure": {
                "phase": "dispatching",
                "kind": "owner-process-exited",
                "detail": "run owner exited before recording a terminal state",
            },
            "finished_at": "2026-07-03T00:05:00+00:00",
            "duration_seconds": 300.0,
        }
    )
    (run_dir / "run.json").write_text(json.dumps(recovered))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="synthesis failed", ok=False, detail="orchestrator timeout"),
    )

    assert run_resume.resume(run_dir) == 2
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["status"] == "failed"
    assert run_json["failure"]["kind"] == "agent-error"
    assert run_json["failure"]["phase"] == "synthesis"
    assert run_json["failure"]["seat"] == "chef"
    assert run_json["recovery_history"] == [recovered["failure"]]
    assert run_json["finished_at"] != recovered["finished_at"]


def test_approval_resume_requires_reserved_snapshot_before_provider_start(tmp_path, monkeypatch):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "approval required",
                "text": "",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    meta = json.loads((run_dir / "run.json").read_text())
    meta["status"] = "running"
    meta["approval_reference"] = {
        "approval_id": "approval-1",
        "source": "daily",
        "fingerprint": "approval-fingerprint",
        "source_fingerprint": "source-fingerprint",
        "contract_fingerprint": "contract-fingerprint",
        "evidence_fingerprint": "evidence-fingerprint",
        "decision_state": "approved",
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    writes = []
    token = "a" * 43

    def write_json(path, payload):
        if path.name == "run.json":
            writes.append(payload["status"])
        path.write_text(json.dumps(payload))

    class OrderedServer(_StubServer):
        def __init__(self, *args, **kwargs):
            assert kwargs["env"] == {runs_cmd._APPROVAL_RESUME_TOKEN_ENV: token}
            super().__init__(*args, **kwargs)

        def start(self):
            assert writes == []

        def resume_thread(self, thread_id, *, cwd, model=None, sandbox=None):
            class TokenEchoThread(_StubThread):
                def run_turn(self, prompt, *, timeout, on_event=None):
                    result = super().run_turn(prompt, timeout=timeout, on_event=on_event)
                    return codex_appserver.TurnResult(
                        text=f"{result.text} {token}",
                        ok=result.ok,
                        status=result.status,
                        thread_id=result.thread_id,
                    )

            return TokenEchoThread(thread_id)

    monkeypatch.setattr(run_resume.aboyeur, "_write_json", write_json)
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", OrderedServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(text="approved result", ok=True),
    )
    monkeypatch.setattr(runs_cmd, "_finalize_redeemed_approval", lambda *args, **kwargs: None)

    assert run_resume._resume_locked(run_dir, approval_resume=True, approval_token=token) == 0
    assert writes == ["ok"]
    worker_results = (run_dir / "worker-results.json").read_text()
    assert token not in worker_results
    assert "[redacted]" in worker_results


def test_approval_resume_retries_after_app_server_start_failure_without_provider_duplication(
    tmp_path,
    monkeypatch,
):
    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "approval required",
                "text": "",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    meta = json.loads((run_dir / "run.json").read_text())
    meta["status"] = "running"
    meta["approval_reference"] = {
        "approval_id": "approval-1",
        "source": "daily",
        "fingerprint": "approval-fingerprint",
        "source_fingerprint": "source-fingerprint",
        "contract_fingerprint": "contract-fingerprint",
        "evidence_fingerprint": "evidence-fingerprint",
        "decision_state": "approved",
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    starts = []
    provider_calls = []
    token = "b" * 43

    class FlakyServer(_StubServer):
        def start(self):
            starts.append("start")
            if len(starts) == 1:
                raise codex_appserver.AppServerError("not ready")

        def resume_thread(self, thread_id, *, cwd, model=None, sandbox=None):
            provider_calls.append(thread_id)
            return super().resume_thread(thread_id, cwd=cwd, model=model, sandbox=sandbox)

    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", FlakyServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(text="approved result", ok=True),
    )
    monkeypatch.setattr(runs_cmd, "_finalize_redeemed_approval", lambda *args, **kwargs: None)

    assert run_resume._resume_locked(run_dir, approval_resume=True, approval_token=token) == 2
    assert run_resume._resume_locked(run_dir, approval_resume=True, approval_token=token) == 0
    assert starts == ["start", "start"]
    assert provider_calls == ["t-1"]


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
    from brigade import runs_cmd

    seen = {}
    monkeypatch.setattr(runs_cmd, "resume", lambda run_dir: seen.update(run_dir=run_dir) or 0)
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


# -- Issue #568 slice 6, Task 7: authority receipt-write lock retention ------------


def test_authority_resume_failed_synthesis_receipt_write_retains_lock(tmp_path, monkeypatch):
    from brigade import aboyeur, run_events, run_lifecycle, runguard

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
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["run_journal_authority_requested"] = True
    run_meta["lifecycle_journal_requested"] = True
    run_meta["cwd"] = str(tmp_path)
    run_meta["lock_workspace"] = str(tmp_path)
    (run_dir / "run.json").write_text(json.dumps(run_meta))
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="synthesis failed", ok=False, detail="timeout"),
    )
    real_write_json = aboyeur._write_json

    def failing_write_json(path, payload, **kwargs):
        if Path(path).name == "run.json":
            raise run_lifecycle.LifecycleJournalError(run_events._bound("projection failed"))
        return real_write_json(path, payload, **kwargs)

    monkeypatch.setattr(aboyeur, "_write_json", failing_write_json)
    with pytest.raises(runguard.RetainRunLockError):
        run_resume.resume(run_dir)
    lock_path = tmp_path / ".brigade" / "run.lock"
    assert lock_path.exists()


def test_authority_resume_successful_synthesis_receipt_write_retains_lock(tmp_path, monkeypatch):
    from brigade import aboyeur, run_events, run_lifecycle, runguard

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
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["run_journal_authority_requested"] = True
    run_meta["lifecycle_journal_requested"] = True
    run_meta["cwd"] = str(tmp_path)
    run_meta["lock_workspace"] = str(tmp_path)
    (run_dir / "run.json").write_text(json.dumps(run_meta))
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    real_write_json = aboyeur._write_json

    def failing_write_json(path, payload, **kwargs):
        if Path(path).name == "run.json":
            raise run_lifecycle.LifecycleJournalError(run_events._bound("projection failed"))
        return real_write_json(path, payload, **kwargs)

    monkeypatch.setattr(aboyeur, "_write_json", failing_write_json)
    with pytest.raises(runguard.RetainRunLockError):
        run_resume.resume(run_dir)
    lock_path = tmp_path / ".brigade" / "run.lock"
    assert lock_path.exists()


# -- Issue #593: resume must honor declared run budgets before AppServer.start ----


_RESUMABLE_COOK = {
    "worker": "cook",
    "task": "write code",
    "ok": False,
    "detail": "timeout",
    "text": "part",
    "thread_id": "t-1",
    "status": "interrupted",
}


def _declared_contract(*, wall_clock_seconds: int | None = 3600, worker_dispatch_count: int | None = 10) -> dict:
    from brigade import verification_contract

    budget: dict = {"latency_seconds": wall_clock_seconds or 3600}
    if wall_clock_seconds is not None:
        budget["wall_clock_seconds"] = wall_clock_seconds
    if worker_dispatch_count is not None:
        budget["worker_dispatch_count"] = worker_dispatch_count
    return {
        "schema": verification_contract.VERIFICATION_CONTRACT_SCHEMA,
        "schema_version": verification_contract.VERIFICATION_CONTRACT_SCHEMA_VERSION,
        "verifier": {"source": "command", "command": "true"},
        "rollback": {"policy": "none"},
        "budget": budget,
    }


class _GuardServer(_StubServer):
    started = 0
    provider_turns = 0

    def start(self):
        type(self).started += 1
        raise AssertionError("AppServer.start must not run when resume budget gate denies")

    def resume_thread(self, thread_id, *, cwd, model=None, sandbox=None):
        type(self).provider_turns += 1
        raise AssertionError("provider turn must not run when resume budget gate denies")


def _patch_resume_denied(monkeypatch) -> None:
    _GuardServer.started = 0
    _GuardServer.provider_turns = 0
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _GuardServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("synthesis must not run")),
    )
    monkeypatch.setattr(run_resume.runguard, "run_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", lambda *a, **k: False)


def _write_trusted_lifecycle_prefix(run_dir: Path) -> Path:
    from brigade import run_journal

    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        expected_previous_sequence=0,
        recorded_at="2026-07-03T00:00:00.000000Z",
    )
    return journal


def test_resume_refuses_expired_declared_wall_clock_before_appserver(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=5, worker_dispatch_count=10)
    (run_dir / "run.json").write_text(json.dumps(meta))
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["verification_contract"] = meta["verification_contract"]
    (run_dir / "plan.json").write_text(json.dumps(plan))
    _write_trusted_lifecycle_prefix(run_dir)

    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "wall-clock budget exhausted" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_budget_exhausted_projection_before_appserver(tmp_path, monkeypatch, capsys):
    from brigade import run_journal

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=1)
    meta["failure"] = {"phase": "dispatch", "kind": "budget-exhausted", "detail": "dispatch ceiling"}
    (run_dir / "run.json").write_text(json.dumps(meta))
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        expected_previous_sequence=0,
        recorded_at="2026-07-03T00:00:00.000000Z",
    )
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run_budget.exhausted",
        payload={
            "dimension": "worker_dispatch_count",
            "mode": "enforceable",
            "declared": 1,
            "used": 1,
            "remaining": 0,
            "reason_class": "exhausted",
        },
        idempotency_key="run_budget.exhausted:worker_dispatch_count",
        expected_previous_sequence=1,
        recorded_at="2026-07-03T00:00:01.000000Z",
    )

    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "run budget exhausted" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_corrupt_lifecycle_journal_before_appserver(tmp_path, monkeypatch, capsys):
    """Declared resume must fail closed when exhausted prefix is followed by a corrupt tail."""
    from datetime import datetime, timedelta, timezone

    from brigade import run_journal

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    # Non-expired wall-clock so a fail-open empty projection would otherwise allow resume.
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=1)
    (run_dir / "run.json").write_text(json.dumps(meta))
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["verification_contract"] = meta["verification_contract"]
    (run_dir / "plan.json").write_text(json.dumps(plan))

    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        expected_previous_sequence=0,
        recorded_at="2026-07-03T00:00:00.000000Z",
    )
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run_budget.exhausted",
        payload={
            "dimension": "worker_dispatch_count",
            "mode": "enforceable",
            "declared": 1,
            "used": 1,
            "remaining": 0,
            "reason_class": "exhausted",
        },
        idempotency_key="run_budget.exhausted:worker_dispatch_count",
        expected_previous_sequence=1,
        recorded_at="2026-07-03T00:00:01.000000Z",
    )
    # Corrupt partial tail after a valid exhausted prefix (no terminating newline).
    with journal.open("ab") as handle:
        handle.write(b'{"event_type":"not-a-valid-envelope"')

    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    err = capsys.readouterr().err
    assert "untrusted" in err or "incompatible" in err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_malformed_persisted_budget_before_appserver(tmp_path, monkeypatch, capsys):
    """Malformed ceiling values must not silently become undeclared/unbounded."""
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    contract = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=10)
    contract["budget"]["wall_clock_seconds"] = "not-an-int"
    contract["budget"]["worker_dispatch_count"] = "also-bad"
    meta["verification_contract"] = contract
    (run_dir / "run.json").write_text(json.dumps(meta))
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["verification_contract"] = contract
    (run_dir / "plan.json").write_text(json.dumps(plan))

    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_missing_lifecycle_journal_for_declared_budget(tmp_path, monkeypatch, capsys):
    """Persisted declared resume must fail closed when the lifecycle journal is absent."""
    from datetime import datetime, timedelta, timezone

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=10)
    (run_dir / "run.json").write_text(json.dumps(meta))
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["verification_contract"] = meta["verification_contract"]
    (run_dir / "plan.json").write_text(json.dumps(plan))
    assert not (run_dir / "events" / "lifecycle.jsonl").exists()

    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    err = capsys.readouterr().err
    assert "journal" in err.lower() or "untrusted" in err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_wrong_type_budget_shape_before_appserver(tmp_path, monkeypatch, capsys):
    """Wrong-type / empty / invalid declaration markers must not degrade to undeclared."""
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    meta["verification_contract"] = {
        "schema": "brigade.verification_contract.v1",
        "schema_version": 1,
        "verifier": {"source": "command", "command": "true"},
        "rollback": {"policy": "none"},
        "budget": "not-an-object",
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


def test_resume_refuses_empty_persisted_run_budget_before_appserver(tmp_path, monkeypatch, capsys):
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    meta["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {},
        "observed": {},
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


def test_resume_refuses_projected_usage_exhaustion_without_exhausted_event(tmp_path, monkeypatch, capsys):
    """Crash after the last allowed dispatch must refuse even without run_budget.exhausted."""
    from datetime import datetime, timedelta, timezone

    from brigade import run_journal

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=1)
    (run_dir / "run.json").write_text(json.dumps(meta))
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["verification_contract"] = meta["verification_contract"]
    (run_dir / "plan.json").write_text(json.dumps(plan))

    journal = _write_trusted_lifecycle_prefix(run_dir)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="run.dispatch.requested",
        payload={"seat": "cook", "attempt": 1, "detail": "dispatch"},
        idempotency_key="dispatch:cook:1",
        expected_previous_sequence=1,
        recorded_at="2026-07-03T00:00:01.000000Z",
    )

    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "budget exhausted" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"


def test_resume_refuses_unknown_verification_budget_dimension_before_appserver(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    contract = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=10)
    contract["budget"]["child_worker_dispatch_count"] = 2
    meta["verification_contract"] = contract
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


def test_resume_refuses_unsupported_verification_budget_schema_version(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    contract = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=10)
    contract["budget"]["schema"] = "brigade.run_budget.v1"
    contract["budget"]["schema_version"] = 99
    meta["verification_contract"] = contract
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_resume_refuses_non_integer_budget_schema_version_before_appserver(
    tmp_path, monkeypatch, capsys, schema_version
):
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    meta["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": schema_version,
        "ceilings": {"worker_dispatch_count": 1},
    }
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


@pytest.mark.parametrize(
    "outer_budget",
    [
        {
            "schema": "brigade.run_budget.v1",
            "schema_version": 99,
            "declaration": {
                "schema": "brigade.run_budget.v1",
                "schema_version": 1,
                "ceilings": {"worker_dispatch_count": 1},
            },
        },
        {
            "schema": "brigade.run_budget.v1",
            "schema_version": 1,
            "declaration": {
                "schema": "brigade.run_budget.v1",
                "schema_version": 1,
                "ceilings": {"worker_dispatch_count": 1},
            },
            "unexpected": True,
        },
    ],
)
def test_resume_refuses_incompatible_outer_run_budget_before_appserver(tmp_path, monkeypatch, capsys, outer_budget):
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    meta["run_budget"] = outer_budget
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_resume_refuses_non_integer_parent_contract_schema_version_before_appserver(
    tmp_path, monkeypatch, capsys, schema_version
):
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    contract = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=1)
    contract["schema_version"] = schema_version
    meta["verification_contract"] = contract
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)
    _patch_resume_denied(monkeypatch)

    assert run_resume.resume(run_dir) == 2
    assert "incompatible" in capsys.readouterr().err
    assert _GuardServer.started == 0
    assert _GuardServer.provider_turns == 0


def test_resume_allows_non_expired_declared_budget(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    meta = json.loads((run_dir / "run.json").read_text())
    started = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    meta["started_at"] = started.isoformat()
    meta["verification_contract"] = _declared_contract(wall_clock_seconds=3600, worker_dispatch_count=10)
    (run_dir / "run.json").write_text(json.dumps(meta))
    _write_trusted_lifecycle_prefix(run_dir)

    monkeypatch.setattr(run_resume, "_utc_now", lambda: started + timedelta(seconds=10))
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", lambda *a, **k: False)

    assert run_resume.resume(run_dir) == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"
    assert (run_dir / "final.txt").read_text().strip() == "final synthesis"


def test_resume_allows_undeclared_budget_for_backward_compatibility(tmp_path, monkeypatch):
    run_dir = _write_run_dir(tmp_path, results=[dict(_RESUMABLE_COOK)])
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    monkeypatch.setattr(run_resume.runguard, "run_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(run_resume.runguard, "recover_stale_run", lambda *a, **k: False)

    assert run_resume.resume(run_dir) == 0
    assert json.loads((run_dir / "run.json").read_text())["status"] == "ok"
