"""Closed aggregate membership, replay and current member state contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace

import pytest

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle
from brigade import run_task_journal as journal
from brigade import run_tasks as tasks
from tests import test_run_tasks as support

task_run = support.task_run


def _specs():
    return (tasks.TaskSpec("one", "worker", "a" * 64), tasks.TaskSpec("two", "worker", "a" * 64))


def _membership(rows, version=1):
    return hashlib.sha256(
        run_events.canonical_bytes(
            {"domain": "brigade.task-batch.members.v1", "descriptor_version": version, "members": rows}
        )
    ).hexdigest()


def _payload():
    rows = [asdict(s) for s in _specs()]
    return {"descriptor_version": 1, "request_id": "batch-one", "members": rows, "membership_digest": _membership(rows)}


def _build(payload):
    return run_events.build_event(
        run_id="test-batch-run",
        sequence=1,
        event_type="task.batch-accepted",
        payload=payload,
        idempotency_key="batch",
        recorded_at="2026-01-01T00:00:00.000000Z",
        previous_digest=None,
    )


def test_batch_replay_retains_complete_ordered_membership(task_run):
    run_dir, workspace = task_run
    journal_path = run_dir / "events/lifecycle.jsonl"
    specs = (tasks.TaskSpec("one", "worker", "a" * 64), tasks.TaskSpec("two", "worker", "a" * 64))
    first = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs)
    assert first.specs == specs
    assert tuple(m.status for m in first.members) == ("queued", "queued")
    assert tasks.read_task_batch(run_dir, request_id="batch-one") == first
    before = journal_path.read_bytes()
    assert tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs) == first
    assert journal_path.read_bytes() == before
    facts = [e for e in run_journal.read_journal_bounded(journal_path).events if e.event_type == "task.batch-accepted"]
    assert len(facts) == 1 and facts[0].payload == _payload()
    assert all(m.version == facts[0].event_digest for m in first.members)
    assert tasks.read_tasks(run_dir) == first.members


@pytest.mark.parametrize(
    "change", ["reorder", "disjoint", "subset", "superset", "seat", "digest", "duplicate", 2, True]
)
def test_batch_identity_conflicts_are_nonmutating(task_run, change):
    run_dir, workspace = task_run
    specs = _specs()
    tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs)
    variants = {
        "reorder": specs[::-1],
        "disjoint": (tasks.TaskSpec("three", "worker", "a" * 64),),
        "subset": specs[:1],
        "superset": (*specs, tasks.TaskSpec("three", "worker", "a" * 64)),
        "seat": (replace(specs[0], seat="other"), specs[1]),
        "digest": (replace(specs[0], payload_digest="b" * 64), specs[1]),
        "duplicate": (specs[0], specs[0]),
    }
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task_batch(
            run_dir,
            workspace=workspace,
            request_id="batch-one",
            specs=variants.get(change, specs),
            descriptor_version=change if type(change) in (bool, int) else 1,
        )
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_batch_occupied_task_and_child_request_collisions_are_nonmutating(task_run):
    run_dir, workspace = task_run
    tasks.accept_task(run_dir, workspace=workspace, request_id="standalone", spec=_specs()[0])
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_batch_child_request_collision_refuses_all_members(task_run):
    run_dir, workspace = task_run
    child = journal.batch_child_request_id(run_id=run_dir.name, request_id="batch-one", task_id="two")
    tasks.accept_task(run_dir, workspace=workspace, request_id=child, spec=tasks.TaskSpec("other", "worker", "b" * 64))
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    assert [r.handle.spec.task_id for r in tasks.read_tasks(run_dir)] == ["other"]


@pytest.mark.parametrize("kind", ["empty", "nine", "list", "dict", "rich", "invalid-snapshot"])
def test_batch_rejects_unsupported_input_without_writes(task_run, kind):
    run_dir, workspace = task_run

    @dataclass(frozen=True)
    class RichSpec(tasks.TaskSpec):
        dependencies: tuple[str, ...] = ()

    invalid = tasks.TaskSpec("invalid", "worker", "a" * 64)
    object.__setattr__(invalid, "seat", {"execution": "bytes"})
    choices = {
        "empty": (),
        "nine": tuple(tasks.TaskSpec(f"task-{i}", "worker", "a" * 64) for i in range(9)),
        "list": list(_specs()),
        "dict": (asdict(_specs()[0]),),
        "rich": (RichSpec("rich", "worker", "a" * 64),),
        "invalid-snapshot": (invalid,),
    }
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=choices[kind])
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"descriptor_version": True},
        {"descriptor_version": 2},
        {"membership_digest": "b" * 64},
        {"members": []},
        {"members": [None]},
        {"members": ["row"]},
        {"members": [{"task_id": "one", "seat": "worker"}]},
        {"members": [{"task_id": "one", "seat": "worker", "payload_digest": "a" * 64, "dependencies": []}]},
        {"members": [{"task_id": "one", "seat": {"nested": "worker"}, "payload_digest": "a" * 64}]},
        {"dependencies": []},
    ],
)
def test_batch_event_closed_nested_schema(change):
    _build(_payload())
    with pytest.raises(run_events.CanonicalizationError):
        _build({**_payload(), **change})


def test_batch_schema_v2_only_and_generic_nested_payload_stays_closed():
    envelope = _build(_payload())
    assert envelope["schema_version"] == 2
    assert run_events.validate_event({**envelope, "schema_version": 1})
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(
            event_type="run.created", payload={"status": ["started"]}, idempotency_key="x"
        )


def test_batch_scoped_reads_and_outer_request_namespace(task_run):
    run_dir, workspace = task_run
    tasks.accept_task(
        run_dir, workspace=workspace, request_id="batch-one", spec=tasks.TaskSpec("other", "worker", "b" * 64)
    )
    first = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    second = tasks.accept_task_batch(
        run_dir, workspace=workspace, request_id="batch-two", specs=(tasks.TaskSpec("three", "worker", "c" * 64),)
    )
    assert tasks.read_task_batch(run_dir, request_id="batch-one") == first
    assert tasks.read_task_batch(run_dir, request_id="batch-two") == second
    assert len(tasks.read_tasks(run_dir)) == 4
    with pytest.raises(tasks.TaskJournalError):
        tasks.read_task_batch(run_dir, request_id="missing")


def test_batch_replay_preserves_current_member_versions(task_run):
    run_dir, workspace = task_run
    first = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    prepared = tasks.read_task_batch(run_dir, request_id="batch-one")
    claimed = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=prepared.members[0].version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert claimed.outcome == "granted"
    finished = tasks.observe_task(
        run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("completed")
    )
    acknowledged = tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=finished.result_id)
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    replay = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    assert replay.members == (acknowledged, first.members[1])
    assert replay.batch_ref == first.batch_ref and replay.members[0].launch_claim is not None
    terminal = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=acknowledged.version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert terminal.outcome == "terminal"
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    # A sibling's old version remains valid after unrelated member updates.
    sibling = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="two")
    current = tasks.read_task_batch(run_dir, request_id="batch-one").members[1]
    decision = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=sibling,
        expected_version=current.version,
        seat="worker",
        dispatch_attempt=2,
    )
    assert decision.outcome == "granted"


def test_member_launch_survives_interleaved_unrelated_batch(task_run):
    run_dir, workspace = task_run
    tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    member = tasks.read_task_batch(run_dir, request_id="batch-one").members[0]
    unrelated = tasks.accept_task_batch(
        run_dir,
        workspace=workspace,
        request_id="batch-two",
        specs=(tasks.TaskSpec("three", "other-worker", "c" * 64),),
    )
    assert member.version is not None
    decision = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=member.version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert decision.outcome == "granted"
    assert decision.task.launch_claim == tasks.TaskLaunchBinding("worker", 1)
    assert tasks.read_task_batch(run_dir, request_id="batch-two") == unrelated


def test_duplicate_aggregate_history_refuses_all_access(task_run):
    run_dir, workspace = task_run
    tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs())
    run_lifecycle.record_lifecycle_event(
        run_dir, workspace=workspace, event_type="task.batch-accepted", payload=_payload(), idempotency_key="duplicate"
    )
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    for read in (
        lambda: tasks.read_tasks(run_dir),
        lambda: tasks.read_task_batch(run_dir, request_id="batch-one"),
        lambda: tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs()),
    ):
        with pytest.raises(tasks.TaskJournalError):
            read()
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_batch_identity_helpers_use_frozen_domain_objects():
    rows = [asdict(s) for s in _specs()]
    assert journal.batch_membership_digest(descriptor_version=1, members=rows) == _membership(rows)
    for helper, domain, extra in (
        (journal.batch_child_request_id, "brigade.task-batch.child.v1", {"task_id": "one"}),
        (journal.batch_reference, "brigade.task-batch.ref.v1", {"membership_digest": "a" * 64}),
    ):
        obj = {"domain": domain, "run_id": "test-batch-run", "request_id": "batch-one", **extra}
        expected = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert helper(run_id="test-batch-run", request_id="batch-one", **extra) == expected


def test_batch_accepts_eight_maximum_length_descriptors(task_run):
    run_dir, workspace = task_run
    specs = tuple(tasks.TaskSpec(str(i) + "t" * 127, "w" * 512, "a" * 64) for i in range(8))
    accepted = tasks.accept_task_batch(run_dir, workspace=workspace, request_id="r" * 512, specs=specs)
    assert accepted.specs == specs and len(accepted.members) == 8
    assert tasks.read_task_batch(run_dir, request_id="r" * 512) == accepted


@pytest.mark.parametrize("bound", ["events", "bytes", "line"])
def test_batch_pair_preflight_refuses_before_checkpoint_publication(task_run, monkeypatch, bound):
    run_dir, workspace = task_run
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    assets = sorted((path.parent / run_checkpoint.CHECKPOINT_DIR_NAME).iterdir())
    if bound == "events":
        monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_EVENTS", len(before.splitlines()) + 1)
    elif bound == "bytes":
        monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(before) + 1)
    else:
        monkeypatch.setattr(run_events, "MAX_LINE_BYTES", 3000)
    specs = tuple(tasks.TaskSpec(str(i) + "t" * 127, "w" * 512, "a" * 64) for i in range(8))
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=specs)
    assert path.read_bytes() == before
    assert sorted((path.parent / run_checkpoint.CHECKPOINT_DIR_NAME).iterdir()) == assets


@pytest.mark.parametrize("batch_first", [True, False])
def test_cross_namespace_child_collision_is_invalid_history(task_run, batch_first):
    run_dir, workspace = task_run
    child = journal.batch_child_request_id(run_id=run_dir.name, request_id="batch-one", task_id="one")
    standalone = tasks.TaskSpec("other", "worker", "a" * 64)
    records = [
        ("task.batch-accepted", _payload()),
        ("task.accepted", {**asdict(standalone), "request_id": child, "spec_digest": standalone.spec_digest}),
    ]
    if not batch_first:
        records.reverse()
    for i, (event_type, payload) in enumerate(records):
        run_lifecycle.record_lifecycle_event(
            run_dir, workspace=workspace, event_type=event_type, payload=payload, idempotency_key=f"history-{i}"
        )
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    for read in (
        lambda: tasks.read_tasks(run_dir),
        lambda: tasks.read_task_batch(run_dir, request_id="batch-one"),
        lambda: tasks.accept_task_batch(run_dir, workspace=workspace, request_id="batch-one", specs=_specs()),
    ):
        with pytest.raises(tasks.TaskJournalError):
            read()
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
