"""Relay first-party steward ledger findings through generic owner delivery."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import grokbot_findings, grokbot_findings_relay, grokbot_packs
from .grokbot_backup.contracts import BackupError
from .grokbot_backup.ledger import BackupLedger
from .grokbot_findings import DEFAULT_LIMIT, FINDINGS_SCHEMA, FindingsError, MAX_ENTRIES
from .grokbot_fleet.contracts import FleetError
from .grokbot_fleet.ledger import FleetLedger

PRODUCER_ORDER = ("backup-steward", "fleet-steward", "wazuh-triage")
LIVE_PRODUCERS = {
    "backup-steward": "backup",
    "fleet-steward": "fleet",
    "wazuh-triage": "wazuh",
}
PUBLIC_KEYS = (
    "apply",
    "eligible",
    "known",
    "created",
    "skipped",
    "pending",
    "reported",
    "limit",
    "relays",
)


class PackRelayError(ValueError):
    """A rejected first-party finding relay request."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def collect_manifests(target: Path) -> list[dict[str, Any]]:
    """Build in-memory generic manifests in producer order. Never writes files."""
    return [
        {"schema": FINDINGS_SCHEMA, "entries": _collect_entries(Path(target), pack_id)} for pack_id in PRODUCER_ORDER
    ]


def preview_relay(
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Count steward findings without writing drafts, markers, or manifests."""
    return _relay(Path(target), Path(owner), apply=False, limit=limit, now=None)


def apply_relay(
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Relay a bounded steward batch through generic findings and Fleet Hub."""
    return _relay(Path(target), Path(owner), apply=True, limit=limit, now=now)


def _relay(
    target: Path,
    owner: Path,
    *,
    apply: bool,
    limit: int,
    now: datetime | None,
) -> dict[str, Any]:
    try:
        bound = grokbot_findings._validate_limit(limit)
        grokbot_findings._validate_owner(owner)
    except FindingsError as exc:
        raise PackRelayError(exc.reason) from exc
    remaining = bound
    totals = {
        "apply": apply,
        "eligible": 0,
        "known": 0,
        "created": 0,
        "skipped": 0,
        "pending": 0,
        "reported": 0,
        "limit": bound,
        "relays": [],
    }
    relays: list[str] = []
    for manifest in collect_manifests(target):
        entries = list(manifest["entries"])
        for offset in range(0, len(entries), MAX_ENTRIES):
            chunk = entries[offset : offset + MAX_ENTRIES]
            try:
                if not apply or remaining <= 0:
                    preview = grokbot_findings_relay.relay_preview(chunk, target, owner, limit=max(remaining, 1))
                    totals["eligible"] += preview["eligible"]
                    totals["known"] += preview["known"]
                    if apply:
                        continue
                    relays.extend(preview["relays"][:remaining])
                    remaining = max(remaining - preview["eligible"], 0)
                    continue
                result = grokbot_findings_relay.relay_apply(chunk, target, owner, limit=remaining, now=now)
            except FindingsError as exc:
                raise PackRelayError(exc.reason) from exc
            totals["eligible"] += result["eligible"]
            totals["known"] += result["known"]
            totals["created"] += result["created"]
            totals["skipped"] += result["skipped"]
            totals["pending"] = result["pending"]
            totals["reported"] += result["reported"]
            relays.extend(result["relays"])
            remaining -= result["created"]
    totals["relays"] = _unique(relays)
    return {key: totals[key] for key in PUBLIC_KEYS}


def _collect_entries(target: Path, pack_id: str) -> list[dict[str, str]]:
    config = _optional_instance(target, pack_id)
    if config is None:
        return []
    ledger_path = config["ledger_path"]
    try:
        if pack_id == "backup-steward":
            return _backup_entries(str(ledger_path))
        if pack_id == "fleet-steward":
            return _fleet_entries(str(ledger_path))
        return _wazuh_entries(str(ledger_path))
    except FindingsError as exc:
        raise PackRelayError(exc.reason) from exc
    except (BackupError, FleetError) as exc:
        raise PackRelayError("unsafe-path") from exc


def _optional_instance(target: Path, pack_id: str) -> dict[str, Any] | None:
    path = grokbot_packs.instance_config_path(target, pack_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackRelayError("unsafe-config") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PackRelayError("unsafe-config")
    try:
        return grokbot_packs._load_instance_config(target, pack_id)
    except grokbot_packs.PackError as exc:
        raise PackRelayError(exc.reason) from exc


def _backup_entries(ledger_path: str) -> list[dict[str, str]]:
    ledger = BackupLedger(ledger_path)
    entries = []
    for index, record in enumerate(ledger.finding_records()):
        finding = record["finding"]
        revision = record["revision"]
        entries.append(
            grokbot_findings.adapt_live_finding(
                {
                    "producer": LIVE_PRODUCERS["backup-steward"],
                    "finding_id": finding["finding_id"],
                    "revision": revision,
                    "observed_at": finding["observed_at"],
                    "severity": finding["severity_class"],
                    "title": finding["summary"],
                    "body": finding["summary"],
                    "source_ref": f"backup:{finding['finding_id']}",
                    "source_digest": revision,
                    "trust": "untrusted",
                    "delivery": "review-only",
                },
                index=index,
            )
        )
    return entries


def _fleet_entries(ledger_path: str) -> list[dict[str, str]]:
    ledger = FleetLedger(ledger_path)
    entries = []
    for index, finding in enumerate(ledger.findings()):
        revision = _fleet_revision(finding)
        entries.append(
            grokbot_findings.adapt_live_finding(
                {
                    "producer": LIVE_PRODUCERS["fleet-steward"],
                    "finding_id": finding["finding_id"],
                    "revision": revision,
                    "observed_at": finding.get("observed_at") or "",
                    "severity": "unknown",
                    "title": finding["reason"],
                    "body": finding["reason"],
                    "source_ref": f"fleet:{finding['finding_id']}",
                    "source_digest": revision,
                    "trust": "untrusted",
                    "delivery": "review-only",
                },
                index=index,
            )
        )
    return entries


def _wazuh_entries(_ledger_path: str) -> list[dict[str, str]]:
    return []


def _fleet_revision(finding: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(finding), separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
