"""Normalize one public Brigade task into a Worklore import observation."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .worklore_validate import (
    ACCEPTANCE_ITEM_MAX,
    ACCEPTANCE_MAX_ITEMS,
    TITLE_MAX,
    WorkloreValidationError,
    as_datetime,
    assert_no_private_data,
    contains_private_path,
)

PRIORITY_MAX = 64
EXTERNAL_STATE_MAX = 64
DEPENDENCY_FIELD_MAX = 128
SOURCE_REVISION_MAX = 64

BLOCKED_KEYS = frozenset(
    {
        "receipt_body",
        "prompt",
        "transcript",
        "token",
        "secret",
        "password",
        "tasks_path",
    }
)
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,127}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEP_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TERMINAL_SUCCESS = "done"
_TERMINAL_EXCLUDED = frozenset({"cancelled", "dismissed"})


class BrigadeObservationError(Exception):
    """A Brigade task cannot be turned into a Worklore observation."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _unknown_field(name: object) -> BrigadeObservationError:
    return BrigadeObservationError(f"unknown field: {name}", code="unknown-field")


def _field_bound(message: str) -> BrigadeObservationError:
    return BrigadeObservationError(message, code="field-bound")


def _reject_blocked_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in BLOCKED_KEYS:
                raise _unknown_field(key)
            _reject_blocked_keys(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_blocked_keys(item)


def _reject_private_data(value: object, field: str) -> str:
    """Apply the hub's canonical private-data rules client side, in the adapter's error type."""
    if not isinstance(value, str):
        raise _field_bound(f"{field} must be a string")
    try:
        return assert_no_private_data(value, field)
    except WorkloreValidationError as exc:
        raise BrigadeObservationError(str(exc), code=exc.code) from None


def _require_identity(value: object) -> str:
    if not isinstance(value, str) or not IDENTITY_RE.fullmatch(value):
        raise _field_bound("identity must match the configured identity pattern")
    return _reject_private_data(value, "identity")


def _require_task_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise _field_bound(f"{field} must be a bounded Brigade task id")
    return value


def _optional_priority(value: object) -> str | None:
    if value is None:
        return None
    text = _reject_private_data(value, "priority")
    if not text or len(text) > PRIORITY_MAX:
        raise _field_bound(f"priority must be 1 to {PRIORITY_MAX} characters")
    return text


def _optional_source_revision(value: object) -> str | None:
    """The task's ``updated_at`` as the source revision the hub's rollback guard compares.

    Brigade tasks carry ``updated_at`` in their public payload, so this adapter has a real
    revision to report and must report it: without one the hub has no high-water for a
    Brigade identity and an expired-key replay is last-write-wins. Only a bounded value the
    hub can parse as an instant is emitted, because a revision the guard cannot compare is
    worse than none: it looks like proof and provides none.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > SOURCE_REVISION_MAX:
        return None
    try:
        as_datetime(text)
    except ValueError:
        return None
    return text


def _acceptance_items(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _field_bound("acceptance must be an array of strings")
    if len(value) > ACCEPTANCE_MAX_ITEMS:
        raise _field_bound(f"acceptance must have at most {ACCEPTANCE_MAX_ITEMS} items")
    items: list[str] = []
    for index, item in enumerate(value):
        text = _reject_private_data(item, f"acceptance[{index}]")
        if not text.strip() or len(text) > ACCEPTANCE_ITEM_MAX:
            raise _field_bound(f"acceptance[{index}] must be a non-empty string")
        items.append(text)
    return items


def _safe_dependency_part(value: object, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return None
    if contains_private_path(value):
        return None
    return value


def _dependencies(edges: object, *, task_id: str) -> list[dict[str, str]]:
    if edges is None:
        return []
    if not isinstance(edges, list):
        raise _field_bound("edges must be an array")
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        if edge.get("source") != task_id:
            continue
        dep_type = _safe_dependency_part(edge.get("type"), DEP_TYPE_RE)
        dep_id = _safe_dependency_part(edge.get("target"), TASK_ID_RE)
        if dep_type is None or dep_id is None:
            continue
        if len(dep_type) > DEPENDENCY_FIELD_MAX or len(dep_id) > DEPENDENCY_FIELD_MAX:
            continue
        key = (dep_type, dep_id)
        if key in seen:
            continue
        seen.add(key)
        items.append({"type": dep_type, "id": dep_id})
    return sorted(items, key=lambda item: (item["type"], item["id"]))


def _evidence_refs(metadata: object) -> list[str]:
    if not isinstance(metadata, Mapping):
        return []
    refs: list[str] = []
    for key in ("verify_run_id", "receipt_id"):
        value = metadata.get(key)
        if isinstance(value, str) and EVIDENCE_REF_RE.fullmatch(value):
            refs.append(value)
    return refs


def _source_policy(status: str) -> tuple[str, str | None]:
    if status == _TERMINAL_SUCCESS:
        return "completed", "completed"
    if status in _TERMINAL_EXCLUDED:
        return "completed", None
    return "eligible", None


def normalize_task(
    task: Mapping[str, Any],
    *,
    identity: str,
    edges: list[Mapping[str, Any]] | list[object] | None,
) -> dict[str, Any]:
    """Map one public Brigade task payload to a Worklore import observation."""
    if not isinstance(task, Mapping):
        raise _field_bound("task must be an object")
    _reject_blocked_keys(task)
    _reject_blocked_keys(edges)
    resolved_identity = _require_identity(identity)
    task_id = _require_task_id(task.get("id"), "id")
    title = _reject_private_data(task.get("text"), "title").strip()[:TITLE_MAX]
    if not title:
        raise _field_bound("title is required")
    status_raw = task.get("status")
    if not isinstance(status_raw, str) or not status_raw.strip():
        raise _field_bound("status is required")
    status = _reject_private_data(status_raw.strip(), "status")
    if len(status) > EXTERNAL_STATE_MAX:
        raise _field_bound(f"external_state must be 1 to {EXTERNAL_STATE_MAX} characters")
    source_policy, proposed_status = _source_policy(status)
    observation: dict[str, Any] = {
        "external_key": f"{resolved_identity}#{task_id}",
        "link_type": "brigade",
        "title": title,
        "acceptance": _acceptance_items(task.get("acceptance")),
        "external_state": status,
        "dependencies": _dependencies(edges, task_id=task_id),
        "evidence_refs": _evidence_refs(task.get("metadata")),
        "source_policy": source_policy,
        "proposed_status": proposed_status,
    }
    priority = _optional_priority(task.get("priority"))
    if priority is not None:
        observation["priority"] = priority
    revision = _optional_source_revision(task.get("updated_at"))
    # Same decision as the GitHub adapter: a task with no usable ``updated_at`` is passed
    # through without a revision rather than rejected here or given a synthesized one. The
    # hub refuses it per identity with a named reason, which an operator can see; an
    # adapter-side skip would report nothing at all.
    if revision is not None:
        observation["external_updated_at"] = revision
    return observation
