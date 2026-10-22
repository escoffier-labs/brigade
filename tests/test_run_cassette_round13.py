"""Real-run retention and fallback stream isolation regressions."""

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from brigade import aboyeur, agents, cli, run_transport, runguard
from brigade import run_cassette_store as store
from brigade.aboyeur import artifacts, run_io
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded
from tests.test_run_cassette import _documents
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_transport_recovery import _invalid_final, _recovery_roster


@pytest.fixture
def recorded_run(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="answer", ok=True))

    def no_control_socket(self):
        raise aboyeur.run_control.ControlError("control socket unavailable under test")

    monkeypatch.setattr(aboyeur.run_control.ControlServer, "start", no_control_socket)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="claude", role="plan"),
            "worker": Agent(name="worker", cli="codex", role="research"),
        },
    )

    def create(name):
        run = tmp_path / ".brigade" / "runs" / name
        assert (
            run_aboyeur_guarded(
                "coordinator task",
                roster,
                worker="worker",
                cwd=tmp_path,
                output_dir=run,
                read_only=True,
                route_enabled=False,
                code_graph_enabled=False,
            )
            == 0
        )
        metadata = json.loads((run / "run.json").read_text())
        assert "run_id" not in metadata
        assert metadata["started_at"].endswith("Z")
        assert any(d["privacy_class"] == "private" for d in _documents(run))
        return run

    return create


def test_purge_latest_accepts_real_artifact_writer_receipt(tmp_path, recorded_run):
    old, current = (recorded_run(name) for name in ("old", "current"))
    journal = (current / "events" / "lifecycle.jsonl").read_bytes()
    assert cli.main(["runs", "cassette", "purge", "latest", "--cwd", str(tmp_path)]) == 0
    assert not _documents(current)
    assert _documents(old)
    assert (current / "events" / "lifecycle.jsonl").read_bytes() == journal


@pytest.mark.parametrize("reason", ["count", "age", "malformed", "out-of-range", "python310"])
def test_retention_prunes_real_runs_with_z_or_bad_timestamp(tmp_path, monkeypatch, recorded_run, reason):
    runs = [recorded_run(name) for name in ("old", "newer", "current")]
    now = time.time()
    for index, run in enumerate(runs):
        days = 40 if reason == "age" and index == 0 else 3 - index
        started = now - days * 86400
        # Use the artifact writer's exact timestamp spelling, including Z.
        with runguard.run_lock(tmp_path, run_dir=run):
            artifacts.update_run_receipt(run, started_at=run_io._utc_iso(datetime.fromtimestamp(started, timezone.utc)))
        modified = now + 100 if index == 0 else now - 60 * 86400
        os.utime(run, (modified, modified))
    if reason in {"malformed", "out-of-range"}:
        # A single bad receipt must fall back to its own directory mtime.
        with runguard.run_lock(tmp_path, run_dir=runs[0]):
            artifacts.update_run_receipt(
                runs[0], started_at="not-a-date" if reason == "malformed" else "10000-01-01T00:00:00Z"
            )
        expired = now - 40 * 86400
        os.utime(runs[0], (expired, expired))
    if reason == "python310":

        class Python310Datetime:
            @staticmethod
            def fromisoformat(value):
                if value.endswith("Z"):
                    raise ValueError("Invalid isoformat string")
                return datetime.fromisoformat(value)

        monkeypatch.setattr(store, "datetime", Python310Datetime)
    monkeypatch.setattr(store, "KEEP_RUNS", 2 if reason in {"count", "python310"} else 50)
    assert store.retain_private(tmp_path, runs[0].parent, current=runs[-1]) == 1
    assert not any(d["privacy_class"] == "private" for d in _documents(runs[0]))
    assert any(d["privacy_class"] == "private" for d in _documents(runs[1]))


def test_fallback_appserver_omits_projection_from_another_seat_window(tmp_path, monkeypatch):
    roster = _recovery_roster()
    fallback = Agent(name="fallback", cli="codex", role="review")
    roster = replace(
        roster,
        agents={
            "grok-review": replace(roster.agents["grok-review"], invalid_final_fallback=fallback.name),
            fallback.name: fallback,
        },
    )
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: _invalid_final())
    run = tmp_path / ".brigade" / "runs" / "current"
    events = run / "events"
    events.mkdir(parents=True)
    stream = events / "fallback.jsonl"
    stream.write_text('{"method":"turn/started","params":{}}\n')

    def worker(*args, **kwargs):
        with stream.open("a") as handle:
            handle.write('{"method":"turn/completed","params":{}}\n')
        return agents.AgentResult(text="fallback answer", ok=True, transport="codex-app-server")

    result = run_transport.dispatch(
        [run_transport.Assignment(worker="grok-review", task="Review the diff.")],
        roster,
        build_prompt=lambda *a, **kw: "review task",
        run_appserver_worker=worker,
        event_writer=lambda *a, **kw: None,
        appserver=object(),
        codex_transport="app-server",
        cwd=tmp_path,
        output_dir=run,
        read_only=True,
        direct=True,
    )[0]
    assert result.ok and result.text == "fallback answer"
    metadata = next(d for d in _documents(run) if d["privacy_class"] == "scrubbed")
    assert metadata["projection_status"] == "unavailable"
    assert "notification_projection" not in metadata
