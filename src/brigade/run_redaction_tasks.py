"""Relationship-preserving task payload preparation for journal redaction."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Sequence

from brigade import run_events, run_journal, run_tasks as tasks

# Closed classifications describe state, not private correlation references.
STRUCTURAL_FIELDS = frozenset({"status", "containment", "artifact_kind"})
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
) -> None:
    """Scrub selected references throughout each affected task's relationships.

    Any selected task fact changes its task identity. This also changes the
    spec, attempt and terminal result identities, including references in
    unselected facts. Private fields selected on one observation are replaced
    on earlier/later observations of the same task so identity remains immutable.
    Substitutes depend only on public operation/sequence/field coordinates,
    never on private values. Unrelated tasks and other unselected fields stay.
    """
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

    states: dict[str, tasks.TaskResult] = {}
    for index, event in enumerate(events):
        if event.event_type not in run_events._TASK_EVENT_TYPES or event.payload["task_id"] not in selected_values:
            continue
        old = event.payload
        task_id = old["task_id"]
        payload = {field: substitutions.get((task_id, field, value), value) for field, value in old.items()}
        if event.event_type == "task.accepted":
            spec = tasks.TaskSpec(payload["task_id"], payload["seat"], payload["payload_digest"])
            payload["spec_digest"] = spec.spec_digest
            states[task_id] = tasks.TaskResult(tasks.TaskHandle(event.run_id, payload["request_id"], spec), "queued")
        elif event.event_type == "task.attempt-prepared":
            current = states[task_id]
            payload["attempt_id"] = tasks._attempt_id(current.handle)
            states[task_id] = replace(current, handle=replace(current.handle, attempt_id=payload["attempt_id"]))
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
        requests[index] = (payload, f"redaction:{operation_id}:{event.sequence}")
