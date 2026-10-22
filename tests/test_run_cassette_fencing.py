"""Cassette maintenance must preserve active publication and lifecycle capacity."""

import hashlib
import json
import os
from contextlib import contextmanager
from threading import Event, Thread, current_thread

import pytest

from brigade import agents, run_cassette, run_events, run_journal, run_lifecycle, run_transport, runguard
from brigade import run_cassette_store as store
from brigade.roster import Agent
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_transport_recovery import _invalid_final, _recovery_roster, _successful_final


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-replace publication uses link then unlink")
def test_overlapping_publications_survive_link_before_unlink(tmp_path, monkeypatch):
    run = tmp_path / ".brigade" / "runs" / "current"
    linked, release, second_at_fence = Event(), Event(), Event()
    link = os.link
    inventory = store.inventory
    pair = run_lifecycle.checkpoint_event_pair
    refs = {}
    inventory_errors = []

    def pause_after_link(source, destination, **kwargs):
        link(source, destination, **kwargs)
        if current_thread() is first and source.startswith(".cassette-") and not linked.is_set():
            assert os.stat(source, dir_fd=kwargs["src_dir_fd"]).st_nlink == 2
            linked.set()
            assert release.wait(10)

    def observe_inventory(*args, **kwargs):
        try:
            return inventory(*args, **kwargs)
        except store.CassetteError as exc:
            # Retain only the known static cause, never arbitrary exception text.
            if str(exc) == "cassette inventory contains unsafe entries":
                inventory_errors.append("CassetteError: cassette inventory contains unsafe entries")
            raise

    @contextmanager
    def observe_fence():
        if current_thread() is second:
            second_at_fence.set()
        with pair():
            yield

    def record(task):
        refs[task] = _record(run, cwd=tmp_path, task=task)

    first = Thread(target=record, args=("first",))
    second = Thread(target=record, args=("second",))
    monkeypatch.setattr(os, "link", pause_after_link)
    monkeypatch.setattr(store, "inventory", observe_inventory)
    monkeypatch.setattr(run_lifecycle, "checkpoint_event_pair", observe_fence)
    with runguard.run_lock(tmp_path, run_dir=run):
        first.start()
        try:
            assert linked.wait(10)
            second.start()
            assert second_at_fence.wait(10)
        finally:
            release.set()
            first.join(10)
            if second.ident is not None:
                second.join(10)
    assert not first.is_alive() and not second.is_alive()
    metadata = [d for d in _documents(run) if d["privacy_class"] == "scrubbed"]
    assert len(metadata) == 2, inventory_errors
    assert len({d["key"] for d in metadata}) == 2
    assert all(d["status"] == "observed" for d in metadata)
    assert set(refs) == {"first", "second"} and all(ref is not None for ref in refs.values())
    assert not (run / "events" / "cassettes" / ".stopped").exists()
    assert not inventory_errors


@pytest.mark.parametrize("gap", ["temporary", "reference"])
@pytest.mark.parametrize("workspace_field", ["lock_workspace", "cwd"])
def test_purge_rejects_live_publication_without_losing_artifacts(tmp_path, monkeypatch, gap, workspace_field):
    run = tmp_path / "custom-runs" / "20261020-120000-cassette"
    journal = run / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(journal)
    (run / "run.json").write_text(json.dumps({workspace_field: str(tmp_path)}))
    reached, release = Event(), Event()
    replace = store.dirfd.replace_children
    append = run_cassette._append_reference
    results = []

    def pause():
        reached.set()
        assert release.wait(5)

    def replace_children(parent, source, destination, **kwargs):
        if source.startswith(".cassette-"):
            pause()
        return replace(parent, source, destination, **kwargs)

    def append_reference(*args):
        pause()
        return append(*args)

    monkeypatch.setattr(store.dirfd, "replace_children", replace_children if gap == "temporary" else replace)
    monkeypatch.setattr(run_cassette, "_append_reference", append_reference if gap == "reference" else append)
    worker = Thread(target=lambda: results.append(_record(run, cwd=tmp_path)))
    worker.start()
    try:
        assert reached.wait(5)
        assert run_cassette.purge_cli(str(run), cwd=tmp_path / "elsewhere") == 2
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1 and results[0] is not None
    report = run_journal.read_journal(journal)
    assert not report.chain_errors and len(report.events) == 1
    manifest = json.loads((run / report.events[0].payload["path"]).read_bytes())
    assert all((run / ref["path"]).exists() for ref in manifest["artifacts"])
    assert not (run / "events" / "cassettes" / ".stopped").exists()


def test_purge_keeps_stop_marker_under_live_owner(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name}))
    marker = run / "events" / "cassettes" / ".stopped"
    marker.touch()
    with runguard.run_lock(tmp_path, run_dir=run):
        assert run_cassette.purge_cli(str(run), cwd=tmp_path) == 2
        assert marker.exists()
    assert run_cassette.purge_cli(str(run), cwd=tmp_path) == 0
    assert not list(marker.parent.iterdir())


def test_cassette_reserves_quarter_of_journal_for_lifecycle(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    journal = run / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(journal)
    previous = None
    rows = []
    for sequence in range(1, 1537):
        event = run_events.build_event(
            run_id=run.name,
            sequence=sequence,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key=f"seed-{sequence}",
            recorded_at="2026-10-20T12:00:00.000000Z",
            previous_digest=previous,
        )
        previous = event["event_digest"]
        rows.append(run_events.canonical_bytes(event) + b"\n")
    journal.write_bytes(b"".join(rows))
    assert _record(run) is None
    assert len(run_journal.read_journal(journal).events) == 1536
    assert not any(d.get("status") == "observed" for d in _documents(run))
    with runguard.run_lock(tmp_path, run_dir=run):
        run_journal.append_event(
            journal,
            run_id=run.name,
            event_type="run.completed",
            payload={"status": "ok"},
            idempotency_key="complete",
            expected_previous_sequence=1536,
            workspace=tmp_path,
        )
    assert run_journal.read_journal(journal).events[-1].event_type == "run.completed"


def test_continuation_manifest_keeps_initial_injected_prompt_digest(tmp_path, monkeypatch):
    calls = []
    results = iter([_invalid_final(), _successful_final("Recovered final.")])

    def worker(cli, prompt, **kwargs):
        calls.append(prompt)
        return next(results)

    monkeypatch.setattr(agents, "run_agent", worker)
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    result = run_transport.dispatch(
        [run_transport.Assignment(worker="grok-review", task="Review the diff.")],
        _recovery_roster(),
        build_prompt=lambda *args, **kwargs: "original task prompt",
        run_appserver_worker=lambda *args, **kwargs: None,
        event_writer=lambda *args, **kwargs: None,
        cwd=tmp_path,
        output_dir=run,
        direct=True,
        read_only=True,
    )[0]
    assert result.ok and len(calls) == 2 and calls[0] != calls[1]
    manifest = next(d for d in _documents(run) if d["privacy_class"] == "public-metadata")
    assert manifest["full_prompt_sha256"] == hashlib.sha256(calls[0].encode()).hexdigest()


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "ACCESS_TOKEN", "PASSWORD", "CLIENT_SECRET", "CREDENTIAL"])
def test_secret_named_literal_override_is_scrubbed_without_rewriting_configuration(tmp_path, name):
    needle = "fake-literal-secret-value"
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(
        run,
        agent=Agent(name="worker", cli="codex", role="write", env={name: needle, "CI": "1"}),
        result=agents.AgentResult(text=f"1 test passed: prefix{needle}suffix", ok=True),
    )
    docs = _documents(run)
    assert needle not in json.dumps(docs)
    body = next(d["result"] for d in docs if d["privacy_class"] == "private")
    assert body["text"] == "1 test passed: prefix[redacted]suffix"


def test_retention_does_not_purge_live_old_run(tmp_path):
    import os
    import time

    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name}))
    expired = time.time() - 40 * 86400
    os.utime(run, (expired, expired))
    before = _documents(run)
    with runguard.run_lock(tmp_path, run_dir=run):
        assert store.retain_private(tmp_path, run.parent, current=run.parent / "next") == 0
        assert _documents(run) == before


def test_purge_recovers_dead_owner_without_restarting_recording(tmp_path, monkeypatch):
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name, "status": "completed"}))
    lock = runguard.lock_path(tmp_path)
    lock.mkdir()
    (lock / "pid").write_text("99999999")
    (lock / "owner.json").write_text(json.dumps({"pid": 99999999, "run_dir": str(run), "owner_token": "dead-owner"}))
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    assert run_cassette.purge_cli(str(run), cwd=tmp_path) == 0
    assert not _documents(run)
    assert not lock.exists()
