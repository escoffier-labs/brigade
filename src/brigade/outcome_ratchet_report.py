"""Advisory outcome ratchet report (#2589).

The weekly memory-care ratchet runs ``outcome reconcile`` as a dry run. Its
decisions lived only in stdout and reason prose, so a repeated
"withheld: verified regression present" left no durable trace. This module
persists one bounded, versioned summary of the last run for the session brief,
the Center dashboard and ``brigade outcome ratchet``.

The report is advisory. ``--apply`` never reads it, no status, decision or
record write depends on it, and an absent or unreadable file is a normal state.
Only counts, artifact ids and decision reasons are stored: no card body, handoff
text or receipt payload.

Write once per call. Recomputation always comes from a fresh reconcile run, so
a stale file reads as stale rather than being refreshed by a reader.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import localio, outcome

REPORT_VERSION = 1
REPORT_MAX_BYTES = 64 * 1024
REPORT_RELATIVE_PATH = Path(".brigade") / "memory-care" / "outcome" / "ratchet-report.json"
_REGRESSION_CODES = outcome.HOLD_CODES | {outcome.REGRESSION_ROLLBACK}
_BRIEF_MAX_IDS = 6


def report_path(target: Path) -> Path:
    """Return the report path for a workspace or repository target."""
    return target.expanduser().resolve() / REPORT_RELATIVE_PATH


def _has_symlinked_component(path: Path, boundary: Path) -> bool:
    """Return True when path or any parent below boundary is a symlink."""
    current = path
    while current != boundary and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def write_report(
    target: Path,
    items: Sequence[Any],
    *,
    apply: bool,
    generated_at: datetime,
) -> Path | None:
    """Write the advisory report once and return its path, or None if skipped.

    ``items`` are reconcile decision items: each carries ``decision``,
    ``prior_status``, ``artifact_kind`` and either fingerprint ``cohorts`` or a
    ``scorecard``. Only the fields below are read from them.

    A symlinked report path is refused, and a payload that cannot fit the byte
    cap even without regression rows is skipped, so a broken report can never
    fail or grow the ratchet run that produced it. An unwritable path (read-only
    directory, full disk, a directory in place of the file) is the same: the run
    warns once on stderr and keeps its own exit code and output.
    """
    try:
        return _write_report(target, items, apply=apply, generated_at=generated_at)
    except OSError as exc:
        print(f"warning: outcome ratchet report not written: {exc}", file=sys.stderr)
        return None


def _write_report(
    target: Path,
    items: Sequence[Any],
    *,
    apply: bool,
    generated_at: datetime,
) -> Path | None:
    path = report_path(target)
    if _has_symlinked_component(path, path.parents[len(REPORT_RELATIVE_PATH.parts) - 1]):
        # The previous report stays in place, so say this run's write was skipped.
        print(f"warning: outcome ratchet report not written: symlinked path refused: {path}", file=sys.stderr)
        return None
    report = build_report(items, apply=apply, generated_at=generated_at, target=target)
    encoded = _encode(report)
    if len(encoded) > REPORT_MAX_BYTES:
        print(
            f"warning: outcome ratchet report not written: still over {REPORT_MAX_BYTES} bytes after truncation",
            file=sys.stderr,
        )
        return None
    localio.write_text_atomic(path, encoded.decode("utf-8"))
    return path


def build_report(
    items: Sequence[Any],
    *,
    apply: bool,
    generated_at: datetime,
    target: Path,
) -> dict[str, Any]:
    """Build the versioned report payload from reconcile decision items."""
    counts: Counter[str] = Counter()
    regressions: list[dict[str, Any]] = []
    for item in items:
        decision = item.decision
        action = str(decision.action)
        counts[action] += 1
        code = getattr(decision, "code", None)
        if code not in _REGRESSION_CODES:
            continue
        helped, hurt = _decision_counts(item)
        regressions.append(
            {
                "artifact_id": str(decision.artifact_id),
                "prior_state": str(item.prior_status),
                "reason_code": code,
                "reason": str(decision.reason),
                "helped": helped,
                "hurt": hurt,
            }
        )
    audit = _receipt_audit(target)
    hold_ids = [row["artifact_id"] for row in regressions if row["reason_code"] in outcome.HOLD_CODES]
    report: dict[str, Any] = {
        "version": REPORT_VERSION,
        "generated_at": generated_at.isoformat(),
        "apply": bool(apply),
        "summary": {
            "decisions": len(items),
            "regression_holds": sum(1 for row in regressions if row["reason_code"] in outcome.HOLD_CODES),
            "regression_rollbacks": sum(1 for row in regressions if row["reason_code"] == outcome.REGRESSION_ROLLBACK),
            # Computed before ``_fit`` can drop trailing rows, so the brief names
            # holds the size cap removed from the list itself.
            "regression_hold_ids": hold_ids[:_BRIEF_MAX_IDS],
        },
        "decisions_by_action": dict(sorted(counts.items())),
        "regressions": regressions,
        "ineligibility_rate": audit["ineligibility_rate"],
        "leading_ineligibility_reason": audit["leading_ineligibility_reason"],
    }
    return _fit(report)


def mode_label(apply: object) -> str:
    """Return ``applied`` or ``dry-run`` for a report's ``apply`` flag."""
    return "applied" if apply is True else "dry-run"


def _opened_the_path_itself(fd: int, path: Path) -> bool:
    opened = os.fstat(fd)
    named = os.lstat(path)
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(opened.st_mode):
        return False
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


def load_report(target: Path) -> dict[str, Any]:
    """Load the report, returning an empty mapping on any path, size or parse error.

    A symlinked path is refused rather than followed, and a file larger than the
    cap is rejected before the body is used, so a reader can never be pointed at
    arbitrary bytes by a tampered workspace.
    """
    path = report_path(target)
    try:
        if _has_symlinked_component(path, path.parents[len(REPORT_RELATIVE_PATH.parts) - 1]):
            return {}
        if not path.is_file():
            return {}
        # O_NOFOLLOW closes the gap between the symlink check above and the open.
        # Windows has no O_NOFOLLOW, so also require that the opened file is the
        # same regular file the path names right now, not a link's target.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            if not _opened_the_path_itself(handle.fileno(), path):
                return {}
            raw = handle.read(REPORT_MAX_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > REPORT_MAX_BYTES:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != REPORT_VERSION
    ):
        return {}
    return payload


def brief_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a loaded report to the bounded fields the session brief prints.

    Hold ids come from ``summary.regression_hold_ids``, which the writer computes
    before the size cap can drop regression rows, so a capped report still names
    holds instead of reading ``(none)`` beside a nonzero hold count.
    """
    if not report:
        return {"present": False}
    raw_summary = report.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_regressions = report.get("regressions")
    regressions = raw_regressions if isinstance(raw_regressions, list) else []
    derived_ids = [
        str(row.get("artifact_id"))
        for row in regressions
        if isinstance(row, dict) and row.get("reason_code") in outcome.HOLD_CODES
    ]
    raw_ids = summary.get("regression_hold_ids")
    ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else derived_ids
    raw_count = summary.get("regression_holds")
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else len(ids)
    return {
        "present": True,
        "generated_at": report.get("generated_at"),
        "mode": mode_label(report.get("apply")),
        "regression_holds": count,
        "regression_hold_ids": ids[:_BRIEF_MAX_IDS],
        "ineligibility_rate": report.get("ineligibility_rate"),
        "leading_reason": report.get("leading_ineligibility_reason"),
    }


def _decision_counts(item: Any) -> tuple[int, int]:
    """Return helped/hurt for a decision item, from the scorer that decided it.

    Skills are decided by ``scorecard.decide_scorecard`` alone, so a skill that
    also carries legacy fingerprint cohorts must report the scorecard's
    effectiveness counts. Reading cohorts first could show ``helped=2 hurt=0``
    beside a regression hold. Cards, and artifacts with no scorecard, keep the
    cohort counts that decided them.
    """
    card = getattr(item, "scorecard", None)
    if str(getattr(item, "artifact_kind", "") or "") != "card" and card is not None:
        dimensions = getattr(card, "dimensions", None)
        effectiveness = dimensions.get("effectiveness") if isinstance(dimensions, dict) else None
        if not isinstance(effectiveness, dict):
            return 0, 0
        return int(effectiveness.get("helped", 0) or 0), int(effectiveness.get("hurt", 0) or 0)
    cohorts = getattr(item, "cohorts", None)
    current = getattr(cohorts, "current", None)
    if current is not None:
        return int(getattr(current, "helped", 0) or 0), int(getattr(current, "hurt", 0) or 0)
    return 0, 0


def _receipt_audit(target: Path) -> dict[str, Any]:
    """Reuse the receipt eligibility audit that ``outcome doctor --json`` reports."""
    from . import scorecard

    try:
        audit = scorecard.receipt_scorecard_audit(target)
    except (OSError, ValueError):
        return {"ineligibility_rate": 0.0, "leading_ineligibility_reason": None}
    return {
        "ineligibility_rate": audit.get("ineligibility_rate", 0.0),
        "leading_ineligibility_reason": audit.get("leading_ineligibility_reason"),
    }


def _encode(report: Mapping[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fit(report: dict[str, Any]) -> dict[str, Any]:
    """Drop trailing regression rows until the payload fits the byte cap.

    Counts stay complete, so a truncated list never understates how many holds
    or rollbacks the run took. ``regressions_omitted`` says how many rows were
    dropped and ``truncated`` marks the payload as partial. Rows are trimmed in
    batches so a large report cannot turn the cap into a long serialization loop.
    """
    if len(_encode(report)) <= REPORT_MAX_BYTES:
        return report
    regressions = report.get("regressions")
    if not isinstance(regressions, list) or not regressions:
        return report
    kept = list(regressions)
    while True:
        drop = max(1, len(kept) // 4)
        kept = kept[:-drop]
        trimmed = {
            **report,
            "regressions": kept,
            "truncated": True,
            "regressions_omitted": len(regressions) - len(kept),
        }
        if not kept or len(_encode(trimmed)) <= REPORT_MAX_BYTES:
            return trimmed
