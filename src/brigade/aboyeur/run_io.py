"""Stage 1 compatibility seam for run directories and sidecar writes."""
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

from . import briefs, planning, prompts, artifacts
from . import orchestrator as _orchestrator_mod


# Journal authority is the default for every new run (issue #568 slice 11).
# Enrollment stays durable and per-run: existing runs are classified only from
# their stored run.json fields, so legacy snapshot-only runs are never migrated
# by a later Brigade release or environment change.
_AUTHORITY_REQUEST_FIELD = "run_journal_authority_requested"


def make_run_dir(base: Path, now: datetime | None = None, *, workspace: Path | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    local_id = f"{stamp}-{uuid4().hex[:8]}"
    from .. import node as node_mod
    from .. import run_id as run_id_mod

    inferred = workspace if workspace is not None else node_mod.infer_workspace_from_runs_dir(base)
    try:
        identity = node_mod.ensure_identity(inferred)
    except node_mod.NodeIdentityError:
        return base / local_id
    return base / run_id_mod.format_run_id(identity.short_id, local_id)


def _resolve_authority_state(run_dir: Path) -> str:
    """Return one of 'legacy', 'authority-requested', 'authoritative'.

    'legacy' only when no durable authority request is present
    (run_journal_authority_requested is not true on run.json). 'authority-
    requested' only when the durable authority request is true and NONE of the
    four projection metadata fields is present on run.json (projector_version,
    journal_present, journal_last_sequence, journal_last_event_digest). Once
    ANY of the four projection metadata fields is present, the run is past the
    not-yet-authoritative fallback: incomplete fields, a stale or wrong
    projector version, a bounded read failure, a chain failure, a cursor
    failure, or a digest failure all raise a bounded LifecycleJournalError and
    never downgrade. 'authoritative' only when run.json carries all four
    journal-metadata fields with projector_version == run_projector.
    PROJECTOR_VERSION and its saved journal_last_sequence /
    journal_last_event_digest verifies against the event at that sequence in a
    bounded, chain-valid journal prefix. A bound failure or chain error on a
    run with NO projection metadata fields returns 'authority-requested' (the
    not-yet-authoritative fallback); it never authorizes projection.
    """
    run_json = run_dir / "run.json"
    try:
        raw = run_json.read_bytes()
    except FileNotFoundError:
        return "legacy"
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        meta = None
    if not isinstance(meta, dict) or meta.get(_AUTHORITY_REQUEST_FIELD) is not True:
        return "legacy"
    metadata_fields = (
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    )
    present = [name for name in metadata_fields if name in meta]
    if not present:
        return "authority-requested"
    # Past the not-yet-authoritative fallback: no downgrade is allowed.
    missing = [name for name in metadata_fields if name not in meta]
    if missing:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound(f"authority run missing projection metadata: {sorted(missing)}")
        )
    if meta.get("projector_version") != run_projector.PROJECTOR_VERSION:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run projector version is not current"))
    # Finding 1: once any projection metadata exists, journal_present must be
    # exactly True. A false or wrong-typed value is invalid authoritative
    # metadata and must fail closed before the checkpoint/lifecycle append.
    if meta.get("journal_present") is not True:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run journal_present is not true"))
    saved_seq = meta.get("journal_last_sequence")
    saved_digest = meta.get("journal_last_event_digest")
    if isinstance(saved_seq, bool) or not isinstance(saved_seq, int) or saved_seq < 1:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved journal_last_sequence is invalid")
        )
    if not isinstance(saved_digest, str):
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved journal_last_event_digest is invalid")
        )
    journal_path = run_lifecycle._journal_path(run_dir)
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except (OSError, run_journal.RunJournalError) as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    if report.partial_tail is not None or report.chain_errors:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run journal is not chain-valid"))
    events = report.events
    # Finding 2: the bounded verified prefix must reject ANY event whose run_id
    # differs from run_dir.name, not only the event at the saved cursor. A
    # chain-valid journal that mixes in a foreign-run-id event is not
    # exclusively this run's and must fail closed.
    for event in events:
        if event.run_id != run_dir.name:
            raise run_lifecycle.LifecycleJournalError(
                run_events._bound("authority run journal contains an event with a foreign run_id")
            )
    if saved_seq > len(events):
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved sequence is beyond the journal tail")
        )
    event = events[saved_seq - 1]
    if event.event_digest != saved_digest:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved cursor does not verify against the journal")
        )
    return "authoritative"


_PROJECTION_METADATA_FIELDS = (
    "projector_version",
    "journal_present",
    "journal_last_sequence",
    "journal_last_event_digest",
)


def _payload_requests_authority(payload: dict[str, object]) -> bool:
    """Cheap payload classification: does the incoming run.json payload carry
    any authority signal that requires filesystem authority resolution?

    Returns True when the payload carries the durable authority request field
    with ANY value (present, including an explicit ``False`` which is a forged
    downgrade attempt) OR any of the four projection metadata fields
    (projector_version, journal_present, journal_last_sequence,
    journal_last_event_digest). Only legacy and lifecycle-only writes (the
    authority request field ABSENT and all four projection metadata fields
    ABSENT) return False so ``_write_json`` can skip the existing-run.json
    authority read entirely. No-downgrade is preserved: any incoming durable
    authority request (true or false) or projected metadata still resolves
    and validates authority through ``_resolve_authority_state``.
    """
    if _AUTHORITY_REQUEST_FIELD in payload:
        return True
    return any(field in payload for field in _PROJECTION_METADATA_FIELDS)


def _genuine_journal_ahead(run_dir: Path) -> bool:
    """Return True only for one verified checkpoint/event pair ahead.

    ``check_projection_readiness`` reports ``REASON_JOURNAL_AHEAD`` whenever
    the artifact's ``last_compared_sequence``/``last_compared_event_digest``
    does not match the journal tail, which conflates a real committed
    checkpoint/event pair whose shadow step has not run yet with a forged,
    stale, or multi-pair cursor. Catch-up is safe only when the artifact cursor
    verifies against an actual event in a clean journal and the tail is exactly
    one structurally covered pair ahead.
    """
    artifact_path = run_shadow.shadow_artifact_path(run_dir)
    try:
        data = json.loads(artifact_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    baseline = run_shadow._verify_stale_baseline(
        run_dir,
        run_dir.name,
        data.get("last_compared_sequence"),
        data.get("last_compared_event_digest"),
    )
    if baseline is None:
        return False
    try:
        journal_report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        return False
    if journal_report.partial_tail is not None or journal_report.chain_errors:
        return False
    if not journal_report.events:
        return False
    tail_seq = journal_report.events[-1].sequence
    return run_shadow._checkpoint_status_write_gap(run_dir, prior_seq=baseline[0], tail_seq=tail_seq)


def _authoritative_prior_decision(run_dir: Path, prior_snapshot: dict[str, object]) -> str:
    """Resolve the prior authority gate for an authoritative run.

    A ready prior returns immediately. A prior that is not ready may catch up
    exactly one verified checkpoint/event pair by recording parity against the
    persisted pre-write snapshot and then requiring readiness to become fully
    green. Forged cursors, multi-pair gaps, mismatches, errors, and a catch-up
    that does not restore readiness all raise before a new pair is appended.
    """
    report = run_shadow.check_projection_readiness(run_dir)
    if report.ready:
        return "ready"
    if report.reasons == (run_shadow.REASON_JOURNAL_AHEAD,) and _genuine_journal_ahead(run_dir):
        run_shadow.record_shadow_comparison(run_dir, prior_snapshot)
        caught_up = run_shadow.check_projection_readiness(run_dir)
        if caught_up.ready:
            artifact_path = run_shadow.shadow_artifact_path(run_dir)
            try:
                artifact = json.loads(artifact_path.read_text())
            except (OSError, ValueError) as exc:
                raise run_lifecycle.LifecycleJournalError(
                    run_events._bound("authoritative run prior catch-up evidence is unreadable")
                ) from exc
            mismatches = artifact.get("mismatches") if isinstance(artifact, dict) else None
            errors = artifact.get("errors") if isinstance(artifact, dict) else None
            if (
                not isinstance(artifact, dict)
                or artifact.get("last_outcome") != run_shadow.OUTCOME_MATCH
                or isinstance(mismatches, bool)
                or not isinstance(mismatches, int)
                or mismatches != 0
                or isinstance(errors, bool)
                or not isinstance(errors, int)
                or errors != 0
                or artifact.get("last_error_category") is not None
            ):
                raise run_lifecycle.LifecycleJournalError(
                    run_events._bound("authoritative run prior catch-up evidence is not a clean match")
                )
            return "ready"
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authoritative run prior catch-up not ready: " + ",".join(sorted(caught_up.reasons)))
        )
    raise run_lifecycle.LifecycleJournalError(
        run_events._bound("authoritative run prior gate not ready: " + ",".join(sorted(report.reasons)))
    )


def _project_authority_candidate(path: Path, run_dir: Path, candidate: dict[str, object]) -> None:
    """Re-read the bounded journal, verify a clean chain, project the
    candidate, and atomically replace run.json with the projected bytes."""
    try:
        journal_report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError) as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    if journal_report.partial_tail is not None or journal_report.chain_errors:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority write journal is not chain-valid"))
    try:
        projection = run_projector.project_run_snapshot(candidate, journal_report.events, journal_present=True)
    except run_projector.ProjectionError as exc:
        raise run_lifecycle.LifecycleJournalError(run_events._bound(exc.diagnostic)) from exc
    localio.write_text_atomic(path, projection.to_bytes().decode("utf-8"))


def _write_json_inner(path: Path, payload: object) -> None:
    # run.json is polled by `brigade runs watch/steer/interrupt` while the run
    # rewrites it, so the write must be atomic or a concurrent reader can
    # observe a truncated file.
    if path.name == "run.json" and isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status:
            transition_status = status
            prior_status: str | None = None
            prior_kind: str | None = None
            if path.is_file():
                try:
                    prior_snapshot = json.loads(path.read_bytes())
                except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                    prior_snapshot = None
                if isinstance(prior_snapshot, dict):
                    raw_prior_status = prior_snapshot.get("status")
                    if isinstance(raw_prior_status, str) and raw_prior_status:
                        prior_status = raw_prior_status
                    raw_prior_kind = prior_snapshot.get("kind")
                    if isinstance(raw_prior_kind, str) and raw_prior_kind:
                        prior_kind = raw_prior_kind
            kind = payload.get("kind") if isinstance(payload.get("kind"), str) else prior_kind
            approval_reference = payload.get("approval_reference")
            if status == "running" and isinstance(approval_reference, Mapping):
                decision_state = approval_reference.get("decision_state")
                if decision_state == "pending":
                    # Keep the compatibility snapshot on a status understood
                    # by the previous reader while journaling the intentional
                    # pause.
                    transition_status = "paused"
                elif decision_state in {"approved", "rejected", "held", "consumed"}:
                    # Approval facts own these state changes. Treat their
                    # compatibility snapshot refreshes as neutral so a
                    # same-status write cannot leave a checkpoint promising a
                    # run.resumed event that record_lifecycle_transition
                    # correctly suppresses.
                    transition_status = "approval-state-refresh"
            elif status == "running" and kind == "research":
                # Research reopen must not emit approval-gated run.resumed.
                # Only a true terminal→running recovery is recorded; same-
                # status research running refreshes stay status-neutral.
                _research_terminal = {
                    "failed",
                    "cancelled",
                    "canceled",
                    "completed",
                    "ok",
                    "timeout",
                    "incomplete",
                    "dry-run",
                }
                if prior_status in _research_terminal:
                    transition_status = "research-reopened"
                else:
                    transition_status = "research-state-refresh"
            run_dir = path.parent
            workspace = runguard.resolve_run_lock_workspace(payload, run_dir)
            # Cheap payload classification BEFORE the filesystem authority
            # resolution. Legacy and lifecycle-only status writes (the durable
            # authority request field ABSENT and all four projection metadata
            # fields ABSENT in the incoming payload) never read/parse the
            # existing run.json only to resolve authority, so an unreadable
            # legacy run.json cannot make a status write fail. No-downgrade is
            # preserved: an incoming durable authority request of ANY value
            # (including an explicit False, which is a forged downgrade
            # attempt) or any projection metadata field still resolves and
            # validates authority through _resolve_authority_state.
            if _payload_requests_authority(payload):
                authority_state = _resolve_authority_state(run_dir)
            else:
                authority_state = "legacy"
            # Construct the canonical legacy candidate and the projection base
            # exactly once. The base strips the four journal-derived metadata
            # fields; the same object is the base-stripped checkpoint body
            # whenever the resolved state is authority-requested or
            # authoritative.
            candidate = copy.deepcopy(payload)
            for derived in (
                "projector_version",
                "journal_present",
                "journal_last_sequence",
                "journal_last_event_digest",
            ):
                candidate.pop(derived, None)
            encoded_candidate = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
            base_bytes = encoded_candidate.encode("utf-8")
            body_kind = (
                run_checkpoint._BODY_KIND_BASE_STRIPPED
                if authority_state in {"authority-requested", "authoritative"}
                else None
            )
            # Prior authority gate (authoritative only): fail closed BEFORE
            # the checkpoint/lifecycle append when the prior committed state
            # is not ready and not exactly one recoverable checkpoint/event
            # pair ahead. The catch-up uses the persisted pre-write snapshot,
            # never the incoming next status. Legacy and authority-requested
            # runs skip this gate.
            if authority_state == "authoritative":
                try:
                    prior_snapshot = json.loads(path.read_bytes())
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    raise run_lifecycle.LifecycleJournalError(
                        run_events._bound("authoritative prior snapshot is unreadable")
                    ) from exc
                if not isinstance(prior_snapshot, dict):
                    raise run_lifecycle.LifecycleJournalError(
                        run_events._bound("authoritative prior snapshot is not an object")
                    )
                _authoritative_prior_decision(run_dir, prior_snapshot)
            # Exact order: activate the journal, publish the recovery
            # checkpoint, append the lifecycle status transition, record the
            # shadow parity, consult the post-parity readiness veto, then
            # atomically replace run.json. A CheckpointError from
            # write_checkpoint fails BEFORE the lifecycle append and BEFORE
            # run.json replacement. The prior gate above raises BEFORE any
            # append for an authoritative run with a real prior defect.
            run_lifecycle.prepare_lifecycle_journal(
                run_dir,
                workspace=workspace,
                incoming_snapshot=payload,
            )
            run_checkpoint.write_checkpoint(
                run_dir,
                base_bytes,
                workspace=workspace,
                paired_event_type=run_lifecycle.STATUS_EVENT_TYPE.get(transition_status),
                body_kind=body_kind,
            )
            run_lifecycle.record_lifecycle_transition(
                run_dir,
                status=transition_status,
                # The receipt payload identifies the lock workspace
                # (lock_workspace or cwd); the run directory layout does not.
                workspace=workspace,
                incoming_snapshot=payload,
            )
            run_shadow.record_shadow_comparison(run_dir, payload)
            if authority_state == "legacy":
                # Legacy runs skip the veto and write the legacy body (slice 5).
                localio.write_text_atomic(path, encoded_candidate)
                return
            readiness = run_shadow.check_projection_readiness(run_dir)
            if authority_state == "authority-requested":
                # Authority-requested projection uses the POST-parity readiness
                # report (a ready first comparison projects that same write),
                # never the pre-parity decision. A not-ready gate falls back to
                # the legacy body until a ready first comparison catches up.
                if readiness.ready:
                    _project_authority_candidate(path, run_dir, candidate)
                else:
                    localio.write_text_atomic(path, encoded_candidate)
                return
            # Authoritative writes require both the prior gate and this
            # post-parity gate to be fully ready. No comparison-gap override
            # survives: the only recoverable lag was consumed before append.
            if readiness.ready:
                _project_authority_candidate(path, run_dir, candidate)
                return
            raise run_lifecycle.LifecycleJournalError(
                run_events._bound("authoritative run gate not ready: " + ",".join(sorted(readiness.reasons)))
            )
    localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if path.name == "run.json" and isinstance(payload, dict):
        run_shadow.record_shadow_comparison(path.parent, payload)


def _write_json(path: Path, payload: object) -> None:
    """Write JSON, serializing every run.json checkpoint/event transaction."""
    if (
        path.name == "run.json"
        and isinstance(payload, dict)
        and isinstance(payload.get("status"), str)
        and payload["status"]
    ):
        with run_lifecycle.checkpoint_event_pair():
            _write_json_inner(path, payload)
        return
    _write_json_inner(path, payload)


def _revision_contains(revisions_dir: Path, projection: bytes) -> bool:
    """Return whether a preserved sidecar revision matches ``projection``."""
    if not revisions_dir.is_dir():
        return False
    for revision_path in revisions_dir.glob("*.json"):
        try:
            if revision_path.read_bytes() == projection:
                return True
        except OSError:
            continue
    return False


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def _fsync_directory(path: Path) -> None:
    """Persist directory entries where the platform supports directory fsync."""
    if not _supports_directory_fsync():
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_directory_durable(path: Path) -> None:
    """Create missing directory levels and persist each new parent entry."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            _fsync_directory(directory.parent)


def _write_new_revision(revisions_dir: Path, encoded: bytes) -> None:
    """Exclusively create the next immutable sidecar revision."""
    _ensure_directory_durable(revisions_dir)
    sequence = max(
        (int(path.stem) for path in revisions_dir.glob("*.json") if path.stem.isascii() and path.stem.isdecimal()),
        default=0,
    )
    while True:
        sequence += 1
        revision_path = revisions_dir / f"{sequence:06d}.json"
        writer_acquired = False
        try:
            fd = os.open(revision_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            writer_acquired = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(revisions_dir)
        except FileExistsError:
            continue
        except BaseException:
            if writer_acquired:
                revision_path.unlink(missing_ok=True)
            raise
        return


def write_sidecar_revision(run_dir: Path, filename: str, payload: object) -> None:
    """Append an immutable sidecar revision, then update its compatibility file."""
    projection_path = run_dir / filename
    revisions_dir = run_dir / "revisions" / Path(filename).stem
    if projection_path.exists():
        legacy_projection = projection_path.read_bytes()
        json.loads(legacy_projection)
        if not _revision_contains(revisions_dir, legacy_projection):
            _write_new_revision(revisions_dir, legacy_projection)
    _write_new_revision(revisions_dir, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    _write_json(projection_path, payload)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "brigade-run"


def _safe_document_content(text: str) -> str:
    # The ingester treats `##` as handoff section boundaries, so keep routed
    # document content at ### or below.
    return re.sub(r"(?m)^##(?!#)", "###", text).strip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def write_run_handoff(
    inbox: Path,
    *,
    task: str,
    cwd: Path | None,
    output_dir: Path | None,
    assignments: list[Assignment],
    worker_results: list[WorkerResult],
    final_text: str,
    read_only: bool = False,
    now: datetime | None = None,
    outcome_id: str | None = None,
    outcome_digest: str | None = None,
) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d-%H%M")
    safe_task = _one_line(task)
    path = inbox / f"{timestamp}-brigade-run-{_slug(safe_task)}.md"
    worker_summary = (
        "\n".join(
            f"- {result.worker}: {'ok' if result.ok else 'failed'}"
            + (f" ({_one_line(result.detail)})" if result.detail else "")
            for result in worker_results
        )
        or "- no workers dispatched"
    )
    assignment_summary = (
        "\n".join(
            f"- stage {assignment.stage} -> {assignment.worker}: {_one_line(assignment.task)}"
            for assignment in assignments
        )
        or "- no worker assignments"
    )
    artifact_line = f"- artifacts: `{output_dir}`" if output_dir is not None else "- artifacts: none"
    cwd_line = f"- cwd: `{cwd}`" if cwd is not None else "- cwd: not set"
    mode_line = "- mode: read-only" if read_only else "- mode: normal"
    document_content = _safe_document_content(
        f"""### Brigade run: {_slug(safe_task)}
- task: {safe_task}
{artifact_line}
{cwd_line}
{mode_line}

Final answer:
{final_text}
"""
    )
    body = f"""# Memory Handoff

## Type

project-context

## Title

Brigade run completed: {_slug(safe_task)}

## Summary

Brigade completed a bounded plan-dispatch-synthesize run and produced a final answer. This handoff captures the task, assignments, worker status, artifact path, and final result for memory ingestion.

## Durable facts

- task: {safe_task}
{cwd_line}
{artifact_line}
{mode_line}
- orchestrated assignments:
{assignment_summary}
- worker status:
{worker_summary}

## Evidence

{artifact_line}
- final answer captured in this handoff

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

{document_content}
"""
    inbox.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if output_dir is not None or outcome_id:
        parent_kind = "outcome" if outcome_id else "run"
        parent_id = outcome_id if outcome_id else output_dir.name if output_dir is not None else ""
        localio.write_json(
            causal_receipt.handoff_sidecar_path(path),
            causal_receipt.recorded_handoff(
                handoff_id=path.stem,
                parent_kind=parent_kind,
                parent_id=parent_id,
                parent_digest=outcome_digest,
            ),
        )
    return path
