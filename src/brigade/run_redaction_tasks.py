"""Relationship-preserving task payload preparation for journal redaction."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Sequence

from brigade import run_checkpoint, run_events, run_journal, run_task_journal, run_tasks as tasks

# Closed classifications describe state, not private correlation references.
STRUCTURAL_FIELDS = frozenset({"status", "containment", "artifact_kind", "dispatch_attempt"})
CONTROL_STRUCTURAL_FIELDS = frozenset({"op", "error_class"})
_DERIVED_FIELDS = frozenset({"spec_digest", "attempt_id", "result_id"})


def private_scalars(event: run_journal.RunEvent) -> list[tuple[str, str]]:
    """Enumerate only the closed aggregate exception, never arbitrary nesting."""
    if event.event_type == "task.batch-accepted":
        return [
            ("request_id", event.payload["request_id"]),
            ("membership_digest", event.payload["membership_digest"]),
            *((field, value) for row in event.payload["members"] for field, value in row.items()),
        ]
    return [(field, value) for field, value in event.payload.items() if isinstance(value, str)]


def _rows(event: run_journal.RunEvent) -> list[dict[str, Any]]:
    return event.payload["members"] if event.event_type == "task.batch-accepted" else [event.payload]


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
    selected.update(
        event.sequence
        for event in events
        if event.event_type == "task.accepted" and event.payload["task_id"] in selected_task_ids
    )
    reserved = {value for event in events for _, value in private_scalars(event)}
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
        if event.event_type not in run_events._TASK_EVENT_TYPES:
            continue
        for row in _rows(event):
            task_id = row["task_id"]
            if event.sequence not in selected and not (
                event.event_type == "task.batch-accepted" and task_id in selected_task_ids
            ):
                continue
            values = selected_values.setdefault(task_id, set())
            values.update(
                value for field, value in row.items() if isinstance(value, str) and field not in STRUCTURAL_FIELDS
            )
            if event.event_type == "task.batch-accepted" and event.sequence in selected:
                values.update((event.payload["request_id"], event.payload["membership_digest"]))
            for field, value in row.items():
                if isinstance(value, str) and field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS:
                    substitute(task_id, field, value, event.sequence)

    # Control-only selection must seed aliases before task/provider rebuilding.
    for event in events:
        if event.sequence in selected_controls:
            selected_values.setdefault(event.payload["task_id"], set()).update(
                value
                for field, value in event.payload.items()
                if value is not None and field not in CONTROL_STRUCTURAL_FIELDS
            )

    sensitive = set().union(*selected_values.values()) if selected_values else set()
    # Equal-value aliases in retained descriptors and provider fields participate
    # in the same selection closure. The outer token has its own namespace.
    for event in events:
        if event.event_type not in run_events._TASK_EVENT_TYPES:
            continue
        for row in _rows(event):
            task_id = row["task_id"]
            for field, value in row.items():
                if isinstance(value, str) and field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS and value in sensitive:
                    selected_values.setdefault(task_id, set()).add(value)
                    substitute(task_id, field, value, event.sequence)
    outer_requests: dict[int, str] = {}
    for event in events:
        if event.event_type != "task.batch-accepted":
            continue
        old_outer = event.payload["request_id"]
        if event.sequence in selected or old_outer in sensitive:
            namespace = f"batch:{event.sequence}"
            substitute(namespace, "request_id", old_outer, event.sequence)
            outer_requests[event.sequence] = substitutions[(namespace, "request_id", old_outer)]

    # First build every derived mapping, then rebuild nonstructural aliases.
    # Provider and descriptor aliases can alter result or sibling identities.
    # Repeat until their derived mappings stabilize. The journal bound also
    # bounds this closure; a cycle refuses rather than publishing partial state.
    derived: dict[tuple[str, str], str] = {}
    aliases: dict[str, str] = {}

    def remember(task_id: str, old: str, new: str) -> None:
        derived[(task_id, old)] = new
        if new != old:
            aliases[old] = new

    def mapped(task_id: str, field: str, value: Any) -> Any:
        replacement = aliases.get(value, substitutions.get((task_id, field, value), value))
        # One private string can name a token and a digest. Derived token
        # aliases must still satisfy the destination's closed scalar type.
        if field.endswith("digest") and replacement != value and not run_task_journal.digest(replacement):
            substitute(task_id, field, value, descriptors[task_id])
            return substitutions[(task_id, field, value)]
        return replacement

    descriptors = {
        row["task_id"]: event.sequence
        for event in events
        if event.event_type in {"task.accepted", "task.batch-accepted"}
        for row in _rows(event)
    }
    for _ in range(len(events) + len(sensitive) + 2):
        prior_derived = dict(derived)
        states: dict[str, tasks.TaskResult] = {}
        attempts: dict[tuple[str, str], str] = {}
        for index, event in enumerate(events):
            if event.event_type not in run_events._TASK_EVENT_TYPES:
                continue
            if event.event_type == "task.batch-accepted":
                old = event.payload
                rows = []
                outer = outer_requests.get(event.sequence, aliases.get(old["request_id"], old["request_id"]))
                for row in _rows(event):
                    task_id = row["task_id"]
                    rewritten_row = {field: mapped(task_id, field, value) for field, value in row.items()}
                    rows.append(rewritten_row)
                    spec = tasks.TaskSpec(**rewritten_row)
                    child = run_task_journal.batch_child_request_id(
                        run_id=event.run_id, request_id=outer, task_id=spec.task_id
                    )
                    old_spec = tasks.TaskSpec(**row)
                    remember(task_id, task_id, spec.task_id)
                    old_child = run_task_journal.batch_child_request_id(
                        run_id=event.run_id, request_id=old["request_id"], task_id=task_id
                    )
                    remember(task_id, old_spec.spec_digest, spec.spec_digest)
                    remember(task_id, old_child, child)
                    states[task_id] = tasks.TaskResult(tasks.TaskHandle(event.run_id, child, spec), "queued")
                payload = {
                    **old,
                    "request_id": outer,
                    "members": rows,
                    "membership_digest": run_task_journal.batch_membership_digest(
                        descriptor_version=old["descriptor_version"], members=rows
                    ),
                }
                for row in _rows(event):
                    task_id = row["task_id"]
                    if payload != old:
                        selected_values.setdefault(task_id, set())
                    remember(task_id, old["request_id"], outer)
                    remember(task_id, old["membership_digest"], payload["membership_digest"])
                    remember(
                        task_id,
                        run_task_journal.batch_reference(
                            run_id=event.run_id,
                            request_id=old["request_id"],
                            membership_digest=old["membership_digest"],
                        ),
                        run_task_journal.batch_reference(
                            run_id=event.run_id, request_id=outer, membership_digest=payload["membership_digest"]
                        ),
                    )
                if payload != old:
                    requests[index] = (payload, f"redaction:{operation_id}:{event.sequence}")
                continue
            old = event.payload
            task_id = old["task_id"]
            payload = {
                field: mapped(task_id, field, value) if field not in STRUCTURAL_FIELDS | _DERIVED_FIELDS else value
                for field, value in old.items()
            }
            if event.event_type == "task.accepted":
                spec = tasks.TaskSpec(payload["task_id"], payload["seat"], payload["payload_digest"])
                payload["spec_digest"] = spec.spec_digest
                remember(task_id, task_id, spec.task_id)
                remember(task_id, old["request_id"], payload["request_id"])
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
                    remember(task_id, old[field], payload[field])
            if payload != old:
                selected_values.setdefault(task_id, set())
                requests[index] = (payload, f"redaction:{operation_id}:{event.sequence}")
        # A selected alias must not reappear as a newly computed identity.
        # Reseed the descriptor, retaining each rejected candidate in reserved.
        collisions = {task_id for (task_id, old), new in derived.items() if new in sensitive}
        if collisions:
            for task_id in sorted(collisions):
                substitutions.pop((task_id, "task_id", task_id), None)
                substitute(task_id, "task_id", task_id, descriptors[task_id])
            derived.clear()
            aliases.clear()
        elif derived == prior_derived:
            break
    else:
        raise error_type("task redaction alias closure did not converge")

    affected_task_ids = set(selected_values)
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
    accepted = {
        row["task_id"]
        for event in events
        if event.event_type in {"task.accepted", "task.batch-accepted"}
        for row in _rows(event)
    }
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
    # Controls are status-neutral facts, but their selectors participate in the
    # same task identity graph. Rewrite both halves even across checkpoints.
    control_requests: dict[str, str] = {}
    for event in events:
        if event.event_type in control_types and event.payload.get("task_id") in selected_values:
            old = event.payload
            task_id = old["task_id"]
            if event.event_type == "control.requested":
                substitute(task_id, "control_request_id", old["request_id"], event.sequence)
                control_requests[old["request_id"]] = substitutions[(task_id, "control_request_id", old["request_id"])]
            if event.sequence in selected_controls:
                for field, value in old.items():
                    if isinstance(value, str) and field not in CONTROL_STRUCTURAL_FIELDS | {"attempt_id", "request_id"}:
                        substitute(task_id, field, value, event.sequence)
    for index, event in enumerate(events):
        if event.event_type not in control_types:
            continue
        old = event.payload
        task_id = old.get("task_id")
        if task_id not in selected_values:
            continue
        payload = dict(old)
        for field, value in old.items():
            if field not in CONTROL_STRUCTURAL_FIELDS | {"attempt_id", "request_id"}:
                if value in sensitive:
                    substitute(task_id, field, value, event.sequence)
                payload[field] = mapped(task_id, field, value)
        if old.get("attempt_id") is not None:
            payload["attempt_id"] = attempts[(task_id, old["attempt_id"])]
        payload["request_id"] = control_requests[old["request_id"]]
        requests[index] = (payload, f"{event.event_type}:{payload['request_id']}")
