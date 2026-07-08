"""Workflow sequence scanner over local Brigade receipts."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import work_cmd

MAX_EVIDENCE = 10
DEFAULT_DAYS = 30
DEFAULT_MIN_COUNT = 2
DEFAULT_MIN_STEPS = 1


def _workflow_root(target: Path) -> Path:
    return target / ".brigade" / "workflow"


def _default_json_path(target: Path) -> Path:
    return _workflow_root(target) / "latest.json"


def _default_markdown_path(target: Path) -> Path:
    return _workflow_root(target) / "latest.md"


def _workshop_root(target: Path) -> Path:
    return _workflow_root(target) / "workshop"


def _short(value: object, limit: int = 180) -> str:
    rendered = " ".join(str(value or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _read_json(path: Path) -> dict[str, Any] | None:
    payload = work_cmd._read_json(path)
    return payload if isinstance(payload, dict) else None


def _read_json_counting(path: Path) -> tuple[dict[str, Any] | None, bool]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, True
    return (payload, False) if isinstance(payload, dict) else (None, True)


def _relative(target: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(target))
    except ValueError:
        return str(path)


# Volatile tokens are replaced with fixed placeholders so the same workflow
# observed across runs hashes to the same pattern_key. Order matters: run-id
# shapes must win over the bare-hex rule, and path collapsing runs last so it
# swallows any placeholders already substituted inside a path.
_TEMPLATE_PATTERNS = (
    (re.compile(r"\b\d{8}-\d{6}-work-verify-[0-9a-f]{6}\b"), "<run-id>"),
    (re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6,}\b"), "<run-id>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<hex>"),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
        "<date>",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"(?:/tmp|" + re.escape(str(Path.home())) + r")/\S+"), "<path>"),
)


def _concrete_command(command: object) -> str:
    return " ".join(str(command or "").split())


def _normalize_command(command: object) -> str:
    text = _concrete_command(command)
    for pattern, placeholder in _TEMPLATE_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def _path_token(value: object) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("._-")
    return token or "workflow-candidate"


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return Path(parts[0]).name if parts else ""


def _sha256_hex(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _candidate_import_fingerprint(item: dict[str, Any]) -> str:
    stable = {
        "id": item.get("id"),
        "sequence": item.get("sequence"),
        "suggested_runbook_id": item.get("suggested_runbook_id"),
    }
    return _sha256_hex(json.dumps(stable, sort_keys=True, separators=(",", ":")), 16)


def _parse_started_at(value: object):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return work_cmd._parse_iso_datetime(value)
    except ValueError:
        return None


def _within_days(receipt: dict[str, Any], *, days: int) -> bool:
    started = _parse_started_at(receipt.get("started_at"))
    if started is None:
        return True
    cutoff = work_cmd._now() - timedelta(days=days)
    return started >= cutoff


def _iter_verify_receipts(
    target: Path, *, max_files: int | None = None
) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    root = work_cmd._verify_runs_root(target)
    if not root.is_dir():
        return [], 0
    receipts: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    children = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda path: path.name, reverse=True)
    if max_files is not None:
        children = children[:max_files]
    for child in children:
        if not child.is_dir():
            continue
        path = child / "receipt.json"
        if not path.is_file():
            continue
        payload, bad_json = _read_json_counting(path)
        if bad_json:
            skipped += 1
        if payload is None:
            continue
        payload.setdefault("path", str(child))
        receipts.append((path, payload))
    receipts.sort(key=lambda item: str(item[1].get("started_at") or item[1].get("run_id") or ""), reverse=True)
    return receipts, skipped


def _iter_daily_runs(target: Path, *, max_files: int | None = None) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    root = target / ".brigade" / "daily" / "runs"
    if not root.is_dir():
        return [], 0
    receipts: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    children = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda path: path.name, reverse=True)
    if max_files is not None:
        children = children[:max_files]
    for child in children:
        if not child.is_dir():
            continue
        path = child / "run.json"
        if not path.is_file():
            continue
        payload, bad_json = _read_json_counting(path)
        if bad_json:
            skipped += 1
        if payload is None:
            continue
        payload.setdefault("path", str(child))
        receipts.append((path, payload))
    receipts.sort(key=lambda item: str(item[1].get("started_at") or item[1].get("run_id") or ""), reverse=True)
    return receipts, skipped


def _command_from_row(row: object) -> str:
    if isinstance(row, dict):
        command = row.get("command")
        if isinstance(command, str) and command.strip():
            return _concrete_command(command)
        argv = row.get("argv")
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            return _concrete_command(shlex.join(argv))
        return ""
    return _concrete_command(row)


def _commands_from_rows(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    commands: list[str] = []
    for row in rows:
        command = _command_from_row(row)
        if command:
            commands.append(command)
    return commands


def _sequence_pattern_key(sequence: list[str]) -> str:
    return _sha256_hex("\n".join(sequence), 12)


def _verify_observations(
    target: Path, *, days: int, min_steps: int, max_files: int | None
) -> tuple[list[dict[str, Any]], int, int]:
    observations: list[dict[str, Any]] = []
    receipts, skipped = _iter_verify_receipts(target, max_files=max_files)
    for path, receipt in receipts:
        if not _within_days(receipt, days=days):
            continue
        run_id = str(receipt.get("run_id") or path.parent.name)
        concrete = _commands_from_rows(receipt.get("commands"))
        if len(concrete) < min_steps:
            continue
        status = str(receipt.get("status") or "")
        rows = receipt.get("commands") if isinstance(receipt.get("commands"), list) else []
        all_exit_zero = all(
            int(row.get("exit_code") or 0) == 0 for row in rows if isinstance(row, dict)
        )
        observations.append(
            {
                "source": "verify",
                "run_id": run_id,
                "started_at": receipt.get("started_at"),
                "path": _relative(target, path),
                "sequence": [_normalize_command(command) for command in concrete],
                "example_commands": concrete,
                "ends_in_verify_pass": status == "completed" and all_exit_zero,
                "status": status,
            }
        )
    return observations, len(receipts), skipped


def _daily_observations(
    target: Path, *, days: int, min_steps: int, max_files: int | None
) -> tuple[list[dict[str, Any]], int, int]:
    observations: list[dict[str, Any]] = []
    receipts, skipped = _iter_daily_runs(target, max_files=max_files)
    for path, receipt in receipts:
        if not _within_days(receipt, days=days):
            continue
        concrete = _commands_from_rows(receipt.get("commands_invoked"))
        if len(concrete) < min_steps:
            continue
        status = str(receipt.get("status") or "")
        observations.append(
            {
                "source": "daily",
                "run_id": str(receipt.get("run_id") or path.parent.name),
                "started_at": receipt.get("started_at"),
                "path": _relative(target, path),
                "sequence": [_normalize_command(command) for command in concrete],
                "example_commands": concrete,
                "ends_in_verify_pass": False,
                "status": status,
            }
        )
    return observations, len(receipts), skipped


def _source_sort_key(source: str) -> tuple[int, str]:
    order = {"verify": 0, "daily": 1}
    return order.get(source, 99), source


def _candidate_from_group(pattern_key: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    observations.sort(key=lambda item: str(item.get("started_at") or ""))
    first = observations[0]
    latest = observations[-1]
    candidate_id = f"workflow-{pattern_key}"
    sequence = list(first.get("sequence") if isinstance(first.get("sequence"), list) else [])
    sources = sorted({str(item.get("source")) for item in observations}, key=_source_sort_key)
    evidence = [
        {
            "source": item.get("source"),
            "path": item.get("path"),
            "run_id": item.get("run_id"),
            "started_at": item.get("started_at"),
            "status": item.get("status"),
            "command_count": len(item.get("sequence") if isinstance(item.get("sequence"), list) else []),
        }
        for item in observations[-MAX_EVIDENCE:]
    ]
    example_commands = _example_commands_from_observations(observations)
    ends_in_verify_pass = any(bool(item.get("ends_in_verify_pass")) for item in observations)
    return {
        "id": candidate_id,
        "pattern_key": pattern_key,
        "sequence": sequence,
        "example_commands": example_commands,
        "occurrence_count": len(observations),
        "session_count": len({f"{item.get('source')}:{item.get('run_id')}" for item in observations}),
        "sources": sources,
        "ends_in_verify_pass": ends_in_verify_pass,
        "first_seen": first.get("started_at"),
        "last_seen": latest.get("started_at"),
        "suggested_runbook_id": candidate_id,
        "evidence": evidence,
        "review_risk": _review_risk(example_commands),
        "suggested_next_command": f"brigade workflow propose-runbook {candidate_id}",
    }


def _example_commands_from_observations(observations: list[dict[str, Any]]) -> list[str]:
    # Concrete commands from the most recent observation, never templates:
    # propose-runbook turns these into runnable steps.
    for item in reversed(observations):
        values = item.get("example_commands")
        if isinstance(values, list) and values:
            return [str(value) for value in values]
    return []


def _review_risk(commands: list[str]) -> str:
    from . import runbook_cmd

    for command in commands:
        if any(pattern.search(command) for pattern in runbook_cmd.DANGEROUS_PATTERNS):
            return "high"
    return "normal"


def _group_candidates(observations: list[dict[str, Any]], *, min_count: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        sequence = item.get("sequence") if isinstance(item.get("sequence"), list) else []
        grouped.setdefault(_sequence_pattern_key([str(command) for command in sequence]), []).append(item)
    candidates = [_candidate_from_group(key, items) for key, items in grouped.items()]
    candidates = [item for item in candidates if int(item.get("occurrence_count") or 0) >= min_count]
    candidates.sort(key=lambda item: (-int(item.get("occurrence_count") or 0), str(item.get("id") or "")))
    return candidates


def scan_payload(
    *,
    target: Path,
    days: int = DEFAULT_DAYS,
    min_count: int = DEFAULT_MIN_COUNT,
    min_steps: int = DEFAULT_MIN_STEPS,
    max_files: int | None = None,
) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    if days < 1:
        print("error: --days must be a positive integer", file=sys.stderr)
        return None, 2
    if min_count < 1:
        print("error: --min-count must be at least 1", file=sys.stderr)
        return None, 2
    if min_steps < 1:
        print("error: --min-steps must be at least 1", file=sys.stderr)
        return None, 2
    if max_files is not None and max_files < 1:
        print("error: --max-files must be a positive integer", file=sys.stderr)
        return None, 2
    verify_observations, verify_count, skipped_verify = _verify_observations(
        target, days=days, min_steps=min_steps, max_files=max_files
    )
    daily_observations, daily_count, skipped_daily = _daily_observations(
        target, days=days, min_steps=min_steps, max_files=max_files
    )
    observations = [*verify_observations, *daily_observations]
    candidates = _group_candidates(observations, min_count=min_count)
    source_counts: dict[str, int] = {}
    for item in candidates:
        for source in item.get("sources") if isinstance(item.get("sources"), list) else []:
            source_counts[str(source)] = source_counts.get(str(source), 0) + 1
    payload = {
        "version": 1,
        "generated_at": work_cmd._now().isoformat(),
        "target": str(target),
        "days": days,
        "min_count": min_count,
        "min_steps": min_steps,
        "max_files": max_files,
        "receipt_counts": {
            "verify": verify_count,
            "daily": daily_count,
        },
        "skipped_bad_json_count": skipped_verify + skipped_daily,
        "observation_count": len(observations),
        "candidate_count": len(candidates),
        "counts": {
            "by_source": dict(sorted(source_counts.items())),
        },
        "candidates": candidates,
    }
    return payload, 0


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Brigade Workflow Scan",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Target: `{payload.get('target')}`",
        f"- Candidates: {payload.get('candidate_count')}",
        f"- Observations: {payload.get('observation_count')}",
        "",
        "## Counts",
        "",
    ]
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    for label, key in (("Source", "by_source"),):
        lines.append(f"### {label}")
        values = counts.get(key) if isinstance(counts, dict) else {}
        if isinstance(values, dict) and values:
            lines.extend(f"- {name}: {count}" for name, count in values.items())
        else:
            lines.append("- none")
        lines.append("")
    lines.append("## Candidate Workflows")
    lines.append("")
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    if not candidates:
        lines.append("No workflow candidates found.")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"### {item.get('id')}",
                "",
                f"- Pattern: {item.get('pattern_key')}",
                f"- Sources: {', '.join(str(source) for source in item.get('sources', []))}",
                f"- Occurrences: {item.get('occurrence_count')}",
                f"- Sessions: {item.get('session_count')}",
                f"- Ends in verify pass: {item.get('ends_in_verify_pass')}",
                f"- Suggested runbook: {item.get('suggested_runbook_id')}",
                "",
            ]
        )
        sequence = item.get("sequence") if isinstance(item.get("sequence"), list) else []
        for command in sequence:
            lines.append(f"  - `{command}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(payload))


def _import_candidates(target: Path, candidates: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, int, int]:
    records: list[dict[str, Any]] = []
    for item in candidates:
        candidate_id = str(item.get("id") or "workflow-candidate")
        sequence = item.get("sequence") if isinstance(item.get("sequence"), list) else []
        preview = " && ".join(str(command) for command in sequence[:3])
        records.append(
            {
                "kind": "task",
                "source": "workflow-scan",
                "text": f"Review workflow sequence candidate {candidate_id}: {_short(preview or candidate_id)}",
                "type": "workflow",
                "priority": "normal",
                "template": "red-green-refactor",
                "acceptance": [
                    "Review the grouped local receipts and decide whether the sequence should become a runbook.",
                    "Use `brigade workflow propose-runbook` only after reviewing the concrete example commands.",
                    "No runbook is generated automatically.",
                ],
                "metadata": {
                    "source_item_key": candidate_id,
                    "source_fingerprint": _candidate_import_fingerprint(item),
                    "workflow_id": candidate_id,
                    "pattern_key": item.get("pattern_key"),
                    "suggested_runbook_id": item.get("suggested_runbook_id"),
                    "sources": item.get("sources"),
                    "review_risk": item.get("review_risk"),
                },
            }
        )
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    return len(imported), len(skipped), len(skipped_dismissed)


def scan(
    *,
    target: Path,
    days: int = DEFAULT_DAYS,
    min_count: int = DEFAULT_MIN_COUNT,
    min_steps: int = DEFAULT_MIN_STEPS,
    max_files: int | None = None,
    import_candidates: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    payload, code = scan_payload(
        target=target, days=days, min_count=min_count, min_steps=min_steps, max_files=max_files
    )
    if payload is None:
        return code
    target = target.expanduser().resolve()
    json_path = _default_json_path(target)
    markdown_path = _default_markdown_path(target)
    if not dry_run:
        _write_payload(json_path, payload)
        _write_markdown(markdown_path, payload)
    imported = 0
    skipped = 0
    skipped_dismissed = 0
    if import_candidates:
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        imported, skipped, skipped_dismissed = _import_candidates(target, candidates, dry_run=dry_run)
    payload["output"] = {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "imports_added": imported,
        "imports_skipped": skipped,
        "imports_skipped_dismissed": skipped_dismissed,
        "dry_run": dry_run,
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return 0
    print("workflow scan:")
    print(f"  target: {payload['target']}")
    print(f"  candidates: {payload['candidate_count']}")
    print(f"  observations: {payload['observation_count']}")
    print(f"  skipped_bad_json: {payload['skipped_bad_json_count']}")
    print(f"  json: {json_path if not dry_run else '(dry-run) ' + str(json_path)}")
    print(f"  markdown: {markdown_path if not dry_run else '(dry-run) ' + str(markdown_path)}")
    if import_candidates:
        print(f"  imports_added: {imported}")
        print(f"  imports_skipped: {skipped}")
        print(f"  imports_skipped_dismissed: {skipped_dismissed}")
    counts = payload.get("counts", {})
    by_source = counts.get("by_source") if isinstance(counts, dict) else {}
    if by_source:
        print("  by_source:")
        for name, count in sorted(by_source.items()):
            print(f"    {name}: {count}")
    return 0


def show(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _default_json_path(target)
    data = _read_json(path)
    if data is None:
        print(f"error: no workflow scan found at {path}; run `brigade workflow scan` first", file=sys.stderr)
        return 2
    candidates_value = data.get("candidates")
    candidates = candidates_value if isinstance(candidates_value, list) else []
    payload = {
        "source": str(path),
        "generated_at": data.get("generated_at"),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"workflow show: {path}")
    print(f"candidates: {len(candidates)}")
    for item in candidates:
        if isinstance(item, dict):
            print(
                f"- [{item.get('review_risk', '?')}] {item.get('id', '?')} "
                f"x{item.get('occurrence_count', 0)}: {item.get('suggested_next_command', '')}"
            )
    return 0


def _latest_candidates(target: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    path = _default_json_path(target)
    data = _read_json(path)
    if data is None:
        return None, f"no workflow scan found at {path}; run `brigade workflow scan` first"
    candidates_value = data.get("candidates")
    if not isinstance(candidates_value, list):
        return None, f"workflow scan has invalid candidates at {path}"
    return [item for item in candidates_value if isinstance(item, dict)], None


def _resolve_workflow_candidate(target: Path, candidate_id: str) -> tuple[dict[str, Any] | None, str | None]:
    candidates, error = _latest_candidates(target)
    if candidates is None:
        return None, error
    needle = candidate_id.strip()
    matches = [
        item
        for item in candidates
        if needle and (str(item.get("id") or "") == needle or str(item.get("id") or "").startswith(needle))
    ]
    if not matches:
        return None, f"workflow candidate not found: {candidate_id}"
    if len(matches) > 1:
        return None, f"workflow candidate id is ambiguous: {candidate_id}"
    return matches[0], None


def _runbook_commands(candidate: dict[str, Any]) -> list[str]:
    values = candidate.get("example_commands")
    commands: list[str] = []
    if isinstance(values, list):
        for value in values:
            command = " ".join(str(value or "").split())
            if command and command not in commands:
                commands.append(command)
    return commands


def _runbook_payload(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    commands = _runbook_commands(candidate)
    candidate_id = str(candidate.get("id") or "workflow-candidate")
    runbook_id = str(candidate.get("suggested_runbook_id") or candidate_id).strip() or candidate_id
    if not commands:
        return None, f"workflow candidate has no example_commands: {candidate_id}"
    allowed_commands: set[str] = set()
    for command in commands:
        name = _command_name(command)
        if not name:
            return None, f"workflow candidate has an unparsable example command: {candidate_id}"
        allowed_commands.add(name)
    sources = candidate.get("sources") if isinstance(candidate.get("sources"), list) else []
    provenance = ", ".join(str(source) for source in sources) or "unknown"
    payload: dict[str, Any] = {
        "id": runbook_id,
        "description": f"Runbook proposed from workflow scan candidate {candidate_id}, sources: {provenance}.",
        "approved": False,
        "allowed_commands": sorted(allowed_commands),
        "pins": [],
        "steps": [
            {"id": f"step-{index}", "run": command, "timeout_seconds": 600}
            for index, command in enumerate(commands, start=1)
        ],
    }
    return payload, None


def _planned_runbook_path(target: Path, candidate: dict[str, Any]) -> Path:
    return (
        _workshop_root(target)
        / _path_token(candidate.get("suggested_runbook_id") or candidate.get("id"))
        / "runbook.json"
    )


def _plan_generated_runbook(
    target: Path, runbook_payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    # Always validate the freshly generated payload via a temp file, never a
    # stale runbook.json already on disk at the destination path.
    from . import runbook_cmd

    with tempfile.TemporaryDirectory(prefix="brigade-workflow-runbook-") as raw_tmp:
        temp_path = Path(raw_tmp) / "runbook.json"
        temp_path.write_text(json.dumps(runbook_payload, indent=2) + "\n")
        return runbook_cmd._plan_payload(target, temp_path)


def propose_runbook(*, target: Path, candidate_id: str, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_workflow_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    runbook_payload, runbook_error = _runbook_payload(candidate)
    if runbook_payload is None:
        print(f"error: {runbook_error}", file=sys.stderr)
        return 2
    runbook_path = _planned_runbook_path(target, candidate)
    plan_payload, plan_error = _plan_generated_runbook(target, runbook_payload)
    if plan_payload is None:
        print(f"error: {plan_error}", file=sys.stderr)
        return 2
    plan_payload["runbook_path"] = str(runbook_path)
    if not plan_payload.get("policy_valid"):
        failures = [
            f"{item.get('step')}: {'; '.join(item.get('failures') or [])}"
            for item in plan_payload.get("policy_failures") or []
        ]
        detail = "; ".join(failures) or "runbook plan policy validation failed"
        print(f"error: generated runbook fails runbook plan policy validation: {detail}", file=sys.stderr)
        return 1
    if not dry_run:
        _write_payload(runbook_path, runbook_payload)
    payload = {
        "target": str(target),
        "candidate": candidate,
        "dry_run": dry_run,
        "runbook_path": str(runbook_path),
        "runbook": runbook_payload,
        "plan": plan_payload,
        "manual_only": True,
        "auto_execute": False,
        "would_write": [] if not dry_run else [str(runbook_path)],
        "next_commands": [
            f"brigade runbook plan {runbook_path} --target {target}",
            f"brigade runbook run {runbook_path} --target {target} --approved --dry-run",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return 0
    label = "workflow propose-runbook dry-run" if dry_run else "workflow propose-runbook"
    print(f"{label}: {candidate.get('id')}")
    print(f"runbook: {runbook_path}")
    print(f"policy_valid: {plan_payload['policy_valid']}")
    if dry_run:
        print("status: no files written")
    else:
        print("status: pending review")
    print(f"next: brigade runbook plan {runbook_path} --target {target}")
    return 0
