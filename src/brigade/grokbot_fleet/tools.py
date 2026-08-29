"""Public Fleet Steward tool handlers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, NoReturn, Sequence

from .actions import (
    PROPOSAL_TTL_MS,
    FleetActionStoreError,
)
from .contracts import (
    ERROR_MESSAGES,
    FleetError,
    HEALTH_CLASSES,
    STATUSES,
    is_offset_datetime,
    omit_undefined,
    parse_execute_input,
    parse_host_status_input,
    parse_identifier,
    parse_incident_input,
    parse_opaque_id,
    parse_overview_input,
    parse_propose_input,
    parse_service_health_input,
)
from .ledger import FleetLedger
from .normalize import normalize_host_observation, sanitize_detail, utf8_byte_length
from .policy import (
    WAZUH_REVALIDATION_KEYS,
    authorize_host_read,
    authorize_incident_read,
    authorize_service_read,
    bind_wazuh_remediation,
)
from .probes import FLEET_FINDING_CATALOG, FleetProbes, remediation_for_finding
from .registry import FleetRegistry

OVERVIEW_CONCURRENCY = 4
PUBLIC_RESULT_LIMIT = 131_072


def _invalid_observation() -> NoReturn:
    raise FleetError("protocol_error", "Fleet observation was invalid")


def _iso(now: Callable[[], datetime]) -> str:
    stamp = now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    text = stamp.isoformat()
    return text.replace("+00:00", "Z")


def _selected_host_probe(target: Mapping[str, Any]) -> str:
    if not target["probe_ids"]:
        _invalid_observation()
    return target["probe_ids"][0]


def _validated_receipt_ref(value: object) -> str | None:
    if value is None:
        return None
    try:
        return parse_identifier(value)
    except FleetError:
        _invalid_observation()


def _catalog_kind(finding_id: str, scope: str) -> str | None:
    for kind in FLEET_FINDING_CATALOG:
        if finding_id == f"{scope}:{kind}":
            return kind
    return None


def _fleet_finding_from_summary(
    summary: Mapping[str, Any],
    target_alias: str,
    receipt_ref: str | None,
) -> dict[str, Any]:
    kind = _catalog_kind(summary["finding_id"], summary["scope"])
    if kind is None:
        _invalid_observation()
    entry = FLEET_FINDING_CATALOG[kind]
    if summary["severity_class"] != entry["severity_class"]:
        _invalid_observation()
    finding = {
        "finding_id": summary["finding_id"],
        "target_alias": target_alias,
        "proposed_action_id": entry["proposed_action_id"],
        "reason": summary["summary"],
        "blast_radius": entry["blast_radius"],
        "verification_id": entry["verification_id"],
        "rollback_id": entry["rollback_id"],
        "observed_at": summary["observed_at"],
    }
    if receipt_ref is not None:
        finding["incident_receipt_ref"] = receipt_ref
    return finding


def _normalize_service_observation(
    raw: object,
    target: Mapping[str, Any],
    service: Mapping[str, Any],
    secrets: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "service_id",
        "target_alias",
        "probe_id",
        "observed_at",
        "health_class",
        "detail",
    }:
        _invalid_observation()
    if (
        raw.get("service_id") != service["service_id"]
        or raw.get("target_alias") != target["alias"]
        or raw.get("probe_id") != service["probe_id"]
    ):
        _invalid_observation()
    if not is_offset_datetime(raw.get("observed_at")) or raw.get("health_class") not in HEALTH_CLASSES:
        _invalid_observation()
    detail = raw.get("detail")
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _invalid_observation()
    return {
        "service_id": service["service_id"],
        "target_alias": target["alias"],
        "health_class": raw["health_class"],
        "observed_at": raw["observed_at"],
        "freshness_seconds": target["freshness_seconds"],
        "changed": False,
        "detail": sanitize_detail(detail, secrets),
    }


def _normalize_incident_bundle(
    raw: object,
    requested_scope: str,
    receipt_ref: str | None,
    secrets: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"scope", "status", "observed_at", "findings"}:
        _invalid_observation()
    if raw.get("scope") != requested_scope or raw.get("status") not in STATUSES:
        _invalid_observation()
    if not is_offset_datetime(raw.get("observed_at")):
        _invalid_observation()
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list) or len(findings_raw) > 128:
        _invalid_observation()
    findings = []
    for finding in findings_raw:
        if not isinstance(finding, Mapping) or finding.get("scope") != requested_scope:
            _invalid_observation()
        summary = finding.get("summary")
        if not isinstance(summary, str) or utf8_byte_length(summary) > 16_384:
            _invalid_observation()
        findings.append(
            {
                "finding_id": parse_identifier(finding.get("finding_id")),
                "scope": parse_identifier(finding.get("scope")),
                "severity_class": finding.get("severity_class"),
                "summary": sanitize_detail(summary, secrets),
                "observed_at": finding.get("observed_at"),
            }
        )
        if findings[-1]["severity_class"] not in {"info", "warning", "critical", "unknown"}:
            _invalid_observation()
        if not is_offset_datetime(findings[-1]["observed_at"]):
            _invalid_observation()
    bundle = {
        "scope": requested_scope,
        "status": raw["status"],
        "observed_at": raw["observed_at"],
        "findings": findings,
    }
    if receipt_ref is not None:
        bundle["receipt_ref"] = receipt_ref
    return bundle


def _public_error(error: object, alias: str) -> dict[str, str]:
    if isinstance(error, FleetError):
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


class FleetHubClaims:
    """Injected Fleet claim port. Acquire and release take catalog aliases only."""

    def acquire(self, target_alias: str) -> dict[str, str]:
        from .. import fleet_client

        parse_identifier(target_alias)
        decision = fleet_client.acquire_claim(target_alias, role="fleet-steward", job="remediation")
        if not getattr(decision, "granted", False):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        holder = getattr(decision, "holder", None)
        if not isinstance(holder, str) or not holder:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        return {"target_alias": target_alias, "holder": holder}

    def release(self, claim: Mapping[str, str]) -> dict[str, Any]:
        from .. import fleet_client

        holder = claim.get("holder")
        alias = claim.get("target_alias")
        if not isinstance(holder, str) or not isinstance(alias, str):
            return {"granted": False, "target_alias": alias if isinstance(alias, str) else ""}
        try:
            parse_identifier(alias)
        except FleetError:
            return {"granted": False, "target_alias": alias}
        decision = fleet_client.release_claim(alias, holder=holder)
        return {
            "granted": bool(getattr(decision, "granted", False)),
            "holder": holder,
            "reason": str(getattr(decision, "reason", "")),
            "target_alias": alias,
        }


class FleetStewardTools:
    """Closed public Fleet Steward tool surface."""

    def __init__(
        self,
        *,
        registry: FleetRegistry,
        probes: FleetProbes,
        ledger: FleetLedger,
        store: Any,
        executor: Any,
        now: Callable[[], datetime],
        request_id: Callable[[], str],
        create_proposal_id: Callable[[], str],
        create_nonce: Callable[[], str],
        create_receipt_id: Callable[[], str],
        secrets: list[str],
        wazuh_source: Any = None,
        remediator: Any = None,
        claims: Any = None,
    ):
        self.registry = registry
        self.probes = probes
        self.ledger = ledger
        self.store = store
        self.executor = executor
        self.now = now
        self.request_id = request_id
        self.create_proposal_id = create_proposal_id
        self.create_nonce = create_nonce
        self.create_receipt_id = create_receipt_id
        self.secrets = secrets
        self.wazuh_source = wazuh_source
        self.remediator = remediator
        self.claims = claims
        self._recovery_pending = False

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "fleet_overview": self.fleet_overview,
            "host_status": self.host_status,
            "service_health": self.service_health,
            "incident_bundle": self.incident_bundle,
            "propose_remediation": self.propose_remediation,
            "execute_remediation": self.execute_remediation,
        }
        handler = handlers.get(name)
        if handler is None:
            raise FleetError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return handler(arguments if arguments is not None else {})

    def fleet_overview(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._overview(raw))

    def host_status(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._host_status(raw))

    def service_health(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._service_health(raw))

    def incident_bundle(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._incident_bundle(raw))

    def propose_remediation(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._propose_remediation(raw))

    def execute_remediation(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._execute_remediation(raw))

    def _guard(self, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            self._recover_claim_release()
            result = work()
            if utf8_byte_length(json_dumps(result)) > PUBLIC_RESULT_LIMIT:
                raise FleetError("protocol_error", ERROR_MESSAGES["protocol_error"])
            return result
        except FleetError:
            raise
        except Exception:
            raise FleetError("protocol_error", ERROR_MESSAGES["protocol_error"]) from None

    def _observe_normalized_host(self, target: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        probe_id = _selected_host_probe(target)
        outcome = self.probes.observe_host(target, probe_id)
        receipt_ref = _validated_receipt_ref(outcome.get("receipt_ref"))
        observation = self.ledger.record_host_observation_with_change(
            normalize_host_observation(outcome["raw"], target, probe_id, self.secrets),
            receipt_ref,
        )
        return observation, receipt_ref

    def _overview(self, raw: object) -> dict[str, Any]:
        parse_overview_input(raw)
        from concurrent.futures import ThreadPoolExecutor

        targets = list(self.registry.targets)
        observations: list[dict[str, Any] | None] = [None] * len(targets)
        errors: list[dict[str, str] | None] = [None] * len(targets)

        def worker(index: int, target: Mapping[str, Any]) -> None:
            try:
                observations[index] = self._observe_normalized_host(target)[0]
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
            errors=public_errors,
        )

    def _host_status(self, raw: object) -> dict[str, Any]:
        alias = parse_host_status_input(raw)["alias"]
        target = authorize_host_read(self.registry, alias)
        observation, receipt_ref = self._observe_normalized_host(target)
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=observation,
            freshness_seconds=target["freshness_seconds"],
            receipt_ref=receipt_ref,
        )

    def _service_health(self, raw: object) -> dict[str, Any]:
        service_id = parse_service_health_input(raw)["service_id"]
        authorized = authorize_service_read(self.registry, service_id)
        outcome = self.probes.observe_service(authorized["target"], authorized["service"])
        receipt_ref = _validated_receipt_ref(outcome.get("receipt_ref"))
        observation = self.ledger.record_service_observation_with_change(
            _normalize_service_observation(outcome["raw"], authorized["target"], authorized["service"], self.secrets),
            receipt_ref,
        )
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=observation,
            freshness_seconds=authorized["target"]["freshness_seconds"],
            receipt_ref=receipt_ref,
        )

    def _incident_bundle(self, raw: object) -> dict[str, Any]:
        scope = parse_incident_input(raw)["scope"]
        authorized = authorize_incident_read(self.registry, scope)
        outcome = (
            self.probes.collect_incident(authorized["target"])
            if authorized.get("service") is None
            else self.probes.collect_incident(authorized["target"], authorized["service"])
        )
        receipt_ref = _validated_receipt_ref(outcome.get("receipt_ref"))
        bundle = _normalize_incident_bundle(outcome["raw"], scope, receipt_ref, self.secrets)
        findings = [
            _fleet_finding_from_summary(finding, authorized["target"]["alias"], receipt_ref)
            for finding in bundle["findings"]
        ]
        self.ledger.replace_findings(scope, findings)
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status=bundle["status"],
            data=bundle,
            freshness_seconds=authorized["target"]["freshness_seconds"],
            receipt_ref=receipt_ref,
            findings=bundle["findings"],
        )

    def _parse_finding_record(self, raw: object, finding_id: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            _invalid_observation()
        allowed = {
            "finding_id",
            "target_alias",
            "proposed_action_id",
            "reason",
            "blast_radius",
            "verification_id",
            "rollback_id",
            "observed_at",
            "incident_receipt_ref",
        }
        required = {
            "finding_id",
            "target_alias",
            "proposed_action_id",
            "reason",
            "blast_radius",
            "verification_id",
            "rollback_id",
        }
        if not set(raw) <= allowed or not required <= set(raw) or raw.get("finding_id") != finding_id:
            _invalid_observation()
        finding = {
            "finding_id": parse_identifier(raw.get("finding_id")),
            "target_alias": parse_identifier(raw.get("target_alias")),
            "proposed_action_id": parse_identifier(raw.get("proposed_action_id")),
            "reason": raw["reason"],
            "blast_radius": raw["blast_radius"],
            "verification_id": parse_identifier(raw.get("verification_id")),
            "rollback_id": parse_identifier(raw.get("rollback_id")),
        }
        if "observed_at" in raw:
            if not is_offset_datetime(raw["observed_at"]):
                _invalid_observation()
            finding["observed_at"] = raw["observed_at"]
        if "incident_receipt_ref" in raw:
            finding["incident_receipt_ref"] = parse_identifier(raw.get("incident_receipt_ref"))
        return omit_undefined(finding)

    def _non_executable_proposal(
        self, finding: Mapping[str, Any], catalog: Mapping[str, str], target
    ) -> dict[str, Any]:
        return {
            "finding_id": finding["finding_id"],
            "target_alias": finding["target_alias"],
            "policy_tier": target["tier"],
            "proposed_action_id": catalog["proposed_action_id"],
            "reason": sanitize_detail(finding["reason"], self.secrets),
            "blast_radius": catalog["blast_radius"],
            "approval_required": True,
            "verification_id": catalog["verification_id"],
            "rollback_id": catalog["rollback_id"],
            "execution_available": False,
        }

    def _active_remediator(self) -> Any:
        if self.remediator is not None:
            return self.remediator
        if hasattr(self.executor, "recheck"):
            return self.executor
        raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])

    def _propose_remediation(self, raw: object) -> dict[str, Any]:
        finding_id = parse_propose_input(raw)["finding_id"]
        if self.wazuh_source is not None:
            wazuh_finding = self.wazuh_source.current_finding(finding_id)
            if wazuh_finding is not None:
                return self._propose_wazuh(wazuh_finding)
        try:
            raw_finding = self.ledger.finding(finding_id)
        except FleetError:
            raise FleetError("denied", ERROR_MESSAGES["denied"]) from None
        if raw_finding is None:
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        finding = self._parse_finding_record(raw_finding, finding_id)
        try:
            target = authorize_host_read(self.registry, finding["target_alias"])
        except FleetError as exc:
            if exc.code in {"not_found", "denied"}:
                raise FleetError("denied", ERROR_MESSAGES["denied"]) from exc
            raise
        catalog = remediation_for_finding(finding["finding_id"])
        if catalog is None:
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if (
            finding["proposed_action_id"] != catalog["proposed_action_id"]
            or finding["verification_id"] != catalog["verification_id"]
            or finding["rollback_id"] != catalog["rollback_id"]
            or finding["blast_radius"] != catalog["blast_radius"]
        ):
            _invalid_observation()
        proposal = self._non_executable_proposal(finding, catalog, target)
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=proposal,
            freshness_seconds=target["freshness_seconds"],
            receipt_ref=None,
        )

    def _public_binding(self, proposal: Mapping[str, Any]) -> dict[str, str]:
        return {
            "finding_id": proposal["finding_id"],
            "service_id": proposal["service_id"],
            "target_alias": proposal["target_alias"],
            "action_id": proposal["action_id"],
            "verification_id": proposal["verification_id"],
            "rollback_id": proposal["rollback_id"],
        }

    def _write_rejection(self, proposal_id: str, outcome: str, binding: Mapping[str, str] | None = None) -> None:
        receipt = {
            "version": 1,
            "receipt_id": self.create_receipt_id(),
            "kind": "rejection",
            "proposal_id": proposal_id,
            "outcome": outcome,
            "created_at": _iso(self.now),
        }
        if binding:
            receipt.update(binding)
        self.store.write_receipt(receipt)

    def _write_post_claim(self, record: Mapping[str, Any]) -> None:
        try:
            self.store.write_receipt(record)
        except Exception:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None

    def _action_receipt(
        self,
        proposal: Mapping[str, Any],
        binding: Mapping[str, str],
        *,
        kind: str,
        outcome: str,
        receipt_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "receipt_id": receipt_id or self.create_receipt_id(),
            "kind": kind,
            "proposal_id": proposal["proposal_id"],
            **binding,
            "outcome": outcome,
            "created_at": created_at or _iso(self.now),
        }

    def _commit_terminal(
        self,
        reservation_id: str | None,
        proposal: Mapping[str, Any],
        binding: Mapping[str, str],
        outcome: str,
        claim: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        record = self._action_receipt(proposal, binding, kind="verification", outcome=outcome)
        self._secure_reserved_evidence(reservation_id, [record], claim=claim, proposal=proposal)
        return [record]

    def _release_completed(self, released: object, claim: Mapping[str, str]) -> bool:
        if released is False:
            return False
        if isinstance(released, Mapping):
            if released.get("target_alias") != claim["target_alias"] or released.get("holder") != claim["holder"]:
                return False
            return bool(released.get("granted", False)) or released.get("reason") == "missing"
        return True

    def _persist_claim_cleanup(
        self,
        claim: Mapping[str, str],
        proposal: Mapping[str, Any],
        reservation_id: str,
        terminal_receipts: Sequence[Mapping[str, Any]],
        *,
        receipt_pending: bool,
        release_pending: bool,
    ) -> None:
        fenced_claim = self._fenced_claim(claim, expected_alias=str(proposal["target_alias"]))
        record: dict[str, Any] = {
            "version": 2,
            "proposal_id": str(proposal["proposal_id"]),
            "target_alias": fenced_claim["target_alias"],
            "holder": fenced_claim["holder"],
            "reservation_id": reservation_id,
            "terminal_receipts": [dict(item) for item in terminal_receipts],
            "receipt_pending": receipt_pending,
            "release_pending": release_pending,
        }
        writer = getattr(self.store, "write_claim_cleanup", None)
        if not callable(writer):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        try:
            writer(record)
        except Exception:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None

    def _fenced_claim(self, claim: Mapping[str, Any], *, expected_alias: str) -> dict[str, str]:
        try:
            target_alias = parse_identifier(claim.get("target_alias"))
            holder = parse_opaque_id(claim.get("holder"))
        except FleetError:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
        if target_alias != expected_alias:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        return {"target_alias": target_alias, "holder": holder}

    def _recover_claim_release(self) -> None:
        reader = getattr(self.store, "read_claim_cleanup", None)
        if not callable(reader):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        try:
            record = reader()
        except Exception:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
        if record is None:
            return
        if self.claims is None:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        if not isinstance(record, Mapping):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        proposal_id = record.get("proposal_id")
        reservation_id = record.get("reservation_id")
        terminal_receipts = record.get("terminal_receipts")
        if not isinstance(proposal_id, str) or not isinstance(reservation_id, str):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        claim = self._fenced_claim(record, expected_alias=str(record.get("target_alias") or ""))
        if (
            not isinstance(terminal_receipts, list)
            or not terminal_receipts
            or not isinstance(record.get("receipt_pending"), bool)
            or not isinstance(record.get("release_pending"), bool)
            or any(
                not isinstance(item, Mapping) or item.get("proposal_id") != proposal_id for item in terminal_receipts
            )
        ):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        if record["receipt_pending"]:
            try:
                self.store.commit_reserved_receipts(reservation_id, terminal_receipts)
                self._persist_claim_cleanup(
                    claim,
                    record,
                    reservation_id,
                    terminal_receipts,
                    receipt_pending=False,
                    release_pending=record["release_pending"],
                )
            except Exception:
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
        if record["release_pending"]:
            try:
                released = self.claims.release(claim)
            except Exception:
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
            if not self._release_completed(released, claim):
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
            self._persist_claim_cleanup(
                claim,
                record,
                reservation_id,
                terminal_receipts,
                receipt_pending=False,
                release_pending=False,
            )
        clearer = getattr(self.store, "clear_claim_cleanup", None)
        if not callable(clearer):
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        try:
            clearer()
        except Exception:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
        self._recovery_pending = False

    def _secure_reserved_evidence(
        self,
        reservation_id: str | None,
        records: Sequence[Mapping[str, Any]],
        *,
        claim: Mapping[str, str] | None = None,
        proposal: Mapping[str, Any] | None = None,
    ) -> None:
        if reservation_id is not None:
            if claim is not None and proposal is not None:
                try:
                    self._persist_claim_cleanup(
                        claim,
                        proposal,
                        reservation_id,
                        records,
                        receipt_pending=True,
                        release_pending=True,
                    )
                except FleetError:
                    self._recovery_pending = True
                    raise
            try:
                self.store.commit_reserved_receipts(reservation_id, records)
            except Exception:
                self._recovery_pending = True
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
            if claim is not None and proposal is not None:
                try:
                    self._persist_claim_cleanup(
                        claim,
                        proposal,
                        reservation_id,
                        records,
                        receipt_pending=False,
                        release_pending=True,
                    )
                except FleetError:
                    self._recovery_pending = True
                    raise
            return
        for record in records:
            try:
                self._write_post_claim(record)
            except FleetError:
                if claim is None or proposal is None:
                    raise

    def _finish_claim_release(
        self,
        claim: Mapping[str, str] | None,
        proposal: Mapping[str, Any],
        reservation_id: str | None,
        terminal_receipts: Sequence[Mapping[str, Any]],
    ) -> None:
        if claim is None or self.claims is None:
            return
        if self._recovery_pending:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        if reservation_id is None or not terminal_receipts:
            self._recovery_pending = True
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        try:
            released = self.claims.release(claim)
        except Exception:
            released = {"granted": False}
        if self._release_completed(released, claim):
            self._persist_claim_cleanup(
                claim,
                proposal,
                reservation_id,
                terminal_receipts,
                receipt_pending=False,
                release_pending=False,
            )
            clearer = getattr(self.store, "clear_claim_cleanup", None)
            if not callable(clearer):
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
            try:
                clearer()
            except Exception:
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
            return
        self._persist_claim_cleanup(
            claim,
            proposal,
            reservation_id,
            terminal_receipts,
            receipt_pending=False,
            release_pending=True,
        )
        raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])

    def _terminalize_reserved_receipt(self, record: Mapping[str, Any], outcome: str) -> dict[str, Any]:
        return {**record, "outcome": outcome}

    def _binding_matches(self, proposal: Mapping[str, Any], approval: Mapping[str, Any]) -> bool:
        keys = [
            "proposal_id",
            "finding_id",
            "service_id",
            "target_alias",
            "finding_revision",
            "system_revision",
            "action_id",
            "verification_id",
            "rollback_id",
            "nonce",
            "expires_at",
        ]
        if "wazuh_fingerprint" in proposal:
            keys.extend(("wazuh_fingerprint", "maintenance_window_id"))
        return all(approval.get(key) == proposal.get(key) for key in keys)

    def _propose_wazuh(self, finding: Mapping[str, Any]) -> dict[str, Any]:
        suppressions = self.wazuh_source.suppressions() if self.wazuh_source is not None else ()
        binding = bind_wazuh_remediation(
            finding,
            registry=self.registry,
            now=self.now(),
            suppressions=suppressions,
        )
        target = authorize_host_read(self.registry, binding["target_alias"])
        remediator = self._active_remediator()
        live = remediator.recheck(binding["action_id"])
        if live.get("health_class") != "unhealthy":
            proposal = {
                "finding_id": binding["finding_id"],
                "target_alias": binding["target_alias"],
                "policy_tier": target["tier"],
                "proposed_action_id": binding["action_id"],
                "reason": sanitize_detail(str(finding.get("reason") or finding.get("title") or ""), self.secrets),
                "blast_radius": binding["blast_radius"],
                "approval_required": True,
                "verification_id": binding["verification_id"],
                "rollback_id": binding["rollback_id"],
                "execution_available": False,
            }
            return _envelope(
                request_id=self.request_id(),
                observed_at=_iso(self.now),
                status="ok",
                data=proposal,
                freshness_seconds=target["freshness_seconds"],
                receipt_ref=None,
            )
        proposal_id = self.create_proposal_id()
        nonce = self.create_nonce()
        created_at = self.now()
        expires_at = (created_at + timedelta(milliseconds=PROPOSAL_TTL_MS)).isoformat().replace("+00:00", "Z")
        if created_at.tzinfo is None:
            expires_at = (created_at + timedelta(milliseconds=PROPOSAL_TTL_MS)).isoformat() + "Z"
        created_text = _iso(lambda: created_at)
        record = {
            "version": 1,
            "proposal_id": proposal_id,
            "finding_id": binding["finding_id"],
            "service_id": binding["service_id"],
            "target_alias": binding["target_alias"],
            "finding_revision": binding["finding_revision"],
            "system_revision": live["system_revision"],
            "action_id": binding["action_id"],
            "verification_id": binding["verification_id"],
            "rollback_id": binding["rollback_id"],
            "nonce": nonce,
            "created_at": created_text,
            "expires_at": expires_at,
            "automatic_rollback": binding["automatic_rollback"],
            "blast_radius": binding["blast_radius"],
            "wazuh_fingerprint": binding["wazuh_fingerprint"],
            "maintenance_window_id": binding["maintenance_window_id"],
        }
        self.store.create_proposal(record)
        self.store.write_receipt(
            {
                "version": 1,
                "receipt_id": self.create_receipt_id(),
                "kind": "proposal",
                "proposal_id": proposal_id,
                "finding_id": record["finding_id"],
                "service_id": record["service_id"],
                "target_alias": record["target_alias"],
                "action_id": record["action_id"],
                "verification_id": record["verification_id"],
                "rollback_id": record["rollback_id"],
                "outcome": "created",
                "created_at": created_text,
            }
        )
        proposal = {
            "finding_id": record["finding_id"],
            "target_alias": record["target_alias"],
            "policy_tier": target["tier"],
            "proposed_action_id": binding["action_id"],
            "reason": sanitize_detail(str(finding.get("reason") or finding.get("title") or ""), self.secrets),
            "blast_radius": binding["blast_radius"],
            "approval_required": True,
            "verification_id": binding["verification_id"],
            "rollback_id": binding["rollback_id"],
            "execution_available": True,
            "proposal_id": proposal_id,
            "expires_at": expires_at,
        }
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data=proposal,
            freshness_seconds=target["freshness_seconds"],
            receipt_ref=None,
        )

    def _approval_expired(self, proposal: Mapping[str, Any], approval: Mapping[str, Any] | None) -> bool:
        if _parse_iso(proposal["expires_at"]) <= self.now():
            return True
        if approval is None:
            return False
        expires = approval.get("expires_at")
        return isinstance(expires, str) and _parse_iso(expires) <= self.now()

    def _execute_wazuh(self, proposal_id: str, proposal: Mapping[str, Any]) -> dict[str, Any]:
        binding = self._public_binding(proposal)
        approval = self.store.read_approval(proposal_id)
        if approval is None:
            self._write_rejection(proposal_id, "denied", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if self._approval_expired(proposal, approval):
            self._write_rejection(proposal_id, "expired", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if not self._binding_matches(proposal, approval):
            self._write_rejection(proposal_id, "cross_bound", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if self.wazuh_source is None:
            self._write_rejection(proposal_id, "stale", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        current = self.wazuh_source.current_finding(proposal["finding_id"])
        if current is None or current.get("revision") != proposal["finding_revision"]:
            self._write_rejection(proposal_id, "stale", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if current.get("fingerprint") != proposal.get("wazuh_fingerprint"):
            self._write_rejection(proposal_id, "stale", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if self.store.is_consumed(proposal_id):
            self._write_rejection(proposal_id, "replayed", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        bound = bind_wazuh_remediation(
            current,
            registry=self.registry,
            now=self.now(),
            suppressions=self.wazuh_source.suppressions(),
        )
        if any(bound[key] != proposal.get(key) for key in WAZUH_REVALIDATION_KEYS):
            self._write_rejection(proposal_id, "stale", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        remediator = self._active_remediator()
        try:
            live = remediator.recheck(proposal["action_id"])
        except Exception as exc:
            self._write_rejection(proposal_id, "failed", binding)
            if isinstance(exc, FleetError):
                raise
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        if live.get("system_revision") != proposal["system_revision"]:
            self._write_rejection(proposal_id, "stale", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if live.get("health_class") != "unhealthy":
            self._write_rejection(proposal_id, "denied", binding)
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if self.claims is None:
            self._write_rejection(proposal_id, "failed", binding)
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        reservation_id = None
        original_reservation_id = None
        claim = None
        terminal: dict[str, Any] | None = None
        terminal_receipts: list[dict[str, Any]] = []
        try:
            hold_at = _iso(self.now)
            reserved_rejection = self._action_receipt(
                proposal, binding, kind="rejection", outcome="unverified", created_at=hold_at
            )
            reserved_execution = self._action_receipt(
                proposal, binding, kind="execution", outcome="unverified", created_at=hold_at
            )
            reserved_verification = self._action_receipt(
                proposal, binding, kind="verification", outcome="unverified", created_at=hold_at
            )
            try:
                reservation_id = self.store.reserve_receipt_capacity(
                    [reserved_rejection, reserved_execution, reserved_verification]
                )
                original_reservation_id = reservation_id
            except FleetActionStoreError:
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from None
            claim = self.claims.acquire(proposal["target_alias"])
            self._persist_claim_cleanup(
                claim,
                proposal,
                reservation_id,
                [reserved_execution],
                receipt_pending=True,
                release_pending=True,
            )
            try:
                self.store.claim_consumed({**approval, "consumed_at": _iso(self.now)})
            except FleetActionStoreError as exc:
                terminal_receipts = [self._terminalize_reserved_receipt(reserved_rejection, exc.outcome)]
                self._secure_reserved_evidence(reservation_id, terminal_receipts, claim=claim, proposal=proposal)
                reservation_id = None
                raise FleetError("denied", ERROR_MESSAGES["denied"]) from exc
            try:
                remediator.execute(proposal["action_id"])
            except Exception as exc:
                terminal_receipts = [self._terminalize_reserved_receipt(reserved_execution, "failed")]
                self._secure_reserved_evidence(reservation_id, terminal_receipts, claim=claim, proposal=proposal)
                reservation_id = None
                if isinstance(exc, FleetError):
                    raise
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            try:
                verified = remediator.verify(proposal["verification_id"])
            except Exception as exc:
                terminal_receipts = [
                    self._terminalize_reserved_receipt(reserved_execution, "verified"),
                    self._terminalize_reserved_receipt(reserved_verification, "failed"),
                ]
                self._secure_reserved_evidence(reservation_id, terminal_receipts, claim=claim, proposal=proposal)
                reservation_id = None
                if isinstance(exc, FleetError):
                    raise
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
            if not verified:
                terminal_receipts = [
                    self._terminalize_reserved_receipt(reserved_execution, "verified"),
                    self._terminalize_reserved_receipt(reserved_verification, "failed"),
                ]
                self._secure_reserved_evidence(reservation_id, terminal_receipts, claim=claim, proposal=proposal)
                reservation_id = None
                raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
            terminal_receipts = [
                self._terminalize_reserved_receipt(reserved_execution, "verified"),
                self._terminalize_reserved_receipt(reserved_verification, "verified"),
            ]
            self._secure_reserved_evidence(reservation_id, terminal_receipts, claim=claim, proposal=proposal)
            reservation_id = None
            verification_receipt_id = reserved_verification["receipt_id"]
            terminal = _envelope(
                request_id=self.request_id(),
                observed_at=_iso(self.now),
                status="ok",
                data={
                    "proposal_id": proposal["proposal_id"],
                    "finding_id": proposal["finding_id"],
                    "service_id": proposal["service_id"],
                    "target_alias": proposal["target_alias"],
                    "action_id": proposal["action_id"],
                    "verification_id": proposal["verification_id"],
                    "outcome": "verified",
                    "receipt_ref": verification_receipt_id,
                },
                freshness_seconds=authorize_host_read(self.registry, proposal["target_alias"])["freshness_seconds"],
                receipt_ref=verification_receipt_id,
            )
        finally:
            if reservation_id is not None and not self._recovery_pending:
                try:
                    self.store.release_receipt_reservation(reservation_id)
                except Exception:
                    pass
            self._finish_claim_release(claim, proposal, original_reservation_id, terminal_receipts)
        if terminal is None:
            raise FleetError("unavailable", ERROR_MESSAGES["unavailable"])
        return terminal

    def _execute_remediation(self, raw: object) -> dict[str, Any]:
        proposal_id = parse_execute_input(raw)["proposal_id"]
        try:
            proposal = self.store.read_proposal(proposal_id)
        except FleetError as exc:
            if exc.code == "protocol_error":
                self._write_rejection(proposal_id, "denied")
                raise FleetError("protocol_error", "Fleet observation was invalid") from exc
            raise
        if proposal is None:
            raise FleetError("denied", ERROR_MESSAGES["denied"])
        if "wazuh_fingerprint" in proposal:
            return self._execute_wazuh(proposal_id, proposal)
        binding = self._public_binding(proposal)
        self._write_rejection(proposal_id, "denied", binding)
        raise FleetError("denied", ERROR_MESSAGES["denied"])


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
