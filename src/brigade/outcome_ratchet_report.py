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
import re
import stat
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import localio, outcome
from .outcome_skill_exec import PendingAction, PendingPhysical, sort_pending

REPORT_VERSION = 1
REPORT_MAX_BYTES = 64 * 1024
REPORT_RELATIVE_PATH = Path(".brigade") / "memory-care" / "outcome" / "ratchet-report.json"
_REGRESSION_CODES = outcome.HOLD_CODES | {outcome.REGRESSION_ROLLBACK}
_BRIEF_MAX_IDS = 6
_PENDING_MAX_ARTIFACTS = 20
_PENDING_MAX_COMMANDS = 8
_PROPOSAL_KEYS = ("filed", "existing", "flagged", "skipped")
DRIFT_VERSION = 1
DRIFT_MAX_ROWS = 20
_DRIFT_FIELD_MAX = 240
_DRIFT_COUNT_KEYS = ("current", "refreshable", "flagged", "skipped")
_DRIFT_ROW_KEYS = ("skill_id", "harness", "path", "reason")
_DRIFT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


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
    pending: PendingPhysical | None = None,
    proposal_counts: Mapping[str, int] | None = None,
) -> Path | None:
    """Write the advisory report once and return its path, or None if skipped.

    ``items`` are reconcile decision items: each carries ``decision``,
    ``prior_status``, ``artifact_kind`` and either fingerprint ``cohorts`` or a
    ``scorecard``. Only the fields below are read from them. ``pending`` holds the
    operator commands (per skill, recent status changes only) that would apply a
    status to the installed copies, plus a count of older ones; reconcile is
    routing only and never runs them. ``proposal_counts`` carries the filed,
    existing, flagged and skipped inbox proposal counts of a ``--proposals`` run;
    it is None when the run did not ask for proposals.

    A symlinked report path is refused, and a payload that cannot fit the byte
    cap even without regression rows is skipped, so a broken report can never
    fail or grow the ratchet run that produced it. An unwritable path (read-only
    directory, full disk, a directory in place of the file) is the same: the run
    warns once on stderr and keeps its own exit code and output.
    """
    try:
        return _write_report(
            target, items, apply=apply, generated_at=generated_at, pending=pending, proposal_counts=proposal_counts
        )
    except OSError as exc:
        print(f"warning: outcome ratchet report not written: {exc}", file=sys.stderr)
        return None


def _write_report(
    target: Path,
    items: Sequence[Any],
    *,
    apply: bool,
    generated_at: datetime,
    pending: PendingPhysical | None = None,
    proposal_counts: Mapping[str, int] | None = None,
) -> Path | None:
    path = report_path(target)
    if _has_symlinked_component(path, path.parents[len(REPORT_RELATIVE_PATH.parts) - 1]):
        # The previous report stays in place, so say this run's write was skipped.
        print(f"warning: outcome ratchet report not written: symlinked path refused: {path}", file=sys.stderr)
        return None
    # The drift check runs as its own runbook step and stores its result here. A later
    # reconcile run rewrites the file, so keep the last drift result rather than drop it.
    # It is built into the report before the size fit, so the cap covers both.
    report = build_report(
        items,
        apply=apply,
        generated_at=generated_at,
        target=target,
        pending=pending,
        proposal_counts=proposal_counts,
        skill_drift=drift_payload(load_report(target).get("skill_drift")),
    )
    return _store(path, report)


def _store(path: Path, report: Mapping[str, Any]) -> Path | None:
    encoded = _encode(report)
    if len(encoded) > REPORT_MAX_BYTES:
        print(
            f"warning: outcome ratchet report not written: still over {REPORT_MAX_BYTES} bytes after truncation",
            file=sys.stderr,
        )
        return None
    localio.write_text_atomic(path, encoded.decode("utf-8"))
    return path


def merge_skill_drift(target: Path, drift: Mapping[str, Any]) -> Path | None:
    """Store the scheduled skill drift result in the ratchet report and return its path.

    A report that loads gets a ``skill_drift`` block and is written back whole, fitted to the
    size cap with the block counted. With no report on disk, only ``version`` and ``skill_drift``
    are stored: no reconcile summary is invented, so readers still say the ratchet has not run.
    A file that exists but cannot be loaded (unreadable, over the cap, a symlink, another
    version) is never replaced, because the bytes were not read. The result is skipped with a
    warning on a symlinked or unwritable path, exactly like ``write_report``.
    """
    try:
        path = report_path(target)
        if _has_symlinked_component(path, path.parents[len(REPORT_RELATIVE_PATH.parts) - 1]):
            print(f"warning: skill drift not recorded: symlinked report path refused: {path}", file=sys.stderr)
            return None
        bounded = drift_payload(drift)
        if bounded is None:
            return None
        report = load_report(target)
        if not report:
            if os.path.lexists(path):
                print(
                    f"warning: skill drift not recorded: the existing ratchet report could not be read: {path}",
                    file=sys.stderr,
                )
                return None
            report = {"version": REPORT_VERSION}
        return _store(path, _fit({**report, "skill_drift": bounded}))
    except OSError as exc:
        print(f"warning: skill drift not recorded: {exc}", file=sys.stderr)
        return None


def has_ratchet(report: Mapping[str, Any]) -> bool:
    """True when the report holds a reconcile summary, not only a stored drift result."""
    return isinstance(report.get("summary"), dict)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def is_workspace_relative_path(value: object) -> bool:
    """True for a normalized workspace-relative POSIX path, the only form a drift row may show.

    Rejects absolute and drive-prefixed paths, backslashes, control characters, empty, ``.`` and
    ``..`` segments, so a hand-written report cannot make a surface print a home directory.
    """
    if not isinstance(value, str) or not value or len(value) > _DRIFT_FIELD_MAX:
        return False
    if value.startswith("/") or "\\" in value or _DRIVE_PREFIX.match(value):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def refresh_command(skill_id: str, harness: str) -> str:
    """The operator command that refreshes one installed copy, run from the workspace root."""
    return f"brigade skills install bundled:{skill_id} --workspace . --target {harness} --force"


def _drift_rows(raw: object, *, refreshable: bool) -> tuple[list[dict[str, str]], int]:
    """Bounded rows from a report block, and how many it held that were not kept.

    A row is kept only when its path is a workspace-relative POSIX path. A refreshable row
    also needs token-safe ids, and its command is rebuilt from them, never read from the file.
    """
    rows: list[dict[str, str]] = []
    seen = 0
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, Mapping):
            continue
        seen += 1
        if len(rows) >= DRIFT_MAX_ROWS or not is_workspace_relative_path(row.get("path")):
            continue
        item = {key: str(row.get(key) or "")[:_DRIFT_FIELD_MAX] for key in _DRIFT_ROW_KEYS}
        if refreshable:
            if not (_DRIFT_TOKEN.fullmatch(item["skill_id"]) and _DRIFT_TOKEN.fullmatch(item["harness"])):
                continue
            item.pop("reason")
            item["command"] = refresh_command(item["skill_id"], item["harness"])
        rows.append(item)
    return rows, seen - len(rows)


def drift_payload(raw: object) -> dict[str, Any] | None:
    """Reduce a drift result, or a ``skill_drift`` block read from the report file, to bounded known fields.

    Returns None when ``raw`` is not a mapping with a ``counts`` mapping. Counts become
    non-negative integers. Each row list keeps at most ``DRIFT_MAX_ROWS`` rows of short strings,
    and a row whose path is not a normalized workspace-relative POSIX path is dropped while the
    counts stay. A tampered report file cannot grow, reshape or point the output at a home path.
    """
    if not isinstance(raw, Mapping) or not isinstance(raw.get("counts"), Mapping):
        return None
    counts_raw = raw["counts"]
    counts = {key: _count(counts_raw.get(key)) for key in _DRIFT_COUNT_KEYS}
    flagged, flagged_dropped = _drift_rows(raw.get("flagged"), refreshable=False)
    refreshable, refreshable_dropped = _drift_rows(raw.get("refreshable"), refreshable=True)
    # Whatever the counts say that the list does not show was omitted.
    return {
        "version": DRIFT_VERSION,
        "generated_at": str(raw.get("generated_at") or "")[:64] or None,
        "counts": counts,
        "flagged": flagged,
        "flagged_omitted": max(_count(raw.get("flagged_omitted")), flagged_dropped, counts["flagged"] - len(flagged)),
        "refreshable": refreshable,
        "refreshable_omitted": max(
            _count(raw.get("refreshable_omitted")), refreshable_dropped, counts["refreshable"] - len(refreshable)
        ),
    }


def build_report(
    items: Sequence[Any],
    *,
    apply: bool,
    generated_at: datetime,
    target: Path,
    pending: PendingPhysical | None = None,
    proposal_counts: Mapping[str, int] | None = None,
    skill_drift: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the versioned report payload from reconcile decision items.

    ``skill_drift`` is an already bounded drift block to carry over. It is attached before
    the size fit so the cap accounts for it, and the fit may drop its listed rows (never its counts).
    """
    counts: Counter[str] = Counter()
    regressions: list[dict[str, Any]] = []
    local_trials_total = 0
    attested_trials_total = 0
    ci_observed_total = 0
    for item in items:
        decision = item.decision
        action = str(decision.action)
        counts[action] += 1
        code = getattr(decision, "code", None)
        local_trials, attested_trials = _decision_attestation_counts(item)
        local_trials_total += local_trials
        attested_trials_total += attested_trials
        ci_observed_total += _decision_ci_observed(item)
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
                "local_trials": local_trials,
                "attested_trials": attested_trials,
            }
        )
    audit = _receipt_audit(target)
    pending_set = pending or PendingPhysical()
    recent = {key: action for key, action in pending_set.recent.items() if action.commands}
    stale = {key: action for key, action in pending_set.stale.items() if action.commands}
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
            "pending_physical_actions": sum(len(action.commands) for action in recent.values()),
            "stale_physical_actions": pending_set.stale_actions,
            "local_trials": local_trials_total,
            "attested_trials": attested_trials_total,
            "ci_observed_trials": ci_observed_total,
        },
        "pending_physical": _pending_rows(recent),
        "stale_physical": _pending_rows(stale),
        "decisions_by_action": dict(sorted(counts.items())),
        "regressions": regressions,
        "ineligibility_rate": audit["ineligibility_rate"],
        "leading_ineligibility_reason": audit["leading_ineligibility_reason"],
    }
    if proposal_counts is not None:
        report["summary"]["outcome_proposals"] = {key: int(proposal_counts.get(key, 0)) for key in _PROPOSAL_KEYS}
    if skill_drift is not None:
        report["skill_drift"] = dict(skill_drift)
    return _fit(report)


def _pending_rows(actions: Mapping[str, PendingAction]) -> list[dict[str, Any]]:
    """Bounded rows, current-run first then newest, so the cap never drops a fresh change."""
    return [
        {
            "artifact_id": artifact_id,
            "status": action.status,
            "status_changed_at": action.changed_at,
            "commands": [str(c) for c in action.commands[:_PENDING_MAX_COMMANDS]],
        }
        for artifact_id, action in sort_pending(actions)[:_PENDING_MAX_ARTIFACTS]
    ]


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


def reported_trial_counts(raw: Mapping[str, Any]) -> tuple[str, str]:
    """Render trial counts read from a report file as reported values, never as trusted ones.

    The report lives in the target workspace and the scored agent can write it, so a
    reader must not mistake these numbers for a recomputed or independent attestation.
    """

    def label(key: str) -> str:
        value = raw.get(key)
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else "not recorded"

    return label("local_trials"), label("attested_trials")


def reported_ci_observed(raw: Mapping[str, Any]) -> str:
    """Render the CI-observed trial count from a report file. Informational only, and never trusted."""
    value = raw.get("ci_observed_trials")
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "not recorded"


def proposal_counts(summary: Mapping[str, Any]) -> dict[str, int] | None:
    """Return the persisted inbox proposal counts, or None when the run did not file proposals."""
    raw = summary.get("outcome_proposals")
    if not isinstance(raw, dict):
        return None
    return {
        key: raw[key] if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) else 0
        for key in _PROPOSAL_KEYS
    }


def brief_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a loaded report to the bounded fields the session brief prints.

    Hold ids come from ``summary.regression_hold_ids``, which the writer computes
    before the size cap can drop regression rows, so a capped report still names
    holds instead of reading ``(none)`` beside a nonzero hold count.
    """
    if not has_ratchet(report):
        # No reconcile summary: either nothing is stored or only the drift check has run.
        # The ratchet is absent either way, and a stored drift result is still shown.
        drift = drift_payload(report.get("skill_drift"))
        return {"present": False} if drift is None else {"present": False, "skill_drift": drift}
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
    raw_pending = summary.get("pending_physical_actions")
    pending_count = raw_pending if isinstance(raw_pending, int) and not isinstance(raw_pending, bool) else 0
    raw_stale = summary.get("stale_physical_actions")
    stale_count = raw_stale if isinstance(raw_stale, int) and not isinstance(raw_stale, bool) else 0
    local_label, attested_label = reported_trial_counts(summary)
    return {
        "present": True,
        "local_trials": local_label,
        "attested_trials": attested_label,
        "ci_observed_trials": reported_ci_observed(summary),
        "outcome_proposals": proposal_counts(summary),
        "generated_at": report.get("generated_at"),
        "pending_physical_actions": pending_count,
        "stale_physical_actions": stale_count,
        "mode": mode_label(report.get("apply")),
        "regression_holds": count,
        "regression_hold_ids": ids[:_BRIEF_MAX_IDS],
        "ineligibility_rate": report.get("ineligibility_rate"),
        "leading_reason": report.get("leading_ineligibility_reason"),
        "skill_drift": drift_payload(report.get("skill_drift")),
    }


def _decision_attestation_counts(item: Any) -> tuple[int, int]:
    """Return (local, attested) trial counts from the scorecard that decided an item."""
    card = getattr(item, "scorecard", None)
    dimensions = getattr(card, "dimensions", None)
    effectiveness = dimensions.get("effectiveness") if isinstance(dimensions, dict) else None
    if not isinstance(effectiveness, dict):
        return 0, 0
    return (
        int(effectiveness.get("local_trials", 0) or 0),
        int(effectiveness.get("attested_trials", 0) or 0),
    )


def _decision_ci_observed(item: Any) -> int:
    """Display-only count of trials whose CI check run was seen passing. Never read by a decision."""
    value = getattr(getattr(item, "scorecard", None), "ci_observed_trials", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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


def _fits(report: Mapping[str, Any]) -> bool:
    return len(_encode(report)) <= REPORT_MAX_BYTES


def _fit(report: dict[str, Any]) -> dict[str, Any]:
    """Drop rows until the payload fits the byte cap, least important first.

    Stale pending rows go first, then trailing regression rows, then the listed drift
    rows. Counts stay complete, so a truncated list never understates how many holds,
    rollbacks or flagged copies there are. ``regressions_omitted`` and the drift block's
    ``flagged_omitted`` say how many rows were dropped and ``truncated`` marks the payload
    as partial. Regression rows are trimmed in batches so a large report cannot turn the
    cap into a long serialization loop.
    """
    if _fits(report):
        return report
    # Stale rows are listed only for reference and their count stays in
    # ``stale_physical_actions``, so drop them before any regression row.
    if report.get("stale_physical"):
        report = {**report, "stale_physical": [], "stale_physical_truncated": True}
        if _fits(report):
            return report
    regressions = report.get("regressions")
    if isinstance(regressions, list) and regressions:
        kept = list(regressions)
        while True:
            drop = max(1, len(kept) // 4)
            kept = kept[:-drop]
            report = {
                **report,
                "regressions": kept,
                "truncated": True,
                "regressions_omitted": len(regressions) - len(kept),
            }
            if not kept or _fits(report):
                break
        if _fits(report):
            return report
    drift = drift_payload(report.get("skill_drift"))
    if drift is not None and (drift["flagged"] or drift["refreshable"]):
        report = {
            **report,
            "skill_drift": {
                **drift,
                "flagged": [],
                "flagged_omitted": max(drift["flagged_omitted"], len(drift["flagged"])),
                "refreshable": [],
                "refreshable_omitted": max(drift["refreshable_omitted"], len(drift["refreshable"])),
            },
        }
    return report
