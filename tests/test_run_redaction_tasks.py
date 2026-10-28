"""Task redaction through the public closed authoritative transaction."""

from __future__ import annotations

import json
import os
from dataclasses import asdict

import pytest

from brigade import aboyeur, run_checkpoint, run_events, run_journal, run_redaction, run_tasks as tasks, runguard


# Inspect OS capabilities independently: a broken production guard must fail
# transaction tests on a capable host, rather than silently skipping them.
requires_secure_transactions = pytest.mark.skipif(
    not (
        os.name == "posix"
        and all(op in os.supports_dir_fd for op in (os.open, os.mkdir, os.rename, os.unlink, os.link))
        and os.listdir in os.supports_fd
        and os.link in os.supports_follow_symlinks
        and getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and callable(getattr(os, "fchmod", None))
        and callable(getattr(os, "fsync", None))
    ),
    reason="safe redaction transactions require POSIX descriptor-relative traversal and durability",
)


@pytest.fixture
def closed_tasks(tmp_path, monkeypatch, request):
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade/runs/task-redaction"
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
        for i in range(2):
            task_id = f"private-task-{i}"
            tasks.accept_task(
                run_dir,
                workspace=workspace,
                request_id=f"private-request-{i}",
                spec=tasks.TaskSpec(task_id, f"private-seat-{i}", str(i + 1) * 64),
            )
            handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
            if getattr(request, "param", None) == "claim":
                current = tasks.read_tasks(run_dir)[i]
                decision = tasks.claim_task_launch(
                    run_dir,
                    workspace=workspace,
                    handle=handle,
                    expected_version=current.version,
                    seat=handle.spec.seat,
                    dispatch_attempt=i + 1,
                )
                assert decision.outcome == "granted"
            tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=handle,
                observation=tasks.TaskObservation(
                    "running",
                    provider_task_id=(handle.attempt_id if request.param == "derived" else f"private-provider-turn-{i}")
                    if getattr(request, "param", None) in {"derived", "control", "claim"}
                    else task_id,
                    original_run_id=(
                        handle.spec.spec_digest if getattr(request, "param", None) == "derived" else f"private-run-{i}"
                    ),
                ),
            )
            result = tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=handle,
                observation=tasks.TaskObservation(
                    "completed",
                    provider_result_id=f"private-result-{i}",
                    artifact_kind="patch",
                    artifact_digest=str(i + 3) * 64,
                    containment="quiescent",
                ),
            )
            tasks.acknowledge_result(run_dir, workspace=workspace, task_id=task_id, result_id=result.result_id)
            # A terminal containment update repeats provider/artifact/result references.
            tasks.observe_task(
                run_dir,
                workspace=workspace,
                handle=handle,
                observation=tasks.TaskObservation("completed", containment="active"),
            )
        aboyeur._write_json(run_dir / "run.json", {**snapshot, "status": "ok"})
    assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
    assert json.loads((run_dir / "run.json").read_bytes())["status"] == "ok"
    return run_dir


@requires_secure_transactions
@pytest.mark.parametrize(
    "selection",
    ["accepted", "attempt-prepared", "running", "completed", "acknowledged", "lifecycle", "multiple", "whole"],
)
def test_task_redaction_preserves_relationships_without_selected_references(closed_tasks, selection):
    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    before = run_journal.read_journal_bounded(journal).events
    original_results = tasks.read_tasks(run_dir)
    first = [e for e in before if e.payload.get("task_id") == "private-task-0"]
    if selection == "whole":
        start, end = 1, len(before)
    elif selection == "multiple":
        start, end = (
            first[0].sequence,
            next(
                e.sequence
                for e in before
                if e.event_type == "task.accepted" and e.payload["task_id"] == "private-task-1"
            ),
        )
    elif selection == "lifecycle":
        start, end = first[0].sequence, first[-1].sequence
    else:
        chosen = next(
            e
            for e in first
            if (
                e.payload.get("status") == selection
                if selection in {"running", "completed"}
                else e.event_type == "task." + selection
            )
        )
        start = end = chosen.sequence
    report = run_redaction.redact_journal(
        run_dir, sequence_start=start, sequence_end=end, reason="credential-exposure", operator_confirmed=True
    )
    committed = journal.read_bytes()
    events = run_journal.read_journal_bounded(journal)
    assert not events.chain_errors and events.partial_tail is None
    results = tasks.read_tasks(run_dir)
    assert len(results) == 2
    assert all(r.status == "completed" and r.containment == "active" and r.acknowledged for r in results)
    # These are structural classifications, not private references.
    structural = {"status", "containment", "artifact_kind"}
    for event in before:
        if start <= event.sequence <= end and event.event_type.startswith("task."):
            for field, value in event.payload.items():
                if field not in structural and value is not None:
                    assert value.encode() not in committed, field
    for old, new in zip(original_results, results, strict=True):
        touched = any(
            start <= e.sequence <= end and e.payload.get("task_id") == old.handle.spec.task_id for e in before
        )
        if touched:
            assert new.handle.spec.task_id != old.handle.spec.task_id
            assert new.handle.spec.spec_digest != old.handle.spec.spec_digest
            assert new.handle.attempt_id != old.handle.attempt_id
            assert new.result_id != old.result_id
        else:
            assert {k: v for k, v in asdict(new).items() if k != "version"} == {
                k: v for k, v in asdict(old).items() if k != "version"
            }
            for event in before:
                if event.payload.get("task_id") == old.handle.spec.task_id:
                    assert events.events[event.sequence - 1].request_digest == event.request_digest
    for event in events.events:
        if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE and "pairing_key" in event.payload:
            following = events.events[event.sequence]
            covered = [event, following]
            run_checkpoint._verify_coverage(
                covered, event, json.loads(run_checkpoint.validate_checkpoint(run_dir, event))
            )
            if following.event_type.startswith("task."):
                assert event.payload["pairing_key"] == following.request_digest
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"
    again = run_redaction.redact_journal(
        run_dir, sequence_start=start, sequence_end=end, reason="credential-exposure", operator_confirmed=True
    )
    assert again.operation_id == report.operation_id
    assert journal.read_bytes() == committed
    # A distinct operation may select an already rewritten task relationship.
    run_redaction.redact_journal(
        run_dir, sequence_start=start, sequence_end=end, reason="policy-removal", operator_confirmed=True
    )
    assert len(tasks.read_tasks(run_dir)) == 2


@requires_secure_transactions
@pytest.mark.parametrize(
    ("event_type", "field"),
    [
        ("task.accepted", "spec_digest"),
        ("task.attempt-prepared", "attempt_id"),
        ("task.observed", "result_id"),
        ("task.acknowledged", "result_id"),
    ],
)
def test_task_redaction_refuses_semantic_corruption_without_publication(closed_tasks, event_type, field):
    from brigade import run_redaction_rewrite

    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    chosen = next(e for e in events if e.event_type == event_type and e.payload.get(field) is not None)
    requests = [(dict(e.payload), e.idempotency_key) for e in events]
    requests[chosen.sequence - 1][0][field] = "f" * 64
    # Rebuild genuine request/chain/checkpoint digests: only the task semantics
    # are corrupt, so an envelope or checkpoint rejection cannot mask this test.
    rewritten, raw = run_redaction_rewrite.rewrite_events(events, requests, error_type=RuntimeError)
    journal.write_bytes(raw)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, rewritten).to_bytes())
    assert not run_journal.read_journal_bounded(journal).chain_errors
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    with pytest.raises(run_redaction.RedactionError, match="^task history verification failed$"):
        run_redaction.redact_journal(
            run_dir, sequence_start=1, sequence_end=len(events), reason="credential-exposure", operator_confirmed=True
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before


@requires_secure_transactions
@pytest.mark.parametrize("bound", ["line", "journal"])
def test_task_redaction_size_preflight_is_atomic(closed_tasks, monkeypatch, bound):
    from brigade import run_events

    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    original_prepare = run_redaction.run_redaction_tasks.prepare_requests

    def tighten_bound(*args, **kwargs):
        original_prepare(*args, **kwargs)
        # Tighten only once original bytes have passed admission. The final
        # staged bytes, including collateral task/checkpoint rewrites, must fit.
        if bound == "line":
            monkeypatch.setattr(
                run_events, "MAX_LINE_BYTES", max(len(line) for line in journal.read_bytes().splitlines(keepends=True))
            )
        else:
            monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(journal.read_bytes()))

    monkeypatch.setattr(run_redaction.run_redaction_tasks, "prepare_requests", tighten_bound)
    with pytest.raises(run_redaction.RedactionError, match="rewritten journal.*(canonicalization failed|size bound)"):
        run_redaction.redact_journal(
            run_dir, sequence_start=1, sequence_end=len(events), reason="credential-exposure", operator_confirmed=True
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before


@requires_secure_transactions
def test_task_redaction_recovers_after_replacement_crash(closed_tasks, monkeypatch):
    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    chosen = next(e for e in events if e.event_type == "task.accepted")
    original_replace = run_redaction._replace_projection

    def crash(*args, **kwargs):
        raise run_redaction.RedactionError("simulated projection crash")

    with monkeypatch.context() as patch:
        patch.setattr(run_redaction, "_replace_projection", crash)
        with pytest.raises(run_redaction.RedactionError, match="simulated projection crash"):
            run_redaction.redact_journal(
                run_dir,
                sequence_start=chosen.sequence,
                sequence_end=chosen.sequence,
                reason="credential-exposure",
                operator_confirmed=True,
            )
    prefix = journal.read_bytes()
    assert run_redaction._replace_projection is original_replace
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=chosen.sequence,
        sequence_end=chosen.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert journal.read_bytes().startswith(prefix)
    assert report.quarantine_path.is_file()
    assert all(r.acknowledged for r in tasks.read_tasks(run_dir))
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"


@requires_secure_transactions
@pytest.mark.parametrize("stage", ["original", "staged"])
def test_task_redaction_refuses_earlier_unkeyed_checkpoint_without_publication(closed_tasks, monkeypatch, stage):
    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    checkpoint = next(e for e in events if e.payload.get("paired_event_type") == "task.accepted")
    assert checkpoint.sequence < run_checkpoint.latest_checkpoint_event(events).sequence

    def without_pairing_key(events):
        rebuilt, lines, previous = [], [], None
        for event in events:
            payload = dict(event.payload)
            if event.sequence == checkpoint.sequence:
                payload.pop("pairing_key")
            # Rechain without the redaction rewriter, which would repair the
            # missing identity. All envelope digests must remain valid.
            envelope = run_events.build_event(
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=payload,
                idempotency_key=event.idempotency_key,
                recorded_at=event.recorded_at,
                previous_digest=previous,
            )
            rebuilt.append(run_journal.RunEvent(**envelope))
            lines.append(run_events.canonical_bytes(envelope) + b"\n")
            previous = envelope["event_digest"]
        return rebuilt, b"".join(lines)

    if stage == "original":
        malformed, raw = without_pairing_key(events)
        journal.write_bytes(raw)
        snapshot = json.loads((run_dir / "run.json").read_bytes())
        (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, malformed).to_bytes())
        assert not run_journal.read_journal_bounded(journal).chain_errors
        with pytest.raises(tasks.TaskJournalError, match="task checkpoint refused: pairing"):
            tasks.read_tasks(run_dir)
    else:
        original_rewrite = run_redaction._rewrite_events

        def invalid_pair(events, **kwargs):
            rewritten, _ = original_rewrite(events, **kwargs)
            return without_pairing_key(rewritten)

        monkeypatch.setattr(run_redaction, "_rewrite_events", invalid_pair)
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    directories = {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()}
    with pytest.raises(run_redaction.RedactionError, match="^checkpoint verification failed$"):
        run_redaction.redact_journal(
            run_dir, sequence_start=1, sequence_end=len(events), reason="credential-exposure", operator_confirmed=True
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before
    assert {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()} == directories
    assert not (run_dir / "events/redactions").exists()


@pytest.mark.parametrize("platform_name", ["nt", "java"])
def test_task_redaction_refuses_unsupported_platform_before_lock_or_mutation(closed_tasks, monkeypatch, platform_name):
    run_dir = closed_tasks
    workspace = run_dir.parents[2]
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    directories = {p.relative_to(workspace) for p in workspace.rglob("*") if p.is_dir()}
    task_results = tasks.read_tasks(run_dir)
    assert not (workspace / ".brigade/run.lock").exists()
    monkeypatch.setattr(run_redaction, "_PLATFORM_NAME", platform_name)

    def refuse_lock(*args, **kwargs):
        pytest.fail("unsupported task redaction reached lock acquisition")

    monkeypatch.setattr(run_redaction, "_exclusive_redaction_lock", refuse_lock)
    with pytest.raises(run_redaction.RedactionError, match="^unsupported platform for safe redaction transactions$"):
        run_redaction.redact_journal(
            run_dir, sequence_start=1, sequence_end=1, reason="credential-exposure", operator_confirmed=True
        )

    assert {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()} == before
    assert {p.relative_to(workspace) for p in workspace.rglob("*") if p.is_dir()} == directories
    assert tasks.read_tasks(run_dir) == task_results
    assert not (workspace / ".brigade/run.lock").exists()
    assert not (run_dir / "events/redactions").exists()


@requires_secure_transactions
@pytest.mark.parametrize("selected_type", ["task.accepted", "task.attempt-prepared", "control.requested", "whole"])
@pytest.mark.parametrize("alias_fields", [False, True, "derived"])
def test_task_redaction_rewrites_interleaved_targeted_controls(closed_tasks, selected_type, alias_fields):
    from brigade import run_control_journal as control

    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    original, unrelated = tasks.read_tasks(run_dir)
    # Insert a request before the last checkpoint, then append its delivery
    # observation after it, exercising cross-checkpoint task/control aliases.
    index = next(i for i, event in enumerate(events) if event.event_type == "task.attempt-prepared") + 1
    kwargs = dict(run_id=run_dir.name, recorded_at=events[index - 1].recorded_at)
    payload = control.control_fingerprint_payload(
        op="interrupt",
        request_id="targeted",
        task_id=original.handle.spec.task_id,
        attempt_id=original.handle.attempt_id,
        text="private-steering-text",
        turn_id="private-control-turn",
    )
    if alias_fields:
        payload["turn_id"] = original.handle.spec.task_id
        payload["text_digest"] = original.handle.spec.payload_digest
    if alias_fields == "derived":
        payload["turn_id"] = original.handle.attempt_id
        payload["text_digest"] = original.result_id
    if alias_fields == "provider":
        payload["turn_id"] = original.provider_task_id
    request = run_journal.RunEvent(
        **run_events.build_event(
            **kwargs,
            sequence=index + 1,
            event_type="control.requested",
            payload=payload,
            idempotency_key="control.requested:targeted",
            previous_digest="0" * 64,
        )
    )
    observation = run_journal.RunEvent(
        **run_events.build_event(
            **kwargs,
            sequence=len(events) + 2,
            event_type="control.observed",
            payload={key: value for key, value in {**payload, "detail": "ok"}.items() if key != "text_digest"},
            idempotency_key="control.observed:targeted",
            previous_digest="0" * 64,
        )
    )
    from dataclasses import replace
    from brigade import run_redaction_rewrite

    events.insert(index, request)
    events.append(observation)
    events = [replace(event, sequence=i + 1) for i, event in enumerate(events)]
    events, raw = run_redaction_rewrite.rewrite_events(
        events, [(dict(e.payload), e.idempotency_key) for e in events], error_type=RuntimeError
    )
    journal.write_bytes(raw)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, events).to_bytes())
    chosen = next(
        e for e in events if e.event_type == ("control.requested" if selected_type == "whole" else selected_type)
    )
    run_redaction.redact_journal(
        run_dir,
        sequence_start=1 if selected_type == "whole" else chosen.sequence,
        sequence_end=len(events) if selected_type == "whole" else chosen.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    current = tasks.read_tasks(run_dir)[0]
    controls = [e for e in run_journal.read_journal_bounded(journal).events if e.event_type.startswith("control.")]
    assert len(controls) == 2
    for event in controls:
        assert event.payload["task_id"] == current.handle.spec.task_id
        assert event.payload["attempt_id"] == current.handle.attempt_id
    if selected_type in {"control.requested", "whole"}:
        assert payload["text_digest"].encode() not in journal.read_bytes()
        assert b"private-control-turn" not in journal.read_bytes()
    assert original.handle.spec.task_id.encode() not in journal.read_bytes()
    assert original.handle.attempt_id.encode() not in journal.read_bytes()
    if alias_fields == "derived":
        assert original.result_id.encode() not in journal.read_bytes()
    if alias_fields == "provider":
        assert original.provider_task_id.encode() not in journal.read_bytes()
        assert current.provider_task_id != original.provider_task_id
        retained = tasks.read_tasks(run_dir)[1]
        assert {k: v for k, v in asdict(retained).items() if k != "version"} == {
            k: v for k, v in asdict(unrelated).items() if k != "version"
        }
    if alias_fields is True and selected_type in {"task.accepted", "control.requested", "whole"}:
        assert original.handle.spec.payload_digest.encode() not in journal.read_bytes()
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"


@requires_secure_transactions
@pytest.mark.parametrize("identity", ["external-attempt", "external-task", "invalid-op"])
@pytest.mark.parametrize("selected_type", ["task.accepted", "control.requested", "control.observed"])
def test_targeted_control_redaction_refuses_unverifiable_history_atomically(closed_tasks, identity, selected_type):
    from brigade import run_control_journal as control, run_redaction_rewrite

    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = list(run_journal.read_journal_bounded(journal).events)
    original = tasks.read_tasks(run_dir)[0]
    payload = control.control_fingerprint_payload(
        op="interrupt",
        request_id="unverifiable",
        task_id=original.handle.spec.task_id,
        attempt_id=original.handle.attempt_id,
    )
    if identity == "external-attempt":
        payload["attempt_id"] = "private-external-attempt"
    elif identity == "external-task":
        payload["task_id"] = "private-external-task"
    else:
        payload["op"] = "private-invalid-operation"
    # Put both control facts before the latest checkpoint. Suffix validation
    # alone cannot detect malformed historical controls here.
    index = next(i for i, event in enumerate(events) if event.event_type == "task.attempt-prepared") + 1
    from dataclasses import replace

    controls = [
        replace(events[index], event_type=kind, payload=body, idempotency_key=f"{kind}:unverifiable")
        for kind, body in (
            ("control.requested", payload),
            ("control.observed", {k: v for k, v in {**payload, "detail": "ok"}.items() if k != "text_digest"}),
        )
    ]
    events[index:index] = controls
    events = [replace(event, sequence=i + 1) for i, event in enumerate(events)]
    events, raw = run_redaction_rewrite.rewrite_events(
        events,
        [(dict(e.payload), e.idempotency_key) for e in events],
        error_type=RuntimeError,
    )
    journal.write_bytes(raw)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, events).to_bytes())
    chosen = next(e for e in events if e.event_type == selected_type)
    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
    directories = {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()}
    if identity == "external-task" and selected_type == "task.accepted":
        # This control belongs to an unrelated task. Keep its pair unchanged.
        run_redaction.redact_journal(
            run_dir,
            sequence_start=chosen.sequence,
            sequence_end=chosen.sequence,
            reason="credential-exposure",
            operator_confirmed=True,
        )
        current = [e for e in run_journal.read_journal_bounded(journal).events if e.event_type.startswith("control.")]
        assert [(e.payload, e.idempotency_key) for e in current] == [(e.payload, e.idempotency_key) for e in controls]
        assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"
        return
    with pytest.raises(run_redaction.RedactionError, match="control history verification failed"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=chosen.sequence,
            sequence_end=chosen.sequence,
            reason="credential-exposure",
            operator_confirmed=True,
        )
    assert {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()} == before
    assert {p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_dir()} == directories


@requires_secure_transactions
@pytest.mark.parametrize("redact_legacy_first", [False, True])
def test_task_redaction_preserves_unrelated_historical_controls(closed_tasks, redact_legacy_first):
    from dataclasses import replace
    from brigade import run_control_journal as control, run_redaction_rewrite

    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    events = list(run_journal.read_journal_bounded(journal).events)
    index = next(i for i, event in enumerate(events) if event.event_type == "task.attempt-prepared") + 1
    payload = control.control_fingerprint_payload(op="interrupt", request_id="legacy-control")
    events[index:index] = [
        replace(events[index], event_type=kind, payload=body, idempotency_key=f"{kind}:legacy-control")
        for kind, body in (
            ("control.requested", payload),
            ("control.observed", {k: v for k, v in {**payload, "detail": "ok"}.items() if k != "text_digest"}),
        )
    ]
    events = [replace(event, sequence=i + 1) for i, event in enumerate(events)]
    events, raw = run_redaction_rewrite.rewrite_events(
        events,
        [(dict(e.payload), e.idempotency_key) for e in events],
        error_type=RuntimeError,
    )
    journal.write_bytes(raw)
    snapshot = json.loads((run_dir / "run.json").read_bytes())
    (run_dir / "run.json").write_bytes(run_redaction._projection(snapshot, events).to_bytes())
    if redact_legacy_first:
        run_redaction.redact_journal(
            run_dir,
            sequence_start=index + 1,
            sequence_end=index + 2,
            reason="credential-exposure",
            operator_confirmed=True,
        )
    before = [e for e in run_journal.read_journal_bounded(journal).events if e.event_type.startswith("control.")]
    if redact_legacy_first:
        assert all(e.payload["op"] == run_redaction.REDACTED_VALUE for e in before)
    chosen = next(e for e in events if e.event_type == "task.accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=chosen.sequence,
        sequence_end=chosen.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    after = [e for e in run_journal.read_journal_bounded(journal).events if e.event_type.startswith("control.")]
    assert [(e.payload, e.idempotency_key) for e in after] == [(e.payload, e.idempotency_key) for e in before]
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"


@requires_secure_transactions
@pytest.mark.parametrize("closed_tasks", ["derived"], indirect=True)
def test_accepted_redaction_remaps_derived_provider_aliases(closed_tasks):
    run_dir = closed_tasks
    journal = run_dir / "events/lifecycle.jsonl"
    original = tasks.read_tasks(run_dir)[0]
    accepted = next(e for e in run_journal.read_journal_bounded(journal).events if e.event_type == "task.accepted")
    run_redaction.redact_journal(
        run_dir,
        sequence_start=accepted.sequence,
        sequence_end=accepted.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    current = tasks.read_tasks(run_dir)[0]
    assert current.provider_task_id == current.handle.attempt_id
    assert current.original_run_id == current.handle.spec.spec_digest
    assert current.result_id != original.result_id and current.acknowledged
    assert original.handle.attempt_id.encode() not in journal.read_bytes()
    assert original.handle.spec.spec_digest.encode() not in journal.read_bytes()
    assert run_checkpoint.recover_from_checkpoint(run_dir, None)["status"] == "ok"


@requires_secure_transactions
@pytest.mark.parametrize("closed_tasks", ["control"], indirect=True)
def test_selected_control_scrubs_provider_aliases_in_task_history(closed_tasks):
    test_task_redaction_rewrites_interleaved_targeted_controls(closed_tasks, "control.observed", "provider")


@requires_secure_transactions
@pytest.mark.parametrize("closed_tasks", ["claim"], indirect=True)
@pytest.mark.parametrize("selected_type", ["task.launch-claimed", "task.accepted", "control.requested", "whole"])
def test_claim_redaction_preserves_binding_and_never_regrants(closed_tasks, selected_type):
    run_dir = closed_tasks
    original, unrelated = tasks.read_tasks(run_dir)
    test_task_redaction_rewrites_interleaved_targeted_controls(run_dir, selected_type, "derived")
    current, retained = tasks.read_tasks(run_dir)
    assert current.launch_claim == tasks.TaskLaunchBinding(current.handle.spec.seat, 1)
    assert current.acknowledged and current.status == "completed"
    if selected_type != "whole":
        assert {k: v for k, v in asdict(retained).items() if k != "version"} == {
            k: v for k, v in asdict(unrelated).items() if k != "version"
        }
    journal = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal).events
    claim = next(e for e in events if e.event_type == "task.launch-claimed")
    assert claim.payload == {
        "task_id": current.handle.spec.task_id,
        "attempt_id": current.handle.attempt_id,
        "seat": current.handle.spec.seat,
        "dispatch_attempt": 1,
    }
    assert type(claim.payload["dispatch_attempt"]) is int
    if selected_type in {"task.launch-claimed", "task.accepted", "control.requested", "whole"}:
        assert original.handle.spec.seat.encode() not in journal.read_bytes()
    for event in events:
        if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE and "pairing_key" in event.payload:
            following = events[event.sequence]
            run_checkpoint._verify_coverage(
                [event, following], event, json.loads(run_checkpoint.validate_checkpoint(run_dir, event))
            )
            if following.event_type.startswith("task."):
                assert event.payload["pairing_key"] == following.request_digest
    before = journal.read_bytes()
    workspace = run_dir.parents[2]
    with runguard.run_lock(workspace, run_dir=run_dir):
        assert runguard.is_active_run_owner(workspace, run_dir)
        decision = tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=current.handle,
            expected_version=current.version,
            seat=current.handle.spec.seat,
            dispatch_attempt=1,
        )
        assert decision.outcome == "terminal"
        assert decision.task.launch_claim == current.launch_claim
        with pytest.raises(tasks.TaskJournalError):
            tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=original.handle,
                expected_version=original.version,
                seat=original.handle.spec.seat,
                dispatch_attempt=1,
            )
    assert journal.read_bytes() == before
    assert tasks.read_tasks(run_dir)[0].launch_claim == current.launch_claim


@requires_secure_transactions
@pytest.mark.parametrize("closed_tasks", ["claim"], indirect=True)
def test_claim_redaction_refuses_corruption_without_publication(closed_tasks):
    test_task_redaction_refuses_semantic_corruption_without_publication(
        closed_tasks, "task.launch-claimed", "attempt_id"
    )
