"""Shadow projection comparison for issue #568 slice 4.

After every committed legacy ``run.json`` write for a run with an activated
lifecycle journal, ``record_shadow_comparison`` independently rebuilds a
shadow candidate from the committed legacy payload plus the five derived
fields read off the verified journal tail, encodes it through
``run_projector.encode_snapshot_bytes``, runs the projector against the same
legacy payload and journal, and compares the two canonical byte strings.
Parity is a full canonical byte comparison. The artifact persists only
SHA-256 digests of the two encodings plus a bounded list of differing field
names. ``check_projection_readiness`` is the fail-closed gate a later slice
consults before making the journal authoritative.

The legacy writer stays authoritative: this module never writes
``run.json``, never appends to the journal, and never raises into the writer
path. Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade import localio, run_checkpoint, run_events, run_journal, run_lifecycle, run_projector

SHADOW_SCHEMA: str = "brigade.run_shadow.v1"
SHADOW_SCHEMA_VERSION: int = 1
SHADOW_ARTIFACT_NAME: str = "shadow-comparison.json"
MAX_RECENT_RECORDS: int = 16
MAX_DIFFERING_FIELDS: int = 16

OUTCOME_MATCH = "match"
OUTCOME_LAG = "lag"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_ERROR = "error"
_OUTCOMES = frozenset({OUTCOME_MATCH, OUTCOME_LAG, OUTCOME_MISMATCH, OUTCOME_ERROR})

REASON_NO_JOURNAL = "no-journal"
REASON_JOURNAL_UNREADABLE = "journal-unreadable"
REASON_NO_EVIDENCE = "no-evidence"
REASON_EVIDENCE_UNREADABLE = "evidence-unreadable"
REASON_EVIDENCE_SCHEMA_MISMATCH = "evidence-schema-mismatch"
REASON_JOURNAL_AHEAD = "journal-ahead-of-evidence"
REASON_NO_COMPARISONS = "no-comparisons"
REASON_MISMATCH_RECORDED = "mismatch-recorded"
REASON_ERROR_RECORDED = "error-recorded"
REASON_STATUS_LAG_CURRENT = "status-lag-current"


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    reasons: tuple[str, ...]


def shadow_artifact_path(run_dir: Path) -> Path:
    return Path(run_dir) / "events" / SHADOW_ARTIFACT_NAME


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _empty_artifact(run_id: str) -> dict[str, Any]:
    return {
        "schema": SHADOW_SCHEMA,
        "schema_version": SHADOW_SCHEMA_VERSION,
        "run_id": run_id,
        "projector_version": run_projector.PROJECTOR_VERSION,
        "comparisons": 0,
        "matches": 0,
        "mismatches": 0,
        "lags": 0,
        "errors": 0,
        "last_compared_sequence": None,
        "last_compared_event_digest": None,
        "last_shadow_digest": None,
        "last_projected_digest": None,
        "last_differing_fields": None,
        "last_outcome": None,
        "last_error_category": None,
        "last_recorded_at": None,
        "recent_records": [],
    }


def _quarantine_corrupt(artifact_path: Path) -> None:
    """Best-effort rename a corrupt artifact out of the way."""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = artifact_path.with_name(f"{artifact_path.name}.corrupt-{stamp}")
        artifact_path.replace(target)
    except Exception:
        return


def _load_prior_artifact(artifact_path: Path) -> tuple[dict[str, Any] | None, bool]:
    """Return (parsed artifact, was_corrupt). Absent -> (None, False)."""
    if not artifact_path.is_file():
        return None, False
    try:
        return json.loads(artifact_path.read_text()), False
    except (OSError, ValueError):
        _quarantine_corrupt(artifact_path)
        return None, True


def _write_artifact(run_dir: Path, data: dict[str, Any]) -> None:
    localio.write_text_atomic(
        shadow_artifact_path(run_dir),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )


def _record_match(
    run_dir: Path,
    run_id: str,
    tail_seq: int,
    tail_digest: str | None,
    shadow_digest: str,
    projected_digest: str,
) -> None:
    _record_outcome(
        run_dir,
        run_id,
        OUTCOME_MATCH,
        None,
        None,
        tail_seq,
        tail_digest,
        shadow_digest,
        projected_digest,
        [],
    )


def _append_evidence_unreadable(data: dict[str, Any], tail_seq: int | None, tail_digest: str | None) -> None:
    now = localio.utc_now_iso()
    data["comparisons"] = int(data.get("comparisons", 0) or 0) + 1
    data["errors"] = int(data.get("errors", 0) or 0) + 1
    data["last_error_category"] = "evidence-unreadable"
    data["recent_records"].append(
        {
            "outcome": OUTCOME_ERROR,
            "category": "evidence-unreadable",
            "sequence": tail_seq,
            "event_digest": tail_digest,
            "shadow_digest": None,
            "projected_digest": None,
            "differing_fields": [],
            "recorded_at": now,
        }
    )


def _checkpoint_status_write_gap(
    run_dir: Path,
    *,
    prior_seq: int | None,
    tail_seq: int,
) -> bool:
    """Return True when ``tail_seq`` advanced by exactly one checkpoint+status write.

    A normal activated-journal mapped transition appends ``run.snapshot.checkpointed``
    immediately before the mapped status event, so the journal tail can jump by two
    sequences between comparisons without a skipped shadow step.
    """
    if prior_seq is None:
        if tail_seq != 2:
            return False
        expected_checkpoint_seq = 1
        expected_status_seq = 2
    elif tail_seq != prior_seq + 2:
        return False
    else:
        expected_checkpoint_seq = prior_seq + 1
        expected_status_seq = tail_seq
    try:
        journal_report = run_journal.read_journal(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        return False
    if journal_report.partial_tail is not None or journal_report.chain_errors:
        return False
    events = journal_report.events
    if len(events) < expected_status_seq:
        return False
    checkpoint = events[expected_checkpoint_seq - 1]
    status_event = events[expected_status_seq - 1]
    if checkpoint.sequence != expected_checkpoint_seq:
        return False
    if checkpoint.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE:
        return False
    if status_event.sequence != expected_status_seq:
        return False
    paired_event_type = checkpoint.payload.get("paired_event_type")
    if not isinstance(paired_event_type, str):
        return False
    if status_event.event_type != paired_event_type:
        return False
    return paired_event_type in run_lifecycle.STATUS_EVENT_TYPE.values()


def _append_gap_record(data: dict[str, Any], tail_seq: int, tail_digest: str | None) -> None:
    now = localio.utc_now_iso()
    data["comparisons"] = int(data.get("comparisons", 0) or 0) + 1
    data["errors"] = int(data.get("errors", 0) or 0) + 1
    data["last_error_category"] = "comparison-gap"
    data["recent_records"].append(
        {
            "outcome": OUTCOME_ERROR,
            "category": "comparison-gap",
            "sequence": tail_seq,
            "event_digest": tail_digest,
            "shadow_digest": None,
            "projected_digest": None,
            "differing_fields": [],
            "recorded_at": now,
        }
    )


def _record_outcome(
    run_dir: Path,
    run_id: str,
    outcome: str,
    category: str | None,
    diagnostic: str | None,
    tail_seq: int | None,
    tail_digest: str | None,
    shadow_digest: str | None,
    projected_digest: str | None,
    differing_fields: list[str],
) -> None:
    prior, was_corrupt = _load_prior_artifact(shadow_artifact_path(run_dir))
    if prior is None:
        data = _empty_artifact(run_id)
    else:
        data = prior
    key = (tail_seq, shadow_digest, projected_digest, outcome, category)
    prior_records = data.get("recent_records") or []
    if prior_records:
        last = prior_records[-1]
        last_key = (
            last.get("sequence"),
            last.get("shadow_digest"),
            last.get("projected_digest"),
            last.get("outcome"),
            last.get("category"),
        )
        if last_key == key:
            return  # idempotent: same tail sequence, digests, outcome, and category
    # A present-but-unparseable prior artifact is quarantined and treated as
    # absent; record an evidence-unreadable error before the main record.
    if was_corrupt:
        _append_evidence_unreadable(data, tail_seq, tail_digest)
    # Comparison-gap defense: if the journal tail advanced by more than one
    # since the last recorded comparison, a committed transition's shadow
    # step was skipped (crash window). Record a comparison-gap error in the
    # same atomic write as the main record. The absent-prior case (no prior
    # artifact, e.g. after a crash that lost the first comparison) fires when
    # the tail is already beyond the first transition.
    prior_seq = data.get("last_compared_sequence")
    if isinstance(tail_seq, int):
        gap = False
        if isinstance(prior_seq, int) and tail_seq > prior_seq + 1:
            gap = not _checkpoint_status_write_gap(run_dir, prior_seq=prior_seq, tail_seq=tail_seq)
        elif prior_seq is None and tail_seq > 1:
            gap = not _checkpoint_status_write_gap(run_dir, prior_seq=None, tail_seq=tail_seq)
        if gap:
            _append_gap_record(data, tail_seq, tail_digest)
    data["comparisons"] = int(data.get("comparisons", 0) or 0) + 1
    if outcome == OUTCOME_MATCH:
        data["matches"] = int(data.get("matches", 0) or 0) + 1
    elif outcome == OUTCOME_LAG:
        data["lags"] = int(data.get("lags", 0) or 0) + 1
    elif outcome == OUTCOME_MISMATCH:
        data["mismatches"] = int(data.get("mismatches", 0) or 0) + 1
    elif outcome == OUTCOME_ERROR:
        data["errors"] = int(data.get("errors", 0) or 0) + 1
    data["last_compared_sequence"] = tail_seq
    data["last_compared_event_digest"] = tail_digest
    data["last_shadow_digest"] = shadow_digest
    data["last_projected_digest"] = projected_digest
    data["last_differing_fields"] = differing_fields
    data["last_outcome"] = outcome
    data["last_error_category"] = category if outcome == OUTCOME_ERROR else None
    now = localio.utc_now_iso()
    data["last_recorded_at"] = now
    record = {
        "outcome": outcome,
        "category": category,
        "sequence": tail_seq,
        "event_digest": tail_digest,
        "shadow_digest": shadow_digest,
        "projected_digest": projected_digest,
        "differing_fields": differing_fields,
        "recorded_at": now,
    }
    if outcome == OUTCOME_ERROR and diagnostic is not None:
        record["diagnostic"] = diagnostic
    data["recent_records"].append(record)
    data["recent_records"] = data["recent_records"][-MAX_RECENT_RECORDS:]
    _write_artifact(run_dir, data)


def _differing_fields(shadow: Mapping[str, Any], projected: Mapping[str, Any]) -> list[str]:
    """Sorted names whose values deep-differ across the two snapshots.

    Capped at ``MAX_DIFFERING_FIELDS``: a longer list is truncated to the
    first ``MAX_DIFFERING_FIELDS - 1`` sorted names plus the literal
    truncation token ``"..."`` so the total length is exactly
    ``MAX_DIFFERING_FIELDS``.
    """
    names = sorted(set(shadow.keys()) | set(projected.keys()))
    differing: list[str] = []
    for name in names:
        if name not in shadow or name not in projected:
            differing.append(name)
            continue
        if shadow[name] != projected[name]:
            differing.append(name)
    if len(differing) > MAX_DIFFERING_FIELDS:
        differing = differing[: MAX_DIFFERING_FIELDS - 1] + ["..."]
    return differing


def _bound(msg: str) -> str:
    limit = run_events.MAX_DIAGNOSTIC_LEN
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"


def _record_error(
    run_dir: Path,
    run_id: str,
    category: str,
    diagnostic: str | None,
    tail_seq: int | None,
    tail_digest: str | None = None,
    shadow_digest: str | None = None,
    projected_digest: str | None = None,
) -> None:
    _record_outcome(
        run_dir,
        run_id,
        OUTCOME_ERROR,
        category,
        diagnostic,
        tail_seq,
        tail_digest,
        shadow_digest,
        projected_digest,
        [],
    )


def record_shadow_comparison(run_dir: Path, legacy_snapshot: Mapping[str, Any]) -> None:
    # Total outer containment: no shadow-path failure (journal read, projection,
    # evidence write, RunJournalError, or anything else) ever escapes into the
    # legacy writer path. The legacy writer is authoritative; shadow evidence is
    # observational. Known journal-read failures are classified
    # journal-unreadable before the outer containment returns; a failing evidence
    # write is swallowed silently and leaves the previous artifact in place.
    try:
        run_dir = Path(run_dir).expanduser().resolve()
        if not isinstance(legacy_snapshot, Mapping):
            return
        status = legacy_snapshot.get("status")
        if not isinstance(status, str) or not status:
            return
        journal_path = run_lifecycle._journal_path(run_dir)
        if not journal_path.is_file():
            return
        run_id = run_dir.name

        try:
            journal_report = run_journal.read_journal(journal_path)
        except (OSError, run_journal.RunJournalError):
            _record_error(run_dir, run_id, "journal-unreadable", None, None)
            return
        if journal_report.partial_tail is not None or journal_report.chain_errors:
            _record_error(run_dir, run_id, "journal-unreadable", None, None)
            return
        if not journal_report.events:
            return
        # Defense in depth: the slice-2 writer derives run_id from the directory
        # name, so a first-event run_id mismatch cannot fire through the normal
        # path. It is checked against the first verified event so a forged
        # journal is classified journal-run-id-mismatch on direct calls.
        if journal_report.events[0].run_id != run_id:
            _record_error(run_dir, run_id, "journal-run-id-mismatch", None, None)
            return
        tail = journal_report.events[-1]
        tail_seq = tail.sequence
        tail_digest = tail.event_digest
        shadow_candidate = copy.deepcopy(dict(legacy_snapshot))
        shadow_candidate["projector_version"] = run_projector.PROJECTOR_VERSION
        shadow_candidate["journal_present"] = True
        shadow_candidate["journal_last_sequence"] = tail_seq
        shadow_candidate["journal_last_event_digest"] = tail_digest
        try:
            shadow_bytes = run_projector.encode_snapshot_bytes(shadow_candidate)
        except run_projector.ProjectionError as exc:
            _record_error(
                run_dir,
                run_id,
                f"projection-error:{type(exc).__name__}",
                _bound(exc.diagnostic),
                tail_seq,
                tail_digest,
            )
            return
        try:
            projection = run_projector.project_run_snapshot(
                legacy_snapshot, journal_report.events, journal_present=True
            )
        except run_projector.ProjectionError as exc:
            _record_error(
                run_dir,
                run_id,
                f"projection-error:{type(exc).__name__}",
                _bound(exc.diagnostic),
                tail_seq,
                tail_digest,
                _sha256_hex(shadow_bytes),
            )
            return
        projected_bytes = projection.to_bytes()
        shadow_digest = _sha256_hex(shadow_bytes)
        projected_digest = _sha256_hex(projected_bytes)
        if shadow_bytes != projected_bytes:
            differing = _differing_fields(shadow_candidate, projection.snapshot)
            legacy_status = legacy_snapshot.get("status")
            if differing == ["status"] and legacy_status not in run_lifecycle.STATUS_EVENT_TYPE:
                _record_outcome(
                    run_dir,
                    run_id,
                    OUTCOME_LAG,
                    None,
                    None,
                    tail_seq,
                    tail_digest,
                    shadow_digest,
                    projected_digest,
                    differing,
                )
            else:
                _record_outcome(
                    run_dir,
                    run_id,
                    OUTCOME_MISMATCH,
                    None,
                    None,
                    tail_seq,
                    tail_digest,
                    shadow_digest,
                    projected_digest,
                    differing,
                )
            return
        _record_match(run_dir, run_id, tail_seq, tail_digest, shadow_digest, projected_digest)
    except Exception:
        return


def check_projection_readiness(run_dir: Path) -> ReadinessReport:
    # Fail-closed, total: never raises, never writes. The no-journal
    # short-circuit and the ready=True happy path land here; every failing
    # gate reason branch lands RED-first in its own later task. The outer
    # containment returns evidence-unreadable on any unexpected failure.
    try:
        run_dir = Path(run_dir).expanduser().resolve()
    except Exception:
        return ReadinessReport(ready=False, reasons=())
    try:
        if not run_lifecycle._journal_path(run_dir).is_file():
            return ReadinessReport(ready=False, reasons=(REASON_NO_JOURNAL,))
        artifact_path = shadow_artifact_path(run_dir)
        if not artifact_path.is_file():
            return ReadinessReport(ready=False, reasons=(REASON_NO_EVIDENCE,))
        data = json.loads(artifact_path.read_text())
        if (
            data.get("schema") != SHADOW_SCHEMA
            or data.get("schema_version") != SHADOW_SCHEMA_VERSION
            or data.get("run_id") != run_dir.name
            or data.get("projector_version") != run_projector.PROJECTOR_VERSION
            or data.get("last_outcome") not in _OUTCOMES
        ):
            return ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_SCHEMA_MISMATCH,))
        if not isinstance(data.get("comparisons"), int) or data["comparisons"] < 1:
            return ReadinessReport(ready=False, reasons=(REASON_NO_COMPARISONS,))
        if data.get("mismatches") or data.get("errors") or data.get("last_outcome") == OUTCOME_LAG:
            reasons: tuple[str, ...] = ()
            if data.get("mismatches"):
                reasons += (REASON_MISMATCH_RECORDED,)
            if data.get("last_outcome") == OUTCOME_LAG:
                reasons += (REASON_STATUS_LAG_CURRENT,)
            if data.get("errors"):
                reasons += (REASON_ERROR_RECORDED,)
                if data.get("last_error_category") == "journal-unreadable":
                    reasons += (REASON_JOURNAL_UNREADABLE,)
            return ReadinessReport(ready=False, reasons=reasons)
        # Re-read the journal and compare its tail to the artifact's last
        # recorded comparison. A journal that is currently unreadable (partial
        # tail or any chain errors) closes the gate with journal-unreadable;
        # a journal that advanced past the evidence (e.g. a committed transition
        # whose shadow step was skipped) closes the gate with
        # journal-ahead-of-evidence.
        try:
            journal_report = run_journal.read_journal(run_lifecycle._journal_path(run_dir))
        except (OSError, run_journal.RunJournalError):
            return ReadinessReport(ready=False, reasons=(REASON_JOURNAL_UNREADABLE,))
        if journal_report.partial_tail is not None or journal_report.chain_errors:
            return ReadinessReport(ready=False, reasons=(REASON_JOURNAL_UNREADABLE,))
        tail = journal_report.events[-1] if journal_report.events else None
        if (
            tail is None
            or tail.sequence != data.get("last_compared_sequence")
            or tail.event_digest != data.get("last_compared_event_digest")
        ):
            return ReadinessReport(ready=False, reasons=(REASON_JOURNAL_AHEAD,))
        return ReadinessReport(ready=True, reasons=())
    except Exception:
        return ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_UNREADABLE,))
