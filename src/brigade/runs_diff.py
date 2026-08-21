"""Read-only durable child-vs-parent (or sibling) run diff (#1071).

Compares stored artifacts without mutating either side. Every string that
enters the versioned JSON contract passes through ``runs_cmd._clean_str``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from . import graphtrail_delta, runs_cmd

RUN_DIFF_SCHEMA = "brigade.run-diff.v1"

RUN_DIFF_KEYS = frozenset(
    {
        "schema",
        "relation",
        "left",
        "right",
        "parent_run_id",
        "branch_point_event_id",
        "lifecycle",
        "workers",
        "verification",
        "outcome",
        "graphtrail",
    }
)
_SIDE_FIELDS = frozenset({"run_id", "role", "status", "task"})
_SECTION_FIELDS = frozenset({"changed", "changes", "left", "right"})
_CHANGE_FIELDS = frozenset({"field", "left", "right"})
_LIFECYCLE_FIELDS = frozenset({"status", "mode", "failure", "resume_available"})
_WORKER_FIELDS = frozenset({"worker", "ok", "status", "exit_code", "timed_out", "failure"})
_VERIFY_VIEW_FIELDS = frozenset({"run_id", "status", "command", "exit_code", "digest"})
_VERIFY_CONTENT_FIELDS = frozenset({"status", "command", "exit_code", "digest"})
_OUTCOME_FIELDS = frozenset(
    {
        "suspected_noop",
        "error",
        "synthesis_ok",
        "synthesis_detail",
        "final_sha256",
        "final_bytes",
    }
)
_GRAPHTRAIL_FIELDS = frozenset({"status", "reason", "changed", "left", "right"})
_GRAPHTRAIL_SIDE_FIELDS = frozenset(
    {
        "status",
        "changed_symbol_count",
        "edge_churn",
        "before_snapshot_sha256",
        "after_snapshot_sha256",
    }
)

_RELATION_CHILD_PARENT = "child-parent"
_RELATION_SIBLINGS = "siblings"
_GRAPHTRAIL_COMPARED = "compared"
_GRAPHTRAIL_SKIPPED = "skipped"
_SKIP_ABSENT = "absent snapshots"
_SKIP_INCOMPATIBLE = "incompatible snapshots"
_UNREADABLE = object()


def run_diff_contract(
    run: str | Path,
    other: str | Path | None = None,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
) -> tuple[dict[str, object] | None, str | None, int]:
    """Return the brigade.run-diff.v1 payload, or a fail-closed diagnostic.

    Does not print and does not write. Unknown, corrupt, or parentless run
    IDs return ``(None, diagnostic, 2)``.
    """
    pair, diagnostic = _resolve_pair(run, other, cwd=cwd, runs_dir=runs_dir)
    if pair is None:
        return None, diagnostic, 2
    left_dir, right_dir, relation, parent_id, branch_point = pair
    left_detail, left_error, left_rc = runs_cmd.run_detail_contract(left_dir)
    if left_detail is None or left_rc == 2:
        return None, left_error, 2
    right_detail, right_error, right_rc = runs_cmd.run_detail_contract(right_dir)
    if right_detail is None or right_rc == 2:
        return None, right_error, 2
    left_run = _mapping(left_detail.get("run"))
    right_run = _mapping(right_detail.get("run"))
    payload = _allow(
        {
            "schema": _clean(RUN_DIFF_SCHEMA),
            "relation": _clean(relation),
            "left": _side_payload(left_dir, left_run, role=_left_role(relation)),
            "right": _side_payload(right_dir, right_run, role=_right_role(relation)),
            "parent_run_id": _clean(parent_id),
            "branch_point_event_id": _clean(branch_point, runs_cmd._DETAIL_TEXT_LIMIT),
            "lifecycle": _lifecycle_section(left_run, right_run),
            "workers": _collection_section(
                _worker_views(left_detail),
                _worker_views(right_detail),
                key_field="worker",
            ),
            "verification": _verification_section(left_dir, left_detail, right_dir, right_detail),
            "outcome": _scalar_section(
                _outcome_view(left_dir, left_detail),
                _outcome_view(right_dir, right_detail),
                _OUTCOME_FIELDS,
            ),
            "graphtrail": _graphtrail_section(left_dir, right_dir),
        },
        RUN_DIFF_KEYS,
    )
    return payload, None, 0


def diff(
    run: str | Path,
    other: str | Path | None = None,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """Compare a durable child against its parent, or two siblings. Read-only."""
    payload, diagnostic, rc = run_diff_contract(run, other, cwd=cwd, runs_dir=runs_dir)
    if payload is None or rc != 0:
        if diagnostic:
            print(diagnostic, file=sys.stderr)
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    _print_human(payload)
    return 0


def _resolve_pair(
    run: str | Path,
    other: str | Path | None,
    *,
    cwd: Path,
    runs_dir: Path | None,
) -> tuple[tuple[Path, Path, str, str, str | None], str | None] | tuple[None, str]:
    left_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        return None, error
    assert left_dir is not None
    left_meta, error = _read_run_meta(left_dir)
    if error is not None:
        return None, error
    assert left_meta is not None
    if other is None:
        parent_id, branch_point, error = _required_parent(left_meta)
        if error is not None:
            return None, error
        assert parent_id is not None
        right_dir, parent_error = runs_cmd._resolve_run_dir(parent_id, cwd=cwd, runs_dir=runs_dir)
        if parent_error is not None:
            return None, f"error: recorded parent run not found: {parent_id}"
        assert right_dir is not None
        _, parent_meta_error = _read_run_meta(right_dir)
        if parent_meta_error is not None:
            return None, parent_meta_error
        return (left_dir, right_dir, _RELATION_CHILD_PARENT, parent_id, branch_point), None

    right_dir, error = runs_cmd._resolve_run_dir(other, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        return None, error
    assert right_dir is not None
    right_meta, error = _read_run_meta(right_dir)
    if error is not None:
        return None, error
    assert right_meta is not None
    return _relation_for_pair(left_dir, left_meta, right_dir, right_meta)


def _relation_for_pair(
    left_dir: Path,
    left_meta: Mapping[str, Any],
    right_dir: Path,
    right_meta: Mapping[str, Any],
) -> tuple[tuple[Path, Path, str, str, str | None], str | None] | tuple[None, str]:
    left_parent, left_branch = _optional_parent(left_meta)
    right_parent, right_branch = _optional_parent(right_meta)
    if left_parent == right_dir.name:
        if left_parent is None:
            return None, "error: runs do not share parent lineage"
        return (left_dir, right_dir, _RELATION_CHILD_PARENT, left_parent, left_branch), None
    if right_parent == left_dir.name:
        if right_parent is None:
            return None, "error: runs do not share parent lineage"
        return (right_dir, left_dir, _RELATION_CHILD_PARENT, right_parent, right_branch), None
    if left_parent and right_parent and left_parent == right_parent:
        return (left_dir, right_dir, _RELATION_SIBLINGS, left_parent, left_branch or right_branch), None
    return None, "error: runs do not share parent lineage"


def _read_run_meta(run_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        meta = runs_cmd._read_json(run_dir / "run.json")
    except ValueError as exc:
        return None, f"error: {exc}"
    if meta is None:
        return None, f"error: run.json not found in {run_dir}"
    return meta, None


def _required_parent(meta: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    parent_id, branch_point = _optional_parent(meta)
    if parent_id is None:
        return None, None, "error: run has no recorded parent lineage"
    return parent_id, branch_point, None


def _optional_parent(meta: Mapping[str, Any]) -> tuple[str | None, str | None]:
    lineage = runs_cmd._lineage(meta)
    if lineage is None:
        return None, None
    parent_id = lineage.get("parent_run_id")
    if not isinstance(parent_id, str) or not parent_id.strip():
        return None, None
    branch = lineage.get("branch_point_event_id")
    branch_point = branch if isinstance(branch, str) and branch else None
    return parent_id, branch_point


def _left_role(relation: str) -> str:
    return "child" if relation == _RELATION_CHILD_PARENT else "sibling"


def _right_role(relation: str) -> str:
    return "parent" if relation == _RELATION_CHILD_PARENT else "sibling"


def _side_payload(run_dir: Path, run_section: Mapping[str, Any], *, role: str) -> dict[str, object]:
    return _allow(
        {
            "run_id": _clean(run_dir.name),
            "role": _clean(role),
            "status": _clean(run_section.get("status"), runs_cmd._DETAIL_TEXT_LIMIT) or _clean("unknown"),
            "task": _clean(run_section.get("task"), runs_cmd._DETAIL_TASK_LIMIT),
        },
        _SIDE_FIELDS,
    )


def _lifecycle_section(left_run: Mapping[str, Any], right_run: Mapping[str, Any]) -> dict[str, object]:
    return _scalar_section(_lifecycle_view(left_run), _lifecycle_view(right_run), _LIFECYCLE_FIELDS)


def _lifecycle_view(run_section: Mapping[str, Any]) -> dict[str, object]:
    failure = run_section.get("failure")
    failure_payload = None
    if isinstance(failure, Mapping):
        failure_payload = runs_cmd._compact(
            {
                "phase": _clean(failure.get("phase")),
                "kind": _clean(failure.get("kind")),
                "detail": _clean(failure.get("detail"), runs_cmd._DETAIL_TEXT_LIMIT),
            }
        )
    return {
        "status": _clean(run_section.get("status"), runs_cmd._DETAIL_TEXT_LIMIT) or _clean("unknown"),
        "mode": _clean(run_section.get("mode"), runs_cmd._DETAIL_TEXT_LIMIT),
        "failure": failure_payload or None,
        "resume_available": True if run_section.get("resume_available") is True else None,
    }


def _worker_views(detail: Mapping[str, Any]) -> list[dict[str, object]]:
    workers = _mapping(detail.get("workers"))
    results = workers.get("results")
    if not isinstance(results, list):
        return []
    views: list[dict[str, object]] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        failure = result.get("failure")
        failure_payload = None
        if isinstance(failure, Mapping):
            failure_payload = runs_cmd._compact(
                {
                    "class": _clean(failure.get("class")),
                    "detail": _clean(failure.get("detail"), runs_cmd._DETAIL_TEXT_LIMIT),
                }
            )
        views.append(
            runs_cmd._compact(
                {
                    "worker": _clean(result.get("worker")),
                    "ok": result.get("ok") if isinstance(result.get("ok"), bool) else None,
                    "status": _clean(result.get("status"), runs_cmd._DETAIL_TEXT_LIMIT),
                    "exit_code": runs_cmd._clean_number(result.get("exit_code")),
                    "timed_out": result.get("timed_out") if isinstance(result.get("timed_out"), bool) else None,
                    "failure": failure_payload or None,
                }
            )
        )
    return views


def _verification_section(
    left_dir: Path,
    left_detail: Mapping[str, Any],
    right_dir: Path,
    right_detail: Mapping[str, Any],
) -> dict[str, object]:
    """Compare verify results on content. ``run_id`` is display-only."""
    left_view = _verification_view(left_dir, left_detail)
    right_view = _verification_view(right_dir, right_detail)
    changes = [
        _change(field, left_view.get(field), right_view.get(field))
        for field in sorted(_VERIFY_CONTENT_FIELDS)
        if left_view.get(field) != right_view.get(field)
    ]
    return _section_payload(bool(changes), changes, left_view, right_view)


def _verification_view(run_dir: Path, detail: Mapping[str, Any]) -> dict[str, object]:
    receipt = _first_verify_receipt(detail)
    command = _first_verify_command(receipt)
    digest, _size = _final_digest(run_dir)
    return _allow(
        {
            "run_id": _clean(receipt.get("run_id")),
            "status": _clean(receipt.get("status"), runs_cmd._DETAIL_TEXT_LIMIT),
            "command": _clean(command.get("command"), runs_cmd._DETAIL_COMMAND_LIMIT),
            "exit_code": runs_cmd._clean_number(command.get("exit_code")),
            "digest": _clean(digest),
        },
        _VERIFY_VIEW_FIELDS,
    )


def _first_verify_receipt(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    receipts = detail.get("verification")
    if not isinstance(receipts, list):
        return {}
    for receipt in receipts:
        if isinstance(receipt, Mapping):
            return receipt
    return {}


def _first_verify_command(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return {}
    for command in commands:
        if isinstance(command, Mapping):
            return command
    return {}


def _outcome_view(run_dir: Path, detail: Mapping[str, Any]) -> dict[str, object]:
    run_section = _mapping(detail.get("run"))
    synthesis = _mapping(detail.get("synthesis"))
    result = synthesis.get("result")
    result_map = result if isinstance(result, Mapping) else {}
    digest, size = _final_digest(run_dir)
    return {
        "suspected_noop": True if run_section.get("suspected_noop") is True else None,
        "error": _clean(run_section.get("error"), runs_cmd._DETAIL_TEXT_LIMIT),
        "synthesis_ok": result_map.get("ok") if isinstance(result_map.get("ok"), bool) else None,
        "synthesis_detail": _clean(result_map.get("detail"), runs_cmd._DETAIL_TEXT_LIMIT),
        "final_sha256": _clean(digest),
        "final_bytes": runs_cmd._clean_number(size),
    }


def _final_digest(run_dir: Path) -> tuple[str | None, int | None]:
    path = run_dir / "final.txt"
    try:
        if not path.is_file() or path.is_symlink():
            return None, None
        data = path.read_bytes()
    except OSError:
        return None, None
    return hashlib.sha256(data).hexdigest(), len(data)


def _graphtrail_section(left_dir: Path, right_dir: Path) -> dict[str, object]:
    left_source = _graphtrail_source(left_dir)
    right_source = _graphtrail_source(right_dir)
    left_view = _graphtrail_view(left_source) if isinstance(left_source, Mapping) else None
    right_view = _graphtrail_view(right_source) if isinstance(right_source, Mapping) else None
    left_ok = _graphtrail_comparable(left_source)
    right_ok = _graphtrail_comparable(right_source)
    if left_ok and right_ok:
        changed = left_view != right_view
        return _allow(
            {
                "status": _clean(_GRAPHTRAIL_COMPARED),
                "reason": None,
                "changed": changed,
                "left": left_view,
                "right": right_view,
            },
            _GRAPHTRAIL_FIELDS,
        )
    reason = (
        _SKIP_INCOMPATIBLE if _graphtrail_present(left_source) or _graphtrail_present(right_source) else _SKIP_ABSENT
    )
    return _allow(
        {
            "status": _clean(_GRAPHTRAIL_SKIPPED),
            "reason": _clean(reason),
            "changed": None,
            "left": left_view,
            "right": right_view,
        },
        _GRAPHTRAIL_FIELDS,
    )


def _graphtrail_source(run_dir: Path) -> Mapping[str, Any] | None | object:
    sidecar_path = run_dir / graphtrail_delta.SIDECAR_NAME
    try:
        sidecar = runs_cmd._read_json(sidecar_path)
    except ValueError:
        return _UNREADABLE
    if isinstance(sidecar, dict):
        return sidecar
    try:
        meta = runs_cmd._read_json(run_dir / "run.json")
    except ValueError:
        meta = None
    if isinstance(meta, dict):
        for key in ("code_graph_delta", "graphtrail_delta"):
            value = meta.get(key)
            if isinstance(value, Mapping):
                return value
    try:
        worker_results = runs_cmd._read_json(run_dir / "worker-results.json")
    except ValueError:
        return None
    ground_truth = worker_results.get("ground_truth") if isinstance(worker_results, dict) else None
    if isinstance(ground_truth, Mapping):
        for key in ("code_graph_delta", "graphtrail_delta"):
            value = ground_truth.get(key)
            if isinstance(value, Mapping):
                return value
    return None


def _graphtrail_present(source: Mapping[str, Any] | None | object) -> bool:
    return source is _UNREADABLE or isinstance(source, Mapping)


def _graphtrail_comparable(source: Mapping[str, Any] | None | object) -> bool:
    if not isinstance(source, Mapping):
        return False
    status = source.get("status")
    if source.get("ok") is not True and status != "ok":
        return False
    before, after = _graphtrail_hashes(source)
    return before is not None or after is not None


def _graphtrail_hashes(source: Mapping[str, Any]) -> tuple[str | None, str | None]:
    attestations = source.get("attestations")
    attest_map = attestations if isinstance(attestations, Mapping) else {}
    before = attest_map.get("before_snapshot_sha256", source.get("before_snapshot_sha256"))
    after = attest_map.get("after_snapshot_sha256", source.get("after_snapshot_sha256"))
    return (
        before if isinstance(before, str) and before else None,
        after if isinstance(after, str) and after else None,
    )


def _graphtrail_view(source: Mapping[str, Any]) -> dict[str, object]:
    before, after = _graphtrail_hashes(source)
    return _allow(
        {
            "status": _clean(source.get("status"), runs_cmd._DETAIL_TEXT_LIMIT),
            "changed_symbol_count": runs_cmd._clean_number(source.get("changed_symbol_count")),
            "edge_churn": runs_cmd._clean_number(source.get("edge_churn")),
            "before_snapshot_sha256": _clean(before, runs_cmd._DETAIL_TEXT_LIMIT),
            "after_snapshot_sha256": _clean(after, runs_cmd._DETAIL_TEXT_LIMIT),
        },
        _GRAPHTRAIL_SIDE_FIELDS,
        source,
    )


def _scalar_section(
    left: Mapping[str, object],
    right: Mapping[str, object],
    fields: frozenset[str],
) -> dict[str, object]:
    left_view = _allow({key: left.get(key) for key in fields}, fields, left)
    right_view = _allow({key: right.get(key) for key in fields}, fields, right)
    changes = [
        _change(field, left_view.get(field), right_view.get(field))
        for field in sorted(fields)
        if left_view.get(field) != right_view.get(field)
    ]
    return _section_payload(bool(changes), changes, left_view, right_view)


def _collection_section(
    left_items: list[dict[str, object]],
    right_items: list[dict[str, object]],
    *,
    key_field: str,
) -> dict[str, object]:
    left_index = _index_by(left_items, key_field)
    right_index = _index_by(right_items, key_field)
    changes: list[dict[str, object]] = []
    for key in sorted(set(left_index) | set(right_index), key=lambda item: item or ""):
        left_item = left_index.get(key)
        right_item = right_index.get(key)
        if left_item == right_item:
            continue
        label = key or key_field
        changes.append(_change(label, left_item, right_item))
    return _section_payload(bool(changes), changes, left_items, right_items)


def _index_by(items: list[dict[str, object]], key_field: str) -> dict[str | None, dict[str, object]]:
    indexed: dict[str | None, dict[str, object]] = {}
    for item in items:
        key = item.get(key_field)
        indexed[key if isinstance(key, str) else None] = item
    return indexed


def _section_payload(
    changed: bool,
    changes: list[dict[str, object]],
    left: object,
    right: object,
) -> dict[str, object]:
    return _allow(
        {
            "changed": changed,
            "changes": changes or None,
            "left": left or None,
            "right": right or None,
        },
        _SECTION_FIELDS,
    )


def _change(field: str, left: object, right: object) -> dict[str, object]:
    return _allow(
        {
            "field": _clean(field, runs_cmd._DETAIL_TEXT_LIMIT),
            "left": _cleaned_json(left),
            "right": _cleaned_json(right),
        },
        _CHANGE_FIELDS,
    )


def _cleaned_json(value: object) -> object:
    """Bound and home-prefix-redact every string through ``_clean_str``."""
    if isinstance(value, str):
        return _clean(value, runs_cmd._DETAIL_TEXT_LIMIT)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return runs_cmd._clean_number(value)
    if isinstance(value, Mapping):
        payload: dict[str, object] = {}
        for key, inner in value.items():
            cleaned_key = _clean(key)
            if cleaned_key is None:
                continue
            cleaned_inner = _cleaned_json(inner)
            if cleaned_inner is None and inner not in (False, 0, 0.0):
                continue
            payload[cleaned_key] = cleaned_inner
        return payload
    if isinstance(value, list):
        return [_cleaned_json(item) for item in value]
    return None


def _allow(
    cleaned: Mapping[str, object],
    allowed: frozenset[str],
    source: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    return runs_cmd._allowlisted(runs_cmd._merge_source(source, cleaned), allowed)


def _clean(value: object, limit: int | None = None) -> str | None:
    return runs_cmd._clean_str(value, limit)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _print_human(payload: Mapping[str, Any]) -> None:
    left = _mapping(payload.get("left"))
    right = _mapping(payload.get("right"))
    print(f"diff: {left.get('run_id')} vs {right.get('run_id')} ({payload.get('relation')})")
    parent = payload.get("parent_run_id")
    if parent:
        print(f"parent: {parent}")
    branch = payload.get("branch_point_event_id")
    if branch:
        print(f"branch point: {branch}")
    for name in ("lifecycle", "workers", "verification", "outcome"):
        section = _mapping(payload.get(name))
        print(f"{name}: {'changed' if section.get('changed') else 'unchanged'}")
    graphtrail = _mapping(payload.get("graphtrail"))
    status = graphtrail.get("status")
    if status == _GRAPHTRAIL_SKIPPED:
        reason = graphtrail.get("reason") or _SKIP_ABSENT
        print(f"graphtrail: skipped ({reason})")
        return
    if graphtrail.get("changed"):
        print("graphtrail: compared (changed)")
        return
    print("graphtrail: compared (unchanged)")
