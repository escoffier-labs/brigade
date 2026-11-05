"""Beads (bd) adapter: mirror bd issues and edges into the ledger, write outcomes out.

Brigade only reads bd. It runs exactly ``bd version --json`` and ``bd export``
(stdout), or reads a saved export file. It never runs a bd command that
changes state and never writes inside a ``.beads`` directory. The outcomes
export writes one JSONL file that bd users can attach or import themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import evidence_redaction
from . import claiming, constants, edges, helpers, ledger
from .import_bd import PRIORITY_BY_BD, BdImportError, _invalid, _validated_rows, check_bd_version, issue_text

MIRROR_SOURCE = "bd"
OUTCOMES_SCHEMA = "brigade.bd-outcomes.v1"
SYNC_RECEIPT_SCHEMA = "brigade.bd-sync.v1"
# The only bd invocations Brigade may make. Anything else is refused before exec.
READ_ONLY_BD_COMMANDS: tuple[tuple[str, ...], ...] = (("version", "--json"), ("export",))
BD_TIMEOUT_SECONDS = 120
# bd stored statuses. Custom "done" statuses are not mapped and fail closed.
BD_STATUS_TO_TASK = {
    "open": "pending",
    "closed": "done",
    "deferred": "deferred",
    "in_progress": "blocked",
    "blocked": "blocked",
    "hooked": "blocked",
    "pinned": "blocked",
}
_HELD_STATUSES = frozenset({"in_progress"}) | edges.TERMINAL_TASK_STATUSES
_GITHUB_REMOTE = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z"
)
_PR_SUBJECT = re.compile(r"\(#([0-9]+)\)\s*\Z|^Merge pull request #([0-9]+)\b")


def _run_bd(bd_bin: str, args: tuple[str, ...], *, cwd: Path) -> bytes:
    """Run one allowlisted read-only bd command and return its stdout."""
    if args not in READ_ONLY_BD_COMMANDS:
        raise BdImportError(
            f"refusing to run 'bd {' '.join(args)}': Brigade runs only read-only bd commands",
            reason="bd_command_not_allowed",
            details={"argv": list(args)},
        )
    try:
        completed = subprocess.run(
            [bd_bin, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=BD_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except FileNotFoundError as exc:
        raise _bd_missing(bd_bin) from exc
    except subprocess.TimeoutExpired as exc:
        raise BdImportError(f"bd {args[0]} did not finish within {BD_TIMEOUT_SECONDS}s", reason="bd_timeout") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BdImportError(
            f"bd {args[0]} exited {completed.returncode}: {stderr[-1] if stderr else 'no error output'}",
            reason="bd_command_failed",
            details={"argv": list(args), "returncode": completed.returncode},
        )
    return completed.stdout


def _bd_missing(bd_bin: str) -> BdImportError:
    return BdImportError(
        f"bd (Beads) was not found: {bd_bin}. Install bd, pass --bd-bin PATH, "
        "or import a saved export with --from-jsonl FILE --bd-version X.Y.Z.",
        reason="bd_not_found",
    )


def detect_bd_version(bd_bin: str, *, cwd: Path) -> str:
    raw = _run_bd(bd_bin, ("version", "--json"), cwd=cwd)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BdImportError("bd version --json did not return JSON", reason="unsupported_bd_schema") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("version"), str)
    ):
        raise BdImportError(
            "bd version --json output has an unknown shape; this bd release is not supported",
            reason="unsupported_bd_schema",
        )
    return check_bd_version(payload["version"])


def _read_export(
    *, bd_repo: Path, bd_bin: str, input_path: Path | None, bd_version: str | None
) -> tuple[bytes, str, dict[str, Any]]:
    limit = ledger._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES
    if input_path is not None:
        if not bd_version:
            raise BdImportError("--from-jsonl requires --bd-version", reason="invalid_bd_version")
        version = check_bd_version(bd_version)
        try:
            with input_path.expanduser().open("rb") as handle:
                raw = handle.read(limit + 1)
        except OSError as exc:
            raise BdImportError(f"bd export is unreadable: {exc}", reason="bd_import_io_error") from exc
        origin: dict[str, Any] = {"kind": "file", "path": str(input_path)}
    else:
        if bd_version:
            raise BdImportError("--bd-version applies only to --from-jsonl", reason="invalid_bd_version")
        resolved = shutil.which(bd_bin)
        if resolved is None:
            raise _bd_missing(bd_bin)
        # which() keeps a relative name relative; resolve it before running in bd_repo.
        resolved = os.path.abspath(resolved)
        if not bd_repo.is_dir():
            raise BdImportError(f"--bd-repo is not a directory: {bd_repo}", reason="bd_import_io_error")
        version = detect_bd_version(resolved, cwd=bd_repo)
        raw = _run_bd(resolved, ("export",), cwd=bd_repo)
        origin = {"kind": "bd", "command": "bd export", "repo": str(bd_repo)}
    if len(raw) > limit:
        raise BdImportError("bd export exceeds the 4 MiB import limit", reason="bd_snapshot_too_large")
    return raw, version, origin


def _redacted(value: str, *, origin: str) -> str:
    persisted, _record, failed = evidence_redaction.apply_and_record(value, origin=origin)
    if failed:
        raise BdImportError("bd text redaction failed; nothing was written", reason="bd_redaction_failed")
    return persisted


def _desired_tasks(raw: bytes) -> dict[str, dict[str, Any]]:
    """Map each bd id to the task content and owned edges the mirror should hold."""
    origin = evidence_redaction.classify_source_origin("bd-jsonl")
    desired: dict[str, dict[str, Any]] = {}
    for line, row, data, proposals in _validated_rows(raw):
        status = row["status"]
        if status not in BD_STATUS_TO_TASK:
            raise _invalid(
                f"bd status {status!r} is not supported (expected one of: {', '.join(sorted(BD_STATUS_TO_TASK))})",
                line=line,
            )
        key = row["id"]
        text = issue_text(row)
        acceptance = [row["acceptance_criteria"]] if row.get("acceptance_criteria") else []
        content = {
            "text": _redacted(text, origin=origin),
            "type": row["issue_type"] if row["issue_type"] in constants.TASK_TYPES else "task",
            "priority": PRIORITY_BY_BD[row["priority"]],
            "acceptance": [_redacted(item, origin=origin) for item in acceptance],
        }
        desired[key] = {
            "content": content,
            "content_digest": helpers._stable_hash(content),
            "bd_status": status,
            "issue_type": row["issue_type"],
            "row_digest": hashlib.sha256(data).hexdigest(),
            "edges": proposals,
        }
    return desired


def _mirror_index(tasks: list[Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or task.get("source") != MIRROR_SOURCE:
            continue
        meta = task.get("metadata")
        bd_meta = meta.get("bd") if isinstance(meta, dict) else None
        bd_id = bd_meta.get("id") if isinstance(bd_meta, dict) else None
        if not isinstance(bd_id, str):
            continue
        if bd_id in index:
            raise BdImportError(
                f"bd issue {bd_id} is mirrored by more than one task; resolve the duplicate before syncing",
                reason="bd_duplicate_mirror",
                details={"bd_id": bd_id, "task_ids": [index[bd_id].get("id"), task.get("id")]},
            )
        index[bd_id] = task
    return index


def _bd_meta(task: dict[str, Any]) -> dict[str, Any]:
    meta = task.setdefault("metadata", {})
    return meta.setdefault("bd", {})


def _apply_mirror(
    ledger_payload: dict[str, Any], desired: dict[str, dict[str, Any]], *, bd_version: str, input_digest: str
) -> dict[str, Any]:
    """Mutate ``ledger_payload`` in place. Raises before returning on any edge or cycle error."""
    now = helpers._now().isoformat()
    mirrors = _mirror_index(ledger_payload["tasks"])
    summary: dict[str, list[Any]] = {
        "created": [],
        "updated": [],
        "status_changed": [],
        "kept_brigade_state": [],
        "edges_added": [],
        "edges_removed": [],
    }
    touched: set[str] = set()

    def stamp(task: dict[str, Any], spec: dict[str, Any], bd_id: str, *, content_applied: bool = False) -> None:
        """Record what bd said. The content digest moves only when that content was applied."""
        bd_meta = _bd_meta(task)
        bd_meta.update(
            {
                "id": bd_id,
                "version": bd_version,
                "row_digest": spec["row_digest"],
                "input_digest": input_digest,
                "status": spec["bd_status"],
                "issue_type": spec["issue_type"],
                "synced_at": now,
            }
        )
        if content_applied:
            bd_meta["content_digest"] = spec["content_digest"]
        task["updated_at"] = now
        touched.add(bd_id)

    for bd_id, spec in desired.items():
        mapped = BD_STATUS_TO_TASK[spec["bd_status"]]
        task = mirrors.get(bd_id)
        if task is None:
            content = spec["content"]
            task = ledger._make_task(
                content["text"],
                source=MIRROR_SOURCE,
                task_type=content["type"],
                priority=content["priority"],
                acceptance=content["acceptance"],
            )
            task["status"] = mapped
            if mapped == "done":
                task["completed_at"] = now
            stamp(task, spec, bd_id, content_applied=True)
            _bd_meta(task).update({"applied_status": mapped, "edges": []})
            ledger_payload["tasks"].append(task)
            mirrors[bd_id] = task
            summary["created"].append({"bd_id": bd_id, "task_id": task["id"], "status": mapped})
            continue
        bd_meta = _bd_meta(task)
        current = str(task.get("status") or "pending")
        if current != mapped:
            # Only move a status the mirror set itself; Brigade-side progress wins.
            if current == bd_meta.get("applied_status") and claiming.claim_record(task) is None:
                task["status"] = mapped
                if mapped == "done":
                    task["completed_at"] = now
                else:
                    # A reopen in bd: the task is no longer complete.
                    task.pop("completed_at", None)
                bd_meta["applied_status"] = mapped
                summary["status_changed"].append({"bd_id": bd_id, "task_id": task["id"], "from": current, "to": mapped})
                stamp(task, spec, bd_id)
                current = mapped
            else:
                summary["kept_brigade_state"].append(
                    {
                        "bd_id": bd_id,
                        "task_id": task["id"],
                        "field": "status",
                        "brigade_status": current,
                        "bd_status": spec["bd_status"],
                    }
                )
        elif bd_meta.get("status") != spec["bd_status"]:
            # bd caught up with a status the mirror did not apply. Record what bd
            # said, but leave applied_status alone: matching statuses do not
            # transfer ownership of Brigade-side progress to the mirror.
            stamp(task, spec, bd_id)
        # Content goes after the status move, so a reopen that makes the task
        # editable also receives the content held back while it was terminal.
        if bd_meta.get("content_digest") != spec["content_digest"]:
            if current in _HELD_STATUSES:
                summary["kept_brigade_state"].append(
                    {"bd_id": bd_id, "task_id": task["id"], "field": "content", "brigade_status": current}
                )
            else:
                task.update(spec["content"])
                summary["updated"].append({"bd_id": bd_id, "task_id": task["id"]})
                stamp(task, spec, bd_id, content_applied=True)

    def task_edge(edge: dict[str, str]) -> tuple[str, str, str]:
        return (edge["type"], str(mirrors[edge["source"]]["id"]), str(mirrors[edge["target"]]["id"]))

    # Remove every owned edge bd dropped before adding new ones, so a reversed
    # dependency is not mistaken for a cycle.
    wanted_by_owner = {bd_id: {task_edge(edge) for edge in spec["edges"]} for bd_id, spec in desired.items()}
    live_ids = {edge.get("id") for edge in edges.ensure_ledger_edges(ledger_payload)}
    for bd_id, wanted in wanted_by_owner.items():
        bd_meta = _bd_meta(mirrors[bd_id])
        kept: list[dict[str, str]] = []
        for owned in bd_meta.get("edges") or []:
            identity = (owned.get("type"), owned.get("source"), owned.get("target"))
            if owned.get("id") not in live_ids:
                # Removed by hand: forget ownership so a still-wanted edge is re-added.
                touched.add(bd_id)
                continue
            if identity in wanted:
                kept.append(owned)
                continue
            edges.remove_edge_from_ledger(ledger_payload, edge_id=owned.get("id"))
            summary["edges_removed"].append({"bd_id": bd_id, **owned})
            touched.add(bd_id)
        bd_meta["edges"] = kept
    for bd_id, wanted in wanted_by_owner.items():
        bd_meta = _bd_meta(mirrors[bd_id])
        owned_identities = {(item["type"], item["source"], item["target"]) for item in bd_meta["edges"]}
        for edge_type, source, target in sorted(wanted - owned_identities):
            try:
                edge, created = edges.add_edge_to_ledger(
                    ledger_payload, source=source, target=target, edge_type=edge_type
                )
            except edges.EdgeError as exc:
                raise BdImportError(str(exc), reason=exc.reason, details=exc.details) from exc
            if created:
                # An identical manual edge stays manual, so a later bd removal never deletes it.
                record = {"id": edge["id"], "type": edge_type, "source": source, "target": target}
                bd_meta["edges"].append(record)
                summary["edges_added"].append({"bd_id": bd_id, **record})
                touched.add(bd_id)
    cycles = edges.find_readiness_cycles(edges.ensure_ledger_edges(ledger_payload))
    if cycles:
        raise BdImportError(
            "dependency cycle rejected after bd sync", reason=edges.REASON_DEPENDENCY_CYCLE, details={"cycles": cycles}
        )
    summary["changed"] = sorted(touched)
    return summary


def _write_sync_receipt(target: Path, payload: dict[str, Any]) -> Path:
    path = helpers._work_root(target) / "bd-sync" / f"{payload['run_id']}.json"
    helpers._write_json(path, payload)
    return path


def sync_bd(
    *,
    target: Path,
    bd_repo: Path | None = None,
    bd_bin: str = "bd",
    input_path: Path | None = None,
    bd_version: str | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    """Mirror a bd export into the task ledger, keyed by bd issue id."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise BdImportError(f"--target is not a directory: {target}", reason="bd_import_io_error")
    repo = (bd_repo or target).expanduser().resolve()
    raw, version, origin = _read_export(bd_repo=repo, bd_bin=bd_bin, input_path=input_path, bd_version=bd_version)
    input_digest = hashlib.sha256(raw).hexdigest()
    desired = _desired_tasks(raw)
    if dry_run:
        # A preview takes no lock: the lock file alone would create .brigade/.
        payload = ledger._read_task_ledger(target)
        summary = _apply_mirror(payload, desired, bd_version=version, input_digest=input_digest)
    else:
        with ledger._task_ledger_lock(target):
            payload = ledger._read_task_ledger(target)
            summary = _apply_mirror(payload, desired, bd_version=version, input_digest=input_digest)
            if summary["changed"]:
                ledger._write_task_ledger(target, payload)
    resolution = edges.resolve_readiness(payload)
    mirrored = {str(task["id"]) for task in _mirror_index(payload["tasks"]).values()}
    run_id = f"{helpers._now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    result: dict[str, Any] = {
        "schema": SYNC_RECEIPT_SCHEMA,
        "run_id": run_id,
        "dry_run": dry_run,
        "bd_version": version,
        "origin": origin,
        "input_digest": input_digest,
        "issues": len(desired),
        "created": summary["created"],
        "updated": summary["updated"],
        "status_changed": summary["status_changed"],
        "kept_brigade_state": summary["kept_brigade_state"],
        "edges_added": summary["edges_added"],
        "edges_removed": summary["edges_removed"],
        "unchanged": len(desired) - len(summary["changed"]),
        "ready": sorted(item["id"] for item in resolution.ready if item["id"] in mirrored),
        "blocked": sorted(item["id"] for item in resolution.blocked if item["id"] in mirrored),
    }
    if not dry_run:
        result["receipt_path"] = str(_write_sync_receipt(target, {**result, "created_at": helpers._now().isoformat()}))
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    prefix = "bd sync (dry run)" if dry_run else "bd sync"
    print(
        f"{prefix}: bd {version}, issues={len(desired)} created={len(summary['created'])} "
        f"updated={len(summary['updated'])} status_changed={len(summary['status_changed'])} "
        f"edges_added={len(summary['edges_added'])} edges_removed={len(summary['edges_removed'])}"
    )
    for item in summary["kept_brigade_state"]:
        print(f"  kept Brigade {item['field']} for {item['bd_id']} ({item['task_id']}): {item['brigade_status']}")
    print(f"ready: {len(result['ready'])} blocked: {len(result['blocked'])}")
    if "receipt_path" in result:
        print(f"receipt: {result['receipt_path']}")
    return 0


def _git_lines(target: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(target), *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _github_base(target: Path) -> str | None:
    remote = (_git_lines(target, "remote", "get-url", "origin") or "").strip()
    match = _GITHUB_REMOTE.match(remote)
    return f"https://github.com/{match.group(1)}/{match.group(2)}" if match else None


def _commit_links(target: Path, bd_ids: list[str]) -> dict[str, dict[str, list[Any]]]:
    """Find commits that name a bd id, and the PR numbers in their squash or merge subjects."""
    links: dict[str, dict[str, list[Any]]] = {bd_id: {"commits": [], "pr_links": []} for bd_id in bd_ids}
    if not bd_ids:
        return links
    log = _git_lines(target, "log", "--format=%H%x1f%s%x1f%b%x1e")
    if log is None:
        return links
    base = _github_base(target)
    names = "|".join(re.escape(bd_id) for bd_id in sorted(bd_ids, key=len, reverse=True))
    mention = re.compile(rf"(?<![A-Za-z0-9._-])({names})(?![A-Za-z0-9_-]|\.[A-Za-z0-9])")
    for entry in log.split("\x1e"):
        parts = entry.strip("\n").split("\x1f")
        if len(parts) != 3:
            continue
        sha, subject, body = parts
        found = set(mention.findall(f"{subject}\n{body}"))
        if not found:
            continue
        pr = _PR_SUBJECT.search(subject)
        number = int(pr.group(1) or pr.group(2)) if pr else None
        for bd_id in sorted(found):
            links[bd_id]["commits"].append(sha)
            if number is not None and base is not None:
                link = {"url": f"{base}/pull/{number}", "number": number, "commit": sha}
                if link not in links[bd_id]["pr_links"]:
                    links[bd_id]["pr_links"].append(link)
    return links


def _closeouts_by_task(target: Path) -> dict[str, list[dict[str, Any]]]:
    root = helpers._work_closeouts_root(target)
    found: dict[str, list[dict[str, Any]]] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        payload = helpers._read_json(child / "closeout.json") if child.is_dir() else None
        if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
            continue
        task_id = payload["task"].get("id")
        if not isinstance(task_id, str):
            continue
        raw_verification = payload.get("verification")
        verification: dict[str, Any] = raw_verification if isinstance(raw_verification, dict) else {}
        found.setdefault(task_id, []).append(
            {
                "kind": "work_closeout",
                "id": payload.get("closeout_id"),
                "status": payload.get("status"),
                "ready": payload.get("ready"),
                "created_at": payload.get("created_at"),
                "verification_run_id": verification.get("run_id"),
                "verification_status": verification.get("status"),
            }
        )
    return found


def outcome_records(target: Path) -> list[dict[str, Any]]:
    """One record per bd issue id with the Brigade state, receipts and PR links."""
    payload = ledger._read_task_ledger(target)
    by_bd: dict[str, list[dict[str, Any]]] = {}
    for task in payload["tasks"]:
        meta = task.get("metadata") if isinstance(task, dict) else None
        bd_meta = meta.get("bd") if isinstance(meta, dict) else None
        bd_id = bd_meta.get("id") if isinstance(bd_meta, dict) else None
        if isinstance(bd_id, str):
            by_bd.setdefault(bd_id, []).append(task)
    closeouts = _closeouts_by_task(target)
    commits = _commit_links(target, sorted(by_bd))
    records: list[dict[str, Any]] = []
    for bd_id in sorted(by_bd):
        tasks = sorted(by_bd[bd_id], key=lambda task: str(task.get("id")))
        records.append(
            {
                "schema": OUTCOMES_SCHEMA,
                "bd_id": bd_id,
                "tasks": [
                    {
                        "task_id": task.get("id"),
                        "source": task.get("source"),
                        "status": task.get("status", "pending"),
                        "completed_at": task.get("completed_at"),
                    }
                    for task in tasks
                ],
                "receipts": [item for task in tasks for item in closeouts.get(str(task.get("id")), [])],
                "pr_links": commits[bd_id]["pr_links"],
                "commits": commits[bd_id]["commits"],
            }
        )
    return records


def export_bd_outcomes(*, target: Path, output: Path, json_output: bool = False) -> int:
    """Write the outcomes JSONL. This file is the only thing Brigade writes for bd."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise BdImportError(f"--target is not a directory: {target}", reason="bd_import_io_error")
    destination = output.expanduser().resolve()
    if ".beads" in destination.parts:
        raise BdImportError(
            "refusing to write inside a .beads directory; Brigade never writes bd storage",
            reason="bd_output_refused",
            details={"output": str(destination)},
        )
    if destination.is_dir():
        raise BdImportError(f"--output is a directory: {destination}", reason="bd_output_refused")
    records = outcome_records(target)
    body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, destination)
    except OSError as exc:
        raise BdImportError(f"bd outcomes export failed: {exc}", reason="bd_import_io_error") from exc
    finally:
        temporary.unlink(missing_ok=True)
    summary = {
        "schema": OUTCOMES_SCHEMA,
        "output": str(destination),
        "records": len(records),
        "with_receipts": sum(1 for record in records if record["receipts"]),
        "with_pr_links": sum(1 for record in records if record["pr_links"]),
    }
    if json_output:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"bd outcomes: records={summary['records']} with_receipts={summary['with_receipts']} "
            f"with_pr_links={summary['with_pr_links']}"
        )
        print(f"output: {destination}")
    return 0
