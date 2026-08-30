"""Public Wazuh triage tool handlers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import grokbot_findings, grokbot_findings_relay
from .contracts import (
    ERROR_MESSAGES,
    TOOLS,
    WazuhError,
    parse_action_status_input,
    parse_alert_status_input,
    parse_classify_input,
    parse_incident_input,
    parse_ingest_input,
    parse_propose_input,
)
from .normalize import normalize_alert
from .policy import classify, is_action_eligible
from .store import PUBLIC_ALERT_KEYS, WazuhStore

PROPOSAL_TTL = timedelta(hours=1)
PUBLIC_RESULT_LIMIT = 131_072


def _iso(now: Callable[[], datetime]) -> str:
    stamp = now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _envelope(*, request_id: str, observed_at: str, status: str, data: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "observed_at": observed_at,
        "status": status,
        "data": data,
    }


class WazuhTriageTools:
    """Closed public Wazuh triage tool surface."""

    def __init__(
        self,
        *,
        store: WazuhStore,
        target: Path,
        owner: Path,
        now: Callable[[], datetime],
        request_id: Callable[[], str],
        create_proposal_id: Callable[[], str],
        secrets: list[str] | None = None,
    ):
        self.store = store
        self.target = Path(target)
        self.owner = Path(owner)
        self.now = now
        self.request_id = request_id
        self.create_proposal_id = create_proposal_id
        self.secrets = list(secrets or [])

    def inventory(self) -> list[str]:
        return sorted(TOOLS)

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "wazuh_ingest": self.wazuh_ingest,
            "wazuh_alert_status": self.wazuh_alert_status,
            "wazuh_classify": self.wazuh_classify,
            "wazuh_incident_bundle": self.wazuh_incident_bundle,
            "wazuh_propose_remediation": self.wazuh_propose_remediation,
            "wazuh_action_status": self.wazuh_action_status,
        }
        if name not in handlers:
            raise WazuhError("invalid_request", ERROR_MESSAGES["invalid_request"])
        result = handlers[name](arguments)
        encoded = __import__("json").dumps(result, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > PUBLIC_RESULT_LIMIT:
            raise WazuhError("protocol_error", ERROR_MESSAGES["protocol_error"])
        return result

    def wazuh_ingest(self, raw: object) -> dict[str, Any]:
        parsed = parse_ingest_input(raw)
        stamp = self.now()
        records = [normalize_alert(alert, self.secrets) for alert in parsed["alerts"]]
        self.store.preflight_capacity(records)
        suppressions = self.store.suppressions()
        items = [(record, classify(record, suppressions=suppressions, now=stamp)) for record in records]
        results = self.store.upsert_alerts(items, now=stamp)
        created = 0
        fingerprints: list[str] = []
        entries: dict[tuple[str, str, str], dict[str, str]] = {}
        for result in results:
            fingerprints.append(result["fingerprint"])
            if result["created"]:
                created += 1
            if result["needs_relay"]:
                stored = self.store.get_alert(result["fingerprint"])
                if stored is not None:
                    entry = self.store.finding_entry(stored)
                    entries[(entry["producer"], entry["finding_id"], entry["revision"])] = entry
        for pending in self.store.pending_relay_entries():
            entries.setdefault((pending["producer"], pending["finding_id"], pending["revision"]), pending)
        if entries:
            payload = list(entries.values())[: grokbot_findings.MAX_ENTRIES]
            grokbot_findings_relay.relay_apply(
                payload,
                self.target,
                self.owner,
                limit=len(payload),
                now=stamp,
            )
            reported = [
                record["relay_id"]
                for record in grokbot_findings_relay._read_outbox_records(self.target)
                if record.get("status") == "reported"
            ]
            if reported:
                self.store.confirm_reported_relays(reported)
        counts = self.store.counts()
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "current": counts["current"],
                "created": created,
                "ingested": len(parsed["alerts"]),
                "fingerprints": fingerprints,
                "last_seen": self.store.last_seen(),
            },
        )

    def wazuh_alert_status(self, raw: object) -> dict[str, Any]:
        parse_alert_status_input(raw)
        counts = _count_projected(self._projected_public_alerts())
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "current": counts["current"],
                "suppressed": counts["suppressed"],
                "watched": counts["watched"],
                "escalated": counts["escalated"],
                "last_seen": self.store.last_seen(),
            },
        )

    def wazuh_classify(self, raw: object) -> dict[str, Any]:
        parsed = parse_classify_input(raw)
        stored = self.store.get_alert(parsed["fingerprint"])
        if stored is None:
            raise WazuhError("not_found", ERROR_MESSAGES["not_found"])
        decision = classify(stored, suppressions=self.store.suppressions(), now=self.now())
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "category": decision["category"],
                "reason": decision["reason"],
                "expires_at": decision["expires_at"],
                "fingerprint": parsed["fingerprint"],
            },
        )

    def wazuh_incident_bundle(self, raw: object) -> dict[str, Any]:
        parsed = parse_incident_input(raw)
        scope = parsed["scope"]
        findings = [
            item
            for item in self._projected_public_alerts()
            if item["rule_class"] == scope or item["finding_id"] == scope or item["finding_id"].endswith(f":{scope}")
        ]
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={"scope": scope, "findings": findings},
        )

    def wazuh_propose_remediation(self, raw: object) -> dict[str, Any]:
        parsed = parse_propose_input(raw)
        stored = self.store.get_finding(parsed["finding_id"])
        if stored is None:
            raise WazuhError("not_found", ERROR_MESSAGES["not_found"])
        decision = classify(stored, suppressions=self.store.suppressions(), now=self.now())
        if not is_action_eligible(decision):
            raise WazuhError("denied", ERROR_MESSAGES["denied"])
        created = _iso(self.now)
        expires = (self.now() + PROPOSAL_TTL).isoformat().replace("+00:00", "Z")
        proposal = self.store.put_proposal(
            {
                "proposal_id": self.create_proposal_id(),
                "finding_id": stored["finding_id"],
                "fingerprint": stored["fingerprint"],
                "state": "proposed",
                "created_at": created,
                "expires_at": expires,
            }
        )
        return _envelope(
            request_id=self.request_id(),
            observed_at=created,
            status="ok",
            data={
                "proposal_id": proposal["proposal_id"],
                "state": proposal["state"],
                "finding_id": proposal["finding_id"],
            },
        )

    def wazuh_action_status(self, raw: object) -> dict[str, Any]:
        parsed = parse_action_status_input(raw)
        proposal = self.store.expire_proposal_if_needed(parsed["proposal_id"], self.now())
        if proposal is None:
            raise WazuhError("not_found", ERROR_MESSAGES["not_found"])
        return _envelope(
            request_id=self.request_id(),
            observed_at=_iso(self.now),
            status="ok",
            data={
                "proposal_id": proposal["proposal_id"],
                "state": proposal["state"],
                "created_at": proposal["created_at"],
                "expires_at": proposal["expires_at"],
            },
        )

    def _projected_public_alerts(self) -> list[dict[str, Any]]:
        stamp = self.now()
        suppressions = self.store.suppressions()
        return [
            _project_public_alert(item, classify(item, suppressions=suppressions, now=stamp))
            for item in self.store.alerts()
        ]


def _project_public_alert(stored: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    public = {key: stored[key] for key in sorted(PUBLIC_ALERT_KEYS) if key in stored}
    public["category"] = decision["category"]
    public["reason"] = decision["reason"]
    return public


def _count_projected(alerts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"current": len(alerts), "suppressed": 0, "watched": 0, "escalated": 0}
    for item in alerts:
        category = item.get("category")
        if category == "suppress":
            counts["suppressed"] += 1
        elif category == "escalate":
            counts["escalated"] += 1
        else:
            counts["watched"] += 1
    return counts
