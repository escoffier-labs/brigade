"""Relationship-preserving task payload preparation for journal redaction."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Sequence

from brigade import run_checkpoint, run_events, run_journal, run_tasks as tasks

# Closed classifications describe state, not private correlation references.
STRUCTURAL_FIELDS = frozenset({"status", "containment", "artifact_kind", "dispatch_attempt"})
CONTROL_STRUCTURAL_FIELDS = frozenset({"op", "error_class"})
_DERIVED_FIELDS = frozenset({"spec_digest", "attempt_id", "result_id"})


def validate_history(events: Sequence[run_journal.RunEvent], *, error_type: type[RuntimeError]) -> None:
    """Refuse corrupt task histories before redaction can normalize them."""
    if not events:
        return
    try:
        tasks._fold(events[0].run_id, list(events))
    except tasks.TaskJournalError as exc:
        raise error_type("task history verification failed") from exc


def prepare_requests(
    events: Sequence[run_journal.RunEvent],
    requests: list[tuple[dict[str, Any], str]],
    *,
    selected: set[int],
    operation_id: str,
    error_type: type[RuntimeError],
) -> None:
    """Scrub selected references throughout each affected task's relationships.

    Any selected task fact changes its task identity. This also changes the
    spec, attempt and terminal result identities, including references in
    unselected facts. Private fields selected on one observation are replaced
    on earlier/later observations of the same task so identity remains immutable.
    Substitutes depend only on public operation/sequence/field coordinates,
    never on private values. Unrelated tasks and other unselected fields stay.
    """
    control_types = {"control.requested", "control.observed", "control.failed"}
    # Selected control facts are recognizable from the generic preparation's
    # changed key, even though the caller's selected set contains task facts.
    selected_controls = {
        event.sequence
        for index, event in enumerate(events)
        if event.event_type in control_types
        and "task_id" in event.payload
        and requests[index][1] != event.idempotency_key
    }
    selected = set(selected)
    selected_task_ids = {event.payload["task_id"] for event in events if event.sequence in selected_controls}
    affected_task_ids = selected_task_ids | {
        event.payload["task_id"]
        for event in events
        if event.event_type in run_events._TASK_EVENT_TYPES and event.sequence in selected
    }
    related_controls = [
        event
        for event in events
        if event.event_type in control_types and event.payload.get("task_id") in affected_task_ids
    ]
    # Check complete request pairs only when their task identity will change.
    # Unrelated legacy controls may already have generic redaction identities.
    try:
        run_checkpoint._verify_control_suffix(list(events), related_controls)
    except run_checkpoint.CheckpointError as exc:
        raise error_type("control history verification failed") from exc
    accepted = {event.payload["task_id"] for event in events if event.event_type == "task.accepted"}
    known_attempts = {
        (event.payload["task_id"], event.payload["attempt_id"])
        for event in events
        if event.event_type == "task.attempt-prepared"
    }
    for event in related_controls:
        task_id = event.payload["task_id"]
        if task_id not in accepted or (
            "attempt_id" in event.payload and (task_id, event.payload["attempt_id"]) not in known_attempts
        ):
            raise error_type("control history verification failed: unmatched durable task or attempt")
    selected.update(
        event.sequence
        for event in events
        if event.event_type == "task.accepted" and event.payload["task_id"] in selected_task_ids
    )
    reserved = {value for e in events for value in e.payload.values() if isinstance(value, str)}
    substitutions: dict[tuple[str, str, str], str] = {}
    selected_values: dict[str, set[str]] = {}

    def substitute(task_id: str, field: str, value: str, sequence: int) -> None:
        key = (task_id, field, value)
        if key in substitutions:
            return
        counter = 0
        while True:
            token = f"redacted-{operation_id}-{sequence}-{field}-{counter}"
            replacement = hashlib.sha256(token.encode("ascii")).hexdigest() if field.endswith("digest") else token
            if replacement not in reserved:
                break
            counter += 1
        reserved.add(replacement)
        substitutions[key] = replacement

    for event in events:
        if event.event_type not in run_events._TASK_EVENT_TYPES or event.sequence not in selected:
            continue
        task_id = event.payload["task_id"]
        values = selected_values.setdefault(task_id, set())
        values.update(
            value for field, value in event.payload.items() if value is not None and field not in STRUCTURAL_FIELDS
        )
        for field, value in event.payload.items():
            if value is not None and field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS:
                substitute(task_id, field, value, event.sequence)

    # Control-only selection must seed aliases before task/provider rebuilding.
    for event in events:
        if event.sequence in selected_controls:
            selected_values.setdefault(event.payload["task_id"], set()).update(
                value
                for field, value in event.payload.items()
                if value is not None and field not in CONTROL_STRUCTURAL_FIELDS
            )

    # A private reference can be repeated under another field name (for
    # example the logical task ID can also be the provider task ID). Scrub
    # those aliases throughout the task, including unselected observations.
    for event in events:
        if event.event_type not in run_events._TASK_EVENT_TYPES:
            continue
        task_id = event.payload["task_id"]
        for field, value in event.payload.items():
            if field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS and value in selected_values.get(task_id, set()):
                substitute(task_id, field, value, event.sequence)

    # First build every derived mapping, then rebuild nonstructural aliases.
    # Provider aliases can alter result identity, so fold again rather than
    # patching already-derived payloads. Controls still run after both folds.
    derived: dict[tuple[str, str], str] = {}
    for _ in range(2):
        states: dict[str, tasks.TaskResult] = {}
        attempts: dict[tuple[str, str], str] = {}
        for index, event in enumerate(events):
            if event.event_type not in run_events._TASK_EVENT_TYPES or event.payload["task_id"] not in selected_values:
                continue
            old = event.payload
            task_id = old["task_id"]
            payload = {
                field: derived.get((task_id, value), substitutions.get((task_id, field, value), value))
                if field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS
                else value
                for field, value in old.items()
            }
            if event.event_type == "task.accepted":
                spec = tasks.TaskSpec(payload["task_id"], payload["seat"], payload["payload_digest"])
                payload["spec_digest"] = spec.spec_digest
                states[task_id] = tasks.TaskResult(
                    tasks.TaskHandle(event.run_id, payload["request_id"], spec), "queued"
                )
            elif event.event_type == "task.attempt-prepared":
                current = states[task_id]
                payload["attempt_id"] = tasks._attempt_id(current.handle)
                attempts[(task_id, old["attempt_id"])] = payload["attempt_id"]
                states[task_id] = replace(current, handle=replace(current.handle, attempt_id=payload["attempt_id"]))
            elif event.event_type == "task.launch-claimed":
                current = states[task_id]
                payload["attempt_id"] = attempts[(task_id, old["attempt_id"])]
                states[task_id] = replace(
                    current,
                    launch_claim=tasks.TaskLaunchBinding(payload["seat"], payload["dispatch_attempt"]),
                    status="unknown",
                )
            elif event.event_type == "task.observed":
                current = states[task_id]
                observation = tasks.TaskObservation(
                    **{
                        field: value
                        for field, value in payload.items()
                        if field not in {"task_id", "attempt_id", "result_id"}
                    }
                )
                states[task_id] = tasks._observe(current, observation)
                payload = tasks._observation_payload(states[task_id])
            else:
                current = states[task_id]
                payload["result_id"] = current.result_id
                states[task_id] = replace(current, acknowledged=True)
            for field in _DERIVED_FIELDS:
                if old.get(field) is not None:
                    derived[(task_id, old[field])] = payload[field]
            requests[index] = (payload, f"redaction:{operation_id}:{event.sequence}")

    # Controls are status-neutral facts, but their selectors participate in the
    # same task identity graph. Rewrite both halves even across checkpoints.
    private_control_fields: dict[tuple[str, str], Any] = {}
    for index, event in enumerate(events):
        if event.sequence in selected_controls:
            for field, value in requests[index][0].items():
                if field not in CONTROL_STRUCTURAL_FIELDS | {"task_id", "attempt_id", "request_id"}:
                    private_control_fields[(event.payload["request_id"], field)] = (
                        hashlib.sha256(f"redacted-{operation_id}-{event.sequence}-{field}".encode("ascii")).hexdigest()
                        if field.endswith("digest")
                        else value
                    )
    control_requests: dict[str, str] = {}
    for index, event in enumerate(events):
        if event.event_type not in control_types:
            continue
        old = event.payload
        task_id = old.get("task_id")
        if task_id not in selected_values:
            continue
        payload = dict(old)
        for field in old:
            if (old["request_id"], field) in private_control_fields:
                payload[field] = private_control_fields[(old["request_id"], field)]
        for field, value in old.items():
            if field not in CONTROL_STRUCTURAL_FIELDS | {"attempt_id", "request_id"}:
                if value in selected_values[task_id]:
                    substitute(task_id, field, value, event.sequence)
                payload[field] = derived.get(
                    (task_id, value), substitutions.get((task_id, field, value), payload[field])
                )
        if old.get("attempt_id") is not None:
            payload["attempt_id"] = attempts[(task_id, old["attempt_id"])]
        request_id = old["request_id"]
        if event.event_type == "control.requested":
            control_requests[request_id] = f"redacted-{operation_id}-{event.sequence}-control"
        payload["request_id"] = control_requests[request_id]
        requests[index] = (payload, f"{event.event_type}:{payload['request_id']}")
