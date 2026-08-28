"""Public Backup Steward tool handlers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .actions import BackupActionExecutor, BackupActionStore, catalog_entry, proposal_finding_revision
from .adapters import BackupObservers
from .contracts import (
    ERROR_MESSAGES,
    BackupError,
    parse_execute_input,
    parse_operation_input,
    parse_overview_input,
    parse_propose_input,
    parse_target_input,
)
from .ledger import BackupLedger
from .normalize import normalize_restore_readiness, normalize_target_observation, utf8_byte_length
from .runtime_config import BackupRegistry

OVERVIEW_CONCURRENCY = 4
PUBLIC_RESULT_LIMIT = 131_072


def _iso(now: Callable[[], datetime]) -> str:
    stamp = now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _public_error(error: object, alias: str) -> dict[str, str]:
    if isinstance(error, BackupError):
        return {"code": error.code, "message": ERROR_MESSAGES[error.code], "target_alias": alias}
    return {"code": "protocol_error", "message": ERROR_MESSAGES["protocol_error"], "target_alias": alias}


def _envelope(
    *,
    request_id: str,
    observed_at: str,
    status: str,
    data: Any,
    freshness_seconds: int,
    receipt_ref: str | None,
    findings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "observed_at": observed_at,
        "status": status,
        "freshness_seconds": freshness_seconds,
        "data": data,
        "findings": findings or [],
        "errors": errors or [],
        "receipt_ref": receipt_ref,
    }


def _findings_from_observation(observation: Mapping[str, Any], receipt_ref: str | None) -> list[dict[str, Any]]:
    if observation["lock_class"] == "active_operation":
        finding = {
            "finding_id": f"{observation['target_alias']}:active-operation",
            "target_alias": observation["target_alias"],
            "kind": "active-operation",
            "severity_class": "info",
            "summary": "An allowlisted backup operation is active",
            "observed_at": observation["observed_at"],
        }
        if receipt_ref is not None:
            finding["receipt_ref"] = receipt_ref
        return [finding]
    if observation["lock_class"] == "stale_lock":
        finding = {
            "finding_id": f"{observation['target_alias']}:stale-lock",
            "target_alias": observation["target_alias"],
            "kind": "stale-lock",
            "severity_class": "warning",
            "summary": "A stale backup lock is present",
            "observed_at": observation["observed_at"],
            "proposed_action_id": "run-backup",
            "blast_radius": "one registered restic target",
            "verification_statement": "compare the next snapshot receipt",
            "recovery_statement": "operator reruns the approved backup",
        }
        if receipt_ref is not None:
            finding["receipt_ref"] = receipt_ref
        return [finding]
    return []


def _finding_summaries(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for finding in findings:
        summary = {
            "finding_id": finding["finding_id"],
            "target_alias": finding["target_alias"],
            "kind": finding["kind"],
            "severity_class": finding["severity_class"],
            "summary": finding["summary"],
            "observed_at": finding["observed_at"],
        }
        if finding.get("receipt_ref") is not None:
            summary["receipt_ref"] = finding["receipt_ref"]
        summaries.append(summary)
    return summaries


class BackupStewardTools:
    """Closed public Backup Steward tool surface."""

    def __init__(
        self,
        *,
        registry: BackupRegistry,
        runtime: Mapping[str, Any],
        observers: BackupObservers,
        ledger: BackupLedger,
        store: BackupActionStore,
        executor: BackupActionExecutor,
        now: Callable[[], datetime],
        request_id: Callable[[], str],
        secrets: Sequence[str],
        allowlisted_operation_ids: Callable[[], Sequence[str]] | None = None,
    ):
        self.registry = registry
        self.runtime = runtime
        self.observers = observers
        self.ledger = ledger
        self.store = store
        self.executor = executor
        self.now = now
        self.request_id = request_id
        self.secrets = list(secrets)
        self.allowlisted_operation_ids = allowlisted_operation_ids or (lambda: ())

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "backup_overview": self.backup_overview,
            "backup_target_status": self.backup_target_status,
            "backup_restore_readiness": self.backup_restore_readiness,
            "backup_operation_status": self.backup_operation_status,
            "backup_propose_action": self.backup_propose_action,
            "backup_execute_action": self.backup_execute_action,
        }
        handler = handlers.get(name)
        if handler is None:
            raise BackupError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return handler(arguments if arguments is not None else {})

    def backup_overview(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._overview(raw))

    def backup_target_status(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._target_status(raw))

    def backup_restore_readiness(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._restore_readiness(raw))

    def backup_operation_status(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._operation_status(raw))

    def backup_propose_action(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._propose(raw))

    def backup_execute_action(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._execute(raw))

    def _guard(self, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = work()
            if utf8_byte_length(json.dumps(result, separators=(",", ":"))) > PUBLIC_RESULT_LIMIT:
                raise BackupError("protocol_error", ERROR_MESSAGES["protocol_error"])
            return result
        except BackupError:
            raise
        except Exception:
            raise BackupError("protocol_error", ERROR_MESSAGES["protocol_error"]) from None

    def _observe_and_persist(self, alias: str) -> dict[str, Any]:
        target = self.registry.target(alias)
        outcome = self.observers.observe_target(alias)
        observation = self.ledger.record_observation(
            normalize_target_observation(
                outcome["raw"],
                target,
                self.secrets,
                allowlisted_operation_ids=self.allowlisted_operation_ids(),
            ),
            outcome.get("receipt_ref"),
        )
        findings = _findings_from_observation(observation, outcome.get("receipt_ref"))
        self.ledger.replace_findings(alias, findings)
        return {"observation": observation, "findings": findings, "receipt_ref": outcome.get("receipt_ref")}

    def _overview(self, raw: object) -> dict[str, Any]:
        parse_overview_input(raw)
        targets = list(self.registry.targets)
        observations: list[dict[str, Any] | None] = [None] * len(targets)
        errors: list[dict[str, str] | None] = [None] * len(targets)
        findings: list[dict[str, Any]] = []

        def worker(index: int, target: Mapping[str, Any]) -> None:
            try:
                result = self._observe_and_persist(target["alias"])
                observations[index] = result["observation"]
                findings.extend(result["findings"])
            except Exception as exc:
                errors[index] = _public_error(exc, target["alias"])

        if targets:
            with ThreadPoolExecutor(max_workers=min(OVERVIEW_CONCURRENCY, len(targets))) as pool:
                list(pool.map(lambda item: worker(*item), enumerate(targets)))
        data = [item for item in observations if item is not None]
        public_errors = [item for item in errors if item is not None]
        status = "ok" if not public_errors else "unavailable" if not data else "partial"
        freshness = max((item.get("freshness_seconds", 0) for item in data), default=0)
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status=status,
            data=data,
            freshness_seconds=freshness,
            receipt_ref=None,
            findings=_finding_summaries(findings),
            errors=public_errors,
        )

    def _target_status(self, raw: object) -> dict[str, Any]:
        alias = parse_target_input(raw)["target_alias"]
        result = self._observe_and_persist(alias)
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=result["observation"],
            freshness_seconds=result["observation"]["freshness_seconds"],
            receipt_ref=result["receipt_ref"],
            findings=_finding_summaries(result["findings"]),
        )

    def _restore_readiness(self, raw: object) -> dict[str, Any]:
        alias = parse_target_input(raw)["target_alias"]
        target = self.registry.target(alias)
        if target.get("readiness_reporter_id") is None:
            raise BackupError("not_found", ERROR_MESSAGES["not_found"])
        outcome = self.observers.observe_restore_readiness(alias)
        readiness = self.ledger.record_readiness(
            normalize_restore_readiness(outcome["raw"], target, self.secrets),
            outcome.get("receipt_ref"),
        )
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=readiness,
            freshness_seconds=readiness.get("evidence_freshness_seconds") or 0,
            receipt_ref=outcome.get("receipt_ref"),
        )

    def _operation_status(self, raw: object) -> dict[str, Any]:
        operation_id = parse_operation_input(raw)["operation_id"]
        operation = self.store.read_operation(operation_id)
        if operation is None:
            raise BackupError("not_found", ERROR_MESSAGES["not_found"])
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=operation,
            freshness_seconds=0,
            receipt_ref=operation.get("receipt_ref"),
        )

    def _propose(self, raw: object) -> dict[str, Any]:
        parsed = parse_propose_input(raw)
        if not self.registry.safety_ready:
            raise BackupError("denied", ERROR_MESSAGES["denied"])
        entry = catalog_entry(parsed["target_alias"], parsed["action_id"])
        if entry is None:
            raise BackupError("denied", ERROR_MESSAGES["denied"])
        finding = self.ledger.last_finding(parsed["finding_id"])
        observation = self.ledger.last_observation(parsed["target_alias"])
        if (
            finding is None
            or observation is None
            or finding.get("target_alias") != parsed["target_alias"]
            or finding.get("receipt_ref") is None
            or _parse_iso(finding["observed_at"]) < _parse_iso(observation["observed_at"])
        ):
            raise BackupError("denied", ERROR_MESSAGES["denied"])
        proposal = self.store.create_proposal(
            {
                "target_alias": parsed["target_alias"],
                "action_id": parsed["action_id"],
                "finding_id": parsed["finding_id"],
                "finding_revision": proposal_finding_revision(finding),
            }
        )
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "proposal_id": proposal["proposal_id"],
                "target_alias": proposal["target_alias"],
                "action_id": proposal["action_id"],
                "finding_id": proposal["finding_id"],
                "approval_nonce": proposal["nonce"],
                "expires_at": proposal["expires_at"],
                "blast_radius": entry["blast_radius"],
                "verification_statement": entry["verification_statement"],
                "recovery_statement": entry["recovery_statement"],
                "automatic_rollback": False,
            },
            freshness_seconds=0,
            receipt_ref=None,
        )

    def _execute(self, raw: object) -> dict[str, Any]:
        proposal_id = parse_execute_input(raw)["proposal_id"]
        if not self.registry.safety_ready:
            raise BackupError("denied", ERROR_MESSAGES["denied"])
        consumed = self.store.consume_approved_proposal(proposal_id)
        current_revision = self.ledger.latest_finding_revision(consumed["finding_id"])
        if current_revision is None or current_revision != consumed["finding_revision"]:
            raise BackupError("denied", ERROR_MESSAGES["denied"])
        operation = self.executor.start(
            {
                "target_alias": consumed["target_alias"],
                "action_id": consumed["action_id"],
                "proposal_id": consumed["proposal_id"],
            }
        )
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "proposal_id": consumed["proposal_id"],
                "operation_id": operation["operation_id"],
                "receipt_ref": operation.get("receipt_ref") or consumed["proposal_id"],
            },
            freshness_seconds=0,
            receipt_ref=operation.get("receipt_ref"),
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
