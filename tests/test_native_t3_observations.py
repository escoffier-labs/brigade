"""Pure lifecycle correlation cannot grant launch or establish result consumption."""

from dataclasses import FrozenInstanceError, replace

import pytest

from brigade.native_t3_observations import (
    NativeObservation,
    NativeTaskReference,
    classify_native_snapshot,
    observation_diagnostic,
)


@pytest.fixture
def reference():
    return NativeTaskReference("example-origin", "example-request", "example-task")


@pytest.fixture
def snapshot():
    # Structured status data, rather than the MCP content wrapper.
    return {
        "taskId": "example-task",
        "status": "completed",
        "workState": "result_available",
        "hasPendingChildRuns": False,
        "hasPendingQuestion": False,
        "pendingApproval": None,
        "waitTimedOut": False,
        "remoteChild": None,
        "latestTerminalStatus": "completed",
        "latestTerminalRunId": "example-terminal-run",
        "latestTerminalResultContextTransferId": "example-transfer",
        "resultContextTransferId": "example-transfer",
        "childRunId": "example-child-run",
        "childThreadId": "example-child-thread",
    }


def classify(reference, snapshot, *, containment="quiescent", origin="example-origin"):
    return classify_native_snapshot(reference, snapshot, observed_origin_environment_id=origin, containment=containment)


@pytest.mark.parametrize("snapshot", [None, {}])
def test_unavailable_snapshot_retains_reference(reference, snapshot):
    result = classify(reference, snapshot)
    assert result.family == "awaiting"
    assert result.reference == reference
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


def test_lost_acceptance_cannot_adopt_snapshot_task(snapshot):
    expected = NativeTaskReference("example-origin", "example-original-request")
    result = classify(expected, snapshot)
    assert observation_diagnostic(result) == {"family": "awaiting", "reason": "acceptance-uncertain"}
    assert result.reference == expected
    assert result.reference.task_id is None
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize("origin,family", [(None, "awaiting"), ("example-other-origin", "refused")])
def test_origin_scope_is_required(reference, snapshot, origin, family):
    result = classify(reference, snapshot, origin=origin)
    assert result.family == family
    assert result.reference == reference


@pytest.mark.parametrize("task_id", ["example-other-task", "example-child-thread", "example-child-run"])
def test_task_identity_cannot_be_replaced_by_child_identity(reference, snapshot, task_id):
    snapshot["taskId"] = task_id
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "refused", "reason": "task-mismatch"}
    assert result.reference == reference


@pytest.mark.parametrize(
    "remote", [None, {"environmentId": "example-remote", "threadId": "example-thread", "label": "Example"}]
)
def test_terminal_candidate_requires_original_correlated_identity(reference, snapshot, remote):
    if remote is not None:
        reference = replace(reference, remote_environment_id="example-remote", remote_thread_id="example-thread")
    snapshot["remoteChild"] = remote
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "terminal-candidate", "reason": "terminal-evidence-complete"}
    assert result.reference == reference
    assert result.status == "completed"
    assert result.terminal_run_id == "example-terminal-run"
    assert result.result_transfer_id == "example-transfer"
    assert result.containment == "quiescent"
    assert classify(reference, snapshot) == result


@pytest.mark.parametrize("change", ["environmentId", "threadId", "absent", "unexpected"])
def test_remote_pair_cannot_be_adopted_or_replaced(reference, snapshot, change):
    remote = {"environmentId": "example-remote", "threadId": "example-thread", "label": "Example"}
    if change != "unexpected":
        reference = replace(reference, remote_environment_id="example-remote", remote_thread_id="example-thread")
    if change in {"environmentId", "threadId"}:
        remote[change] = "example-other"
    snapshot["remoteChild"] = None if change == "absent" else remote
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "refused", "reason": "remote-mismatch"}
    assert result.reference == reference


@pytest.mark.parametrize(
    "mismatch,changes",
    [
        ("task", {"hasPendingChildRuns": 0}),
        ("task", {"remoteChild": False}),
        ("remote", {"hasPendingChildRuns": 0}),
        ("remote", {"status": True}),
    ],
)
def test_known_identity_refusal_precedes_malformed_evidence(reference, snapshot, mismatch, changes):
    if mismatch == "task":
        snapshot["taskId"] = "example-other-task"
    else:
        snapshot["remoteChild"] = {
            "environmentId": "example-other-remote",
            "threadId": "example-other-thread",
            "label": "Example",
        }
    snapshot.update(changes)
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "refused", "reason": f"{mismatch}-mismatch"}
    assert result.reference == reference
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize("change", ["environmentId", "threadId", "unexpected"])
@pytest.mark.parametrize("label_fields", [{}, {"label": 0}], ids=["missing-label", "non-string-label"])
def test_remote_identity_refusal_precedes_malformed_label(reference, snapshot, change, label_fields):
    remote = {"environmentId": "example-remote", "threadId": "example-thread", **label_fields}
    if change != "unexpected":
        reference = replace(reference, remote_environment_id="example-remote", remote_thread_id="example-thread")
        remote[change] = "example-other"
    snapshot["remoteChild"] = remote
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "refused", "reason": "remote-mismatch"}
    assert result.reference == reference
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize("status", ["queued", "running"])
def test_queued_and_running_work_is_active(reference, snapshot, status):
    snapshot.update(status=status, workState="working", latestTerminalStatus=None, latestTerminalRunId=None)
    result = classify(reference, snapshot, containment="active")
    assert observation_diagnostic(result) == {"family": "active", "reason": "native-active"}
    assert result.status == status
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"hasPendingChildRuns": True},
        {"hasPendingQuestion": True},
        {"pendingApproval": {"kind": "permission", "requestId": "example-approval", "runId": "example-run"}},
        {"waitTimedOut": True},
        {"workState": "waiting_for_children"},
        {"status": "waiting", "workState": "working"},
    ],
)
def test_pending_work_and_waits_are_awaiting(reference, snapshot, changes):
    snapshot.update(changes)
    result = classify(reference, snapshot)
    assert result.family == "awaiting"
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("containment", ["active", "unknown", "quiescent"])
def test_terminal_turn_needs_quiescent_containment(reference, snapshot, status, containment):
    snapshot.update(status=status, latestTerminalStatus=status)
    result = classify(reference, snapshot, containment=containment)
    assert result.family == ("terminal-candidate" if containment == "quiescent" else "awaiting")
    snapshot["hasPendingChildRuns"] = True
    assert classify(reference, snapshot, containment=containment).family == "awaiting"


def test_false_child_flag_and_cancel_request_do_not_prove_containment(reference, snapshot):
    snapshot["cancellationRequested"] = True
    result = classify_native_snapshot(reference, snapshot, observed_origin_environment_id="example-origin")
    assert result.family == "awaiting"
    assert result.containment == "unknown"


@pytest.mark.parametrize(
    "field",
    [
        "taskId",
        "status",
        "workState",
        "hasPendingChildRuns",
        "hasPendingQuestion",
        "pendingApproval",
        "waitTimedOut",
        "latestTerminalStatus",
        "latestTerminalRunId",
    ],
)
def test_each_required_terminal_fact_must_be_present(reference, snapshot, field):
    del snapshot[field]
    assert classify(reference, snapshot).family == "awaiting"


@pytest.mark.parametrize(
    "field,value",
    [
        ("taskId", True),
        ("taskId", "bad\nidentity"),
        ("taskId", "x" * 513),
        ("status", 1),
        ("status", "canceled"),
        ("workState", True),
        ("workState", "unknown"),
        ("hasPendingChildRuns", 0),
        ("hasPendingChildRuns", None),
        ("hasPendingQuestion", "false"),
        ("pendingApproval", False),
        ("pendingApproval", {}),
        ("pendingApproval", {"kind": "unknown", "requestId": "example-request", "runId": "example-run"}),
        ("pendingApproval", {"kind": "command", "requestId": 1, "runId": "example-run"}),
        ("pendingApproval", {"kind": "command", "requestId": "example-request", "runId": []}),
        ("waitTimedOut", 0),
        ("remoteChild", False),
        ("remoteChild", {}),
        ("remoteChild", {"environmentId": True, "threadId": "example-thread", "label": "Example"}),
        ("remoteChild", {"environmentId": "example-remote", "threadId": [], "label": "Example"}),
        ("latestTerminalStatus", "running"),
        ("latestTerminalRunId", []),
        ("latestTerminalResultContextTransferId", ""),
        ("resultContextTransferId", "x" * 513),
    ],
)
def test_malformed_recognized_fields_are_bounded_awaiting(reference, snapshot, field, value):
    snapshot[field] = value
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "awaiting", "reason": "malformed-snapshot"}
    assert result.reference == reference


@pytest.mark.parametrize("label_fields", [{}, {"label": 0}], ids=["missing-label", "non-string-label"])
def test_matching_remote_pair_with_malformed_label_is_awaiting(reference, snapshot, label_fields):
    reference = replace(reference, remote_environment_id="example-remote", remote_thread_id="example-thread")
    snapshot["remoteChild"] = {"environmentId": "example-remote", "threadId": "example-thread", **label_fields}
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "awaiting", "reason": "malformed-snapshot"}
    assert result.reference == reference
    assert result.terminal_run_id is None
    assert result.result_transfer_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"latestTerminalStatus": None},
        {"latestTerminalStatus": "failed"},
        {"latestTerminalRunId": None},
        {"workState": "working"},
        {"resultContextTransferId": "example-different-transfer"},
        {"latestTerminalResultContextTransferId": None, "resultContextTransferId": None},
    ],
)
def test_contradictory_or_missing_terminal_correlation_is_awaiting(reference, snapshot, changes):
    snapshot.update(changes)
    result = classify(reference, snapshot)
    assert result.family == "awaiting"
    assert result.result_transfer_id is None


@pytest.mark.parametrize("field", ["latestTerminalResultContextTransferId", "resultContextTransferId"])
@pytest.mark.parametrize("remove", [True, False])
def test_one_nonnull_transfer_reference_suffices(reference, snapshot, field, remove):
    if remove:
        del snapshot[field]
    else:
        snapshot[field] = None
    result = classify(reference, snapshot)
    assert result.family == "terminal-candidate"
    assert result.result_transfer_id == "example-transfer"


def test_unknown_payload_is_neither_authority_nor_retained(reference, snapshot):
    snapshot.update(
        error="example-private-error", summary="example-private-summary", consumableResultId="example-result"
    )
    result = classify(reference, snapshot)
    assert observation_diagnostic(result) == {"family": "terminal-candidate", "reason": "terminal-evidence-complete"}
    assert "private" not in repr(result)
    del snapshot["latestTerminalRunId"]
    snapshot["childRunId"] = "example-terminal-run"
    assert classify(reference, snapshot).family == "awaiting"


@pytest.mark.parametrize("snapshot", [False, [], "example-error", {"structuredContent": {"taskId": "example-task"}}])
def test_nonstatus_input_cannot_supply_authority(reference, snapshot):
    assert classify(reference, snapshot).family == "awaiting"


@pytest.mark.parametrize(
    "field",
    ["origin_environment_id", "native_client_request_key", "task_id", "remote_environment_id", "remote_thread_id"],
)
@pytest.mark.parametrize("value", ["", True, 1, "x" * 513, "bad\nreference", "bad\x7freference", "bad\u0085reference"])
def test_reference_rejects_unsafe_identity_without_echo(reference, field, value):
    reference = replace(reference, remote_environment_id="example-remote", remote_thread_id="example-thread")
    with pytest.raises(ValueError, match="^invalid native task reference$"):
        replace(reference, **{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"remote_environment_id": "example-remote"},
        {"remote_thread_id": "example-thread"},
        {"task_id": None, "remote_environment_id": "example-remote", "remote_thread_id": "example-thread"},
    ],
)
def test_remote_identity_requires_complete_pair_and_known_task(reference, changes):
    with pytest.raises(ValueError, match="^invalid native task reference$"):
        replace(reference, **changes)


def test_references_are_not_truncated_and_observations_are_frozen(reference, snapshot):
    expected = replace(reference, task_id="x" * 512)
    snapshot["taskId"] = "x" * 512
    result = classify(expected, snapshot)
    assert result.family == "terminal-candidate"
    assert result.reference.task_id == "x" * 512
    with pytest.raises(FrozenInstanceError):
        result.reference.task_id = "example-other"
    with pytest.raises(FrozenInstanceError):
        result.family = "active"


def test_diagnostic_cannot_echo_caller_chosen_tokens(reference, snapshot):
    result = classify(reference, snapshot)
    assert isinstance(result, NativeObservation)
    with pytest.raises(ValueError, match="^invalid native observation$"):
        replace(result, reason="example-private-error")
    with pytest.raises(ValueError, match="^invalid native observation$"):
        replace(result, family="example-private-family")
    with pytest.raises(ValueError, match="^invalid containment$"):
        classify(reference, snapshot, containment="example-private-containment")
