"""Owner-only Fleet Steward ledger with bounded retention."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from .contracts import FleetError, TIERS, is_offset_datetime, omit_undefined, parse_identifier

LEDGER_VERSION = 1
MAX_HOST_OBSERVATIONS = 512
MAX_SERVICE_OBSERVATIONS = 512
MAX_FINDINGS = 128


def _ledger_invalid() -> NoReturn:
    raise FleetError("protocol_error", "Fleet ledger is invalid")


def _ledger_write_failed() -> NoReturn:
    raise FleetError("unavailable", "Fleet ledger write failed")


def _write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _parse_receipt_ref(value: object) -> str | None:
    if value is None:
        return None
    return parse_identifier(value)


def _belongs_to_scope(finding: Mapping[str, Any], scope: str) -> bool:
    finding_id = finding["finding_id"]
    return finding_id == scope or finding_id.startswith(f"{scope}:")


def _host_state_changed(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return (
        previous.get("reachability") != current.get("reachability")
        or previous.get("uptime_class") != current.get("uptime_class")
        or previous.get("storage_pressure") != current.get("storage_pressure")
        or previous.get("failed_service_count") != current.get("failed_service_count")
        or previous.get("reboot_pending") != current.get("reboot_pending")
    )


def _service_state_changed(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return previous.get("health_class") != current.get("health_class")


def _retain(items: list[Any], maximum: int) -> list[Any]:
    return items if len(items) <= maximum else items[-maximum:]


def _parse_host_observation(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
    allowed = {
        "alias",
        "tier",
        "reachability",
        "uptime_class",
        "storage_pressure",
        "failed_service_count",
        "reboot_pending",
        "observed_at",
        "freshness_seconds",
        "changed",
        "detail",
    }
    if not set(raw) <= allowed or "alias" not in raw or "tier" not in raw:
        _ledger_invalid()
    alias = parse_identifier(raw.get("alias"))
    if raw.get("tier") not in TIERS:
        _ledger_invalid()
    observation = {"alias": alias, "tier": raw["tier"]}
    for key in (
        "reachability",
        "uptime_class",
        "storage_pressure",
    ):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            _ledger_invalid()
        observation[key] = value
    if "failed_service_count" in raw:
        count = raw["failed_service_count"]
        if type(count) is not int or count < 0:
            _ledger_invalid()
        observation["failed_service_count"] = count
    if "reboot_pending" in raw:
        if not isinstance(raw["reboot_pending"], bool):
            _ledger_invalid()
        observation["reboot_pending"] = raw["reboot_pending"]
    if "observed_at" in raw:
        if not is_offset_datetime(raw["observed_at"]):
            _ledger_invalid()
        observation["observed_at"] = raw["observed_at"]
    if "freshness_seconds" in raw:
        freshness = raw["freshness_seconds"]
        if type(freshness) is not int or not 0 <= freshness <= 86_400:
            _ledger_invalid()
        observation["freshness_seconds"] = freshness
    if "changed" in raw:
        if not isinstance(raw["changed"], bool):
            _ledger_invalid()
        observation["changed"] = raw["changed"]
    if "detail" in raw:
        detail = raw["detail"]
        if not isinstance(detail, str) or len(detail.encode("utf-8")) > 4_096:
            _ledger_invalid()
        observation["detail"] = detail
    return omit_undefined(observation)


def _parse_service_observation(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
    allowed = {
        "service_id",
        "target_alias",
        "health_class",
        "observed_at",
        "freshness_seconds",
        "changed",
        "detail",
    }
    if not set(raw) <= allowed or "service_id" not in raw or "target_alias" not in raw:
        _ledger_invalid()
    observation: dict[str, Any] = {
        "service_id": parse_identifier(raw.get("service_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
    }
    if "health_class" in raw:
        value = raw["health_class"]
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            _ledger_invalid()
        observation["health_class"] = value
    if "observed_at" in raw:
        if not is_offset_datetime(raw["observed_at"]):
            _ledger_invalid()
        observation["observed_at"] = raw["observed_at"]
    if "freshness_seconds" in raw:
        freshness = raw["freshness_seconds"]
        if type(freshness) is not int or not 0 <= freshness <= 86_400:
            _ledger_invalid()
        observation["freshness_seconds"] = freshness
    if "changed" in raw:
        if not isinstance(raw["changed"], bool):
            _ledger_invalid()
        observation["changed"] = raw["changed"]
    if "detail" in raw:
        detail = raw["detail"]
        if not isinstance(detail, str) or len(detail.encode("utf-8")) > 4_096:
            _ledger_invalid()
        observation["detail"] = detail
    return omit_undefined(observation)


def _parse_finding(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
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
    if not set(raw) <= allowed or not required <= set(raw):
        _ledger_invalid()
    finding = {
        "finding_id": parse_identifier(raw.get("finding_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "proposed_action_id": parse_identifier(raw.get("proposed_action_id")),
        "verification_id": parse_identifier(raw.get("verification_id")),
        "rollback_id": parse_identifier(raw.get("rollback_id")),
    }
    reason = raw.get("reason")
    blast = raw.get("blast_radius")
    if not isinstance(reason, str) or len(reason.encode("utf-8")) > 16_384:
        _ledger_invalid()
    if not isinstance(blast, str) or len(blast.encode("utf-8")) > 16_384:
        _ledger_invalid()
    finding["reason"] = reason
    finding["blast_radius"] = blast
    if "observed_at" in raw:
        if not is_offset_datetime(raw["observed_at"]):
            _ledger_invalid()
        finding["observed_at"] = raw["observed_at"]
    if "incident_receipt_ref" in raw:
        finding["incident_receipt_ref"] = parse_identifier(raw.get("incident_receipt_ref"))
    return omit_undefined(finding)


def _validated_document(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"version", "hosts", "services", "findings"}:
        _ledger_invalid()
    if value.get("version") != LEDGER_VERSION:
        _ledger_invalid()
    hosts_raw = value.get("hosts")
    services_raw = value.get("services")
    findings_raw = value.get("findings")
    if not isinstance(hosts_raw, dict) or not isinstance(services_raw, dict) or not isinstance(findings_raw, list):
        _ledger_invalid()
    if len(findings_raw) > MAX_FINDINGS:
        _ledger_invalid()
    hosts: dict[str, list[dict[str, Any]]] = {}
    for alias, records in hosts_raw.items():
        parse_identifier(alias)
        if not isinstance(records, list) or len(records) > MAX_HOST_OBSERVATIONS:
            _ledger_invalid()
        parsed_records = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"observation", "receipt_ref"}:
                _ledger_invalid()
            observation = _parse_host_observation(record.get("observation"))
            if observation["alias"] != alias:
                _ledger_invalid()
            parsed_records.append(
                {"observation": observation, "receipt_ref": _parse_receipt_ref(record.get("receipt_ref"))}
            )
        hosts[alias] = parsed_records
    services: dict[str, list[dict[str, Any]]] = {}
    for service_id, records in services_raw.items():
        parse_identifier(service_id)
        if not isinstance(records, list) or len(records) > MAX_SERVICE_OBSERVATIONS:
            _ledger_invalid()
        parsed_records = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != {"observation", "receipt_ref"}:
                _ledger_invalid()
            observation = _parse_service_observation(record.get("observation"))
            if observation["service_id"] != service_id:
                _ledger_invalid()
            parsed_records.append(
                {"observation": observation, "receipt_ref": _parse_receipt_ref(record.get("receipt_ref"))}
            )
        services[service_id] = parsed_records
    findings = []
    seen: set[str] = set()
    for entry in findings_raw:
        finding = _parse_finding(entry)
        if finding["finding_id"] in seen:
            _ledger_invalid()
        seen.add(finding["finding_id"])
        findings.append(finding)
    return {"version": LEDGER_VERSION, "hosts": hosts, "services": services, "findings": findings}


class FleetLedger:
    """Serialized owner-only ledger file."""

    def __init__(self, ledger_path: str):
        if not isinstance(ledger_path, str) or not ledger_path:
            _ledger_invalid()
        self._path = Path(ledger_path)
        self._temp = Path(f"{ledger_path}.tmp")
        self._lock = threading.Lock()

    def ready(self) -> None:
        with self._lock:
            self._ensure_state_dir()
            self._load_document()

    def record_observation(self, observation: Mapping[str, Any], receipt_ref: str | None) -> None:
        with self._lock:
            parsed = _parse_host_observation(observation)
            receipt = _parse_receipt_ref(receipt_ref)
            document = self._load_document()
            alias = parsed["alias"]
            current = document["hosts"].get(alias, [])
            document["hosts"][alias] = _retain(
                [*current, {"observation": parsed, "receipt_ref": receipt}],
                MAX_HOST_OBSERVATIONS,
            )
            self._persist(document)

    def record_service_observation(self, observation: Mapping[str, Any], receipt_ref: str | None) -> None:
        with self._lock:
            parsed = _parse_service_observation(observation)
            receipt = _parse_receipt_ref(receipt_ref)
            document = self._load_document()
            service_id = parsed["service_id"]
            current = document["services"].get(service_id, [])
            document["services"][service_id] = _retain(
                [*current, {"observation": parsed, "receipt_ref": receipt}],
                MAX_SERVICE_OBSERVATIONS,
            )
            self._persist(document)

    def record_host_observation_with_change(
        self, observation: Mapping[str, Any], receipt_ref: str | None
    ) -> dict[str, Any]:
        with self._lock:
            parsed = _parse_host_observation(observation)
            receipt = _parse_receipt_ref(receipt_ref)
            document = self._load_document()
            alias = parsed["alias"]
            current = document["hosts"].get(alias, [])
            previous = current[-1]["observation"] if current else None
            next_observation = dict(parsed)
            next_observation["changed"] = False if previous is None else _host_state_changed(previous, parsed)
            document["hosts"][alias] = _retain(
                [*current, {"observation": next_observation, "receipt_ref": receipt}],
                MAX_HOST_OBSERVATIONS,
            )
            self._persist(document)
            return next_observation

    def record_service_observation_with_change(
        self, observation: Mapping[str, Any], receipt_ref: str | None
    ) -> dict[str, Any]:
        with self._lock:
            parsed = _parse_service_observation(observation)
            receipt = _parse_receipt_ref(receipt_ref)
            document = self._load_document()
            service_id = parsed["service_id"]
            current = document["services"].get(service_id, [])
            previous = current[-1]["observation"] if current else None
            next_observation = dict(parsed)
            next_observation["changed"] = False if previous is None else _service_state_changed(previous, parsed)
            document["services"][service_id] = _retain(
                [*current, {"observation": next_observation, "receipt_ref": receipt}],
                MAX_SERVICE_OBSERVATIONS,
            )
            self._persist(document)
            return next_observation

    def last_host_observation(self, alias: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(alias)
            document = self._load_document()
            records = document["hosts"].get(key)
            return None if not records else dict(records[-1]["observation"])

    def last_service_observation(self, service_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(service_id)
            document = self._load_document()
            records = document["services"].get(key)
            return None if not records else dict(records[-1]["observation"])

    def finding(self, finding_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(finding_id)
            document = self._load_document()
            for entry in document["findings"]:
                if entry["finding_id"] == key:
                    return dict(entry)
            return None

    def findings(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._load_document()
            return [dict(entry) for entry in sorted(document["findings"], key=lambda item: item["finding_id"])]

    def replace_findings(self, scope: str, findings: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            requested = parse_identifier(scope)
            if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
                _ledger_invalid()
            incoming = []
            seen: set[str] = set()
            for entry in findings:
                finding = _parse_finding(entry)
                if not _belongs_to_scope(finding, requested):
                    _ledger_invalid()
                if finding["finding_id"] in seen:
                    _ledger_invalid()
                seen.add(finding["finding_id"])
                incoming.append(finding)
            document = self._load_document()
            preserved = [entry for entry in document["findings"] if not _belongs_to_scope(entry, requested)]
            if len(preserved) + len(incoming) > MAX_FINDINGS:
                _ledger_invalid()
            document["findings"] = [*preserved, *incoming]
            self._persist(document)

    def _ensure_state_dir(self) -> None:
        directory = self._path.parent
        try:
            info = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(parents=True, mode=0o700)
                os.chmod(directory, 0o700)
            except OSError as exc:
                raise FleetError("unavailable", "Fleet ledger write failed") from exc
            info = directory.lstat()
        except OSError:
            _ledger_invalid()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            _ledger_invalid()

    def _load_document(self) -> dict[str, Any]:
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return {"version": LEDGER_VERSION, "hosts": {}, "services": {}, "findings": []}
        except OSError:
            _ledger_invalid()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            _ledger_invalid()
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _ledger_invalid()
        return _validated_document(parsed)

    def _persist(self, document: dict[str, Any]) -> None:
        self._ensure_state_dir()
        handle = None
        try:
            handle = os.open(self._temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            os.fchmod(handle, 0o600)
            _write_all(handle, json.dumps(document).encode("utf-8"))
            os.fsync(handle)
            os.close(handle)
            handle = None
            os.replace(self._temp, self._path)
        except FleetError:
            raise
        except OSError as exc:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            try:
                self._temp.unlink()
            except OSError:
                pass
            raise FleetError("unavailable", "Fleet ledger write failed") from exc
