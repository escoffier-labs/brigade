"""Read-only status projection and snapshot-cursor validation for run readers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping


def read_run_state(
    run_dir: Path,
    meta: Mapping[str, Any],
    *,
    nonterminal_statuses: frozenset[str],
    terminal_event_types: frozenset[str],
    tolerate_io_errors: bool = False,
    retry_partial_tail: bool = False,
) -> tuple[dict[str, Any], Any | None]:
    """Overlay verified journal status in memory; never repair stored artifacts.

    The journal commits before run.json, so a crashed writer can leave its
    snapshot behind a terminal event or a reopen. Use the existing projector's
    status rules, including status-neutral worker facts, over the entire
    bounded history once a terminal fact exists. Histories with no terminal
    fact keep snapshot semantics, as do legacy runs without a journal.
    """
    from . import run_journal, run_lifecycle, run_projector

    effective = dict(meta)
    projection_fields = ("projector_version", "journal_present", "journal_last_sequence", "journal_last_event_digest")
    authoritative = meta.get("run_journal_authority_requested") is True and any(
        field in meta for field in projection_fields
    )
    if authoritative and (
        not all(field in meta for field in projection_fields)
        or meta.get("journal_present") is not True
        or type(meta.get("projector_version")) is not int
        or not 1 <= meta["projector_version"] <= run_projector.PROJECTOR_VERSION
    ):
        raise ValueError("lifecycle journal snapshot metadata is invalid or incomplete")
    journal_path = run_lifecycle._journal_path(run_dir)
    try:
        try:
            journal_path.lstat()
        except FileNotFoundError as exc:
            if authoritative:
                raise ValueError("lifecycle journal is missing for a projected snapshot") from exc
            return effective, None
        expected_run_id = run_lifecycle._run_id_from_dir(run_dir)
        report = run_journal.read_journal_bounded(journal_path)
        if retry_partial_tail:
            # Only live watch polls opt into this short, bounded append grace.
            for _ in range(2):
                if report.partial_tail is None or report.chain_errors:
                    break
                prefix_size = len(report.events)
                prefix_digest = report.events[-1].event_digest if prefix_size else None
                time.sleep(0.01)
                journal_path.lstat()
                report = run_journal.read_journal_bounded(journal_path)
                if len(report.events) < prefix_size or (
                    prefix_size and report.events[prefix_size - 1].event_digest != prefix_digest
                ):
                    raise ValueError("lifecycle journal prefix changed during partial trailing record retry")
                if not report.events and report.partial_tail is None and not report.chain_errors:
                    # A lost journal must not become a legacy snapshot fallback.
                    raise ValueError("lifecycle journal lost during partial trailing record retry")
    except run_journal.RunJournalError as exc:
        raise ValueError(f"lifecycle journal is unreadable: {exc.diagnostic}") from exc
    except OSError as exc:
        if tolerate_io_errors and not authoritative:
            # Preserve show's best-effort snapshot display when I/O fails.
            # Watch and strict journal inspection must report the failure.
            return effective, None
        raise ValueError(f"lifecycle journal is unreadable: {exc}") from exc
    if report.partial_tail is not None:
        raise ValueError("lifecycle journal ends in a partial trailing record")
    if report.chain_errors:
        raise ValueError(f"lifecycle journal is corrupt: {report.chain_errors[0]}")
    saved_sequence = 0
    if authoritative:
        cursor_sequence = meta.get("journal_last_sequence")
        if (
            isinstance(cursor_sequence, bool)
            or not isinstance(cursor_sequence, int)
            or not 1 <= cursor_sequence <= len(report.events)
            or report.events[cursor_sequence - 1].event_digest != meta.get("journal_last_event_digest")
        ):
            raise ValueError("lifecycle journal does not cover the saved snapshot cursor")
        saved_sequence = cursor_sequence
    if not report.events:
        return effective, None
    if any(event.run_id != expected_run_id for event in report.events):
        raise ValueError("lifecycle journal run_id does not match the run directory")
    if not any(event.event_type in terminal_event_types for event in report.events):
        return effective, None
    try:
        # Only status is derived here. Unknown snapshot fields remain readable
        # and still pass through the existing public-contract allowlists.
        projected = run_projector.project_run_snapshot(
            {"status": str(meta.get("status") or "unknown")}, report.events, journal_present=True
        )
    except run_projector.ProjectionError as exc:
        raise ValueError(f"lifecycle journal state cannot be derived: {exc.diagnostic}") from exc
    effective["status"] = projected.status
    terminal_event = None
    if projected.status not in nonterminal_statuses:
        terminal_event = next(
            (event for event in reversed(report.events) if event.event_type in terminal_event_types),
            None,
        )
    stale_outcome = projected.status != meta.get("status") or (
        authoritative and terminal_event is not None and terminal_event.sequence > saved_sequence
    )
    if stale_outcome:
        # A prior attempt's timing and failure text cannot describe a reopened
        # or later journal outcome. Unknown current detail stays absent.
        for field in ("finished_at", "duration_seconds", "failure", "failure_kind", "failure_phase", "error"):
            effective.pop(field, None)
    elif projected.status in nonterminal_statuses:
        effective.pop("finished_at", None)
        effective.pop("duration_seconds", None)
    return effective, terminal_event
