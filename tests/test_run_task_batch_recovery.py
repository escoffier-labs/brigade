"""Offline acceptance proof for descriptor membership recovery and authority."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import asdict

import pytest

from brigade import aboyeur, run_checkpoint, run_events, run_journal, run_lifecycle, run_projector, run_shadow
from brigade import run_tasks as tasks
from tests import test_run_task_journal as journal_support
from tests import test_run_tasks as support

task_run = support.task_run
authoritative_task_run = journal_support.authoritative_task_run


def _specs():
    return (tasks.TaskSpec("one", "worker", "a" * 64), tasks.TaskSpec("two", "worker", "a" * 64))


def _wire_bytes(value):
    # Deliberately permits hostile standard JSON, including booleans that
    # violate Brigade's canonical value rules. This is not a canonical encoder.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _wire_digest(value):
    return hashlib.sha256(_wire_bytes(value)).hexdigest()


def _wire_request_digest(envelope):
    return _wire_digest({key: envelope[key] for key in ("event_type", "payload", "idempotency_key")})


def _wire_event_digest(envelope):
    return _wire_digest({key: value for key, value in envelope.items() if key not in {"event_digest", "event_id"}})


def _accept(task_run, specs=None):
    run_dir, workspace = task_run
    return tasks.accept_task_batch(
        run_dir, workspace=workspace, request_id="batch-one", specs=_specs() if specs is None else specs
    )


def _refuse_unchanged(task_run, reason):
    run_dir, _workspace = task_run
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    for operation in (
        lambda: tasks.read_task_batch(run_dir, request_id="batch-one"),
        lambda: _accept(task_run),
    ):
        with pytest.raises(tasks.TaskJournalError, match=reason) as error:
            operation()
        assert len(str(error.value)) <= run_events.MAX_DIAGNOSTIC_LEN
        assert path.read_bytes() == before


@pytest.mark.parametrize("authoritative", [False, True])
def test_batch_restart_enumerates_without_caller_rows(tmp_path, authoritative):
    """Two independent owners reconstruct current ordered members from bytes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade/runs/restart-batch"
    run_dir.mkdir(parents=True)
    code = r"""
import json, os, sys
from dataclasses import asdict
from pathlib import Path
from brigade import aboyeur, runguard, run_tasks as tasks
workspace, run_dir = map(Path, sys.argv[1:3])
mode, authoritative = sys.argv[3:]
if authoritative == 'True':
    os.environ['BRIGADE_RUN_JOURNAL_AUTHORITY'] = '1'
with runguard.run_lock(workspace, run_dir=run_dir):
    assert runguard.is_active_run_owner(workspace, run_dir)
    if mode == 'first':
        snapshot = {'schema': 'brigade.run.v1', 'status': 'started',
                    'lock_workspace': str(workspace), 'lifecycle_journal_requested': True}
        if authoritative == 'True':
            snapshot['run_journal_authority_requested'] = True
        aboyeur._write_json(run_dir / 'run.json', snapshot)
        specs = (tasks.TaskSpec('one', 'worker', 'a' * 64),
                 tasks.TaskSpec('two', 'worker', 'a' * 64))
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id='batch-one', specs=specs)
        tasks.accept_task(run_dir, workspace=workspace, request_id='standalone',
                          spec=tasks.TaskSpec('other', 'worker', 'b' * 64))
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id='batch-two',
                          specs=(tasks.TaskSpec('three', 'worker', 'c' * 64),))
        handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id='one')
        current = tasks.read_task_batch(run_dir, request_id='batch-one').members[0]
        assert tasks.claim_task_launch(run_dir, workspace=workspace, handle=handle,
            expected_version=current.version, seat='worker', dispatch_attempt=1).outcome == 'granted'
        result = tasks.observe_task(run_dir, workspace=workspace, handle=handle,
            observation=tasks.TaskObservation('completed', containment='quiescent'))
        tasks.acknowledge_result(run_dir, workspace=workspace, task_id='one', result_id=result.result_id)
    # The restarted reader receives only directory and outer request identity.
    batch = tasks.read_task_batch(run_dir, request_id='batch-one')
    assert [m.handle.spec.task_id for m in batch.members] == ['one', 'two']
    assert [m.status for m in batch.members] == ['completed', 'queued']
    assert batch.members[0].acknowledged and batch.members[0].launch_claim is not None
    assert len(tasks.read_tasks(run_dir)) == 4
    path = run_dir / 'events/lifecycle.jsonl'
    before = path.read_bytes()
    assert tasks.accept_task_batch(run_dir, workspace=workspace, request_id='batch-one',
                                  specs=batch.specs) == batch
    current = batch.members[0]
    assert tasks.claim_task_launch(run_dir, workspace=workspace, handle=current.handle,
        expected_version=current.version, seat='worker', dispatch_attempt=1).outcome == 'terminal'
    assert path.read_bytes() == before
    print(json.dumps({'pid': os.getpid(), 'batch': asdict(batch)}))
"""
    results = []
    before_restart = None
    for mode in ("first", "restart"):
        result = subprocess.run(
            [sys.executable, "-c", code, str(workspace), str(run_dir), mode, str(authoritative)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
        path = run_dir / "events/lifecycle.jsonl"
        if mode == "first":
            before_restart = path.read_bytes()
        else:
            assert path.read_bytes() == before_restart
    assert results[0]["pid"] != results[1]["pid"]
    assert results[0]["batch"] == results[1]["batch"]


@pytest.mark.parametrize("conflicting", [False, True], ids=["identical", "conflicting"])
def test_concurrent_batch_acceptance_has_one_fact(task_run, conflicting):
    """The actual owner's threads race through journal transaction locks."""
    run_dir, _workspace = task_run
    candidates = [_specs(), (tasks.TaskSpec("three", "worker", "b" * 64),) if conflicting else _specs()]
    barrier = threading.Barrier(2)
    successes, errors = [], []

    def accept(specs):
        try:
            barrier.wait(timeout=5)
            successes.append(_accept(task_run, specs))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=accept, args=(specs,), daemon=True) for specs in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "batch transaction deadlocked"
    assert len(successes) == (1 if conflicting else 2), repr(errors)
    if conflicting:
        assert len(errors) == 1 and isinstance(errors[0], tasks.TaskJournalError)
        assert "identity conflict" in str(errors[0])
    else:
        assert not errors and successes[0] == successes[1]
    batch = tasks.read_task_batch(run_dir, request_id="batch-one")
    assert batch.specs in candidates
    assert tasks.read_tasks(run_dir) == batch.members
    facts = run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").events
    aggregates = [e for e in facts if e.event_type == "task.batch-accepted"]
    assert len(aggregates) == 1
    assert aggregates[0].payload["members"] == [asdict(s) for s in batch.specs]
    assert not any(e.event_type == "task.accepted" for e in facts)


@pytest.mark.parametrize("phase", ["before-checkpoint", "checkpoint-only", "partial-line", "barrier", "lost-return"])
def test_batch_crash_windows_refuse_or_replay_complete_membership(task_run, monkeypatch, phase):
    run_dir, _workspace = task_run
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    assets = {p.name: p.read_bytes() for p in run_checkpoint.checkpoint_dir(run_dir).iterdir()}
    real_append = run_lifecycle._append_owner_event
    real_write = run_journal.os.write
    real_sync = run_journal._sync_journal_fd
    fragments = []

    def fail(*args, **kwargs):
        raise OSError("simulated batch boundary failure")

    def write(fd, line):
        if b'"event_type":"task.batch-accepted"' in line:
            fragment = line[: len(line) // 2]
            written = real_write(fd, fragment)
            fragments.append(fragment[:written])
            return written
        return real_write(fd, line)

    def append(path, **kwargs):
        if kwargs["event_type"] != "task.batch-accepted":
            return real_append(path, **kwargs)
        if phase == "checkpoint-only":
            fail()
        with monkeypatch.context() as patch:
            if phase == "partial-line":
                patch.setattr(run_journal.os, "write", write)
            if phase == "barrier":
                patch.setattr(run_journal, "_sync_journal_fd", fail)
            event = real_append(path, **kwargs)
        if phase == "lost-return":
            fail()
        return event

    with monkeypatch.context() as patch:
        patch.setattr(run_lifecycle, "_append_owner_event", append)
        if phase == "before-checkpoint":
            patch.setattr(run_checkpoint, "publish_checkpoint_file", fail)
        with pytest.raises(tasks.TaskJournalError, match="operation refused or durability indeterminate"):
            _accept(task_run)
    damaged = path.read_bytes()
    if phase == "before-checkpoint":
        assert damaged == before
        assert {p.name: p.read_bytes() for p in run_checkpoint.checkpoint_dir(run_dir).iterdir()} == assets
        assert _accept(task_run).specs == _specs()
        return
    assert damaged.startswith(before) and damaged != before
    report = run_journal.read_journal_bounded(path)
    assert not report.chain_errors
    if phase in {"checkpoint-only", "partial-line"}:
        assert report.events[-1].payload["paired_event_type"] == "task.batch-accepted"
        assert not any(e.event_type == "task.batch-accepted" for e in report.events)
        if phase == "partial-line":
            assert len(fragments) == 1 and report.partial_tail == fragments[0]
            assert damaged.endswith(fragments[0])
        else:
            assert report.partial_tail is None and not fragments
        _refuse_unchanged(task_run, "partial tail" if fragments else "incomplete-pair")
        return
    assert report.partial_tail is None
    assert report.events[-1].event_type == "task.batch-accepted"
    assert report.events[-1].payload["members"] == [asdict(s) for s in _specs()]
    # Visible complete bytes are insufficient while a fresh barrier still fails.
    with monkeypatch.context() as patch:
        patch.setattr(run_journal, "_sync_journal_fd", fail)
        _refuse_unchanged(task_run, "operation refused or durability indeterminate")
    barriers = []

    def sync(fd, journal_path):
        real_sync(fd, journal_path)
        barriers.append(journal_path)

    with monkeypatch.context() as patch:
        patch.setattr(run_journal, "_sync_journal_fd", sync)
        batch = tasks.read_task_batch(run_dir, request_id="batch-one")
        assert _accept(task_run) == batch
    assert len(barriers) >= 2 and all(p == path for p in barriers)
    assert batch.specs == _specs() and [m.status for m in batch.members] == ["queued", "queued"]
    assert path.read_bytes() == damaged


def test_later_pair_cannot_hide_batch_orphan(task_run, monkeypatch):
    run_dir, workspace = task_run
    real_append = run_lifecycle._append_owner_event

    def append(path, **kwargs):
        if kwargs["event_type"] == "task.batch-accepted":
            raise OSError("simulated batch orphan")
        return real_append(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(run_lifecycle, "_append_owner_event", append)
        with pytest.raises(tasks.TaskJournalError):
            _accept(task_run)
    path = run_dir / "events/lifecycle.jsonl"
    orphaned = path.read_bytes()
    event_type, key = "run.redaction.recorded", "after-batch-orphan"
    checkpoint = run_checkpoint.write_checkpoint(
        run_dir,
        (run_dir / "run.json").read_bytes(),
        workspace=workspace,
        paired_event_type=event_type,
        pairing_key=run_events.request_digest(event_type=event_type, payload={}, idempotency_key=key),
    )
    assert checkpoint is not None
    run_journal.append_event(
        path,
        run_id=run_dir.name,
        event_type=event_type,
        payload={},
        idempotency_key=key,
        expected_previous_sequence=checkpoint.sequence,
        workspace=workspace,
    )
    assert path.read_bytes().startswith(orphaned) and path.read_bytes() != orphaned
    report = run_journal.read_journal_bounded(path)
    assert not report.chain_errors and report.partial_tail is None
    body = json.loads(run_checkpoint.validate_checkpoint(run_dir, checkpoint))
    run_checkpoint._verify_coverage(list(report.events), checkpoint, body)
    _refuse_unchanged(task_run, "incomplete-pair")


def test_batch_unsupported_reader_refuses_without_ignoring_fact(task_run):
    run_dir, _workspace = task_run
    _accept(task_run)
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    code = r"""
import sys
from pathlib import Path
from brigade import run_events, run_tasks as tasks
run_events.EVENT_TYPES.pop('task.batch-accepted')
try:
    tasks.read_task_batch(Path(sys.argv[1]), request_id='batch-one')
except tasks.TaskJournalError as exc:
    assert 'unsupported reader' in str(exc) and len(str(exc)) <= run_events.MAX_DIAGNOSTIC_LEN
    print('unsupported-reader-refused')
else:
    raise AssertionError('unsupported aggregate silently ignored')
"""
    result = subprocess.run([sys.executable, "-c", code, str(run_dir)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unsupported-reader-refused"
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["version", "boolean-version", "nested-row", "commitment", "duplicate", "occupied"])
def test_batch_version_and_hostile_history_refuse_boundedly(task_run, damage):
    """Consistently hashed hostile JSON refuses even when its links match."""
    run_dir, workspace = task_run
    _accept(task_run)
    path = run_dir / "events/lifecycle.jsonl"
    envelopes = [json.loads(line) for line in path.read_bytes().splitlines()]
    payload = envelopes[-1]["payload"]
    if damage in {"duplicate", "occupied"}:
        if damage == "occupied":
            payload = {**payload, "request_id": "another-batch"}
        run_lifecycle.record_lifecycle_event(
            run_dir, workspace=workspace, event_type="task.batch-accepted", payload=payload, idempotency_key="hostile"
        )
        report = run_journal.read_journal_bounded(path)
        assert not report.chain_errors and report.partial_tail is None
        reason = "duplicate task batch" if damage == "duplicate" else "identity conflict"
    else:
        if damage in {"version", "boolean-version"}:
            payload["descriptor_version"] = 2 if damage == "version" else True
            if damage == "version":
                # Isolate the version guard from membership-commitment refusal.
                payload["membership_digest"] = _wire_digest(
                    {
                        "domain": "brigade.task-batch.members.v1",
                        "descriptor_version": payload["descriptor_version"],
                        "members": payload["members"],
                    }
                )
        elif damage == "nested-row":
            payload["members"][0]["seat"] = {"execution": "unsupported"}
        else:
            payload["membership_digest"] = "f" * 64
        # Rebuild the request, matching checkpoint key, complete chain and IDs.
        # The boolean record is standard JSON with consistent hashes, but
        # violates Brigade canonical value rules as well as the payload contract.
        fact = envelopes[-1]
        fact["request_digest"] = _wire_request_digest(fact)
        envelopes[-2]["payload"]["pairing_key"] = fact["request_digest"]
        previous = None
        for envelope in envelopes:
            envelope["previous_digest"] = previous
            envelope["request_digest"] = _wire_request_digest(envelope)
            envelope["event_digest"] = _wire_event_digest(envelope)
            envelope["event_id"] = run_events.make_event_id(
                run_id=run_dir.name, sequence=envelope["sequence"], event_digest=envelope["event_digest"]
            )
            previous = envelope["event_digest"]
        path.write_bytes(b"".join(_wire_bytes(e) + b"\n" for e in envelopes))
        reason = "chain error or unsupported reader"
    stored = [json.loads(line) for line in path.read_bytes().splitlines()]
    previous = None
    for envelope in stored:
        assert envelope["previous_digest"] == previous
        assert _wire_event_digest(envelope) == envelope["event_digest"]
        previous = envelope["event_digest"]
    _refuse_unchanged(task_run, reason)


def test_batch_is_status_neutral_and_preserves_next_authoritative_write(authoritative_task_run):
    run_dir, _workspace = authoritative_task_run
    path = run_dir / "events/lifecycle.jsonl"
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    old_events = run_journal.read_journal_bounded(path).events
    old = run_projector.project_run_snapshot(snapshot, old_events, journal_present=True)
    batch = _accept(authoritative_task_run)
    events = run_journal.read_journal_bounded(path).events
    projected = run_projector.project_run_snapshot(snapshot, events, journal_present=True)
    assert projected.last_sequence == old.last_sequence + 2 == events[-1].sequence
    assert projected.snapshot["status"] == old.snapshot["status"] == "planning"
    assert projected.snapshot["journal_last_event_digest"] == events[-1].event_digest
    assert _accept(authoritative_task_run) == batch
    assert path.read_bytes() == b"".join(run_events.canonical_bytes(e.to_dict()) + b"\n" for e in events)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    snapshot["status"] = "dispatching"
    aboyeur._write_json(run_dir / "run.json", snapshot)
    assert json.loads((run_dir / "run.json").read_bytes())["status"] == "dispatching"
    assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
    assert run_shadow.check_projection_readiness(run_dir).ready
    artifact = json.loads(run_shadow.shadow_artifact_path(run_dir).read_bytes())
    assert artifact["errors"] == artifact["mismatches"] == 0
    assert tasks.read_task_batch(run_dir, request_id="batch-one") == batch


def test_batch_replay_does_not_regrant_terminal_claim(task_run, monkeypatch):
    """Descriptor operations and consumed result replay invoke no execution path."""
    from brigade import fleet_t3_transport, run_control_journal, run_transport

    def forbidden(*args, **kwargs):
        pytest.fail("descriptor membership invoked execution")

    for module, name in (
        (aboyeur, "plan"),
        (aboyeur, "build_synth_prompt"),
        (run_transport, "dispatch"),
        (fleet_t3_transport.T3FleetClient, "submit"),
        (run_control_journal, "execute_control_request"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    run_dir, workspace = task_run
    batch = _accept(task_run)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    current = tasks.read_task_batch(run_dir, request_id="batch-one").members[0]
    decision = tasks.claim_task_launch(
        run_dir, workspace=workspace, handle=handle, expected_version=current.version, seat="worker", dispatch_attempt=1
    )
    assert decision.outcome == "granted"
    completed = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation("completed", containment="quiescent", provider_result_id="result-one"),
    )
    acknowledged = tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=completed.result_id)
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    for replay in (_accept(task_run), tasks.read_task_batch(run_dir, request_id="batch-one")):
        assert replay.members == (acknowledged, batch.members[1])
        assert replay.batch_ref == batch.batch_ref
        assert replay.members[0].launch_claim == tasks.TaskLaunchBinding("worker", 1)
    assert (
        tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=completed.result_id)
        == acknowledged
    )
    terminal = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=acknowledged.version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert terminal.outcome == "terminal" and terminal.task == acknowledged
    assert path.read_bytes() == before
