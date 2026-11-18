"""Inbox review requests for ratchet regressions (#2590, criterion 1).

``brigade outcome reconcile --proposals`` turns a verified regression on a
Brigade-bundled skill into one skills inbox proposal that carries a behavioral
contract. The trigger is a ratchet decision coded ``regression_hold`` or
``regression_rollback``. ``observed_regression_hold`` files nothing in this
slice, because its session attribution is not verified.

A proposal here is a review request, never authorization. It stages the current
skill text unchanged and is marked ``requires_revision``: Brigade drafts no
text and calls no model, and ``inbox accept`` refuses it until the operator files
a revised candidate. The contract is filled only from observed evidence: the
artifact id, the reason code, the receipt run ids and paths from the scorecard
receipt trail, and the failing check ids and commands read back from those
receipts. A receipt that is gone yields ``command unavailable``, never an
invented command.

This module never writes a SKILL.md, the registry or any skill file. It only
stages inbox copies through ``inbox_add``. Filing is idempotent through the
``outcome:<artifact_id>:<reason_code>:<fingerprint>`` dedupe key, so a rejected
proposal stays muted until the skill text changes. Every failure here warns on
stderr and is reported as a skipped row; none changes reconcile's exit code.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import outcome
from .skills_cmd import registry as _registry_mod
from .skills_cmd.adapters import _fleet_receipts
from .skills_cmd.inbox import inbox_add, proposal_for_dedupe_key

FILED = "filed"
EXISTING = "existing"
FLAGGED = "flagged"
SKIPPED = "skipped"
COUNT_KEYS = (FILED, EXISTING, FLAGGED, SKIPPED)

TRIGGER_CODES = frozenset({outcome.REGRESSION_HOLD, outcome.REGRESSION_ROLLBACK})
COMMAND_UNAVAILABLE = "command unavailable"

_MAX_RECEIPTS = 5
_MAX_CHECKS_PER_RECEIPT = 5
_MAX_COMMAND_CHARS = 160
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_ROWS_SHOWN = 20
_VERIFY_RUNS = Path(".brigade") / "work" / "verify-runs"


@dataclass(frozen=True)
class ProposalOutcome:
    """One regression decision and what became of its proposal."""

    artifact_id: str
    reason_code: str
    status: str
    detail: str = ""
    proposal_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "reason_code": self.reason_code,
            "status": self.status,
        }
        if self.detail:
            row["detail"] = self.detail
        if self.proposal_id:
            row["proposal_id"] = self.proposal_id
        return row


@dataclass(frozen=True)
class _FailedReceipt:
    run_id: str
    receipt: str
    checks: tuple[tuple[str, str], ...]  # (check_id, command), empty when the receipt is gone


def count_proposals(outcomes: Sequence[ProposalOutcome]) -> dict[str, int]:
    """Return the four bounded counts persisted in the ratchet report."""
    counts = {key: 0 for key in COUNT_KEYS}
    for row in outcomes:
        counts[row.status] += 1
    return counts


def file_regression_proposals(target: Path, items: Sequence[Any]) -> list[ProposalOutcome]:
    """File one inbox proposal per triggering skill decision and report each.

    ``items`` are reconcile decision items (``decision``, ``artifact_kind``,
    ``scorecard``). Cards and decisions outside ``TRIGGER_CODES`` are ignored.
    """
    target = target.expanduser().resolve()
    triggered = [
        item
        for item in items
        if str(getattr(item, "artifact_kind", "skill") or "skill") == "skill"
        and getattr(item.decision, "code", None) in TRIGGER_CODES
    ]
    if not triggered:
        return []
    try:
        receipts = _fleet_receipts(target)
    except Exception as exc:  # noqa: BLE001 - proposals never fail the ratchet
        receipts = {}
        print(f"warning: outcome proposals could not read install receipts: {exc}", file=sys.stderr)
    outcomes: list[ProposalOutcome] = []
    for item in triggered:
        artifact_id = str(item.decision.artifact_id)
        code = str(item.decision.code)
        try:
            outcomes.append(_propose(target, item, receipts))
        except Exception as exc:  # noqa: BLE001 - one artifact must not abort the run
            reason = f"{type(exc).__name__}: {exc}"[:200]
            print(f"warning: outcome proposal for {artifact_id} not filed: {reason}", file=sys.stderr)
            outcomes.append(ProposalOutcome(artifact_id, code, SKIPPED, f"error: {reason}"))
    return sorted(outcomes, key=lambda row: (row.artifact_id, row.reason_code))


def _source_kind(skill_id: str, receipts: dict[tuple[str, str], dict[str, Any]]) -> str:
    """Classify a skill as ``bundled``, ``registry`` or ``other`` from its install receipts.

    Only a Brigade bundle with a reviewed identity counts as bundled. Nothing
    here guesses an upstream for a registry or path skill.
    """
    kinds: set[str] = set()
    for (receipt_skill, _harness), receipt in receipts.items():
        if receipt_skill != skill_id:
            continue
        source = receipt.get("source")
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        if (
            kind == "brigade-bundle"
            and source.get("reviewed") is True
            and source.get("identity") == f"{_registry_mod.BUNDLED_SOURCE_PREFIX}{skill_id}"
        ):
            kinds.add("bundled")
        elif kind == "registry":
            kinds.add("registry")
    if "bundled" in kinds:
        return "bundled"
    return "registry" if "registry" in kinds else "other"


def _propose(target: Path, item: Any, receipts: dict[tuple[str, str], dict[str, Any]]) -> ProposalOutcome:
    from . import outcome_cmd

    decision = item.decision
    artifact_id = str(decision.artifact_id)
    code = str(decision.code)
    skill_id = _registry_mod._slug(artifact_id)
    kind = _source_kind(skill_id, receipts)
    if kind == "registry":
        return ProposalOutcome(artifact_id, code, FLAGGED, "registry")
    if kind != "bundled":
        return ProposalOutcome(artifact_id, code, SKIPPED, "unclassified")
    card = item.scorecard
    if card is None:
        return ProposalOutcome(artifact_id, code, SKIPPED, "no scorecard evidence")
    fingerprint = str(card.subject.content_fingerprint or "").removeprefix("sha256:")
    if not fingerprint:
        return ProposalOutcome(artifact_id, code, SKIPPED, "no content fingerprint")
    key = f"outcome:{artifact_id}:{code}:{fingerprint}"
    prior = proposal_for_dedupe_key(target, key)
    if prior is not None:
        return ProposalOutcome(artifact_id, code, EXISTING, str(prior.get("status") or ""), _proposal_id(prior))
    content = outcome_cmd._artifact_content_path(target, artifact_id, "skill")
    if content is None or not content.parent.is_dir():
        return ProposalOutcome(artifact_id, code, SKIPPED, "skill text unavailable")
    with _registry_mod._held_state_root(target, writable=False) as anchor:
        baseline = _registry_mod._baseline_identity(anchor, skill_id)
    contract = build_contract(target, item, baseline=baseline)
    helped, hurt = _effectiveness_counts(card)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = inbox_add(
            target=target,
            source=content.parent,
            skill_id=skill_id,
            summary=(
                f"Outcome ratchet {code} for {artifact_id} (helped={helped} hurt={hurt}); "
                "review request only, the staged text is the unrevised skill."
            ),
            behavioral_contract=contract,
            json_output=True,
            dedupe_key=key,
            provenance=f"brigade://outcome-regression/{skill_id}",
            requires_revision=True,
        )
    if rc not in (0, 1):
        return ProposalOutcome(artifact_id, code, SKIPPED, f"inbox refused the proposal (rc={rc})")
    filed = json.loads(out.getvalue())
    return ProposalOutcome(artifact_id, code, FILED, "", _proposal_id(filed))


def _proposal_id(proposal: dict[str, Any]) -> str | None:
    value = proposal.get("proposal_id")
    return str(value) if value else None


def _effectiveness_counts(card: Any) -> tuple[int, int]:
    effectiveness = card.dimensions.get("effectiveness") if isinstance(card.dimensions, dict) else None
    if not isinstance(effectiveness, dict):
        return 0, 0
    return int(effectiveness.get("helped", 0) or 0), int(effectiveness.get("hurt", 0) or 0)


def build_contract(target: Path, item: Any, *, baseline: str | None) -> dict[str, Any]:
    """Build the version 1 behavioral contract from observed evidence only."""
    decision = item.decision
    artifact_id = str(decision.artifact_id)
    helped, hurt = _effectiveness_counts(item.scorecard)
    failed = _failed_receipts(target, item.scorecard)
    run_ids = ", ".join(row.run_id for row in failed) or "none readable"
    check_ids = sorted({check_id for row in failed for check_id, _command in row.checks})
    checks = ", ".join(check_ids) if check_ids else COMMAND_UNAVAILABLE
    return {
        "schema_version": 1,
        "reason": (
            f"The outcome ratchet decided {decision.code} for {artifact_id} ({decision.reason}); "
            f"verified effectiveness for its current text is helped={helped} hurt={hurt}."
        ),
        "addressed_failure": (
            f"{hurt} verified effectiveness failure(s) against the current text of {artifact_id}. "
            f"Failing receipt run(s): {run_ids}. Failing check(s): {checks}."
        ),
        "change_category": "bugfix",
        "baseline_fingerprint": baseline,
        "expected_observable_behavior": _expectations(artifact_id, failed),
    }


def _expectations(artifact_id: str, failed: Sequence[_FailedReceipt]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for receipt in failed:
        if not receipt.checks:
            entries = [
                (
                    f"{receipt.run_id}:receipt-unavailable",
                    f"The failing check recorded by receipt run {receipt.run_id} ({receipt.receipt}) passes "
                    f"against the revised skill; {COMMAND_UNAVAILABLE}, the receipt could not be read.",
                )
            ]
        else:
            entries = [
                (
                    f"{receipt.run_id}:{check_id}",
                    f"Check {check_id} (command: {command}) completes with exit code 0 against the revised "
                    f"skill; it failed in receipt run {receipt.run_id} ({receipt.receipt}).",
                )
                for check_id, command in receipt.checks
            ]
        for entry_id, description in entries:
            if entry_id not in seen:
                seen.add(entry_id)
                rows.append({"id": entry_id, "description": description})
    if not rows:
        rows.append(
            {
                "id": "ratchet-regression",
                "description": (
                    f"A verified run of the effectiveness checks for {artifact_id} records no hurt outcome; "
                    f"{COMMAND_UNAVAILABLE}, no failing receipt could be read."
                ),
            }
        )
    return rows[:100]


def _failed_receipts(target: Path, card: Any) -> list[_FailedReceipt]:
    """Newest hurt receipts from the scorecard trail, with failing checks read back from each."""
    hurt = [audit for audit in card.receipt_trail if audit.eligible and not audit.reused and audit.effectiveness == -1]
    rows: list[_FailedReceipt] = []
    for audit in hurt[-_MAX_RECEIPTS:]:
        path = Path(audit.receipt_path)
        receipt = _read_receipt(target, path)
        run_id = str(audit.run_id or path.parent.name)
        rows.append(_FailedReceipt(run_id, _relative(target, path), _failing_checks(receipt)))
    return rows


def _relative(target: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(target).as_posix()
    except (OSError, ValueError):
        return f"{_VERIFY_RUNS.as_posix()}/{path.parent.name}/{path.name}"


def _read_receipt(target: Path, path: Path) -> dict[str, Any] | None:
    """Read one receipt under ``.brigade/work/verify-runs``, or None when gone, large or unreadable."""
    try:
        resolved = path.resolve()
        resolved.relative_to((target / _VERIFY_RUNS).resolve())
        with resolved.open("rb") as handle:
            raw = handle.read(_MAX_RECEIPT_BYTES + 1)
        if len(raw) > _MAX_RECEIPT_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _failing_checks(receipt: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    commands = receipt.get("commands") if receipt else None
    if not isinstance(commands, list):
        return ()
    rows: list[tuple[str, str]] = []
    for command in commands:
        if not isinstance(command, dict) or command.get("check_role") != "effectiveness":
            continue
        if command.get("status") == "completed" and command.get("exit_code") == 0:
            continue
        check_id = command.get("check_id")
        if isinstance(check_id, str) and check_id:
            rows.append((check_id[:120], _command_text(command)))
    return tuple(rows[:_MAX_CHECKS_PER_RECEIPT])


def _command_text(command: dict[str, Any]) -> str:
    argv = command.get("argv")
    text = command.get("command")
    if isinstance(argv, list) and argv:
        value = " ".join(str(part) for part in argv)
    elif isinstance(text, str) and text:
        value = text
    else:
        return COMMAND_UNAVAILABLE
    value = " ".join(value.split())
    return value if len(value) <= _MAX_COMMAND_CHARS else value[: _MAX_COMMAND_CHARS - 3] + "..."


def payload_rows(outcomes: Sequence[ProposalOutcome]) -> dict[str, Any]:
    """Bounded JSON block for reconcile output: counts plus up to 20 rows."""
    return {
        **count_proposals(outcomes),
        "proposals": [row.to_dict() for row in outcomes[:_MAX_ROWS_SHOWN]],
        "omitted": max(0, len(outcomes) - _MAX_ROWS_SHOWN),
    }


def text_lines(outcomes: Sequence[ProposalOutcome], *, apply: bool) -> list[str]:
    """Human lines for reconcile output; a dry run says it filed because ``--proposals`` was passed."""
    counts = count_proposals(outcomes)
    head = "proposals: " + " ".join(f"{key}={counts[key]}" for key in COUNT_KEYS)
    lines = [head]
    if not apply:
        lines.append("proposals: filed in a dry run because --proposals was passed; no status or decision changed")
    for row in outcomes[:_MAX_ROWS_SHOWN]:
        tail = f" {row.proposal_id}" if row.proposal_id else ""
        detail = f" ({row.detail})" if row.detail else ""
        lines.append(f"  - {row.status}: {row.artifact_id} [{row.reason_code}]{tail}{detail}")
    if len(outcomes) > _MAX_ROWS_SHOWN:
        lines.append(f"  ... {len(outcomes) - _MAX_ROWS_SHOWN} more not listed")
    return lines
