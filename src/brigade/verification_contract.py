"""VerificationContract: declare verifier, rollback, and budget before execution.

Issue #500. Consequential run/work templates declare an independent verifier,
a failure/rollback path, and a token/latency budget up front. Plan surfaces
incompleteness before anything runs. Receipts record budget use and
verification outcome separately from model/step completion. Budget
*enforcement* is owned by #593; this module owns declaration and recording.
Optional ``wall_clock_seconds`` and ``worker_dispatch_count`` are the first-slice
enforceable ceilings. When unset, ``latency_seconds`` maps to wall-clock for
enforcement. Aggregate ``token_budget`` remains observed-only (receipt
``tokens_used``) and is never an input-token alias. Optional ``input_tokens`` /
``output_tokens`` declare explicit split observed caps when present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VERIFICATION_CONTRACT_SCHEMA = "brigade.verification_contract.v1"
VERIFICATION_CONTRACT_SCHEMA_VERSION = 1

ROLLBACK_POLICIES = frozenset({"none", "manual", "command", "git-restore"})
VERIFIER_SOURCES = frozenset({"command", "argv", "manifest_id", "manifest_checks"})


@dataclass(frozen=True)
class VerificationBudget:
    latency_seconds: int
    token_budget: int | None = None
    wall_clock_seconds: int | None = None
    worker_dispatch_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class VerificationRollback:
    policy: str
    command: str | list[str] | None = None


@dataclass(frozen=True)
class VerificationVerifier:
    source: str
    command: str | None = None
    argv: tuple[str, ...] | None = None
    manifest_id: str | None = None


@dataclass(frozen=True)
class VerificationContract:
    verifier: VerificationVerifier
    rollback: VerificationRollback
    budget: VerificationBudget

    def to_payload(self) -> dict[str, Any]:
        verifier: dict[str, Any] = {"source": self.verifier.source}
        if self.verifier.command is not None:
            verifier["command"] = self.verifier.command
        if self.verifier.argv is not None:
            verifier["argv"] = list(self.verifier.argv)
        if self.verifier.manifest_id is not None:
            verifier["manifest_id"] = self.verifier.manifest_id
        rollback: dict[str, Any] = {"policy": self.rollback.policy}
        if self.rollback.command is not None:
            rollback["command"] = self.rollback.command
        budget: dict[str, Any] = {"latency_seconds": self.budget.latency_seconds}
        if self.budget.token_budget is not None:
            budget["token_budget"] = self.budget.token_budget
        if self.budget.wall_clock_seconds is not None:
            budget["wall_clock_seconds"] = self.budget.wall_clock_seconds
        if self.budget.worker_dispatch_count is not None:
            budget["worker_dispatch_count"] = self.budget.worker_dispatch_count
        if self.budget.input_tokens is not None:
            budget["input_tokens"] = self.budget.input_tokens
        if self.budget.output_tokens is not None:
            budget["output_tokens"] = self.budget.output_tokens
        return {
            "schema": VERIFICATION_CONTRACT_SCHEMA,
            "schema_version": VERIFICATION_CONTRACT_SCHEMA_VERSION,
            "verifier": verifier,
            "rollback": rollback,
            "budget": budget,
        }


def _positive_int(value: object, *, field: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} must be a positive integer")
        return None
    if value <= 0:
        errors.append(f"{field} must be a positive integer")
        return None
    return value


def _optional_non_negative_int(value: object, *, field: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} must be a non-negative integer when set")
        return None
    if value < 0:
        errors.append(f"{field} must be a non-negative integer when set")
        return None
    return value


def _parse_command_value(value: object, *, field: str, errors: list[str]) -> str | list[str] | None:
    if isinstance(value, str):
        if not value.strip():
            errors.append(f"{field} must be a non-empty string when set")
            return None
        return value
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) and item for item in value):
            errors.append(f"{field} must be a non-empty string array when set")
            return None
        return list(value)
    errors.append(f"{field} must be a string or string array when set")
    return None


def validate_contract_payload(
    payload: dict[str, Any] | None,
    *,
    allow_manifest_checks: bool = False,
) -> list[str]:
    """Return validation errors for a contract object. Empty means syntactically valid."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["verification_contract must be an object"]
    schema = payload.get("schema")
    if schema is not None and schema != VERIFICATION_CONTRACT_SCHEMA:
        errors.append(f"schema must be {VERIFICATION_CONTRACT_SCHEMA!r}")
    version = payload.get("schema_version")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version != VERIFICATION_CONTRACT_SCHEMA_VERSION
    ):
        errors.append(f"schema_version must be {VERIFICATION_CONTRACT_SCHEMA_VERSION}")

    verifier = payload.get("verifier")
    if verifier is None:
        errors.append("verifier is required")
    elif not isinstance(verifier, dict):
        errors.append("verifier must be an object")
    else:
        source = verifier.get("source")
        has_command = "command" in verifier and verifier.get("command") is not None
        has_argv = "argv" in verifier and verifier.get("argv") is not None
        has_manifest = "manifest_id" in verifier and verifier.get("manifest_id") is not None
        if source is None:
            if has_command:
                source = "command"
            elif has_argv:
                source = "argv"
            elif has_manifest:
                source = "manifest_id"
            elif allow_manifest_checks:
                source = "manifest_checks"
            else:
                errors.append("verifier.source is required")
        if isinstance(source, str) and source not in VERIFIER_SOURCES:
            errors.append(f"verifier.source must be one of {sorted(VERIFIER_SOURCES)}")
        elif source == "command":
            if not has_command:
                errors.append("verifier.command is required when source is 'command'")
            else:
                _parse_command_value(verifier.get("command"), field="verifier.command", errors=errors)
        elif source == "argv":
            if not has_argv:
                errors.append("verifier.argv is required when source is 'argv'")
            else:
                raw = verifier.get("argv")
                if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item for item in raw):
                    errors.append("verifier.argv must be a non-empty string array")
        elif source == "manifest_id":
            mid = verifier.get("manifest_id")
            if not isinstance(mid, str) or not mid.strip():
                errors.append("verifier.manifest_id is required when source is 'manifest_id'")
        elif source == "manifest_checks":
            if not allow_manifest_checks:
                errors.append("verifier.source 'manifest_checks' is only valid on verify manifests")

    rollback = payload.get("rollback")
    if rollback is None:
        errors.append("rollback is required")
    elif not isinstance(rollback, dict):
        errors.append("rollback must be an object")
    else:
        policy = rollback.get("policy")
        if not isinstance(policy, str) or policy not in ROLLBACK_POLICIES:
            errors.append(f"rollback.policy must be one of {sorted(ROLLBACK_POLICIES)}")
        elif policy == "command":
            if "command" not in rollback or rollback.get("command") is None:
                errors.append("rollback.command is required when policy is 'command'")
            else:
                _parse_command_value(rollback.get("command"), field="rollback.command", errors=errors)

    budget = payload.get("budget")
    if budget is None:
        errors.append("budget is required")
    elif not isinstance(budget, dict):
        errors.append("budget must be an object")
    else:
        _positive_int(budget.get("latency_seconds"), field="budget.latency_seconds", errors=errors)
        if "token_budget" in budget:
            _optional_non_negative_int(budget.get("token_budget"), field="budget.token_budget", errors=errors)
        if "wall_clock_seconds" in budget:
            _positive_int(budget.get("wall_clock_seconds"), field="budget.wall_clock_seconds", errors=errors)
        if "worker_dispatch_count" in budget:
            _positive_int(budget.get("worker_dispatch_count"), field="budget.worker_dispatch_count", errors=errors)
        if "input_tokens" in budget:
            _positive_int(budget.get("input_tokens"), field="budget.input_tokens", errors=errors)
        if "output_tokens" in budget:
            _positive_int(budget.get("output_tokens"), field="budget.output_tokens", errors=errors)

    return errors


def incompleteness_reasons(
    payload: dict[str, Any] | None,
    *,
    allow_manifest_checks: bool = False,
) -> list[str]:
    """Stable reasons a workflow is incomplete before execution (plan surface)."""
    if payload is None:
        return ["missing_verification_contract"]
    if not isinstance(payload, dict):
        return ["invalid_verification_contract"]
    reasons: list[str] = []
    if payload.get("verifier") is None:
        reasons.append("missing_verifier")
    if payload.get("rollback") is None:
        reasons.append("missing_rollback")
    if payload.get("budget") is None:
        reasons.append("missing_budget")
    errors = validate_contract_payload(payload, allow_manifest_checks=allow_manifest_checks)
    for error in errors:
        if error.startswith("verifier") and "missing_verifier" not in reasons:
            reasons.append("invalid_verifier")
        elif error.startswith("rollback") and "missing_rollback" not in reasons:
            reasons.append("invalid_rollback")
        elif error.startswith("budget") and "missing_budget" not in reasons:
            reasons.append("invalid_budget")
        elif error.startswith("schema") and "invalid_verification_contract" not in reasons:
            reasons.append("invalid_verification_contract")
    # De-dupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    if not ordered and errors:
        return ["invalid_verification_contract"]
    return ordered


def contract_from_payload(
    payload: dict[str, Any],
    *,
    allow_manifest_checks: bool = False,
) -> VerificationContract:
    errors = validate_contract_payload(payload, allow_manifest_checks=allow_manifest_checks)
    if errors:
        raise ValueError("; ".join(errors))
    verifier_raw = payload["verifier"]
    assert isinstance(verifier_raw, dict)
    source = verifier_raw.get("source")
    if source is None:
        if verifier_raw.get("command") is not None:
            source = "command"
        elif verifier_raw.get("argv") is not None:
            source = "argv"
        elif verifier_raw.get("manifest_id") is not None:
            source = "manifest_id"
        else:
            source = "manifest_checks"
    argv_raw = verifier_raw.get("argv")
    argv = tuple(str(item) for item in argv_raw) if isinstance(argv_raw, list) else None
    command_raw = verifier_raw.get("command")
    command: str | None
    if isinstance(command_raw, list):
        command = None
        argv = tuple(str(item) for item in command_raw)
        source = "argv"
    elif isinstance(command_raw, str):
        command = command_raw
    else:
        command = None
    verifier = VerificationVerifier(
        source=str(source),
        command=command,
        argv=argv,
        manifest_id=str(verifier_raw["manifest_id"]).strip()
        if isinstance(verifier_raw.get("manifest_id"), str)
        else None,
    )
    rollback_raw = payload["rollback"]
    assert isinstance(rollback_raw, dict)
    rollback_command = rollback_raw.get("command")
    if isinstance(rollback_command, list):
        parsed_command: str | list[str] | None = list(rollback_command)
    elif isinstance(rollback_command, str):
        parsed_command = rollback_command
    else:
        parsed_command = None
    rollback = VerificationRollback(policy=str(rollback_raw["policy"]), command=parsed_command)
    budget_raw = payload["budget"]
    assert isinstance(budget_raw, dict)
    token_budget = budget_raw.get("token_budget")
    wall_clock = budget_raw.get("wall_clock_seconds")
    dispatch_count = budget_raw.get("worker_dispatch_count")
    input_tokens = budget_raw.get("input_tokens")
    output_tokens = budget_raw.get("output_tokens")
    budget = VerificationBudget(
        latency_seconds=int(budget_raw["latency_seconds"]),
        token_budget=int(token_budget)
        if isinstance(token_budget, int) and not isinstance(token_budget, bool)
        else None,
        wall_clock_seconds=int(wall_clock)
        if isinstance(wall_clock, int) and not isinstance(wall_clock, bool)
        else None,
        worker_dispatch_count=int(dispatch_count)
        if isinstance(dispatch_count, int) and not isinstance(dispatch_count, bool)
        else None,
        input_tokens=int(input_tokens)
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
        else None,
        output_tokens=int(output_tokens)
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool)
        else None,
    )
    return VerificationContract(verifier=verifier, rollback=rollback, budget=budget)


def extract_contract_payload(container: dict[str, Any]) -> dict[str, Any] | None:
    """Return nested verification_contract object when present."""
    raw = container.get("verification_contract")
    return raw if isinstance(raw, dict) else None


def contract_plan_view(
    payload: dict[str, Any] | None,
    *,
    consequential: bool,
    allow_manifest_checks: bool = False,
) -> dict[str, Any]:
    """Plan-time view: completeness, blockers, and declared contract when valid."""
    if payload is None:
        errors: list[str] = []
        reasons = ["missing_verification_contract"] if consequential else []
        complete = False
    else:
        errors = validate_contract_payload(payload, allow_manifest_checks=allow_manifest_checks)
        reasons = incompleteness_reasons(payload, allow_manifest_checks=allow_manifest_checks)
        complete = not errors

    blockers: list[str] = []
    if consequential and not complete:
        if payload is None:
            blockers.append(
                "consequential template is incomplete: missing verification_contract (verifier, rollback, budget)"
            )
        else:
            detail = ", ".join(reasons) if reasons else "; ".join(errors)
            blockers.append(f"consequential template has incomplete verification_contract: {detail}")

    view: dict[str, Any] = {
        "present": payload is not None,
        "complete": complete,
        "consequential": consequential,
        "incompleteness_reasons": reasons,
        "blockers": blockers,
    }
    if complete and payload is not None:
        contract = contract_from_payload(payload, allow_manifest_checks=allow_manifest_checks)
        view["contract"] = contract.to_payload()
    elif payload is not None:
        view["declared"] = payload
        view["errors"] = errors
    return view


def budget_use_payload(
    contract: VerificationContract,
    *,
    latency_seconds_used: float | None,
    tokens_used: int | None = None,
) -> dict[str, Any]:
    """Receipt fragment: declared budget vs observed use (no enforcement)."""
    latency_budget = contract.budget.latency_seconds
    latency_used = None if latency_seconds_used is None else float(latency_seconds_used)
    exhausted = False
    if latency_used is not None and latency_used > latency_budget:
        exhausted = True
    if (
        tokens_used is not None
        and contract.budget.token_budget is not None
        and tokens_used > contract.budget.token_budget
    ):
        exhausted = True
    payload: dict[str, Any] = {
        "latency_seconds_budget": latency_budget,
        "latency_seconds_used": latency_used,
        "token_budget": contract.budget.token_budget,
        "tokens_used": tokens_used,
        "exhausted": exhausted,
    }
    if contract.budget.input_tokens is not None:
        payload["input_tokens_budget"] = contract.budget.input_tokens
    if contract.budget.output_tokens is not None:
        payload["output_tokens_budget"] = contract.budget.output_tokens
    return payload


def verification_outcome_payload(
    *,
    status: str,
    exit_code: int | None = None,
    detail: str | None = None,
    command: str | list[str] | None = None,
    independent_of_model_completion: bool = True,
) -> dict[str, Any]:
    """Receipt fragment: verification outcome, never conflated with model completion."""
    payload: dict[str, Any] = {
        "status": status,
        "independent_of_model_completion": independent_of_model_completion,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if detail is not None:
        payload["detail"] = detail
    if command is not None:
        payload["command"] = command
    return payload


def model_completion_payload(*, status: str, detail: str | None = None) -> dict[str, Any]:
    """Receipt fragment: model/step completion, separate from verification."""
    payload: dict[str, Any] = {"status": status}
    if detail is not None:
        payload["detail"] = detail
    return payload


def rollback_outcome_payload(
    *,
    status: str,
    policy: str,
    exit_code: int | None = None,
    detail: str | None = None,
    command: str | list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "policy": policy}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if detail is not None:
        payload["detail"] = detail
    if command is not None:
        payload["command"] = command
    return payload


def display_command(command: str | list[str] | None) -> str | None:
    if command is None:
        return None
    if isinstance(command, list):
        return " ".join(command)
    return command
