"""Sweep and plan-proposal operations."""

from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import constants, helpers, ledger as ledger_mod

from . import scanners as scanners_mod
from . import services as services_mod


def _sweep_run_references(run: dict[str, Any]) -> dict[str, Any]:
    ingest = run.get("ingest_output") if isinstance(run.get("ingest_output"), dict) else {}
    created_import_ids = [
        str(item) for item in ingest.get("created_import_ids", []) if isinstance(item, str) and item.strip()
    ]
    for item in run.get("stamped_import_ids", []):
        if isinstance(item, str) and item.strip() and item not in created_import_ids:
            created_import_ids.append(item)
    skipped_source_fingerprints = [
        str(item) for item in ingest.get("skipped_source_fingerprints", []) if isinstance(item, str) and item.strip()
    ]
    dismissed_source_fingerprints = [
        str(item) for item in ingest.get("dismissed_source_fingerprints", []) if isinstance(item, str) and item.strip()
    ]
    return {
        "scanner_id": run.get("scanner_id"),
        "scanner_source": run.get("source"),
        "scanner_run_id": run.get("run_id"),
        "receipt_path": scanners_mod._scanner_run_receipt_path(run),
        "import_path": ingest.get("path"),
        "created_import_ids": created_import_ids,
        "skipped_source_fingerprints": skipped_source_fingerprints,
        "dismissed_source_fingerprints": dismissed_source_fingerprints,
    }


def _sweep_import_references(report: dict[str, Any]) -> dict[str, Any]:
    existing = report.get("import_references")
    if isinstance(existing, dict):
        return existing
    runs = []
    run_result = report.get("run_result") if isinstance(report.get("run_result"), dict) else {}
    for run in run_result.get("runs", []):
        if isinstance(run, dict):
            runs.append(_sweep_run_references(run))
    return _sweep_references_from_runs(runs)


def _sweep_references_from_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    created_import_ids: list[str] = []
    skipped_source_fingerprints: list[str] = []
    dismissed_source_fingerprints: list[str] = []
    for run in runs:
        created_import_ids.extend(
            str(item) for item in run.get("created_import_ids", []) if isinstance(item, str) and item.strip()
        )
        skipped_source_fingerprints.extend(
            str(item) for item in run.get("skipped_source_fingerprints", []) if isinstance(item, str) and item.strip()
        )
        dismissed_source_fingerprints.extend(
            str(item) for item in run.get("dismissed_source_fingerprints", []) if isinstance(item, str) and item.strip()
        )
    return {
        "created_import_ids": sorted(set(created_import_ids)),
        "skipped_source_fingerprints": sorted(set(skipped_source_fingerprints)),
        "dismissed_source_fingerprints": sorted(set(dismissed_source_fingerprints)),
        "runs": runs,
    }


def _sweep_import_counts(run_payload: dict[str, Any]) -> dict[str, int]:
    runs = run_payload.get("runs") if isinstance(run_payload.get("runs"), list) else []
    created = 0
    skipped = 0
    dismissed = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        ingest = run.get("ingest_output") if isinstance(run.get("ingest_output"), dict) else {}
        created += int(ingest.get("created", 0) or 0)
        skipped += int(ingest.get("skipped", 0) or 0)
        dismissed += int(ingest.get("dismissed", 0) or 0)
    before = run_payload.get("imports_before") if isinstance(run_payload.get("imports_before"), dict) else {}
    after = run_payload.get("imports_after") if isinstance(run_payload.get("imports_after"), dict) else {}
    delta = int(after.get("total", 0) or 0) - int(before.get("total", 0) or 0)
    if delta > created:
        created = delta
    return {"created": created, "skipped": skipped, "dismissed": dismissed}


def _write_sweep_report(target: Path, report: dict[str, Any]) -> None:
    sweep_id = str(report.get("sweep_id") or "sweep")
    helpers._write_json(helpers._scanner_sweeps_root(target) / sweep_id / "sweep.json", report)


def _sweep_closeout_status(report: dict[str, Any]) -> str | None:
    closeout = report.get("review_closeout")
    if not isinstance(closeout, dict):
        return None
    status = closeout.get("status")
    return str(status) if isinstance(status, str) else None


def _sweep_is_closed(report: dict[str, Any]) -> bool:
    return _sweep_closeout_status(report) in {"reviewed", "reviewed_with_deferrals"}


def sweep(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest: bool = True,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if scanner_id and all_matching:
        print("error: pass --scanner or --all, not both", file=sys.stderr)
        return 2
    started = helpers._now()
    sweep_id = f"{started.strftime('%Y%m%d-%H%M%S')}-scanner-sweep-{uuid4().hex[:6]}"
    run_payload, run_rc = scanners_mod._scanners_run_payload(
        target=target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=not scanner_id and not all_matching,
        include_disabled=include_disabled,
        force=force,
        ingest_output=ingest,
    )
    completed = helpers._now()
    runs = run_payload.get("runs") if isinstance(run_payload.get("runs"), list) else []
    errors = run_payload.get("errors") if isinstance(run_payload.get("errors"), list) else []
    status_text = "failed" if run_rc != 0 else "completed"
    inbox_hygiene = services_mod._inbox_hygiene_payload(target)
    run_references = [_sweep_run_references(run) for run in runs if isinstance(run, dict)]
    report = {
        "sweep_id": sweep_id,
        "status": status_text,
        "target": str(target),
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "mode": "all" if all_matching else ("scanner" if scanner_id else "due"),
        "scanner": scanner_id,
        "include_disabled": include_disabled,
        "force": force,
        "ingest": ingest,
        "run_result": run_payload,
        "run_rc": run_rc,
        "errors": errors,
        "scanner_run_ids": [run.get("run_id") for run in runs if isinstance(run, dict)],
        "receipt_paths": [scanners_mod._scanner_run_receipt_path(run) for run in runs if isinstance(run, dict)],
        "import_counts": _sweep_import_counts(run_payload),
        "import_references": _sweep_references_from_runs(run_references),
        "inbox_hygiene": {
            "issue_count": inbox_hygiene["issue_count"],
            "top_issue": inbox_hygiene["top_issue"],
        },
        "suggested_commands": [
            "brigade work inbox",
            "brigade work inbox doctor",
            "brigade work import plan <import-id>",
        ],
    }
    _write_sweep_report(target, report)
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
        return run_rc
    print(f"work sweep: {target}")
    print(f"sweep: {sweep_id}")
    print(f"status: {status_text}")
    print(f"runs: {len(runs)}")
    print(f"created: {report['import_counts']['created']}")
    print(f"skipped: {report['import_counts']['skipped']}")
    print(f"dismissed: {report['import_counts']['dismissed']}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"report: {helpers._scanner_sweeps_root(target) / sweep_id / 'sweep.json'}")
    print("next: brigade work inbox")
    return run_rc


def sweeps(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    reports = scanners_mod._scanner_sweeps(target)[:limit]
    payload = {"target": str(target), "sweeps_root": str(helpers._scanner_sweeps_root(target)), "sweeps": reports}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work sweeps: {target}")
    print(f"sweeps_root: {payload['sweeps_root']}")
    if not reports:
        print("sweeps: none")
        return 0
    for report in reports:
        print(
            f"- {report.get('sweep_id')} [{report.get('status')}] runs={len(report.get('scanner_run_ids') or [])} {report.get('started_at')}"
        )
    return 0


def plans(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    plans_dir = helpers._plans_dir(target)
    entries: list[dict[str, Any]] = []
    if plans_dir.is_dir():
        for json_path in plans_dir.glob("*.json"):
            name = json_path.name
            if name.endswith(".meta.json"):
                kind = "meta"
                task_id = name[: -len(".meta.json")]
            else:
                kind = "plan"
                task_id = name[: -len(".json")]
            _, md_path = helpers._plan_paths(target, task_id, kind)
            try:
                data = json.loads(json_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = None
            if not isinstance(data, dict):
                entries.append(
                    {
                        "task_id": task_id,
                        "kind": kind,
                        "status": "unreadable",
                        "updated_at": "",
                        "path": ledger_mod._plan_rel_path(target, md_path),
                    }
                )
                continue
            entries.append(
                {
                    "task_id": str(data.get("task_id") or task_id),
                    "kind": str(data.get("kind") or kind),
                    "status": str(data.get("status") or ""),
                    "updated_at": str(data.get("updated_at") or ""),
                    "path": ledger_mod._plan_rel_path(target, md_path),
                }
            )
    entries.sort(key=lambda item: (item.get("updated_at") or "", item.get("task_id") or ""), reverse=True)
    entries = entries[:limit]
    if json_output:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no plan artifacts")
        return 0
    for entry in entries:
        print(f"- {entry['task_id']} [{entry['kind']}] [{entry['status']}] {entry['updated_at']} {entry['path']}")
    return 0


def _plan_proposals_dir(target: Path) -> Path:
    return helpers._work_root(target) / "plan-proposals"


def _proposal_path(target: Path, task_id: str, as_kind: str) -> Path:
    return _plan_proposals_dir(target) / f"{task_id}-{as_kind}.md"


def _render_proposal_md(receipt: dict[str, Any], as_kind: str) -> str:
    def _bullets(items: Any) -> list[str]:
        values = [str(item) for item in items] if isinstance(items, list) else []
        if not values:
            return ["_none recorded_"]
        return [f"- {item}" for item in values]

    def _checklist(items: Any) -> list[str]:
        values = [str(item) for item in items] if isinstance(items, list) else []
        if not values:
            return ["_none recorded_"]
        return [f"- [ ] {item}" for item in values]

    title = str(receipt.get("title") or "")
    acceptance = receipt.get("acceptance")
    if title:
        intent = title
    elif isinstance(acceptance, list) and acceptance:
        intent = str(acceptance[0])
    else:
        intent = "_none recorded_"

    lines: list[str] = []
    lines.append(f"# Draft {as_kind}: {title}")
    lines.append("")
    lines.append(
        "> DRAFT proposal generated from an accepted plan. Review and move it into "
        "place yourself; Brigade does not install it."
    )
    lines.append("")
    lines.append(f"- **Source task:** {receipt.get('task_id', '')}")
    lines.append(f"- **Generated at:** {helpers._now().isoformat()}")
    lines.append("")
    lines.append("## Intent")
    lines.append(intent)
    lines.append("")
    lines.append("## Acceptance checklist")
    lines.extend(_checklist(acceptance))
    lines.append("")
    lines.append("## Steps")
    lines.extend(_bullets(receipt.get("steps")))
    lines.append("")
    lines.append("## Assumptions")
    lines.extend(_bullets(receipt.get("assumptions")))
    lines.append("")
    lines.append("## Risks")
    lines.extend(_bullets(receipt.get("risks")))
    lines.append("")
    lines.append("## Next safe command")
    lines.append(f"`{receipt.get('next_command', '')}`")
    lines.append("")
    return "\n".join(lines)


def plan_promote(*, target: Path, task_id: str, as_kind: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if as_kind not in constants._PROPOSAL_KINDS:
        print(
            f"error: --as must be one of {', '.join(constants._PROPOSAL_KINDS)}: {as_kind}",
            file=sys.stderr,
        )
        return 2
    task, _ = ledger_mod._find_task(target, task_id)
    lookup_id = str(task.get("id") or task_id) if task is not None else task_id
    receipt = ledger_mod._read_plan_receipt(target, lookup_id, kind="plan")
    if receipt is None:
        print(f"error: no plan artifact for task: {task_id}", file=sys.stderr)
        return 1
    if receipt.get("status") != "accepted":
        print(
            "error: plan not accepted (run: brigade work task plan {id} --write --accept)".format(id=task_id),
            file=sys.stderr,
        )
        return 1
    resolved_id = str(receipt.get("task_id") or task_id)
    proposal_path = _proposal_path(target, resolved_id, as_kind)
    _plan_proposals_dir(target).mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(_render_proposal_md(receipt, as_kind))
    rel = ledger_mod._plan_rel_path(target, proposal_path)
    if json_output:
        print(json.dumps({"task_id": resolved_id, "as": as_kind, "path": rel}, indent=2, sort_keys=True))
        return 0
    print(f"wrote draft proposal: {rel}")
    print("review then move it into place yourself (not installed)")
    return 0


def plan_proposals(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    proposals_dir = _plan_proposals_dir(target)
    entries: list[dict[str, Any]] = []
    if proposals_dir.is_dir():
        for md_path in proposals_dir.glob("*.md"):
            stem = md_path.name[: -len(".md")]
            task_id, _, as_kind = stem.rpartition("-")
            if not task_id:
                task_id, as_kind = stem, ""
            try:
                mtime = md_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append(
                {
                    "task_id": task_id,
                    "as": as_kind,
                    "path": ledger_mod._plan_rel_path(target, md_path),
                    "_mtime": mtime,
                }
            )
    entries.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
    for entry in entries:
        entry.pop("_mtime", None)
    if json_output:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no plan proposals")
        return 0
    for entry in entries:
        print(f"- {entry['task_id']} [{entry['as']}] {entry['path']}")
    return 0


def sweep_show(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [
        report
        for report in scanners_mod._scanner_sweeps(target)
        if str(report.get("sweep_id") or "").startswith(sweep_id)
    ]
    if not matches:
        print(f"error: sweep not found: {sweep_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: sweep id is ambiguous: {sweep_id}", file=sys.stderr)
        return 2
    report = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "sweep": report}, indent=2, sort_keys=True))
        return 0
    print(f"sweep: {report.get('sweep_id')}")
    print(f"status: {report.get('status')}")
    print(f"started_at: {report.get('started_at')}")
    print(f"completed_at: {report.get('completed_at')}")
    print(f"runs: {len(report.get('scanner_run_ids') or [])}")
    counts = report.get("import_counts") if isinstance(report.get("import_counts"), dict) else {}
    print(f"created: {counts.get('created', 0)}")
    print(f"skipped: {counts.get('skipped', 0)}")
    print(f"dismissed: {counts.get('dismissed', 0)}")
    hygiene = report.get("inbox_hygiene") if isinstance(report.get("inbox_hygiene"), dict) else {}
    print(f"inbox_hygiene: {hygiene.get('issue_count', 0)} issue(s)")
    return 0


def _find_sweep_report(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if sweep_id == "latest":
        latest = scanners_mod._scanner_latest_sweep(target)
        if latest is None:
            return None, "sweep not found: latest"
        return latest, None
    matches = [
        report
        for report in scanners_mod._scanner_sweeps(target)
        if str(report.get("sweep_id") or "").startswith(sweep_id)
    ]
    if not matches:
        return None, f"sweep not found: {sweep_id}"
    if len(matches) > 1:
        return None, f"sweep id is ambiguous: {sweep_id}"
    return matches[0], None


def _sweep_import_suggested_commands(import_id: str, kind: str) -> list[str]:
    commands = [
        f"brigade work import plan {import_id}",
        f'brigade work import dismiss {import_id} --reason "..."',
    ]
    if kind == "task":
        commands.insert(1, f"brigade work import promote {import_id}")
        commands.append(f"brigade work import promote --run {import_id}")
    elif kind in constants.HANDOFF_READY_KINDS:
        commands.insert(1, f"brigade work import plan-handoff {import_id}")
        commands.insert(2, f"brigade work import promote-handoff {import_id}")
    return commands


def _sweep_import_review_summary(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    summary = ledger_mod._import_summary(item, now=now)
    metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
    required = ("scanner_id", "scanner_source", "scanner_run_id", "source_fingerprint")
    provenance_complete = all(metadata.get(key) for key in required)
    acceptance_count = int(summary.get("acceptance_count", 0) or 0)
    if summary.get("kind") == "task":
        acceptance_coverage = "ready" if acceptance_count else "missing"
        priority = str(summary.get("priority") or "normal")
    else:
        acceptance_coverage = "n/a"
        priority = "n/a"
    import_id = str(summary.get("id") or "")
    summary.update(
        {
            "priority": priority,
            "acceptance_coverage": acceptance_coverage,
            "provenance_complete": provenance_complete,
            "provenance_status": "complete" if provenance_complete else "missing",
            "suggested_commands": _sweep_import_suggested_commands(import_id, str(summary.get("kind") or "task"))
            if summary.get("status") == "pending" and import_id
            else [],
        }
    )
    if summary.get("kind") in constants.HANDOFF_READY_KINDS:
        summary["handoff_ready"] = True
        summary["target_document"] = ledger_mod._handoff_target_document(item)
    return summary


def _sweep_group_key(item: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(item.get("source") or "manual"),
        str(item.get("kind") or "task"),
        str(item.get("priority") or "n/a"),
        str(item.get("acceptance_coverage") or "n/a"),
        str(item.get("provenance_status") or "missing"),
        str(item.get("status") or "pending"),
    )


def _sweep_review_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str, str], list[str]] = {}
    for item in items:
        import_id = item.get("id")
        if isinstance(import_id, str):
            grouped.setdefault(_sweep_group_key(item), []).append(import_id)
    result: list[dict[str, Any]] = []
    for key, import_ids in sorted(grouped.items()):
        source, kind, priority, acceptance_coverage, provenance_status, status = key
        result.append(
            {
                "source": source,
                "kind": kind,
                "priority": priority,
                "acceptance_coverage": acceptance_coverage,
                "provenance_status": provenance_status,
                "status": status,
                "count": len(import_ids),
                "import_ids": sorted(import_ids),
            }
        )
    return result


def _sweep_review_checks(
    *,
    report: dict[str, Any],
    references: dict[str, Any],
    items: list[dict[str, Any]],
    missing_import_ids: list[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    pending_ids = [
        str(item.get("id")) for item in items if item.get("status") == "pending" and isinstance(item.get("id"), str)
    ]
    completed = helpers._parse_iso_datetime(report.get("completed_at") or report.get("started_at"))
    stale_pending: list[str] = []
    if pending_ids and completed is not None:
        age_hours = (helpers._now() - completed).total_seconds() / 3600
        if age_hours > constants.SCANNER_SWEEP_REVIEW_STALE_HOURS:
            stale_pending = pending_ids
    checks.append(
        services_mod._import_hygiene_issue(
            constants.WARN if stale_pending else constants.OK,
            "scanner_sweep_unreviewed",
            f"{len(stale_pending)} pending sweep import(s) older than {constants.SCANNER_SWEEP_REVIEW_STALE_HOURS}h"
            if stale_pending
            else "none",
            stale_pending[:10],
        )
    )
    checks.append(
        services_mod._import_hygiene_issue(
            constants.WARN if missing_import_ids else constants.OK,
            "scanner_sweep_missing_imports",
            f"{len(missing_import_ids)} sweep import reference(s) missing from inbox" if missing_import_ids else "none",
            missing_import_ids[:10],
        )
    )
    missing_provenance = [
        str(item.get("id")) for item in items if not item.get("provenance_complete") and isinstance(item.get("id"), str)
    ]
    checks.append(
        services_mod._import_hygiene_issue(
            constants.WARN if missing_provenance else constants.OK,
            "scanner_sweep_missing_provenance",
            f"{len(missing_provenance)} sweep import(s) missing scanner provenance" if missing_provenance else "none",
            missing_provenance[:10],
        )
    )
    created = len(
        references.get("created_import_ids", []) if isinstance(references.get("created_import_ids"), list) else []
    )
    skipped = len(
        references.get("skipped_source_fingerprints", [])
        if isinstance(references.get("skipped_source_fingerprints"), list)
        else []
    )
    dismissed = len(
        references.get("dismissed_source_fingerprints", [])
        if isinstance(references.get("dismissed_source_fingerprints"), list)
        else []
    )
    noisy = created == 0 and (skipped + dismissed) > 0
    checks.append(
        services_mod._import_hygiene_issue(
            constants.WARN if noisy else constants.OK,
            "scanner_sweep_noisy_noop",
            f"created=0 skipped={skipped} dismissed={dismissed}" if noisy else "none",
        )
    )
    return checks


def _sweep_review_payload(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    report, error = _find_sweep_report(target, sweep_id)
    if report is None:
        return None, error
    references = _sweep_import_references(report)
    imports_by_id = {
        str(item.get("id")): item
        for item in ledger_mod._read_imports(target)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    now = helpers._now()
    import_ids = [
        str(item) for item in references.get("created_import_ids", []) if isinstance(item, str) and item.strip()
    ]
    missing_import_ids = sorted(import_id for import_id in import_ids if import_id not in imports_by_id)
    items = [
        _sweep_import_review_summary(imports_by_id[import_id], now=now)
        for import_id in import_ids
        if import_id in imports_by_id
    ]
    actionable = [item for item in items if item.get("status") == "pending"]
    checks = _sweep_review_checks(
        report=report,
        references=references,
        items=items,
        missing_import_ids=missing_import_ids,
    )
    closeout = report.get("review_closeout") if isinstance(report.get("review_closeout"), dict) else None
    if _sweep_is_closed(report):
        checks = [
            check
            for check in checks
            if check.get("name") not in {"scanner_sweep_unreviewed", "scanner_sweep_noisy_noop"}
        ]
        checks.append(
            services_mod._import_hygiene_issue(
                constants.OK,
                "scanner_sweep_closeout",
                f"{closeout.get('status')} at {closeout.get('closed_at')}" if closeout else "reviewed",
            )
        )
    return (
        {
            "target": str(target),
            "sweep": report,
            "references": references,
            "imports": items,
            "groups": _sweep_review_groups(items),
            "actionable_imports": actionable,
            "top_pending_import": actionable[0] if actionable else None,
            "missing_import_ids": missing_import_ids,
            "closeout": closeout,
            "checks": checks,
            "issues": [check for check in checks if check.get("status") != constants.OK],
        },
        None,
    )


def sweep_review(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _sweep_review_payload(target, sweep_id)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sweep_data = payload["sweep"]
    print(f"sweep_review: {sweep_data.get('sweep_id')}")
    print(f"status: {sweep_data.get('status')}")
    print(f"created_imports: {len(payload['references'].get('created_import_ids') or [])}")
    print(f"missing_imports: {len(payload['missing_import_ids'])}")
    if payload["groups"]:
        print("groups:")
        for group in payload["groups"]:
            print(
                f"- {group['source']} {group['kind']} priority={group['priority']} "
                f"acceptance={group['acceptance_coverage']} provenance={group['provenance_status']} "
                f"status={group['status']} count={group['count']}"
            )
    if payload["actionable_imports"]:
        print("actionable:")
        for item in payload["actionable_imports"]:
            print(
                f"- {item.get('id')} [{item.get('kind')}] {item.get('source')}: {helpers._short(str(item.get('text', '')))}"
            )
            for command in item.get("suggested_commands", []):
                print(f"  next: {command}")
    for check in payload["checks"]:
        if check.get("status") != constants.OK:
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    return 0


def sweep_closeout(
    *,
    target: Path,
    sweep_id: str,
    reason: str | None = None,
    deferred_imports: list[str] | None = None,
    defer_all: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _sweep_review_payload(target, sweep_id)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report = payload["sweep"]
    pending_ids = sorted(
        str(item.get("id"))
        for item in payload.get("actionable_imports", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    missing_import_ids = list(payload.get("missing_import_ids") or [])
    deferred = sorted(set(deferred_imports or []))
    unknown_deferred = sorted(import_id for import_id in deferred if import_id not in pending_ids)
    blocked: list[str] = []
    if missing_import_ids:
        blocked.append("missing sweep import references")
    if unknown_deferred:
        blocked.append("deferred imports are not pending sweep imports")
    if pending_ids and not defer_all:
        unresolved = sorted(import_id for import_id in pending_ids if import_id not in deferred)
        if unresolved:
            blocked.append("pending imports remain unreviewed")
    else:
        unresolved = []
    closeout = {
        "sweep_id": report.get("sweep_id"),
        "closed_at": helpers._now().isoformat(),
        "status": "blocked" if blocked else ("reviewed_with_deferrals" if pending_ids else "reviewed"),
        "pending_import_ids": pending_ids,
        "deferred_import_ids": pending_ids if defer_all and pending_ids else deferred,
        "missing_import_ids": missing_import_ids,
        "unresolved_import_ids": unresolved,
        "blocked_reasons": blocked,
        "reason": reason or "",
    }
    if not blocked:
        report["review_closeout"] = closeout
        _write_sweep_report(target, report)
    output = {"target": str(target), "sweep_id": report.get("sweep_id"), "closeout": closeout}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if not blocked else 2
    print(f"sweep_closeout: {report.get('sweep_id')}")
    print(f"status: {closeout['status']}")
    print(f"pending_imports: {len(pending_ids)}")
    print(f"deferred_imports: {len(closeout['deferred_import_ids'])}")
    if blocked:
        for item in blocked:
            print(f"blocked: {item}", file=sys.stderr)
        return 2
    print(f"closed_at: {closeout['closed_at']}")
    return 0
