"""The production dispatch and SeatInvoker paths own cassette recording."""

from __future__ import annotations

import hashlib
import json
from threading import Event, Thread

import pytest

from brigade import aboyeur, agents, fleet_session_bootstrap, proc, run_journal, run_transport, runguard
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from tests.run_test_helpers import run_aboyeur_guarded
from tests.test_aboyeur import _IntentOnlyAppServer
from tests.test_run_cassette import _documents
from tests.test_run_cassette import auto_recording as auto_recording


@pytest.mark.parametrize("retry_ok,reprobe", [(True, True), (False, True), (False, False)])
def test_retry_records_only_the_terminal_worker_result(tmp_path, monkeypatch, retry_ok, reprobe):
    calls = []

    def worker(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1 or not retry_ok:
            return agents.AgentResult(text="failed attempt", ok=False, failure_kind="network-error")
        return agents.AgentResult(text="recovered answer", ok=True)

    monkeypatch.setattr(agents, "run_agent", worker)
    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_journal.ensure_journal(run_dir / "events" / "lifecycle.jsonl")
    with runguard.run_lock(tmp_path, run_dir=run_dir):
        result = run_transport.dispatch(
            [run_transport.Assignment(worker="worker", task="task")],
            roster,
            build_prompt=lambda *args, **kw: "task",
            run_appserver_worker=lambda *args, **kw: None,
            event_writer=lambda *args, **kw: None,
            reprobe_seat=lambda agent: reprobe,
            cwd=tmp_path,
            output_dir=run_dir,
        )[0]
    assert len(calls) == (2 if reprobe else 1)
    docs = _documents(run_dir)
    assert len([d for d in docs if d["privacy_class"] == "public-metadata"]) == 1
    bodies = [d["result"] for d in docs if d["privacy_class"] == "private"]
    assert len(bodies) == 1
    assert bodies[0]["text"] == result.text
    assert bodies[0]["ok"] == result.ok
    assert len(run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events) == 1


def test_auto_dispatch_does_not_enable_lifecycle_journaling(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kw: agents.AgentResult(text="answer", ok=True))
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_dir.mkdir(parents=True)
    snapshot = {"status": "dispatching", "lifecycle_journal_requested": False}
    (run_dir / "run.json").write_text(json.dumps(snapshot))
    invoker = SeatInvoker(
        roster=Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")}),
        cwd=tmp_path,
        run_id=run_dir.name,
        process_registry=proc.ProcessRegistry(),
        output_dir=run_dir,
    )
    assert invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10).ok
    assert any(d["privacy_class"] == "private" for d in _documents(run_dir))
    assert not (run_dir / "events" / "lifecycle.jsonl").exists()
    assert json.loads((run_dir / "run.json").read_text()) == snapshot


def test_each_dispatched_worker_records_exact_injected_prompt_once(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(fleet_session_bootstrap, "ensure_prompt", lambda prompt, **kw: prompt + "\ninjected policy")

    def worker(cli, prompt, **kwargs):
        prompts.append(prompt)
        return agents.AgentResult(text="worker-model-text-needle", ok=True, transport="cli")

    monkeypatch.setattr(agents, "run_agent", worker)
    roster = Roster(
        orchestrator="chef",
        agents={
            "one": Agent(name="one", cli="codex", role="write", model="test-model"),
            "two": Agent(name="two", cli="codex", role="write", model="test-model"),
        },
    )
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_journal.ensure_journal(run_dir / "events" / "lifecycle.jsonl")
    with runguard.run_lock(tmp_path, run_dir=run_dir):
        result = run_transport.dispatch(
            [run_transport.Assignment(worker=name, task="coordinator task") for name in ("one", "two")],
            roster,
            build_prompt=lambda *args, **kw: "worker-prompt-needle",
            run_appserver_worker=lambda *args, **kw: None,
            event_writer=lambda *args, **kw: None,
            cwd=tmp_path,
            output_dir=run_dir,
            direct=True,
            read_only=True,
        )
    assert len(result) == 2 and all(r.ok for r in result)
    manifests = [d for d in _documents(run_dir) if d["privacy_class"] == "public-metadata"]
    assert len(manifests) == 2
    assert {d["full_prompt_sha256"] for d in manifests} == {hashlib.sha256(p.encode()).hexdigest() for p in prompts}
    journal = run_dir / "events" / "lifecycle.jsonl"
    events = run_journal.read_journal(journal).events
    assert len(events) == 2
    # Measured cassette growth only. No dispatch callbacks are installed here.
    assert journal.stat().st_size / len(result) < 1024
    assert len(events) < 2048 and journal.stat().st_size < 8 * 1024 * 1024
    for needle in ("worker-model-text-needle", "worker-prompt-needle", "injected policy"):
        assert needle not in journal.read_text()


def test_seat_invoker_records_through_dispatch_and_storage_failure_keeps_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kw: agents.AgentResult(text="answer", ok=True))
    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    invoker = SeatInvoker(
        roster=roster, cwd=tmp_path, run_id=run_dir.name, process_registry=proc.ProcessRegistry(), output_dir=run_dir
    )
    result = invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10)
    assert result.ok and result.text == "answer"
    assert len([d for d in _documents(run_dir) if d["privacy_class"] == "private"]) == 1
    # Corrupt sidecar storage must not turn a successful worker into a failed run.
    (run_dir / "events" / "cassettes" / "unexpected-directory").mkdir()
    result = invoker.invoke(seat="worker", phase="research.extract", prompt="next task", timeout=10)
    assert result.ok and result.text == "answer"


def test_appserver_digest_is_exact_prompt_including_transport_suffix(tmp_path, monkeypatch):
    seen = []

    def worker(appserver, agent, name, prompt, **kwargs):
        seen.append(prompt)
        return agents.AgentResult(text="answer", ok=True, transport="codex-app-server")

    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_transport.dispatch(
        [run_transport.Assignment(worker="worker", task="task")],
        roster,
        build_prompt=lambda *args, **kw: "task",
        run_appserver_worker=worker,
        event_writer=lambda *args, **kw: None,
        appserver=object(),
        cwd=tmp_path,
        output_dir=run_dir,
    )
    manifest = next(d for d in _documents(run_dir) if d["privacy_class"] == "public-metadata")
    assert manifest["full_prompt_sha256"] == hashlib.sha256(seen[0].encode()).hexdigest()
    assert "Approval correlation" not in json.dumps(_documents(run_dir))


def test_run_transport_override_keys_the_effective_worker_transport(tmp_path, monkeypatch):
    observed = []

    def exec_worker(*args, **kwargs):
        observed.append("exec")
        return agents.AgentResult(text="completed answer", ok=True, transport="exec")

    def appserver_worker(*args, **kwargs):
        observed.append("app-server")
        return agents.AgentResult(text="completed answer", ok=True, transport="codex-app-server")

    def no_control_socket(self):
        raise aboyeur.run_control.ControlError("control socket unavailable under test")

    monkeypatch.setattr(agents, "run_agent", exec_worker)
    monkeypatch.setattr(aboyeur, "_run_codex_appserver_worker", appserver_worker)
    monkeypatch.setattr(aboyeur.codex_appserver, "AppServer", _IntentOnlyAppServer)
    monkeypatch.setattr(aboyeur.run_control.ControlServer, "start", no_control_socket)
    keys = []
    for i, (default, override) in enumerate((("exec", "app-server"), ("app-server", None), ("exec", None))):
        run = tmp_path / ".brigade" / "runs" / f"run-{i}"
        roster = Roster(
            orchestrator="chef",
            agents={
                "chef": Agent(name="chef", cli="claude", role="plan"),
                "worker": Agent(name="worker", cli="codex", role="research"),
            },
            codex_transport=default,
        )
        assert (
            run_aboyeur_guarded(
                "coordinator task",
                roster,
                worker="worker",
                codex_transport=override,
                cwd=tmp_path,
                output_dir=run,
                read_only=True,
                route_enabled=False,
                code_graph_enabled=False,
            )
            == 0
        )
        manifest = next(d for d in _documents(run) if d["privacy_class"] == "public-metadata")
        keys.append(manifest["key"])
    assert observed == ["app-server", "app-server", "exec"]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


def test_late_result_does_not_append_to_a_terminal_run(tmp_path, monkeypatch):
    started, release = Event(), Event()

    def worker(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return agents.AgentResult(text="late worker answer", ok=True)

    monkeypatch.setattr(agents, "run_agent", worker)
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_dir.mkdir(parents=True)
    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})
    invoker = SeatInvoker(
        roster=roster, cwd=tmp_path, run_id=run_dir.name, process_registry=proc.ProcessRegistry(), output_dir=run_dir
    )
    thread = Thread(target=lambda: invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10))
    thread.start()
    try:
        assert started.wait(5)
        invoker.close_receipts()
        (run_dir / "run.json").write_text(json.dumps({"status": "cancelled", "finished_at": "2026-10-20T12:00:00Z"}))
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not _documents(run_dir)
    assert not (run_dir / "events" / "lifecycle.jsonl").exists()
