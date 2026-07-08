"""Import command surface (add, ingest, triage, promote, dismiss)."""

from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any
from .. import evidence_brief, scrub
from ..untrusted import scan_untrusted, wrap_untrusted
from . import constants, helpers, ledger as ledger_mod
from . import scanners as scanners_mod
from . import services as services_mod


def import_add(
    *,
    target: Path,
    text: str,
    kind: str = "task",
    source: str = "manual",
    metadata: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: import text is required", file=sys.stderr)
        return 2
    if kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    source_text = source.strip() or "manual"
    try:
        parsed_metadata = ledger_mod._parse_metadata(metadata)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    imports = ledger_mod._read_imports(target)
    item = ledger_mod._make_import(rendered, kind=kind, source=source_text, metadata=parsed_metadata)
    imports.append(item)
    ledger_mod._write_imports(target, imports)
    print(f"import: {item['id']}")
    print(f"status: {item['status']}")
    print(f"kind: {item['kind']}")
    print(f"source: {item['source']}")
    print(f"text: {item['text']}")
    return 0


def import_context(
    *,
    target,
    text,
    source="manual",
    context_kind="note",
    from_file=None,
    max_chars=20000,
    json_output=False,
) -> int:
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if context_kind not in constants.CONTEXT_KINDS:
        print(
            f"error: --kind must be one of: {', '.join(constants.CONTEXT_KINDS)}",
            file=sys.stderr,
        )
        return 2

    if from_file is not None:
        body_path = Path(from_file).expanduser()
        try:
            raw = body_path.read_text()
        except OSError as exc:
            print(f"error: cannot read --from-file: {exc}", file=sys.stderr)
            return 2
    else:
        raw = text

    body = (raw or "").strip()
    if not body:
        print("error: context body is required", file=sys.stderr)
        return 2

    sig = scan_untrusted(body)
    framed = wrap_untrusted(body, source_kind="tool-output", max_chars=max_chars)
    metadata = {
        "context_kind": context_kind,
        "injection_flagged": sig.flagged,
        "injection_count": sig.count,
        "needs_review": sig.flagged,
        "source_chars": len(body),
        "truncated": len(body) > max_chars,
    }
    source_text = source.strip() or "manual"

    imports = ledger_mod._read_imports(target)
    item = ledger_mod._make_import(framed, kind="context", source=source_text, metadata=metadata)
    imports.append(item)
    ledger_mod._write_imports(target, imports)

    if json_output:
        print(json.dumps(item, indent=2, sort_keys=True))
        return 0

    print(f"import: {item['id']}")
    print(f"status: {item['status']}")
    print(f"kind: {item['kind']}")
    print(f"source: {item['source']}")
    print(f"context_kind: {context_kind}")
    if sig.flagged:
        print(f"needs_review: injection signal ({sig.count})")
    return 0


def _append_active_context_note(target: Path, rendered: str) -> Path | None:
    session_dir = helpers._active_session_dir(target)
    if session_dir is None:
        return None
    session_json = session_dir / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    notes = payload.setdefault("notes", [])
    if not isinstance(notes, list):
        return None
    entry = {
        "created_at": helpers._now().isoformat(),
        "text": rendered,
    }
    notes.append(entry)
    helpers._write_json(session_json, payload)

    notes_path = session_dir / "notes.md"
    prefix = "" if notes_path.exists() and notes_path.read_text().endswith("\n") else "\n"
    with notes_path.open("a") as handle:
        if notes_path.stat().st_size == 0:
            handle.write("# Brigade Work Session Notes\n")
        else:
            handle.write(prefix)
        handle.write(f"\n## {entry['created_at']}\n\n{rendered}\n")
    return notes_path


def import_context_from_miseledger(
    *,
    target: Path,
    query: str,
    limit: int = 5,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered_query = " ".join(str(query or "").split())
    if not rendered_query:
        print("error: --from-miseledger query is required", file=sys.stderr)
        return 2
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    bundle = evidence_brief.fetch_evidence_bundle(target, rendered_query, limit=limit)
    if bundle is None:
        payload = {
            "attached": False,
            "query": rendered_query,
            "results": [],
            "warnings": ["MiseLedger evidence unavailable; continuing without imported context."],
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("miseledger evidence: unavailable", file=sys.stderr)
            print("persistence: print-only (no evidence brief)", file=sys.stderr)
        return 0

    rendered = evidence_brief.render_evidence_bundle(bundle, limit=limit)
    notes_path = _append_active_context_note(target, rendered) if rendered else None
    if json_output:
        print(json.dumps(bundle, indent=2, sort_keys=True))
    elif rendered:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    else:
        print("miseledger evidence: no results", file=sys.stderr)

    if rendered:
        if notes_path is not None:
            print(f"persisted: {notes_path}", file=sys.stderr)
        else:
            print("persistence: print-only (no active work session)", file=sys.stderr)
    return 0


def import_list(
    *,
    target: Path,
    all_imports: bool = False,
    json_output: bool = False,
    limit: int = 20,
    source: str | None = None,
    kind: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    imports.sort(key=ledger_mod._import_sort_key)
    if not all_imports:
        imports = [item for item in imports if item.get("status", "pending") == "pending"]
    if source:
        imports = [item for item in imports if item.get("source") == source]
    if kind:
        imports = [item for item in imports if item.get("kind") == kind]
    if metadata_filters:
        imports = [item for item in imports if ledger_mod._import_metadata_matches(item, metadata_filters)]
    imports = imports[:limit]

    if json_output:
        print(
            json.dumps(
                {"imports_path": str(helpers._imports_path(target)), "imports": imports}, indent=2, sort_keys=True
            )
        )
        return 0

    print(f"work imports: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    if not imports:
        print("imports: none")
        return 0
    for item in imports:
        status_text = item.get("status", "pending")
        kind = item.get("kind", "task")
        source = item.get("source", "manual")
        print(f"- {item.get('id')} [{status_text}] {kind} from {source}: {helpers._short(str(item.get('text', '')))}")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata:
            rendered = ", ".join(f"{key}={metadata[key]}" for key in sorted(metadata))
            print(f"  metadata: {rendered}")
        if item.get("task_id"):
            print(f"  task: {item['task_id']}")
    return 0


def import_validate(*, input_path: Path, json_output: bool = False) -> int:
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = ledger_mod._load_import_jsonl(path)
    payload = {
        "path": str(path),
        "valid": not errors,
        "records": len(records),
        "errors": errors,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"import file: {path}")
    print(f"records: {len(records)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    print("status: valid")
    return 0


def import_ingest(
    *,
    target: Path,
    input_path: Path,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = ledger_mod._load_import_jsonl(path)
    if errors:
        if json_output:
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "imports_path": str(helpers._imports_path(target)),
                        "valid": False,
                        "errors": errors,
                        "created": 0,
                        "skipped": 0,
                        "dismissed": 0,
                        "invalid": len(errors),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: import file is invalid: {path}", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        return 2

    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "path": str(path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"import file: {path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(
            f"- {item.get('id')} [{item.get('kind')}] {item.get('source')}: {helpers._short(str(item.get('text', '')))}"
        )
    return 0


def import_issue_repairs(
    *,
    target: Path,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = ledger_mod._issue_repair_records(target)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "candidate_count": len(records),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"issue repair imports: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        print(f"- {item.get('id')} {metadata.get('issue_type')}: {helpers._short(str(item.get('text', '')))}")
    return 0


def import_memory_care(
    *,
    target: Path,
    queue: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    return services_mod._import_memory_refresh_queue(
        target=target,
        queue=queue,
        dry_run=dry_run,
        json_output=json_output,
        source="memory-care",
        command_name="memory-care",
    )


def import_memory_refresh(
    *,
    target: Path,
    queue: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    return services_mod._import_memory_refresh_queue(
        target=target,
        queue=queue,
        dry_run=dry_run,
        json_output=json_output,
        source="memory-refresh",
        command_name="memory-refresh",
    )


def import_chat_sweep(
    *,
    target: Path,
    input_path: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    sweep_path = (
        input_path.expanduser().resolve()
        if input_path is not None
        else target / ".brigade" / "chat-memory-sweeps" / "latest.json"
    )
    if not sweep_path.is_file():
        print(f"error: chat memory sweep not found: {sweep_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sweep_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid chat memory sweep JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: chat memory sweep must be an object: {sweep_path}", file=sys.stderr)
        return 2
    records, errors, issue_count = services_mod._chat_sweep_records(payload, sweep_path=sweep_path)
    if errors:
        output = {
            "input": str(sweep_path),
            "imports_path": str(helpers._imports_path(target)),
            "valid": False,
            "errors": errors,
            "created": 0,
            "skipped": 0,
            "dismissed": 0,
            "invalid": len(errors),
        }
        if json_output:
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2

    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "input": str(sweep_path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "valid": True,
        "issues": issue_count,
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"chat memory sweep: {sweep_path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"issues: {issue_count}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def import_content_guard(
    *,
    target: Path,
    scan_target: Path | None = None,
    policy: str = "public-repo",
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    effective_scan_target = scan_target.expanduser().resolve() if scan_target is not None else target
    result = scrub.run_scan(effective_scan_target, repo_target=target, policy=policy)
    records = services_mod._content_guard_import_records(result)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "target": str(target),
        "scan_target": str(effective_scan_target),
        "policy": policy,
        "dry_run": dry_run,
        "scan": {
            "available": result.get("available"),
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "detail": result.get("detail"),
            "stdout_summary": scanners_mod._scanner_run_summary(str(result.get("stdout") or ""), limit=12),
            "stderr_summary": scanners_mod._scanner_run_summary(str(result.get("stderr") or ""), limit=8),
        },
        "imports_path": str(helpers._imports_path(target)),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if result.get("available") else 2
    print(f"content-guard import: {effective_scan_target}")
    print(f"policy: {policy}")
    print(f"scan: {result.get('status')} ({result.get('detail')})")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0 if result.get("available") else 2


def import_triage(
    *,
    target: Path,
    json_output: bool = False,
    limit: int = 50,
    source: str | None = None,
    kind: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pending = ledger_mod._matching_pending_imports(target, kind=kind, source=source, metadata_filters=metadata_filters)
    counts = ledger_mod._import_counts(pending)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in pending:
        source = str(item.get("source") or "manual")
        kind = str(item.get("kind") or "task")
        groups.setdefault(source, {}).setdefault(kind, []).append(item)

    if json_output:
        print(
            json.dumps(
                {
                    "imports_path": str(helpers._imports_path(target)),
                    "counts": counts,
                    "groups": groups,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"work import triage: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"pending_imports: {counts['total']}")
    if not pending:
        return 0
    print("sources:")
    for source, by_kind in sorted(groups.items()):
        source_count = sum(len(items) for items in by_kind.values())
        print(f"- {source}: {source_count}")
        for kind, items in sorted(by_kind.items()):
            print(f"  {kind}: {len(items)}")
            for item in items[:limit]:
                print(f"    - {item.get('id')} {helpers._short(str(item.get('text', '')))}")
            if len(items) > limit:
                print(f"    ... {len(items) - limit} more")
    return 0


def import_provenance(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = services_mod._import_provenance_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work import provenance: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"audited_imports: {payload['audited_import_count']}")
    print(f"complete: {payload['complete_count']}")
    print(f"incomplete: {payload['incomplete_count']}")
    if payload["missing_by_field"]:
        print("missing_by_field:")
        for field, count in payload["missing_by_field"].items():
            print(f"  {field}: {count}")
    if payload["missing_by_source"]:
        print("missing_by_source:")
        for source, count in payload["missing_by_source"].items():
            print(f"  {source}: {count}")
    for item in payload["issues"][:20]:
        fields = ", ".join(str(field) for field in item.get("missing_fields", []))
        print(f"- {item.get('id')} {item.get('source')} {item.get('kind')} missing={fields}")
    return 0


def import_show(*, target: Path, import_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status', 'pending')}")
    print(f"kind: {item.get('kind', '')}")
    print(f"source: {item.get('source', '')}")
    print(f"created_at: {item.get('created_at', '')}")
    print(f"updated_at: {item.get('updated_at', '')}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    if item.get("promoted_at"):
        print(f"promoted_at: {item['promoted_at']}")
    if item.get("task_id"):
        print(f"task: {item['task_id']}")
    if item.get("handoff_path"):
        print(f"handoff: {item['handoff_path']}")
    if item.get("handoff_target_document"):
        print(f"handoff_target_document: {item['handoff_target_document']}")
    print(f"text: {item.get('text', '')}")
    return 0


def import_plan(*, target: Path, import_id: str, json_output: bool = False) -> int:
    payload, rc = services_mod._import_plan_payload(target, import_id)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    item = payload["import"]
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"kind: {item.get('kind')}")
    print(f"source: {item.get('source')}")
    print(f"text: {item.get('text')}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    task = payload.get("task")
    if isinstance(task, dict):
        print("task:")
        print(f"  type: {task.get('type')}")
        print(f"  priority: {task.get('priority')}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
        print(f"  acceptance: {len(acceptance)}")
        for criterion in acceptance:
            print(f"    - {criterion}")
    if payload.get("guidance"):
        print("guidance:")
        for item in payload["guidance"]:
            print(f"  - {item}")
    handoff = payload.get("handoff")
    if isinstance(handoff, dict):
        print("handoff:")
        print(f"  ready: {handoff.get('ready')}")
        print(f"  target_document: {handoff.get('target_document')}")
        print(f"  type: {handoff.get('handoff_type')}")
        blockers = handoff.get("blockers") if isinstance(handoff.get("blockers"), list) else []
        if blockers:
            print("  blockers:")
            for blocker in blockers:
                print(f"    - {blocker}")
    if payload.get("recommended_action"):
        print(f"recommended: {payload['recommended_action']}")
    print(f"promote: {payload['suggested_promote_command']}")
    if payload.get("suggested_promote_handoff_command"):
        print(f"handoff: {payload['suggested_promote_handoff_command']}")
    if payload.get("suggested_run_command"):
        print(f"run: {payload['suggested_run_command']}")
    print(f"dismiss: {payload['suggested_dismiss_command']}")
    return 0


def import_plan_handoff(*, target: Path, import_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    payload = ledger_mod._import_handoff_plan_payload(target, item)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["handoff_ready"] else 2
    source = payload["import"].get("source") if isinstance(payload.get("import"), dict) else item.get("source")
    kind = payload["import"].get("kind") if isinstance(payload.get("import"), dict) else item.get("kind")
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status', 'pending')}")
    print(f"kind: {kind}")
    print(f"source: {source}")
    print(f"text: {ledger_mod._handoff_safe_text(item.get('text') or '')}")
    print(f"handoff_ready: {payload['handoff_ready']}")
    print(f"handoff_inbox: {payload['handoff_inbox']}")
    print(f"target_document: {payload['target_document']}")
    print(f"type: {payload['handoff_type']}")
    if payload["blockers"]:
        print("blockers:")
        for blocker in payload["blockers"]:
            print(f"  - {blocker}")
    print(f"promote_handoff: {payload['suggested_promote_handoff_command']}")
    print(f"dismiss: {payload['suggested_dismiss_command']}")
    return 0 if payload["handoff_ready"] else 2


def import_promote_handoff(
    *,
    target: Path,
    import_id: str,
    run_after: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if run_after:
        if item.get("kind") != "task":
            print(f"error: --run requires a task import: {item.get('id')}", file=sys.stderr)
            return 2
        return import_promote(target=target, import_id=str(item.get("id")), run_after=True)
    payload = ledger_mod._import_handoff_plan_payload(target, item)
    if payload["blockers"]:
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for blocker in payload["blockers"]:
                print(f"error: {blocker}", file=sys.stderr)
        return 2
    target_document = str(payload["target_document"])
    handoff_path = ledger_mod._write_import_handoff(target, item, target_document)
    from .. import handoff_cmd

    lint_result = handoff_cmd.lint_file(handoff_path)
    if not lint_result.valid:
        try:
            handoff_path.unlink()
        except OSError:
            pass
        failure_payload = dict(payload)
        failure_payload.update(
            {
                "handoff_path": str(handoff_path),
                "lint": lint_result.as_dict(),
                "handoff_ready": False,
                "blockers": [*payload["blockers"], *lint_result.errors],
            }
        )
        if json_output:
            print(json.dumps(failure_payload, indent=2, sort_keys=True))
        else:
            for error in lint_result.errors:
                print(f"error: handoff lint failed: {error}", file=sys.stderr)
        return 2
    ledger_mod._mark_import_handoff_promoted(target, item, handoff_path=handoff_path, target_document=target_document)
    ledger_mod._write_imports(target, imports)
    output = dict(payload)
    output.update(
        {
            "handoff_ready": True,
            "handoff_path": str(handoff_path),
            "lint": lint_result.as_dict(),
            "import": ledger_mod._import_summary(item),
        }
    )
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"handoff: {handoff_path}")
    print(f"target_document: {target_document}")
    print("lint: ok")
    return 0


def import_promote(
    *,
    target: Path,
    import_id: str | None = None,
    all_matching: bool = False,
    kind: str | None = None,
    source: str | None = None,
    metadata: list[str] | None = None,
    run_after: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    if all_matching and import_id:
        print("error: pass an import id or --all, not both", file=sys.stderr)
        return 2
    if run_after and all_matching:
        print("error: --run can only be used with one import id", file=sys.stderr)
        return 2
    if all_matching:
        imports = ledger_mod._read_imports(target)
        wanted_ids = {
            item.get("id")
            for item in ledger_mod._matching_pending_imports(
                target,
                kind=kind,
                source=source,
                metadata_filters=metadata_filters,
            )
        }
        promoted: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        for item in imports:
            if item.get("id") not in wanted_ids:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            task, created = ledger_mod._mark_import_promoted(target, item)
            promoted.append((item, task, created))
        ledger_mod._write_imports(target, imports)
        created_count = len([item for item in promoted if item[2]])
        print(f"promoted: {len(promoted)}")
        print(f"created: {created_count}")
        print(f"existing: {len(promoted) - created_count}")
        for item, task, created in promoted:
            status = "created" if created else "existing"
            print(
                f"- {item.get('id')} -> {task['id']} [{status} acceptance={len(ledger_mod._task_acceptance(task))}] "
                f"{helpers._short(str(task.get('text', '')))}"
            )
        return 0
    if not import_id:
        print("error: import id is required unless --all is passed", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if item.get("status", "pending") != "pending":
        print(f"error: import is not pending: {item.get('id')} ({item.get('status')})", file=sys.stderr)
        return 2
    if run_after and item.get("kind") != "task":
        print(f"error: --run requires a task import: {item.get('id')}", file=sys.stderr)
        return 2
    text = str(item.get("text") or "").strip()
    if not text:
        print(f"error: import has no text: {import_id}", file=sys.stderr)
        return 2
    task, created = ledger_mod._mark_import_promoted(target, item)
    ledger_mod._write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"task: {task['id']}")
    print(f"created: {created}")
    print(f"acceptance: {len(ledger_mod._task_acceptance(task))}")
    print(f"text: {task['text']}")
    if run_after:
        print("run: starting")
        from brigade import work_cmd as _facade

        return _facade.run(None, target=target, task_id=str(task["id"]))
    return 0


def import_dismiss(
    *,
    target: Path,
    import_id: str | None = None,
    reason: str | None = None,
    all_matching: bool = False,
    kind: str | None = None,
    source: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    if all_matching and import_id:
        print("error: pass an import id or --all, not both", file=sys.stderr)
        return 2
    if all_matching:
        imports = ledger_mod._read_imports(target)
        wanted_ids = {
            item.get("id")
            for item in ledger_mod._matching_pending_imports(
                target,
                kind=kind,
                source=source,
                metadata_filters=metadata_filters,
            )
        }
        now = helpers._now().isoformat()
        dismissed: list[dict[str, Any]] = []
        for item in imports:
            if item.get("id") not in wanted_ids:
                continue
            item["status"] = "dismissed"
            item["updated_at"] = now
            item["dismissed_at"] = now
            if reason and reason.strip():
                item["dismiss_reason"] = reason.strip()
            dismissed.append(item)
        ledger_mod._write_imports(target, imports)
        print(f"dismissed: {len(dismissed)}")
        if reason and reason.strip():
            print(f"reason: {reason.strip()}")
        for item in dismissed:
            print(f"- {item.get('id')} {helpers._short(str(item.get('text', '')))}")
        return 0
    if not import_id:
        print("error: import id is required unless --all is passed", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if item.get("status", "pending") != "pending":
        print(f"error: import is not pending: {item.get('id')} ({item.get('status')})", file=sys.stderr)
        return 2
    now = helpers._now().isoformat()
    item["status"] = "dismissed"
    item["updated_at"] = now
    item["dismissed_at"] = now
    if reason and reason.strip():
        item["dismiss_reason"] = reason.strip()
    ledger_mod._write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print("status: dismissed")
    if item.get("dismiss_reason"):
        print(f"reason: {item['dismiss_reason']}")
    return 0
