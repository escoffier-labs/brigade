"""Stage 1 compatibility seam for synthesis prompts and ground truth."""
# ruff: noqa: F401

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial, wraps
from json import JSONDecoder
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from .. import agents
from .. import codex_appserver
from .. import context_eval
from .. import evidence_brief as evidence_brief_mod
from .. import graphtrail_delta
from .. import causal_receipt
from .. import localio
from .. import message_envelope
from .. import proc, receipt_schema, runguard
from .. import run_control
from .. import run_checkpoint
from .. import run_budget
from .. import run_events
from .. import run_journal
from .. import run_lifecycle
from .. import run_projector
from .. import run_shadow
from .. import seat_health
from .. import seat_health_policy
from .. import verification_contract
from ..result_integrity import validate_final_output
from ..run_receipts import (
    agent_result_from_worker as _agent_result_from_worker,
    agent_result_payload as _agent_result_payload,
    assignment_payload as _assignment_payload,
    worker_payload as _worker_payload,
    write_agent_logs as _write_agent_logs,
    write_worker_logs as _write_worker_logs,
)
from ..run_transport import Assignment, WorkerResult, dag_cycle_members
from ..roster import Agent, Roster, is_cli_allowed, read_only_capability_error, timeout_for, workers
from ..route_catalog import RouteBrief, route_brief, uncovered_stages, unknown_covers
from ..route_policy import (
    RoutePolicyDecision,
    direct_worker_skill_ids,
    planner_skill_policy_section,
    validate_plan_skill_bindings,
    worker_skill_policy_constraint,
)

from . import briefs, run_io, planning, artifacts
from . import orchestrator as _orchestrator_mod

NOOP_DETAIL = "no-op"


def build_synth_prompt(
    task: str,
    results: list[WorkerResult],
    read_only: bool = False,
    ground_truth: dict[str, object] | None = None,
    code_graph: briefs.CodeGraphBrief | None = None,
    drift_impact: briefs.DriftImpactBrief | None = None,
    evidence: briefs.EvidenceBrief | None = None,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    if results:
        rendered = "\n\n".join(
            "\n".join(
                [
                    f"Worker: {result.worker}",
                    f"Sub-task: {result.task}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    f"Detail: {result.detail}" if result.detail else "Detail:",
                    "Output:",
                    planning._admitted_result_output(result, run_id=run_id, to_seat=to_seat) or "(no output)",
                ]
            )
            for result in results
        )
    else:
        rendered = "(No workers were assigned.)"

    policy = f"\n\n{planning._read_only_rules()}" if read_only else ""
    facts = _ground_truth_facts(ground_truth)
    facts_block = f"\n\n{facts}" if facts else ""
    prompt = (
        "You are the Brigade orchestrator. Synthesize the final answer for the user.\n"
        "Account for worker failures if any are present. Do not include implementation chatter."
        f"{facts_block}\n\n"
        f"Original task:\n{task}\n\n"
        f"Worker results:\n{rendered}\n"
        f"{policy}"
    )
    return briefs._prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)


def _print_plan(assignments: list[Assignment]) -> None:
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    stages = sorted({assignment.stage for assignment in assignments})
    if len(stages) == 1:
        for assignment in assignments:
            print(f"  -> {assignment.worker}: {assignment.task}")
        return
    for stage in stages:
        print(f"  stage {stage}:")
        for assignment in assignments:
            if assignment.stage == stage:
                print(f"    -> {assignment.worker}: {assignment.task}")


def _print_worker_status(results: list[WorkerResult]) -> None:
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        marker = "ok" if result.ok else "failed"
        detail = f": {result.detail}" if result.detail else ""
        print(f"  [{marker}] {result.worker}{detail}")


def _is_brigade_path(value: str) -> bool:
    normalized = value.replace("\\", "/").strip("/")
    return normalized == ".brigade" or normalized.startswith(".brigade/")


def _non_brigade_paths(paths: object) -> list[str]:
    if not isinstance(paths, list):
        return []
    return [item for item in paths if isinstance(item, str) and item.strip() and not _is_brigade_path(item)]


def _suspected_noop(
    *,
    ground_truth: dict[str, object],
    worker_results: list[WorkerResult],
    dry_run: bool,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
) -> bool:
    if dry_run or read_only or sandbox_read_only is True or sandbox == "read-only":
        return False
    if ground_truth.get("available") is not True or not worker_results:
        return False
    if not all(result.ok for result in worker_results):
        return False
    changed = _non_brigade_paths(ground_truth.get("changed_files"))
    untracked = _non_brigade_paths(ground_truth.get("untracked_files"))
    return not changed and not untracked


def _mark_noop_worker_results(worker_results: list[WorkerResult], suspected_noop: bool) -> list[WorkerResult]:
    if not suspected_noop:
        return worker_results
    return [replace(result, detail=NOOP_DETAIL) if result.ok else result for result in worker_results]


def _code_graph_delta_skip(status: str) -> dict[str, object]:
    reasons = {
        "disabled": "disabled",
        "skipped_read_only": "read-only run",
        "skipped_dry_run": "dry run",
        "unavailable": "cwd not set",
    }
    reason = reasons.get(status, status.replace("_", " "))
    return {
        "status": status,
        "ok": False,
        "summary": f"code graph delta skipped: {reason}",
        "raw_counts": {},
        "edge_churn": 0,
        "changed_symbols": [],
        "changed_symbol_count": 0,
    }


def _initial_code_graph_delta(
    *,
    code_graph_enabled: bool,
    dry_run: bool,
    read_only: bool,
    cwd: Path | None,
) -> dict[str, object] | None:
    if not code_graph_enabled:
        return _code_graph_delta_skip("disabled")
    if read_only:
        return _code_graph_delta_skip("skipped_read_only")
    if dry_run:
        return _code_graph_delta_skip("skipped_dry_run")
    if cwd is None:
        return _code_graph_delta_skip("unavailable")
    return None


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _git_stdout(cwd: Path, *args: str) -> tuple[str, str | None]:
    result = proc.run(["git", *args], cwd=cwd)
    if result.code == 0:
        return result.stdout, None
    detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
    return "", detail


_ROUTE_CHANGED_PATHS_CAP = 200


def _route_changed_paths(cwd: Path | None) -> tuple[str, ...]:
    """Tracked-modified plus untracked paths, for route surface derivation.
    Best-effort per source: a repo with no commits still yields its untracked
    files, and any git failure means fewer path signals, never a failed run."""
    if cwd is None:
        return ()
    paths: list[str] = []
    try:
        changed, error = _git_stdout(cwd, "diff", "--name-only", "HEAD")
        if error is None:
            paths.extend(line.strip() for line in changed.splitlines() if line.strip())
    except Exception:
        pass
    try:
        paths.extend(runguard._untracked_files(cwd))
    except Exception:
        pass
    return tuple(paths[:_ROUTE_CHANGED_PATHS_CAP])


def _receipt_git_snapshot(cwd: Path | None) -> dict[str, object] | None:
    if cwd is None:
        return None
    try:
        head, head_error = _git_stdout(cwd, "rev-parse", "HEAD")
        branch, branch_error = _git_stdout(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        status, status_error = _git_stdout(cwd, "status", "--porcelain")
    except Exception:
        return None
    if head_error or branch_error or status_error:
        return None
    return {"head": head.strip(), "branch": branch.strip(), "dirty_files": len(status.splitlines())}


def _receipt_command_payload(command: object) -> dict[str, object] | None:
    if not isinstance(command, dict):
        return None
    raw_command = command.get("command")
    if not isinstance(raw_command, str):
        return None
    payload: dict[str, object] = {"command": raw_command}
    status = command.get("status")
    if isinstance(status, str):
        payload["status"] = status
    exit_code = command.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        payload["exit_code"] = exit_code
    elif exit_code is None:
        payload["exit_code"] = None
    return payload


def _verify_receipt_payload(data: dict[str, Any]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("run_id", "status", "started_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, str):
            payload[key] = value
    commands = data.get("commands")
    if isinstance(commands, list):
        payload["commands"] = [
            command_payload for item in commands if (command_payload := _receipt_command_payload(item)) is not None
        ]
    else:
        payload["commands"] = []
    return payload


def _verify_receipts_since(cwd: Path, started_at: datetime) -> list[dict[str, object]]:
    root = cwd / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return []
    receipts: list[dict[str, object]] = []
    for receipt_path in sorted(root.glob("*/receipt.json")):
        try:
            data = json.loads(receipt_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        receipt_started_at = _parse_iso_datetime(data.get("started_at"))
        if receipt_started_at is None or receipt_started_at < started_at:
            continue
        receipts.append(_verify_receipt_payload(data))

    def _sort_key(item: dict[str, object]) -> tuple[datetime, str]:
        # Sort by parsed UTC time: lexical started_at ordering misorders
        # receipts with mixed timezone offsets.
        parsed = _parse_iso_datetime(item.get("started_at"))
        return (parsed or datetime.min.replace(tzinfo=timezone.utc), str(item.get("run_id") or ""))

    receipts.sort(key=_sort_key, reverse=True)
    return receipts


def build_ground_truth(
    cwd: Path | None,
    started_at: datetime,
    pre_run_snapshot: runguard.PreRunSnapshot | None = None,
) -> dict[str, object]:
    verify_receipts = _verify_receipts_since(cwd, started_at) if cwd is not None else []
    payload: dict[str, object] = {
        "available": False,
        "cwd": str(cwd) if cwd is not None else None,
        "diffstat": "",
        "changed_files": [],
        "untracked_files": [],
        "patch_ref": None,
        "verify_receipts": verify_receipts,
        "latest_verify": verify_receipts[0] if verify_receipts else None,
    }
    if cwd is None:
        payload["reason"] = "cwd not set"
        return payload

    inside, error = _git_stdout(cwd, "rev-parse", "--is-inside-work-tree")
    if error is not None or inside.strip() != "true":
        payload["reason"] = error or "not a git worktree"
        return payload

    # When a pre-run snapshot exists, attribute only state changes relative to
    # it so pre-existing dirty or untracked files (possible only in a linked
    # worktree; the primary checkout rejects --allow-dirty) are not charged to
    # the worker. For a clean baseline the full HEAD diffstat equals the worker
    # delta and is preserved. For a dirty baseline the full HEAD diffstat is
    # contaminated by pre-existing dirt, so it is never reused as the run delta:
    # only content-changed paths relative to baseline are reported and line
    # counts are left unavailable (they cannot be computed without baseline
    # contamination).
    if pre_run_snapshot is not None:
        try:
            changed_names_list, untracked_files = runguard.changes_relative_to_snapshot(cwd, pre_run_snapshot)
        except runguard.RunGuardError as exc:
            # Fail closed: a final git query failure must not become an
            # available clean result. Surface unavailable ground truth with a
            # precise reason instead of charging (or clearing) the worker.
            payload["reason"] = str(exc)
            return payload
        changed_names = "\n".join(changed_names_list)
        baseline_dirty = bool(pre_run_snapshot.tracked_dirty_paths) or bool(pre_run_snapshot.untracked_paths)
        if baseline_dirty:
            diffstat = ""
            payload["diffstat_unavailable"] = True
        else:
            diffstat, error = _git_stdout(cwd, "diff", "--stat", "HEAD")
            if error is not None:
                payload["reason"] = error
                return payload
    else:
        diffstat, error = _git_stdout(cwd, "diff", "--stat", "HEAD")
        if error is not None:
            payload["reason"] = error
            return payload
        changed_names, error = _git_stdout(cwd, "diff", "--name-only", "HEAD")
        if error is not None:
            payload["reason"] = error
            return payload
        try:
            untracked_files = runguard._untracked_files(cwd)
        except runguard.RunGuardError as exc:
            payload["reason"] = str(exc)
            return payload

    payload.update(
        {
            "available": True,
            # Changes are observed during the run; the author is unknown. Brigade
            # does not assert the worker authored them (a concurrent commit,
            # branch switch, or pre-existing edit could also be the source).
            "change_provenance": "observed-during-run-author-unknown",
            "diffstat": diffstat.strip(),
            "changed_files": [line for line in changed_names.splitlines() if line.strip()],
            "untracked_files": untracked_files,
        }
    )
    return payload


def _ground_truth_str_list(ground_truth: dict[str, object], key: str) -> list[str]:
    value = ground_truth.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _ground_truth_facts(ground_truth: dict[str, object] | None) -> str:
    if ground_truth is None:
        return ""
    lines = ["Brigade-computed facts:"]
    if ground_truth.get("available") is not True:
        reason = ground_truth.get("reason")
        detail = f" ({run_io._one_line(str(reason))})" if reason else ""
        lines.append(f"- ground_truth: unavailable{detail}")
    else:
        changed_files = _ground_truth_str_list(ground_truth, "changed_files")
        untracked_files = _ground_truth_str_list(ground_truth, "untracked_files")
        # Ground truth describes changes *observed during the run*; the author is
        # unknown. Brigade does not assert the worker authored them (a concurrent
        # commit, branch switch, or pre-existing edit could also have produced
        # them). Drift checks narrow this, but the provenance stays "author
        # unknown" so synthesis never claims the worker wrote concurrent edits.
        lines.append(
            "- changed_files: observed during the run, author unknown "
            f"({len(changed_files)})" + (f": {', '.join(changed_files[:6])}" if changed_files else "")
        )
        lines.append(
            "- untracked_files: observed during the run, author unknown "
            f"({len(untracked_files)})" + (f": {', '.join(untracked_files[:6])}" if untracked_files else "")
        )
        if ground_truth.get("diffstat_unavailable") is True:
            lines.append("- diffstat: unavailable (dirty baseline; line counts would include pre-existing changes)")
        else:
            diffstat = run_io._one_line(str(ground_truth.get("diffstat") or "none"))
            if len(diffstat) > 240:
                diffstat = diffstat[:237] + "..."
            lines.append(f"- diffstat: {diffstat}")
    patch_ref = ground_truth.get("patch_ref")
    if isinstance(patch_ref, str) and patch_ref:
        lines.append(f"- patch_ref: {patch_ref}")
    verify_receipts = ground_truth.get("verify_receipts")
    if isinstance(verify_receipts, list) and verify_receipts:
        latest = verify_receipts[0] if isinstance(verify_receipts[0], dict) else {}
        latest_status = latest.get("status") if isinstance(latest.get("status"), str) else "unknown"
        latest_run = latest.get("run_id") if isinstance(latest.get("run_id"), str) else "unknown"
        lines.append(f"- verify_receipts: {len(verify_receipts)} latest={latest_run} status={latest_status}")
    else:
        lines.append("- verify_receipts: 0")
    code_graph_delta = ground_truth.get("code_graph_delta")
    if isinstance(code_graph_delta, dict):
        summary = run_io._one_line(str(code_graph_delta.get("summary") or code_graph_delta.get("status") or "unknown"))
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(f"- code_graph_delta: {summary}")
    context_eval_payload = ground_truth.get("context_eval")
    if isinstance(context_eval_payload, dict):
        summary = _context_eval_fact(context_eval_payload)
        if summary:
            lines.append(f"- context eval: {summary}")
    return "\n".join(lines)


def _context_eval_fact(payload: dict[str, object]) -> str:
    try:
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            return ""
        rate = payload.get("brief_hit_rate")
        hits = counts.get("hits")
        delta_files = counts.get("delta_files")
        missed = counts.get("missed")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or isinstance(hits, bool)
            or not isinstance(hits, int)
            or isinstance(delta_files, bool)
            or not isinstance(delta_files, int)
            or isinstance(missed, bool)
            or not isinstance(missed, int)
        ):
            return ""
        return f"brief hit rate {rate:.2f} ({hits}/{delta_files} files, {missed} missed)"
    except Exception:
        return ""


def _with_patch_ref(ground_truth: object, patch_ref: str) -> object:
    if not isinstance(ground_truth, dict):
        return ground_truth
    updated = dict(ground_truth)
    updated["patch_ref"] = patch_ref
    return updated


def _context_eval_for_run(
    code_graph: briefs.CodeGraphBrief | None,
    code_graph_delta: dict[str, object] | None,
) -> dict[str, object] | None:
    try:
        if code_graph is None or not code_graph.attached or not code_graph.text:
            return None
        if not isinstance(code_graph_delta, dict) or code_graph_delta.get("ok") is not True:
            return None
        if code_graph_delta.get("stale_graph_used") is True:
            return None
        sidecar_path = code_graph_delta.get("sidecar_path")
        if not isinstance(sidecar_path, str) or not sidecar_path:
            return None
        delta_files = context_eval.extract_delta_files(sidecar_path)
        if not delta_files:
            return None
        brief_files = context_eval.extract_brief_files(code_graph.text)
        return context_eval.evaluate(brief_files, delta_files)
    except Exception:
        return None
