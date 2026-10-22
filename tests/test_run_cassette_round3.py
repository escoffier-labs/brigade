"""Regressions for cassette transaction, dispatch, and input boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from brigade import agents, proc, run_journal, run_redaction, run_transport, runguard
from brigade import run_cassette_store as store
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_cassette_privacy import _finish_recording
from tests.test_run_redaction import _authority_run


@pytest.mark.parametrize("scheduler", ["waves", "dag"])
def test_interrupt_preserves_chained_retained_lock_error(tmp_path, monkeypatch, scheduler):
    started, release = Event(), Event()
    retained = runguard.RetainRunLockError("cleanup receipt unavailable")

    def fail(*args, **kwargs):
        started.set()
        assert release.wait(5)
        raise RuntimeError("cleanup replaced failure") from retained

    def interrupt(futures):
        assert started.wait(5)
        raise KeyboardInterrupt
        yield  # pragma: no cover

    monkeypatch.setattr(agents, "run_agent", fail)
    monkeypatch.setattr(run_transport, "as_completed", interrupt)
    caught = None
    try:
        run_transport.dispatch(
            [run_transport.Assignment(worker="worker", task="task")],
            Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")}),
            build_prompt=lambda *a, **kw: "task",
            run_appserver_worker=lambda *a, **kw: None,
            event_writer=lambda *a, **kw: None,
            cwd=tmp_path,
            output_dir=tmp_path / ".brigade" / "runs" / "current",
            scheduler=scheduler,
            route_dependencies={"worker": ()},
            on_interrupt=release.set,
        )
    except BaseException as exc:
        caught = exc
    assert caught is retained


def test_seat_invoker_key_tracks_coordinator_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="answer", ok=True))
    run = tmp_path / ".brigade" / "runs" / "current"
    invoker = SeatInvoker(
        roster=Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="research")}),
        cwd=tmp_path,
        run_id="current",
        process_registry=proc.ProcessRegistry(),
        output_dir=run,
    )
    keys = []
    for prompt in ("first synthesis", "second synthesis", "first synthesis"):
        assert invoker.invoke(seat="worker", phase="synthesis", prompt=prompt, timeout=10).ok
        manifest = next(
            d
            for d in _documents(run)
            if d["privacy_class"] == "public-metadata"
            and d["full_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        )
        assert manifest["coordinator_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        keys.append(manifest["key"])
    assert keys[0] == keys[2]
    assert keys[0] != keys[1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link publication")
@pytest.mark.parametrize("prune", [True, False], ids=["retention", "purge"])
def test_cleanup_removes_owned_crash_temps_before_final_inventory(tmp_path, prune):
    run = tmp_path / ".brigade" / "runs" / "old"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name}))
    directory = run / "events" / "cassettes"
    private = next(p for p in directory.glob("*.json") if json.loads(p.read_bytes())["privacy_class"] == "private")
    linked = directory / (".cassette-" + "a" * 32 + ".tmp")
    os.link(private, linked)
    unlinked = directory / (".cassette-" + "b" * 32 + ".tmp")
    unlinked.write_bytes(b"complete private crash body")
    unlinked.chmod(0o600)
    if prune:
        expired = time.time() - 40 * 86400
        os.utime(run, (expired, expired))
        store.retain_private(tmp_path, run.parent, current=run.parent / "new")
        assert {d["privacy_class"] for d in _documents(run)} == {"public-metadata", "scrubbed"}
    else:
        store.purge(run)
        assert not list(directory.iterdir())
    assert not linked.exists() and not unlinked.exists()
    assert not private.exists()


@pytest.mark.parametrize("bound", ["bytes", "rows", "references"])
def test_retention_skips_ledger_beyond_aggregate_bounds(tmp_path, monkeypatch, capsys, bound):
    run = tmp_path / ".brigade" / "runs" / "old"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name}))
    expired = time.time() - 40 * 86400
    os.utime(run, (expired, expired))
    ledger = tmp_path / "memory" / "outcome" / "records.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = [{"evidence_ref": f"other-{i}/run.json"} for i in range(3)]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    limit = {"bytes": "MAX_OUTCOME_BYTES", "rows": "MAX_OUTCOME_ROWS", "references": "MAX_OUTCOME_REFERENCES"}[bound]
    monkeypatch.setattr(store, limit, 2, raising=False)
    assert store.retain_private(tmp_path, run.parent, current=run.parent / "new") == 0
    assert any(d["privacy_class"] == "private" for d in _documents(run))
    assert "cassette retention skipped:" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["checkpoint-only", "journal-changed", "anchors"])
def test_rejected_redaction_preserves_cassette_sidecars(tmp_path, monkeypatch, failure):
    run, snapshot, _ = _authority_run(tmp_path)
    _record(run)
    _finish_recording(run, snapshot)
    sidecars = {p.name: p.read_bytes() for p in (run / "events" / "cassettes").iterdir()}
    journal = run / "events" / "lifecycle.jsonl"
    before = journal.read_bytes()
    if failure == "journal-changed":
        read = run_redaction._read_bounded_regular

        def changed(path, **kwargs):
            raw = read(path, **kwargs)
            return raw + b" " if path == journal else raw

        monkeypatch.setattr(run_redaction, "_read_bounded_regular", changed)
    elif failure == "anchors":

        def invalid(*args, **kwargs):
            raise run_redaction.RedactionError("anchor mismatch")

        monkeypatch.setattr(run_redaction, "_validate_chained_anchors", invalid)
    expected = {
        "checkpoint-only": "no redactable payloads",
        "journal-changed": "journal changed during redaction preflight",
        "anchors": "anchor mismatch",
    }[failure]
    with pytest.raises(run_redaction.RedactionError, match=expected):
        run_redaction.redact_journal(
            run,
            sequence_start=3 if failure == "checkpoint-only" else 2,
            sequence_end=3 if failure == "checkpoint-only" else 2,
            reason="credential-exposure",
            operator_confirmed=True,
        )
    assert journal.read_bytes() == before
    assert {p.name: p.read_bytes() for p in (run / "events" / "cassettes").iterdir()} == sidecars


def test_sidecar_cleanup_failure_is_anchored_and_retryable(tmp_path, monkeypatch):
    run, snapshot, _ = _authority_run(tmp_path)
    _record(run)
    _finish_recording(run, snapshot)
    purge = store.purge

    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(store, "purge", fail)
    kwargs = dict(sequence_start=2, sequence_end=2, reason="credential-exposure", operator_confirmed=True)
    with pytest.raises(run_redaction.RedactionError, match="cassette sidecar redaction failed"):
        run_redaction.redact_journal(run, **kwargs)
    events = run_journal.read_journal(run / "events" / "lifecycle.jsonl").events
    assert events[-1].event_type == "run.redaction.recorded"
    monkeypatch.setattr(store, "purge", purge)
    run_redaction.redact_journal(run, **kwargs)
    assert not list((run / "events" / "cassettes").iterdir())
    assert (
        sum(
            e.event_type == "run.redaction.recorded"
            for e in run_journal.read_journal(run / "events" / "lifecycle.jsonl").events
        )
        == 1
    )


@pytest.mark.parametrize("scheduler", ["waves", "dag"])
def test_adapter_exception_records_a_terminal_failure(tmp_path, monkeypatch, scheduler):
    def fail(*args, **kwargs):
        raise OSError("adapter pipe failed")

    monkeypatch.setattr(agents, "run_agent", fail)
    run = tmp_path / ".brigade" / "runs" / "current"
    result = run_transport.dispatch(
        [run_transport.Assignment(worker="worker", task="task")],
        Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")}),
        build_prompt=lambda *a, **kw: "task",
        run_appserver_worker=lambda *a, **kw: None,
        event_writer=lambda *a, **kw: None,
        cwd=tmp_path,
        output_dir=run,
        scheduler=scheduler,
    )[0]
    assert not result.ok and result.detail == "adapter pipe failed"
    docs = _documents(run)
    assert len([d for d in docs if d["privacy_class"] == "public-metadata"]) == 1
    body = next(d["result"] for d in docs if d["privacy_class"] == "private")
    assert not body["ok"] and body["detail"] == "worker transport unavailable"


@pytest.mark.skipif(os.name != "posix", reason="POSIX special-file boundary")
@pytest.mark.parametrize("kind", ["symlink", "fifo", "symlink-fifo"])
def test_direct_read_rejects_links_and_fifos_without_blocking(tmp_path, kind):
    run = tmp_path / "run"
    with store.directory(run, create=True):
        pass
    path = run / "events" / "cassettes" / "input"
    outside = tmp_path / "outside"
    if "fifo" in kind:
        os.mkfifo(outside if kind == "symlink-fifo" else path)
    else:
        outside.write_bytes(b"outside payload")
        outside.chmod(0o600)
    if kind.startswith("symlink"):
        path.symlink_to(outside)
    script = """
import sys
from pathlib import Path
from brigade import run_cassette_store as store
with store.directory(Path(sys.argv[1])) as parent:
    try:
        store.read(parent, "input")
    except (OSError, store.CassetteError):
        sys.exit(0)
sys.exit(1)
"""
    result = subprocess.run([sys.executable, "-c", script, str(run)], capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("bad_stream", [None, "invalid", "oversized", "large-projection"])
def test_appserver_projection_is_per_dispatch_and_failures_are_local(tmp_path, bad_stream):
    run = tmp_path / ".brigade" / "runs" / "current"
    events = run / "events"
    events.mkdir(parents=True)
    path = events / "worker.jsonl"
    path.write_text('{"method":"thread/started","params":{"threadId":"earlier"}}\n')
    chunks = []

    def worker(*args, **kwargs):
        index = len(chunks)
        raw = json.dumps({"method": "turn/started" if index == 0 else "turn/completed", "params": {}}) + "\n"
        if index == 0 and bad_stream == "invalid":
            raw = '{"method":"cut'
        elif index == 0 and bad_stream == "oversized":
            raw = "x" * (store.MAX_ENTRY_BYTES * 2 + 1)
        elif index == 0 and bad_stream == "large-projection":
            raw *= 20000
        chunks.append(raw.encode())
        with path.open("ab") as handle:
            handle.write(chunks[-1])
        return agents.AgentResult(text=f"answer {index}", ok=True, transport="codex-app-server")

    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})
    for task in ("first", "second"):
        run_transport.dispatch(
            [run_transport.Assignment(worker="worker", task=task)],
            roster,
            build_prompt=lambda *a, **kw: "task",
            run_appserver_worker=worker,
            event_writer=lambda *a, **kw: None,
            appserver=object(),
            cwd=tmp_path,
            output_dir=run,
        )
    docs = _documents(run)
    manifests = [d for d in docs if d["privacy_class"] == "public-metadata"]
    assert len(manifests) == 2
    metadata = [d for d in docs if d["privacy_class"] == "scrubbed"]
    projections = [d["notification_projection"] for d in metadata if "notification_projection" in d]
    assert any(
        p["source_digest"] == hashlib.sha256(chunks[1]).hexdigest() and p["event_count"] == 1 for p in projections
    )
    if bad_stream is None:
        assert {p["source_digest"] for p in projections} == {hashlib.sha256(c).hexdigest() for c in chunks}
        assert all(p["event_count"] == 1 for p in projections)
    else:
        assert any(d.get("projection_status") == "unavailable" and "notification_projection" not in d for d in metadata)
    assert not (events / "cassettes" / ".stopped").exists()


def test_unresolved_seat_env_downgrades_only_its_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "resolve_env_overrides", lambda env: (None, "session expired"))
    run = tmp_path / ".brigade" / "runs" / "current"
    bad = Agent(name="bad", cli="codex", role="write", env={"TOKEN_REF": "missing"})
    assert _record(run, agent=bad) is not None
    assert _record(run) is not None
    docs = _documents(run)
    assert len([d for d in docs if d["privacy_class"] == "public-metadata"]) == 2
    metadata = next(d for d in docs if d.get("reason") == "env-resolution-unavailable")
    assert metadata["status"] == "not-observed" and "result_sha256" not in metadata
    assert len([d for d in docs if d["privacy_class"] == "private"]) == 1
    assert not (run / "events" / "cassettes" / ".stopped").exists()


def test_overlapping_seat_dispatches_omit_ambiguous_projections(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "current"
    events = run / "events"
    events.mkdir(parents=True)
    barrier = Barrier(2)

    def worker(appserver, agent, name, prompt, **kwargs):
        with (events / "worker.jsonl").open("ab") as handle:
            handle.write(b'{"method":"turn/completed","params":{}}\n')
        barrier.wait(timeout=5)
        return agents.AgentResult(text=prompt.splitlines()[0], ok=True, transport="codex-app-server")

    roster = Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")})

    def dispatch(task):
        return run_transport.dispatch(
            [run_transport.Assignment(worker="worker", task=task)],
            roster,
            build_prompt=lambda agent, assignment, **kw: assignment.task,
            run_appserver_worker=worker,
            event_writer=lambda *a, **kw: None,
            appserver=object(),
            cwd=tmp_path,
            output_dir=run,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, ("first", "second")))
    assert all(result.ok for result in results)
    metadata = [d for d in _documents(run) if d["privacy_class"] == "scrubbed"]
    assert len(metadata) == 2
    assert all(d.get("projection_status") == "unavailable" and "notification_projection" not in d for d in metadata)
    assert len([d for d in _documents(run) if d["privacy_class"] == "private"]) == 2
