"""Batch membership redaction at the closed authoritative transaction boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from brigade import (
    aboyeur,
    run_checkpoint,
    run_control_journal as control,
    run_journal,
    run_redaction,
    run_redaction_rewrite,
    run_task_journal,
    run_tasks as tasks,
    runguard,
)
from tests.test_run_redaction_tasks import requires_secure_transactions


def _events(run_dir):
    return list(run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").events)


def _publish(run_dir, events):
    events = [replace(event, sequence=i + 1) for i, event in enumerate(events)]
    rewritten, raw = run_redaction_rewrite.rewrite_events(
        events, [(dict(e.payload), e.idempotency_key) for e in events], error_type=RuntimeError
    )
    (run_dir / "events/lifecycle.jsonl").write_bytes(raw)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, rewritten).to_bytes())


@pytest.fixture
def closed_batches(tmp_path, monkeypatch, request):
    mode = getattr(request, "param", None)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    if mode in {"recomputed-spec", "recomputed-result"}:
        monkeypatch.setattr(run_redaction, "_operation_id", lambda *args: "redact-0123456789abcdef")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade/runs/batch-redaction"
    run_dir.mkdir(parents=True)
    snapshot = {
        "schema": "brigade.run.v1",
        "status": "started",
        "lock_workspace": str(workspace),
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", snapshot)
        aboyeur._write_json(run_dir / "run.json", snapshot)
        first_sequence = len(_events(run_dir)) + 2
        operation = run_redaction._operation_id(run_dir.name, first_sequence, first_sequence, "credential-exposure")
        for batch in range(2):
            specs = tuple(
                tasks.TaskSpec(f"member-{batch}-{i}", f"seat-{batch}-{i}", str(batch * 2 + i + 1) * 64)
                for i in range(2)
            )
            if mode == "digest-alias" and batch == 0:
                specs = (tasks.TaskSpec("1" * 64, specs[0].seat, specs[0].payload_digest), specs[1])
            if mode == "collision" and batch == 1:
                specs = (
                    tasks.TaskSpec(
                        specs[0].task_id, f"redacted-{operation}-{first_sequence}-seat-0", specs[0].payload_digest
                    ),
                    specs[1],
                )
            outer = f"private-batch-{batch}"
            if mode == "derived-outer" and batch == 1:
                outer = tasks.read_task_batch(run_dir, request_id="private-batch-0").specs[0].spec_digest
            tasks.accept_task_batch(
                run_dir,
                workspace=workspace,
                request_id=outer,
                specs=specs,
            )
        tasks.accept_task(
            run_dir,
            workspace=workspace,
            request_id="standalone",
            spec=tasks.TaskSpec("standalone", "solo-seat", "a" * 64),
        )
        handles = []
        for i, task in enumerate(tasks.read_tasks(run_dir)):
            handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task.handle.spec.task_id)
            current = next(t for t in tasks.read_tasks(run_dir) if t.handle.spec.task_id == handle.spec.task_id)
            assert (
                tasks.claim_task_launch(
                    run_dir,
                    workspace=workspace,
                    handle=handle,
                    expected_version=current.version,
                    seat=handle.spec.seat,
                    dispatch_attempt=i + 1,
                ).outcome
                == "granted"
            )
            handles.append(handle)
        for i, handle in enumerate(handles):
            batch_state = tasks.read_task_batch(run_dir, request_id="private-batch-0")
            aliases = {
                "task": handle.spec.task_id,
                "spec": handle.spec.spec_digest,
                "child": handle.request_id,
                "attempt": handle.attempt_id,
                "outer": batch_state.request_id,
                "membership": batch_state.membership_digest,
                "reference": batch_state.batch_ref,
            }
            provider_id = aliases[mode] if i == 0 and mode in aliases else f"provider-task-{i}"
            if i == 0 and mode == "recomputed-spec":
                provider_id = handle.spec.spec_digest
            if i == 0 and mode == "sibling-alias":
                provider_id = handles[1].spec.seat
            if i == 0 and mode == "other-batch-alias":
                provider_id = handles[2].spec.seat
            result = tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=handle,
                observation=tasks.TaskObservation(
                    "completed",
                    provider_task_id=provider_id,
                    original_run_id=f"provider-run-{i}",
                    provider_result_id=f"provider-result-{i}",
                    containment="quiescent",
                    artifact_kind="patch",
                    artifact_digest=str(i + 5) * 64,
                ),
            )
            tasks.acknowledge_result(
                run_dir, workspace=workspace, task_id=handle.spec.task_id, result_id=result.result_id
            )
        aboyeur._write_json(run_dir / "run.json", {**snapshot, "status": "ok"})
    # Targeted controls straddle later task/checkpoint facts, without transport.
    events = _events(run_dir)
    first = tasks.read_tasks(run_dir)[0]
    payload = control.control_fingerprint_payload(
        op="interrupt",
        request_id="private-control",
        task_id=first.handle.spec.task_id,
        attempt_id=first.handle.attempt_id,
        text="fake control",
        turn_id="private-control-turn",
    )
    if mode in {"recomputed-spec", "recomputed-result"}:
        prefix = f"redacted-redact-0123456789abcdef-{first_sequence}"
        candidate = tasks.TaskSpec(
            f"{prefix}-task_id-0",
            f"{prefix}-seat-0",
            hashlib.sha256(f"{prefix}-payload_digest-0".encode("ascii")).hexdigest(),
        )
        if mode == "recomputed-spec":
            payload["turn_id"] = candidate.spec_digest
        else:
            candidate_handle = tasks.TaskHandle(run_dir.name, "unused", candidate)
            candidate_handle = replace(candidate_handle, attempt_id=tasks._attempt_id(candidate_handle))
            candidate_result = replace(first, handle=candidate_handle)
            payload["turn_id"] = tasks._result_id(candidate_result)
    index = next(i for i, e in enumerate(events) if e.event_type == "task.observed") + 1
    template = events[index]
    events.insert(
        index,
        replace(
            template,
            event_type="control.requested",
            payload=payload,
            idempotency_key="control.requested:private-control",
        ),
    )
    events.append(
        replace(
            template,
            event_type="control.observed",
            payload={k: v for k, v in {**payload, "detail": "delivered"}.items() if k != "text_digest"},
            idempotency_key="control.observed:private-control",
        )
    )
    _publish(run_dir, events)
    assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
    return run_dir


@requires_secure_transactions
@pytest.mark.parametrize("selection", ["aggregate", "observation", "acknowledgement", "control", "interleaved"])
def test_batch_member_redaction_preserves_unrelated_membership(closed_batches, selection):
    run_dir = closed_batches
    before = _events(run_dir)
    original = tasks.read_task_batch(run_dir, request_id="private-batch-0")
    unrelated_specs = tasks.read_task_batch(run_dir, request_id="private-batch-1").specs
    standalone = next(t for t in tasks.read_tasks(run_dir) if t.handle.spec.task_id == "standalone")
    kinds = {
        "aggregate": "task.batch-accepted",
        "observation": "task.observed",
        "acknowledgement": "task.acknowledged",
        "control": "control.observed",
        "interleaved": "task.observed",
    }
    chosen = next(e for e in before if e.event_type == kinds[selection])
    start = chosen.sequence
    end = (
        next(e.sequence for e in before if e.event_type == "task.acknowledged") if selection == "interleaved" else start
    )
    private_selected_ref = original.specs[0].task_id
    run_redaction.redact_journal(
        run_dir, sequence_start=start, sequence_end=end, reason="credential-exposure", operator_confirmed=True
    )
    rewritten_bytes = (run_dir / "events/lifecycle.jsonl").read_bytes()
    assert private_selected_ref not in rewritten_bytes.decode()
    rewritten = _events(run_dir)
    batches = [e for e in rewritten if e.event_type == "task.batch-accepted"]
    rewritten_selected = tasks.read_task_batch(run_dir, request_id=batches[0].payload["request_id"])
    untouched = tasks.read_task_batch(run_dir, request_id=batches[1].payload["request_id"])
    assert untouched.specs == unrelated_specs
    assert rewritten_selected.members[0].acknowledged
    assert rewritten_selected.members[0].launch_claim is not None
    assert rewritten_selected.members[0].launch_claim.seat == rewritten_selected.specs[0].seat
    assert rewritten_selected.descriptor_version == 1 and len(rewritten_selected.members) == 2
    if selection != "interleaved":
        assert {
            k: v
            for k, v in asdict(
                next(t for t in tasks.read_tasks(run_dir) if t.handle.spec.task_id == "standalone")
            ).items()
            if k != "version"
        } == {k: v for k, v in asdict(standalone).items() if k != "version"}
    if selection not in {"aggregate", "interleaved"}:
        assert rewritten_selected.request_id == original.request_id
        assert rewritten_selected.specs[1] == original.specs[1]
    for event in rewritten:
        if event.payload.get("paired_event_type", "").startswith("task."):
            assert event.payload["pairing_key"] == rewritten[event.sequence].request_digest
    assert not run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").chain_errors
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"
    current = rewritten_selected.members[0]
    journal_before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with runguard.run_lock(run_dir.parents[2], run_dir=run_dir):
        decision = tasks.claim_task_launch(
            run_dir,
            workspace=run_dir.parents[2],
            handle=current.handle,
            expected_version=current.version,
            seat=current.handle.spec.seat,
            dispatch_attempt=1,
        )
        assert decision.outcome == "terminal"
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == journal_before


@requires_secure_transactions
@pytest.mark.parametrize("alias", ["task", "spec", "child", "attempt", "result", "outer", "membership", "reference"])
def test_batch_redaction_removes_nested_and_derived_aliases(closed_batches, alias):
    run_dir = closed_batches
    batch = tasks.read_task_batch(run_dir, request_id="private-batch-0")
    first = batch.members[0]
    old = {
        "task": first.handle.spec.task_id,
        "spec": first.handle.spec.spec_digest,
        "child": first.handle.request_id,
        "attempt": first.handle.attempt_id,
        "result": first.result_id,
        "outer": batch.request_id,
        "membership": batch.membership_digest,
        "reference": batch.batch_ref,
    }[alias]
    events = _events(run_dir)
    # Control aliases can name final result IDs without changing those results.
    for i, event in enumerate(events):
        if event.event_type.startswith("control."):
            events[i] = replace(event, payload={**event.payload, "turn_id": old})
    _publish(run_dir, events)
    chosen = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=chosen.sequence,
        sequence_end=chosen.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    raw = (run_dir / "events/lifecycle.jsonl").read_text()
    assert old not in raw
    aggregates = [e for e in _events(run_dir) if e.event_type == "task.batch-accepted"]
    current = tasks.read_task_batch(run_dir, request_id=aggregates[0].payload["request_id"])
    assert [s.task_id for s in current.specs] == [row["task_id"] for row in aggregates[0].payload["members"]]
    assert current.members[0].acknowledged and current.members[0].launch_claim is not None


@requires_secure_transactions
@pytest.mark.parametrize("corruption", ["duplicate", "commitment", "task", "control"])
def test_corrupt_batch_redaction_refuses_without_replacement(closed_batches, corruption):
    run_dir = closed_batches
    events = _events(run_dir)
    if corruption == "control":
        index = next(i for i, e in enumerate(events) if e.event_type == "control.observed")
        events[index] = replace(events[index], payload={**events[index].payload, "attempt_id": "f" * 64})
    elif corruption == "task":
        index = next(i for i, e in enumerate(events) if e.event_type == "task.acknowledged")
        events[index] = replace(events[index], payload={**events[index].payload, "result_id": "f" * 64})
    else:
        index = next(i for i, e in enumerate(events) if e.event_type == "task.batch-accepted")
        payload = {**events[index].payload, "members": [dict(row) for row in events[index].payload["members"]]}
        if corruption == "duplicate":
            payload["members"][1] = dict(payload["members"][0])
            payload["membership_digest"] = run_task_journal.batch_membership_digest(
                descriptor_version=1, members=payload["members"]
            )
        else:
            payload["membership_digest"] = "f" * 64
        events[index] = replace(events[index], payload=payload)
    # Envelope rebuilding validates payloads, so rechain malformed aggregates
    # using canonical envelope hashes directly, leaving semantic damage intact.
    if corruption in {"duplicate", "commitment"}:
        from brigade import run_events

        lines, previous = [], None
        for i, event in enumerate(events):
            if event.payload.get("paired_event_type") == "task.batch-accepted":
                following = events[i + 1]
                pair = run_events.request_digest(
                    event_type=following.event_type,
                    payload=following.payload,
                    idempotency_key=following.idempotency_key,
                )
                payload = {**event.payload, "pairing_key": pair}
                event = replace(
                    event,
                    payload=payload,
                    idempotency_key=run_checkpoint._checkpoint_idempotency_key(
                        payload["sha256"],
                        paired_event_type="task.batch-accepted",
                        body_kind=payload["body_kind"],
                        pairing_key=pair,
                    ),
                )
            envelope = asdict(event)
            envelope["payload"] = event.payload
            envelope["request_digest"] = run_events.request_digest(
                event_type=event.event_type, payload=event.payload, idempotency_key=event.idempotency_key
            )
            envelope["previous_digest"] = previous
            envelope["event_digest"] = run_events.compute_event_digest(envelope)
            envelope["event_id"] = run_events.make_event_id(
                run_id=event.run_id, sequence=event.sequence, event_digest=envelope["event_digest"]
            )
            previous = envelope["event_digest"]
            lines.append(run_events.canonical_bytes(envelope) + b"\n")
        (run_dir / "events/lifecycle.jsonl").write_bytes(b"".join(lines))
    else:
        _publish(run_dir, events)
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    directories = {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()}
    with pytest.raises(run_redaction.RedactionError):
        run_redaction.redact_journal(
            run_dir, sequence_start=1, sequence_end=len(events), reason="credential-exposure", operator_confirmed=True
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before
    assert {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()} == directories


@requires_secure_transactions
@pytest.mark.parametrize(
    "closed_batches", ["task", "spec", "child", "attempt", "outer", "membership", "reference"], indirect=True
)
def test_batch_redaction_remaps_provider_aliases(closed_batches):
    run_dir = closed_batches
    before = tasks.read_task_batch(run_dir, request_id="private-batch-0")
    private_alias = before.members[0].provider_task_id
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=aggregate.sequence,
        sequence_end=aggregate.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    current = tasks.read_task_batch(run_dir, request_id=aggregate.payload["request_id"])
    assert private_alias not in (run_dir / "events/lifecycle.jsonl").read_text()
    assert current.members[0].acknowledged
    assert current.members[0].provider_task_id != private_alias


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["collision"], indirect=True)
def test_nested_reserved_values_cannot_be_replacement_candidates(closed_batches):
    run_dir = closed_batches
    unrelated = tasks.read_task_batch(run_dir, request_id="private-batch-1")
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=aggregate.sequence,
        sequence_end=aggregate.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    selected = tasks.read_task_batch(run_dir, request_id=aggregate.payload["request_id"])
    assert selected.specs[0].seat != unrelated.specs[0].seat
    assert tasks.read_task_batch(run_dir, request_id=unrelated.request_id).specs == unrelated.specs


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["outer"], indirect=True)
def test_member_selection_rekeys_aliased_outer_without_scrubbing_sibling_descriptor(closed_batches):
    run_dir = closed_batches
    before = tasks.read_task_batch(run_dir, request_id="private-batch-0")
    selected = next(e for e in _events(run_dir) if e.event_type == "task.observed")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=selected.sequence,
        sequence_end=selected.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    current = tasks.read_task_batch(run_dir, request_id=aggregate.payload["request_id"])
    assert before.request_id not in (run_dir / "events/lifecycle.jsonl").read_text()
    assert current.request_id != before.request_id
    assert current.specs[1] == before.specs[1]
    assert current.members[1].handle.request_id != before.members[1].handle.request_id
    assert current.members[1].launch_claim == before.members[1].launch_claim


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["recomputed-spec", "recomputed-result"], indirect=True)
def test_selected_control_alias_cannot_reappear_as_recomputed_identity(closed_batches):
    run_dir = closed_batches
    selected = next(e for e in _events(run_dir) if e.event_type == "control.observed")
    private_alias = selected.payload["turn_id"]
    run_redaction.redact_journal(
        run_dir,
        sequence_start=selected.sequence,
        sequence_end=selected.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert private_alias not in (run_dir / "events/lifecycle.jsonl").read_text()
    assert all(t.acknowledged and t.launch_claim is not None for t in tasks.read_tasks(run_dir))


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["sibling-alias", "other-batch-alias"], indirect=True)
def test_selected_member_value_scrubs_equal_nested_alias_only(closed_batches):
    run_dir = closed_batches
    before = tasks.read_tasks(run_dir)
    private_alias = before[0].provider_task_id
    aliased = next(t for t in before if t.handle.spec.seat == private_alias)
    selected = next(e for e in _events(run_dir) if e.event_type == "task.observed")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=selected.sequence,
        sequence_end=selected.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert private_alias not in (run_dir / "events/lifecycle.jsonl").read_text()
    current = next(t for t in tasks.read_tasks(run_dir) if t.handle.spec.task_id == aliased.handle.spec.task_id)
    assert current.handle.spec.payload_digest == aliased.handle.spec.payload_digest
    assert current.handle.spec.seat != aliased.handle.spec.seat
    assert current.acknowledged and current.launch_claim.seat == current.handle.spec.seat


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["digest-alias"], indirect=True)
def test_task_identity_alias_in_digest_field_remains_a_digest(closed_batches):
    run_dir = closed_batches
    selected = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=selected.sequence,
        sequence_end=selected.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert "1" * 64 not in (run_dir / "events/lifecycle.jsonl").read_text()
    aggregate = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    current = tasks.read_task_batch(run_dir, request_id=aggregate.payload["request_id"])
    assert run_task_journal.digest(current.specs[0].payload_digest)
    assert current.members[0].acknowledged


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["derived-outer"], indirect=True)
def test_derived_alias_in_other_outer_request_rekeys_children_only(closed_batches):
    run_dir = closed_batches
    before = tasks.read_task_batch(run_dir, request_id="private-batch-0")
    other = tasks.read_task_batch(run_dir, request_id=before.specs[0].spec_digest)
    selected = next(e for e in _events(run_dir) if e.event_type == "task.batch-accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=selected.sequence,
        sequence_end=selected.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert other.request_id not in (run_dir / "events/lifecycle.jsonl").read_text()
    aggregate = [e for e in _events(run_dir) if e.event_type == "task.batch-accepted"][1]
    current = tasks.read_task_batch(run_dir, request_id=aggregate.payload["request_id"])
    assert current.specs == other.specs
    assert all(
        new.handle.request_id != old.handle.request_id for new, old in zip(current.members, other.members, strict=True)
    )
    assert all(
        new.launch_claim == old.launch_claim and new.acknowledged
        for new, old in zip(current.members, other.members, strict=True)
    )


@requires_secure_transactions
@pytest.mark.parametrize("closed_batches", ["sibling-alias"], indirect=True)
def test_nested_affected_value_check_refuses_semantically_valid_leak_before_publication(closed_batches, monkeypatch):
    run_dir = closed_batches
    events = _events(run_dir)
    selected = next(e for e in events if e.event_type == "task.observed")
    sibling_id = tasks.read_task_batch(run_dir, request_id="private-batch-0").specs[1].task_id
    prepare = run_redaction.run_redaction_tasks.prepare_requests

    def retain_nested_alias(events, requests, **kwargs):
        prepare(events, requests, **kwargs)
        for index, event in enumerate(events):
            if event.event_type == "task.batch-accepted" and event.payload["request_id"] == "private-batch-0":
                payload, key = requests[index]
                rows = [payload["members"][0], dict(event.payload["members"][1])]
                requests[index] = (
                    {
                        **payload,
                        "members": rows,
                        "membership_digest": run_task_journal.batch_membership_digest(
                            descriptor_version=1, members=rows
                        ),
                    },
                    key,
                )
            elif event.payload.get("task_id") == sibling_id:
                requests[index] = (dict(event.payload), event.idempotency_key)
        # The injected leak is coherent task history, not a corrupt result or
        # checkpoint that could mask the final nested-value privacy check.
        rebuilt, _ = run_redaction_rewrite.rewrite_events(events, requests, error_type=RuntimeError)
        tasks._fold(run_dir.name, rebuilt)

    monkeypatch.setattr(run_redaction.run_redaction_tasks, "prepare_requests", retain_nested_alias)
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    directories = {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()}
    with pytest.raises(run_redaction.RedactionError, match="rewritten journal still contains affected private values"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=selected.sequence,
            sequence_end=selected.sequence,
            reason="credential-exposure",
            operator_confirmed=True,
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before
    assert {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()} == directories
