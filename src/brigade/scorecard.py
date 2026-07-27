"""Pure receipt-only skill scorecard projection from verify receipts (#572)."""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import outcome as outcome_core, outcome_cmd, verify_trial

SCORECARD_POLICY_VERSION = "scorecard.v1"
EFFECTIVE_WILSON_MIN = 0.15
_UTILITY_REQUIRED_TRIALS = 2


@dataclass(frozen=True)
class SubjectRef:
    artifact_kind: str
    artifact_id: str
    content_fingerprint: str


@dataclass
class ReceiptAudit:
    receipt_path: str
    run_id: str | None
    started_at: str | None
    eligible: bool
    reason: str
    attributed: bool
    subject_binding: dict[str, Any] | None
    effectiveness: int | None
    verifier_cost_s: float | None
    reused: bool
    evidence_unit_key: tuple[Any, ...] | None


@dataclass
class SubjectScorecard:
    subject: SubjectRef
    dimensions: dict[str, Any]
    utility_guardrails: dict[str, Any]
    ineligible_summary: dict[str, int] = field(default_factory=dict)
    receipt_trail: list[ReceiptAudit] = field(default_factory=list)


def _verify_runs_root(target: Path) -> Path:
    return target / ".brigade" / "work" / "verify-runs"


def discover_verify_receipt_paths(target: Path) -> list[Path]:
    root = _verify_runs_root(target)
    if not root.is_dir():
        return []
    return sorted(root.glob("*/receipt.json"))


def _load_receipt(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _binding_dict(receipt: dict[str, Any]) -> dict[str, Any] | None:
    binding = receipt.get("subject_binding")
    return binding if isinstance(binding, dict) else None


def _normalized_command(command: dict[str, Any]) -> str | None:
    argv = command.get("argv")
    if isinstance(argv, list) and argv:
        return " ".join(str(item) for item in argv)
    value = command.get("command")
    if isinstance(value, str) and value:
        return value
    return None


def _effectiveness_plan_tuple(receipt: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    pairs: list[tuple[str, str]] = []
    for command in _effectiveness_commands(receipt):
        check_id = command.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            return None
        normalized = _normalized_command(command)
        if normalized is None:
            return None
        pairs.append((check_id, normalized))
    if not pairs:
        return None
    return tuple(pairs)


def _patch_unit_key(binding: dict[str, Any]) -> tuple[Any, ...] | None:
    patch_binding = binding.get("patch_binding")
    if not isinstance(patch_binding, dict):
        return None
    parts: list[Any] = []
    for key in ("baseline_commit", "tree_fingerprint", "changes_patch_sha256", "subject_path"):
        value = patch_binding.get(key)
        if not isinstance(value, str) or not value:
            return None
        parts.append(value)
    return tuple(parts)


def _fixture_unit_key(binding: dict[str, Any]) -> tuple[Any, ...] | None:
    fixture_binding = binding.get("fixture_binding")
    if not isinstance(fixture_binding, dict):
        return None
    parts: list[Any] = []
    for key in ("manifest_id", "case_id", "check_id"):
        value = fixture_binding.get(key)
        if not isinstance(value, str) or not value:
            return None
        parts.append(value)
    return tuple(parts)


def _binding_unit_key(binding: dict[str, Any]) -> tuple[Any, ...] | None:
    mode = binding.get("binding_mode")
    if mode == "patch_backed":
        return _patch_unit_key(binding)
    if mode == "fixture_eval":
        return _fixture_unit_key(binding)
    return None


def utility_evidence_unit_key(binding: dict[str, Any], check_id: str) -> tuple[Any, ...] | None:
    fingerprint = binding.get("content_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    unit = _binding_unit_key(binding)
    if unit is None:
        return None
    return (fingerprint, unit, check_id)


def utility_trial_unit_key(binding: dict[str, Any]) -> tuple[Any, ...] | None:
    fingerprint = binding.get("content_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    unit = _binding_unit_key(binding)
    if unit is None:
        return None
    return (fingerprint, unit)


def evidence_unit_key(receipt: dict[str, Any], binding: dict[str, Any]) -> tuple[Any, ...] | None:
    fingerprint = binding.get("content_fingerprint")
    mode = binding.get("binding_mode")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not isinstance(mode, str) or not mode:
        return None
    unit = _binding_unit_key(binding)
    if unit is None:
        return None
    plan = _effectiveness_plan_tuple(receipt)
    if plan is None:
        return None
    return (fingerprint, mode, unit, plan)


def _effectiveness_commands(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return []
    return [
        command for command in commands if isinstance(command, dict) and command.get("check_role") == "effectiveness"
    ]


def effectiveness_outcome(receipt: dict[str, Any]) -> int | None:
    """Return +1, -1, or None when no effectiveness commands are present."""
    effectiveness = _effectiveness_commands(receipt)
    if not effectiveness:
        return None
    for command in effectiveness:
        if command.get("status") != "completed" or command.get("exit_code") != 0:
            return -1
    return 1


def _utility_commands(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return []
    return [
        command
        for command in commands
        if isinstance(command, dict) and command.get("check_role") == "utility_guardrail"
    ]


def utility_check_outcomes(receipt: dict[str, Any]) -> dict[str, bool]:
    outcomes: dict[str, bool] = {}
    for command in _utility_commands(receipt):
        check_id = command.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            continue
        outcomes[check_id] = command.get("status") == "completed" and command.get("exit_code") == 0
    return outcomes


def _receipt_duration_seconds(receipt: dict[str, Any]) -> float | None:
    duration = receipt.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration >= 0:
        return float(duration)
    return None


def _command_durations_seconds(receipt: dict[str, Any]) -> list[float]:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return []
    durations: list[float] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        duration = command.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            durations.append(float(duration))
    return durations


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _current_fingerprint(target: Path, artifact_id: str, artifact_kind: str) -> str | None:
    return outcome_cmd.artifact_fingerprint(target, artifact_id, artifact_kind)


def _normalize_fingerprint(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("sha256:")


def _fingerprints_match(left: str | None, right: str | None) -> bool:
    normalized_left = _normalize_fingerprint(left)
    normalized_right = _normalize_fingerprint(right)
    if normalized_left is None or normalized_right is None:
        return False
    return normalized_left == normalized_right


def _subject_ref(binding: dict[str, Any]) -> SubjectRef | None:
    artifact_kind = binding.get("artifact_kind")
    artifact_id = binding.get("artifact_id")
    fingerprint = binding.get("content_fingerprint")
    if not isinstance(artifact_kind, str) or not artifact_kind:
        return None
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    return SubjectRef(artifact_kind=artifact_kind, artifact_id=artifact_id, content_fingerprint=fingerprint)


def _audit_receipt(
    *,
    receipt_path: Path,
    receipt: dict[str, Any],
    target: Path,
) -> ReceiptAudit:
    binding = _binding_dict(receipt)
    projection = verify_trial.project_trial(
        receipt,
        target=target,
        patch_path=receipt_path.parent / "changes.patch",
    )
    reused = isinstance(receipt.get("reused_from"), str) and bool(receipt.get("reused_from"))
    unit_key = evidence_unit_key(receipt, binding) if binding is not None else None
    return ReceiptAudit(
        receipt_path=str(receipt_path),
        run_id=str(receipt.get("run_id")) if receipt.get("run_id") is not None else None,
        started_at=str(receipt.get("started_at")) if receipt.get("started_at") is not None else None,
        eligible=projection.eligible,
        reason=projection.reason,
        attributed=projection.attributed,
        subject_binding=binding,
        effectiveness=effectiveness_outcome(receipt),
        verifier_cost_s=_receipt_duration_seconds(receipt),
        reused=reused,
        evidence_unit_key=unit_key,
    )


def _dedupe_audits(audits: list[ReceiptAudit]) -> list[ReceiptAudit]:
    """Keep the first non-reused receipt per evidence unit."""
    seen: set[tuple[Any, ...]] = set()
    deduped: list[ReceiptAudit] = []
    for audit in sorted(audits, key=lambda item: (item.started_at or "", item.receipt_path)):
        if audit.reused:
            continue
        if audit.evidence_unit_key is None:
            deduped.append(audit)
            continue
        if audit.evidence_unit_key in seen:
            continue
        seen.add(audit.evidence_unit_key)
        deduped.append(audit)
    return deduped


def _retry_stability_metrics(audits: list[ReceiptAudit]) -> dict[str, Any]:
    cohorts: dict[tuple[Any, ...], list[ReceiptAudit]] = defaultdict(list)
    for audit in audits:
        if not audit.eligible or audit.reused or audit.evidence_unit_key is None:
            continue
        cohorts[audit.evidence_unit_key].append(audit)

    consistent = 0
    sequences = 0
    first_pass = 0
    fail_to_pass = 0
    flips = 0
    for cohort_audits in cohorts.values():
        ordered = sorted(cohort_audits, key=lambda item: (item.started_at or "", item.receipt_path))
        outcomes = [audit.effectiveness for audit in ordered if audit.effectiveness in {1, -1}]
        if not outcomes:
            continue
        sequences += 1
        if outcomes[0] == 1:
            first_pass += 1
        previous = outcomes[0]
        for outcome in outcomes[1:]:
            if previous == -1 and outcome == 1:
                fail_to_pass += 1
            if outcome != previous:
                flips += 1
            previous = outcome
        if len(set(outcomes)) == 1:
            consistent += 1
    rate = consistent / sequences if sequences else 0.0
    return {
        "consistent": consistent,
        "sequences": sequences,
        "first_pass_yield": first_pass / sequences if sequences else 0.0,
        "fail_to_pass": fail_to_pass,
        "flips": flips,
        "rate": rate,
    }


def _utility_guardrail_summary(
    *,
    audits: list[ReceiptAudit],
    receipts_by_path: dict[str, dict[str, Any]],
    required_check_ids: list[str],
) -> dict[str, Any]:
    if not required_check_ids:
        return {
            "required_check_ids": [],
            "passing_trials": 0,
            "required_trials": 0,
            "per_check": {},
        }
    passing_units = 0
    seen_trial_units: set[tuple[Any, ...]] = set()
    seen_check_units: dict[str, set[tuple[Any, ...]]] = {check_id: set() for check_id in required_check_ids}
    per_check = {check_id: {"passing_units": 0, "failing_units": 0} for check_id in required_check_ids}
    for audit in audits:
        if not audit.eligible or audit.reused:
            continue
        binding = audit.subject_binding
        if not isinstance(binding, dict):
            continue
        trial_unit = utility_trial_unit_key(binding)
        receipt = receipts_by_path.get(audit.receipt_path)
        if receipt is None:
            continue
        outcomes = utility_check_outcomes(receipt)
        for check_id in required_check_ids:
            unit_key = utility_evidence_unit_key(binding, check_id)
            if unit_key is None or unit_key in seen_check_units[check_id]:
                continue
            seen_check_units[check_id].add(unit_key)
            if outcomes.get(check_id) is True:
                per_check[check_id]["passing_units"] += 1
            else:
                per_check[check_id]["failing_units"] += 1
        if trial_unit is None or trial_unit in seen_trial_units:
            continue
        seen_trial_units.add(trial_unit)
        if all(outcomes.get(check_id) is True for check_id in required_check_ids):
            passing_units += 1
    return {
        "required_check_ids": required_check_ids,
        "passing_trials": passing_units,
        "required_trials": _UTILITY_REQUIRED_TRIALS,
        "per_check": per_check,
    }


def _build_subject_scorecard(
    *,
    subject: SubjectRef,
    audits: list[ReceiptAudit],
    receipts_by_path: dict[str, dict[str, Any]],
) -> SubjectScorecard:
    ineligible_summary = Counter(audit.reason for audit in audits if audit.attributed and not audit.eligible)
    eligible_audits = [audit for audit in audits if audit.eligible and not audit.reused]
    deduped = _dedupe_audits(eligible_audits)
    stability_audits = sorted(eligible_audits, key=lambda item: (item.started_at or "", item.receipt_path))

    helped = sum(1 for audit in deduped if audit.effectiveness == 1)
    hurt = sum(1 for audit in deduped if audit.effectiveness == -1)
    trials = helped + hurt
    wilson = outcome_core.wilson_lower_bound(helped, trials)

    receipt_durations = [audit.verifier_cost_s for audit in deduped if audit.verifier_cost_s is not None]
    command_durations: list[float] = []
    for audit in deduped:
        receipt = receipts_by_path.get(audit.receipt_path)
        if receipt is None:
            continue
        command_durations.extend(_command_durations_seconds(receipt))
    verifier_cost = {
        "median_s": statistics.median(receipt_durations) if receipt_durations else 0.0,
        "p95_s": _percentile(receipt_durations, 95.0),
        "trials": len(receipt_durations),
        "receipt_median_s": statistics.median(receipt_durations) if receipt_durations else 0.0,
        "receipt_p95_s": _percentile(receipt_durations, 95.0),
        "receipt_samples": len(receipt_durations),
        "command_median_s": statistics.median(command_durations) if command_durations else 0.0,
        "command_p95_s": _percentile(command_durations, 95.0),
        "command_samples": len(command_durations),
    }

    integrity_eligible = len(eligible_audits)
    integrity_audit = len([audit for audit in audits if audit.attributed])
    evidence_integrity = {
        "eligible": integrity_eligible,
        "audit": integrity_audit,
        "ratio": integrity_eligible / integrity_audit if integrity_audit else 0.0,
    }

    required_ids: list[str] = []
    for audit in audits:
        receipt = receipts_by_path.get(audit.receipt_path)
        if receipt is None:
            continue
        required = receipt.get("required_utility_check_ids")
        if isinstance(required, list):
            for check_id in required:
                if isinstance(check_id, str) and check_id and check_id not in required_ids:
                    required_ids.append(check_id)

    return SubjectScorecard(
        subject=subject,
        dimensions={
            "effectiveness": {
                "helped": helped,
                "hurt": hurt,
                "wilson": wilson,
                "trials": trials,
            },
            "verifier_cost": verifier_cost,
            "retry_stability": _retry_stability_metrics(stability_audits),
            "evidence_integrity": evidence_integrity,
        },
        utility_guardrails=_utility_guardrail_summary(
            audits=deduped,
            receipts_by_path=receipts_by_path,
            required_check_ids=required_ids,
        ),
        ineligible_summary=dict(sorted(ineligible_summary.items())),
        receipt_trail=sorted(audits, key=lambda item: (item.started_at or "", item.receipt_path)),
    )


def build_scorecards(target: Path) -> list[SubjectScorecard]:
    target = target.expanduser().resolve()
    grouped: dict[tuple[str, str, str], list[ReceiptAudit]] = defaultdict(list)
    receipts_by_path: dict[str, dict[str, Any]] = {}

    for receipt_path in discover_verify_receipt_paths(target):
        receipt = _load_receipt(receipt_path)
        if receipt is None:
            continue
        receipts_by_path[str(receipt_path)] = receipt
        binding = _binding_dict(receipt)
        if binding is None:
            continue
        subject = _subject_ref(binding)
        if subject is None:
            continue
        current_fp = _current_fingerprint(target, subject.artifact_id, subject.artifact_kind)
        cohort_fp = current_fp or subject.content_fingerprint
        if not _fingerprints_match(subject.content_fingerprint, cohort_fp):
            continue
        audit = _audit_receipt(receipt_path=receipt_path, receipt=receipt, target=target)
        normalized_fp = _normalize_fingerprint(cohort_fp) or cohort_fp
        grouped[(subject.artifact_kind, subject.artifact_id, normalized_fp)].append(audit)

    cards: list[SubjectScorecard] = []
    for (artifact_kind, artifact_id, fingerprint), audits in sorted(grouped.items()):
        subject = SubjectRef(
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            content_fingerprint=fingerprint,
        )
        cards.append(
            _build_subject_scorecard(
                subject=subject,
                audits=audits,
                receipts_by_path=receipts_by_path,
            )
        )
    return cards


def scorecard_for_artifact(
    target: Path,
    artifact_id: str,
    *,
    artifact_kind: str | None = None,
) -> SubjectScorecard | None:
    for card in build_scorecards(target):
        if card.subject.artifact_id != artifact_id:
            continue
        if artifact_kind is not None and card.subject.artifact_kind != artifact_kind:
            continue
        return card
    return None


def _receipt_audit_to_dict(audit: ReceiptAudit) -> dict[str, Any]:
    return {
        "receipt_path": audit.receipt_path,
        "run_id": audit.run_id,
        "started_at": audit.started_at,
        "eligible": audit.eligible,
        "reason": audit.reason,
        "attributed": audit.attributed,
        "subject_binding": audit.subject_binding,
        "effectiveness": audit.effectiveness,
        "verifier_cost_s": audit.verifier_cost_s,
        "reused": audit.reused,
        "evidence_unit_key": list(audit.evidence_unit_key) if audit.evidence_unit_key is not None else None,
    }


def subject_scorecard_to_dict(card: SubjectScorecard) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": SCORECARD_POLICY_VERSION,
        "subject": {
            "artifact_kind": card.subject.artifact_kind,
            "artifact_id": card.subject.artifact_id,
            "content_fingerprint": card.subject.content_fingerprint,
        },
        "dimensions": card.dimensions,
        "utility_guardrails": card.utility_guardrails,
        "ineligible_summary": card.ineligible_summary,
        "receipt_trail": [_receipt_audit_to_dict(audit) for audit in card.receipt_trail],
    }


def explain_payload(target: Path, artifact_id: str, *, artifact_kind: str | None = None) -> dict[str, Any] | None:
    card = scorecard_for_artifact(target, artifact_id, artifact_kind=artifact_kind)
    if card is None:
        return None
    return subject_scorecard_to_dict(card)


def classify_band(
    card: SubjectScorecard | None,
    *,
    persisted_status: str | None = None,
    policy_marker: str | None = None,
) -> str:
    """Exploration band classifier used by route policy (#573)."""
    from .route_policy import classify_band as _classify_band

    return _classify_band(card, persisted_status=persisted_status, policy_marker=policy_marker)


def route_policy_marker_for_promotion() -> dict[str, str]:
    """Persisted on promoted status so route_policy grants full authority (#503)."""
    return {
        "policy_version": SCORECARD_POLICY_VERSION,
        "route_authority": "full",
    }


def utility_gate_reason(
    card: SubjectScorecard,
    *,
    min_passing_units: int = _UTILITY_REQUIRED_TRIALS,
) -> str | None:
    """Return a withhold reason when utility guardrails fail, else None."""
    utility = card.utility_guardrails
    required_ids = utility.get("required_check_ids") or []
    if not required_ids:
        return None
    per_check = utility.get("per_check") or {}
    for check_id in required_ids:
        check_stats = per_check.get(check_id) if isinstance(per_check, dict) else None
        if not isinstance(check_stats, dict):
            return f"withheld: utility_guardrail {check_id}"
        failing = int(check_stats.get("failing_units", 0))
        if failing > 0:
            return f"withheld: utility_guardrail {check_id}"
        passing = int(check_stats.get("passing_units", 0))
        if passing < min_passing_units:
            return f"withheld: utility_guardrail {check_id}"
    return None


def effectiveness_gate_reason(
    card: SubjectScorecard,
    *,
    config: outcome_core.ReconcileConfig,
) -> str | None:
    """Return a withhold reason when effectiveness fails promotion, else None."""
    effectiveness = card.dimensions.get("effectiveness", {})
    helped = int(effectiveness.get("helped", 0))
    hurt = int(effectiveness.get("hurt", 0))
    trials = helped + hurt
    wilson = outcome_core.wilson_lower_bound(helped, trials, config.z)
    if hurt > 0:
        return "withheld: verified regression present"
    if helped < config.install_min_helped:
        return "insufficient verified evidence"
    wilson_min = config.effective_wilson_min
    if wilson < wilson_min:
        return f"withheld: effectiveness wilson below {wilson_min:g}"
    return None


def dual_gate_passes(
    card: SubjectScorecard,
    *,
    config: outcome_core.ReconcileConfig,
) -> tuple[bool, str]:
    """Evaluate effectiveness AND utility promotion criteria (#503)."""
    effectiveness_reason = effectiveness_gate_reason(card, config=config)
    if effectiveness_reason is not None:
        return False, effectiveness_reason
    utility_reason = utility_gate_reason(card, min_passing_units=config.utility_min_passing_units)
    if utility_reason is not None:
        return False, utility_reason
    return True, "verified helped, no regressions"


def decide_scorecard(
    card: SubjectScorecard | None,
    *,
    artifact_id: str,
    current_status: str,
    last_action_ts: dt.datetime | None,
    now: dt.datetime,
    config: outcome_core.ReconcileConfig,
) -> outcome_core.Decision:
    """Decide promote/hold/rollback from a receipt-only scorecard (#503)."""
    if card is None:
        if current_status == "promoted":
            return outcome_core.Decision(
                artifact_id,
                "rollback",
                "demoted",
                "withheld: missing scorecard",
            )
        return outcome_core.Decision(artifact_id, "hold", current_status, "withheld: missing scorecard")

    effectiveness = card.dimensions.get("effectiveness", {})
    helped = int(effectiveness.get("helped", 0))
    hurt = int(effectiveness.get("hurt", 0))

    # Any trusted current-cohort hurt on a promoted skill demotes immediately,
    # before cooldown and regardless of revert_min_hurt.
    if current_status == "promoted" and hurt > 0:
        return outcome_core.Decision(artifact_id, "rollback", "demoted", "verified regression measured")

    if last_action_ts is not None and (now - last_action_ts).total_seconds() < config.cooldown_seconds:
        return outcome_core.Decision(artifact_id, "hold", current_status, "cooldown active")

    if current_status == "candidate":
        if hurt > 0:
            return outcome_core.Decision(artifact_id, "hold", "candidate", "withheld: verified regression present")
        passes, reason = dual_gate_passes(card, config=config)
        if passes:
            return outcome_core.Decision(artifact_id, "install", "promoted", reason)
        return outcome_core.Decision(artifact_id, "hold", "candidate", reason)

    if current_status == "promoted":
        if helped >= config.bump_min_helped:
            passes, reason = dual_gate_passes(card, config=config)
            if passes:
                return outcome_core.Decision(artifact_id, "bump", "promoted", "sustained verified helped")
        return outcome_core.Decision(artifact_id, "hold", "promoted", "no change")

    return outcome_core.Decision(artifact_id, "hold", current_status, "terminal status")


def project_scorecard_statuses(
    scorecards: dict[str, SubjectScorecard],
    *,
    config: outcome_core.ReconcileConfig,
    now: dt.datetime,
) -> dict[str, outcome_core.Decision]:
    """Fork primitive: project scorecard ratchet from a clean candidate baseline."""
    return {
        artifact_id: decide_scorecard(
            card,
            artifact_id=artifact_id,
            current_status="candidate",
            last_action_ts=None,
            now=now,
            config=config,
        )
        for artifact_id, card in scorecards.items()
    }
