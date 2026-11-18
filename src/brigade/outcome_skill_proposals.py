"""Inbox review requests for ratchet regressions (#2590, criterion 1).

``brigade outcome reconcile --proposals`` turns a verified regression on a
Brigade-bundled skill into one skills inbox proposal that carries a behavioral
contract. The trigger is a ratchet decision coded ``regression_hold`` or
``regression_rollback``. ``observed_regression_hold`` files nothing in this
slice, because its session attribution is not verified.

A proposal here is a review request, never authorization. It stages the current
skill text unchanged and is marked ``requires_revision``: Brigade drafts no
text and calls no model, and ``inbox accept`` refuses it until the operator files
a revised candidate. Only a copy whose own install receipt is a reviewed
``brigade-bundle`` and whose files still match that receipt is staged. A skill whose copies come from
different sources is flagged ``mixed-source``, and a bundled copy with a local edit is flagged
``local-edit``, because the regression is then not evidence about the bundled text. The contract is filled only from observed evidence: the
artifact id, the reason code, the receipt run ids and paths from the scorecard
receipt trail, and the failing check ids and commands read back from those
receipts. A receipt that is gone yields ``command unavailable``, never an
invented command.

This module never writes a SKILL.md, the registry or any skill file. It only
stages inbox copies through ``inbox_add``. Filing is idempotent through the
``outcome:<artifact_id>:<fingerprint>`` dedupe key (the reason code lives in the contract, not the
key), so one request exists per skill text and a rejected proposal stays muted, for a hold and a later
rollback alike, until the skill text changes. Every failure here warns on
stderr and is reported as a skipped row; none changes reconcile's exit code.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from . import outcome, scorecard
from .skills_cmd import registry as _registry_mod
from .skills_cmd.adapters import _fleet_receipts
from .skills_cmd.inbox import inbox_add, proposal_for_dedupe_key

if TYPE_CHECKING:
    import brigade.skills_cmd.install as _install_mod
else:
    # ``skills_cmd`` re-exports the ``install`` function over the submodule's attribute.
    _install_mod = importlib.import_module("brigade.skills_cmd.install")

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
    readable: bool
    checks: tuple[tuple[str | None, str], ...]  # (check_id or None, command) per failing effectiveness command


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


def _receipt_kind(skill_id: str, receipt: dict[str, Any]) -> str:
    """Classify one install receipt as ``bundled``, ``registry`` or ``other``.

    Only a Brigade bundle with a reviewed identity counts as bundled. A path
    install, or a receipt that does not parse as a source, is ``other``; nothing
    here guesses an upstream for it.
    """
    source = receipt.get("source")
    if not isinstance(source, dict):
        return "other"
    kind = source.get("kind")
    if (
        kind == "brigade-bundle"
        and source.get("reviewed") is True
        and source.get("identity") == f"{_registry_mod.BUNDLED_SOURCE_PREFIX}{skill_id}"
    ):
        return "bundled"
    return "registry" if kind == "registry" else "other"


def _copy_harness(target: Path, content: Path, skill_id: str) -> str | None:
    """Harness key whose adapter ``install_path`` is the directory holding ``content``, else None."""
    try:
        copy_dir = content.resolve().parent
        adapters = _install_mod._adapter_map(target)
    except (OSError, _registry_mod.SkillsStatePathError):
        return None
    for harness in adapters:
        try:
            install_dir, _escapes = _install_mod._evaluate_install_dir(target, harness, skill_id)
            if install_dir.resolve() == copy_dir:
                return harness
        except (OSError, ValueError, KeyError, _registry_mod.SkillsStatePathError):
            continue
    return None


def _classify(
    target: Path, skill_id: str, content: Path, receipts: dict[tuple[str, str], dict[str, Any]]
) -> tuple[str, str | None]:
    """Return ``(kind, flag)`` for the copy at ``content``.

    ``kind`` comes from that copy's own receipt (``other`` when it has none, as for
    a registry master), and a ``bundled`` copy must still match that receipt: a
    changed copy is flagged ``local-edit`` and one that cannot be judged is ``other``.
    ``flag`` is ``mixed-source`` when the skill's receipts disagree about their source,
    so a bundle copy in one harness never vouches for a registry or path copy in another.
    """
    kinds = {_receipt_kind(skill_id, receipt) for (name, _harness), receipt in receipts.items() if name == skill_id}
    harness = _copy_harness(target, content, skill_id)
    receipt = receipts.get((skill_id, harness)) if harness else None
    copy_kind = _receipt_kind(skill_id, receipt) if receipt is not None else "other"
    flag = "mixed-source" if len(kinds) > 1 else None
    if copy_kind == "bundled" and receipt is not None and harness is not None:
        inspection = _install_mod._inspect_installed_tree(target, content.parent)
        state = _install_mod._local_edit_state(receipt, inspection.files, skill_id=skill_id, harness=harness)
        if state == "changed":
            flag = flag or "local-edit"
        elif state != "current":
            copy_kind = "other"
    return copy_kind, flag


def _propose(target: Path, item: Any, receipts: dict[tuple[str, str], dict[str, Any]]) -> ProposalOutcome:
    from . import outcome_cmd

    decision = item.decision
    artifact_id = str(decision.artifact_id)
    code = str(decision.code)
    skill_id = _registry_mod._slug(artifact_id)
    content = outcome_cmd._artifact_content_path(target, artifact_id, "skill")
    if content is None or not content.parent.is_dir():
        return ProposalOutcome(artifact_id, code, SKIPPED, "skill text unavailable")
    try:
        content.resolve().relative_to(target)
    except (OSError, ValueError):
        # A linked worktree can resolve the parent clone's copy; never stage text from outside the target.
        return ProposalOutcome(artifact_id, code, SKIPPED, "skill text outside the target")
    kind, flag = _classify(target, skill_id, content, receipts)
    if flag:
        return ProposalOutcome(artifact_id, code, FLAGGED, flag)
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
    key = f"outcome:{artifact_id}:{fingerprint}"
    prior = proposal_for_dedupe_key(target, key)
    if prior is not None:
        return ProposalOutcome(artifact_id, code, EXISTING, str(prior.get("status") or ""), _proposal_id(prior))
    with _registry_mod._held_state_root(target, writable=False) as anchor:
        baseline = _registry_mod._baseline_identity(anchor, skill_id)
    contract = build_contract(target, item, baseline=baseline)
    evidence = _evidence(card)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = inbox_add(
            target=target,
            source=content.parent,
            skill_id=skill_id,
            summary=(
                f"Outcome ratchet {code} for {artifact_id} (helped={evidence.helped} hurt={evidence.hurt}); "
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
    if filed.get("deduped"):
        # A concurrent run filed this key between the pre-check and the held write.
        return ProposalOutcome(artifact_id, code, EXISTING, str(filed.get("status") or ""), _proposal_id(filed))
    return ProposalOutcome(artifact_id, code, FILED, "", _proposal_id(filed))


def _proposal_id(proposal: dict[str, Any]) -> str | None:
    value = proposal.get("proposal_id")
    return str(value) if value else None


@dataclass(frozen=True)
class _Evidence:
    """The one deduped receipt set that the card's helped and hurt counts come from."""

    helped: int
    hurt: int
    failed: tuple[scorecard.ReceiptAudit, ...]  # every deduped hurt receipt, oldest first


def _evidence(card: Any) -> _Evidence:
    """Dedupe the receipt trail by evidence unit exactly as the scorecard does."""
    eligible = [audit for audit in card.receipt_trail if audit.eligible and not audit.reused]
    deduped = scorecard._dedupe_audits(eligible)
    failed = tuple(audit for audit in deduped if audit.effectiveness == -1)
    return _Evidence(sum(1 for audit in deduped if audit.effectiveness == 1), len(failed), failed)


def build_contract(target: Path, item: Any, *, baseline: str | None) -> dict[str, Any]:
    """Build the version 1 behavioral contract from observed evidence only."""
    decision = item.decision
    artifact_id = str(decision.artifact_id)
    evidence = _evidence(item.scorecard)
    failed = _failed_receipts(target, evidence)
    run_ids = ", ".join(row.run_id for row in failed) or "none readable"
    shown = f" (newest {len(failed)} of {evidence.hurt})" if len(failed) < evidence.hurt else ""
    labels = sorted({_check_label(check_id, command) for row in failed for check_id, command in row.checks})
    checks = ", ".join(labels) if labels else COMMAND_UNAVAILABLE
    return {
        "schema_version": 1,
        "reason": (
            f"The outcome ratchet decided {decision.code} for {artifact_id} ({decision.reason}); "
            f"verified effectiveness for its current text is helped={evidence.helped} hurt={evidence.hurt}."
        ),
        "addressed_failure": (
            f"{evidence.hurt} verified effectiveness failure(s) against the current text of {artifact_id}. "
            f"Failing receipt run(s){shown}: {run_ids}. Failing check(s): {checks}."
        ),
        "change_category": "bugfix",
        "baseline_fingerprint": baseline,
        "expected_observable_behavior": _expectations(artifact_id, failed),
    }


def _check_label(check_id: str | None, command: str) -> str:
    return check_id if check_id else f"command `{command}` (no check id recorded)"


def _expectations(artifact_id: str, failed: Sequence[_FailedReceipt]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for receipt in failed:
        if not receipt.readable:
            entries = [
                (
                    f"{receipt.run_id}:receipt-unavailable",
                    f"The failing check recorded by receipt run {receipt.run_id} ({receipt.receipt}) passes "
                    f"against the revised skill; {COMMAND_UNAVAILABLE}, the receipt could not be read.",
                )
            ]
        elif not receipt.checks:
            entries = [
                (
                    f"{receipt.run_id}:no-failing-command",
                    f"The effectiveness checks recorded by receipt run {receipt.run_id} ({receipt.receipt}) "
                    f"pass against the revised skill; the receipt lists no failing effectiveness command, "
                    f"{COMMAND_UNAVAILABLE}.",
                )
            ]
        else:
            entries = [
                _check_entry(receipt, check_id, command, number)
                for number, (check_id, command) in enumerate(receipt.checks, start=1)
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


def _check_entry(receipt: _FailedReceipt, check_id: str | None, command: str, number: int) -> tuple[str, str]:
    where = f"it failed in receipt run {receipt.run_id} ({receipt.receipt})"
    if check_id:
        return (
            f"{receipt.run_id}:{check_id}",
            f"Check {check_id} (command: {command}) completes with exit code 0 against the revised skill; {where}.",
        )
    return (
        f"{receipt.run_id}:command-{number}",
        f"The command `{command}` completes with exit code 0 against the revised skill; {where} "
        "and the receipt records no check id for it.",
    )


def _failed_receipts(target: Path, evidence: _Evidence) -> list[_FailedReceipt]:
    """Newest deduped hurt receipts, with failing commands read back from each."""
    rows: list[_FailedReceipt] = []
    for audit in evidence.failed[-_MAX_RECEIPTS:]:
        path = Path(audit.receipt_path)
        receipt = _read_receipt(target, path)
        run_id = str(audit.run_id or path.parent.name)
        rows.append(_FailedReceipt(run_id, _relative(target, path), receipt is not None, _failing_checks(receipt)))
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


def _failing_checks(receipt: dict[str, Any] | None) -> tuple[tuple[str | None, str], ...]:
    commands = receipt.get("commands") if receipt else None
    if not isinstance(commands, list):
        return ()
    rows: list[tuple[str | None, str]] = []
    for command in commands:
        if not isinstance(command, dict) or command.get("check_role") != "effectiveness":
            continue
        if command.get("status") == "completed" and command.get("exit_code") == 0:
            continue
        check_id = command.get("check_id")
        rows.append((check_id[:120] if isinstance(check_id, str) and check_id else None, _command_text(command)))
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
