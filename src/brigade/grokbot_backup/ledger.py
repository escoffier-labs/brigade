"""Owner-only Backup Steward ledger with bounded JSONL retention."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from .contracts import (
    ACTION_IDS,
    FINDING_KINDS,
    HEALTH_CLASSES,
    LOCK_CLASSES,
    SEVERITY_CLASSES,
    BackupError,
    is_offset_datetime,
    omit_undefined,
    parse_identifier,
)

LEDGER_VERSION = 1
MAX_RECORDS = 2_048
MAX_TEXT_BYTES = 4_096


def _ledger_invalid() -> NoReturn:
    raise BackupError("protocol_error", "Backup ledger is invalid")


def _ledger_write_failed() -> NoReturn:
    raise BackupError("unavailable", "Backup ledger write failed")


def _write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _assert_public_text(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        _ledger_invalid()
    if "://" in value or "\\" in value or __import__("re").search(r"(?<![A-Za-z0-9])/[A-Za-z]", value):
        _ledger_invalid()


def _optional_text(raw: Mapping[str, Any], key: str, *, maximum: int = 64) -> str | None:
    if key not in raw:
        return None
    value = raw.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _ledger_invalid()
    return value


def _optional_int(raw: Mapping[str, Any], key: str, *, maximum: int | None = None) -> int | None:
    if key not in raw:
        return None
    value = raw.get(key)
    if type(value) is not int or value < 0:
        _ledger_invalid()
    if maximum is not None and value > maximum:
        _ledger_invalid()
    return value


def backup_finding_revision(finding: Mapping[str, Any]) -> str:
    payload = {
        "blast_radius": finding.get("blast_radius") or "",
        "finding_id": finding["finding_id"],
        "kind": finding["kind"],
        "observed_at": finding["observed_at"],
        "proposed_action_id": finding.get("proposed_action_id") or "",
        "receipt_ref": finding.get("receipt_ref") or "",
        "recovery_statement": finding.get("recovery_statement") or "",
        "severity_class": finding["severity_class"],
        "summary": finding["summary"],
        "target_alias": finding["target_alias"],
        "verification_statement": finding.get("verification_statement") or "",
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _observation_fingerprint(observation: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "detail": observation.get("detail") or "",
            "health": observation.get("health"),
            "integrity_state": observation.get("integrity_state") or "",
            "last_successful_operation": observation.get("last_successful_operation") or "",
            "lock_class": observation.get("lock_class"),
            "retention_evidence_state": observation.get("retention_evidence_state") or "",
            "scheduler_state": observation.get("scheduler_state") or "",
            "snapshot_age_seconds": observation.get("snapshot_age_seconds"),
            "target_alias": observation.get("target_alias"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _belongs_to_scope(finding: Mapping[str, Any], scope: str) -> bool:
    finding_id = finding["finding_id"]
    return finding_id == scope or finding_id.startswith(f"{scope}:")


def _parse_observation(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
    allowed = {
        "target_alias",
        "health",
        "lock_class",
        "snapshot_age_seconds",
        "last_successful_operation",
        "scheduler_state",
        "integrity_state",
        "retention_evidence_state",
        "observed_at",
        "freshness_seconds",
        "changed",
        "detail",
    }
    if not set(raw) <= allowed or "target_alias" not in raw:
        _ledger_invalid()
    if raw.get("health") not in HEALTH_CLASSES or raw.get("lock_class") not in LOCK_CLASSES:
        _ledger_invalid()
    if not is_offset_datetime(raw.get("observed_at")):
        _ledger_invalid()
    freshness = raw.get("freshness_seconds")
    if type(freshness) is not int or not 0 <= freshness <= 86_400:
        _ledger_invalid()
    observation = {
        "target_alias": parse_identifier(raw.get("target_alias")),
        "health": raw["health"],
        "lock_class": raw["lock_class"],
        "snapshot_age_seconds": _optional_int(raw, "snapshot_age_seconds"),
        "last_successful_operation": _optional_text(raw, "last_successful_operation"),
        "scheduler_state": _optional_text(raw, "scheduler_state"),
        "integrity_state": _optional_text(raw, "integrity_state"),
        "retention_evidence_state": _optional_text(raw, "retention_evidence_state"),
        "observed_at": raw["observed_at"],
        "freshness_seconds": freshness,
    }
    if "changed" in raw:
        if not isinstance(raw["changed"], bool):
            _ledger_invalid()
        observation["changed"] = raw["changed"]
    if "detail" in raw:
        detail = raw["detail"]
        if not isinstance(detail, str) or len(detail.encode("utf-8")) > MAX_TEXT_BYTES:
            _ledger_invalid()
        _assert_public_text(detail)
        observation["detail"] = detail
    return omit_undefined(observation)


def _parse_readiness(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
    allowed = {
        "target_alias",
        "readiness",
        "last_rehearsal_at",
        "last_rehearsal_result",
        "evidence_freshness_seconds",
        "supported_recovery_scope",
        "observed_at",
        "detail",
    }
    if not set(raw) <= allowed or "target_alias" not in raw:
        _ledger_invalid()
    if raw.get("readiness") not in HEALTH_CLASSES or not is_offset_datetime(raw.get("observed_at")):
        _ledger_invalid()
    readiness = {
        "target_alias": parse_identifier(raw.get("target_alias")),
        "readiness": raw["readiness"],
        "last_rehearsal_at": raw.get("last_rehearsal_at") if "last_rehearsal_at" in raw else None,
        "last_rehearsal_result": _optional_text(raw, "last_rehearsal_result"),
        "evidence_freshness_seconds": _optional_int(raw, "evidence_freshness_seconds"),
        "supported_recovery_scope": _optional_text(raw, "supported_recovery_scope"),
        "observed_at": raw["observed_at"],
    }
    if readiness["last_rehearsal_at"] is not None and not is_offset_datetime(readiness["last_rehearsal_at"]):
        _ledger_invalid()
    if "detail" in raw:
        detail = raw["detail"]
        if not isinstance(detail, str) or len(detail.encode("utf-8")) > MAX_TEXT_BYTES:
            _ledger_invalid()
        _assert_public_text(detail)
        readiness["detail"] = detail
    return omit_undefined(readiness)


def _parse_finding(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _ledger_invalid()
    allowed = {
        "finding_id",
        "target_alias",
        "kind",
        "severity_class",
        "summary",
        "observed_at",
        "receipt_ref",
        "proposed_action_id",
        "blast_radius",
        "verification_statement",
        "recovery_statement",
    }
    required = {"finding_id", "target_alias", "kind", "severity_class", "summary", "observed_at"}
    if not set(raw) <= allowed or not required <= set(raw):
        _ledger_invalid()
    if raw.get("kind") not in FINDING_KINDS or raw.get("severity_class") not in SEVERITY_CLASSES:
        _ledger_invalid()
    if not is_offset_datetime(raw.get("observed_at")):
        _ledger_invalid()
    summary = raw.get("summary")
    if not isinstance(summary, str) or len(summary.encode("utf-8")) > MAX_TEXT_BYTES:
        _ledger_invalid()
    _assert_public_text(summary)
    finding = {
        "finding_id": parse_identifier(raw.get("finding_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "kind": raw["kind"],
        "severity_class": raw["severity_class"],
        "summary": summary,
        "observed_at": raw["observed_at"],
        "receipt_ref": None if "receipt_ref" not in raw else parse_identifier(raw.get("receipt_ref")),
        "proposed_action_id": None if "proposed_action_id" not in raw else raw.get("proposed_action_id"),
        "blast_radius": raw.get("blast_radius") if "blast_radius" in raw else None,
        "verification_statement": raw.get("verification_statement") if "verification_statement" in raw else None,
        "recovery_statement": raw.get("recovery_statement") if "recovery_statement" in raw else None,
    }
    if finding["proposed_action_id"] is not None and finding["proposed_action_id"] not in ACTION_IDS:
        _ledger_invalid()
    for key in ("blast_radius", "verification_statement", "recovery_statement"):
        value = finding[key]
        if value is None:
            continue
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            _ledger_invalid()
        _assert_public_text(value)
    return omit_undefined(finding)


def _parse_receipt_ref(value: object) -> str | None:
    if value is None:
        return None
    return parse_identifier(value)


def _parse_record(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or raw.get("version") != LEDGER_VERSION:
        return None
    kind = raw.get("kind")
    if kind == "observation":
        if set(raw) != {"version", "kind", "recorded_at", "receipt_ref", "observation"}:
            return None
        if not is_offset_datetime(raw.get("recorded_at")):
            return None
        return {
            "version": LEDGER_VERSION,
            "kind": "observation",
            "recorded_at": raw["recorded_at"],
            "receipt_ref": _parse_receipt_ref(raw.get("receipt_ref")),
            "observation": _parse_observation(raw.get("observation")),
        }
    if kind == "readiness":
        if set(raw) != {"version", "kind", "recorded_at", "receipt_ref", "readiness"}:
            return None
        if not is_offset_datetime(raw.get("recorded_at")):
            return None
        return {
            "version": LEDGER_VERSION,
            "kind": "readiness",
            "recorded_at": raw["recorded_at"],
            "receipt_ref": _parse_receipt_ref(raw.get("receipt_ref")),
            "readiness": _parse_readiness(raw.get("readiness")),
        }
    if kind == "finding":
        if set(raw) != {"version", "kind", "recorded_at", "finding", "revision"}:
            return None
        revision = raw.get("revision")
        if not isinstance(revision, str) or not __import__("re").fullmatch(r"^[0-9a-f]{64}$", revision):
            return None
        if not is_offset_datetime(raw.get("recorded_at")):
            return None
        return {
            "version": LEDGER_VERSION,
            "kind": "finding",
            "recorded_at": raw["recorded_at"],
            "finding": _parse_finding(raw.get("finding")),
            "revision": revision,
        }
    return None


class BackupLedger:
    """Serialized owner-only JSONL ledger."""

    def __init__(self, ledger_path: str):
        if not isinstance(ledger_path, str) or not ledger_path:
            _ledger_invalid()
        self._path = Path(ledger_path)
        self._lock = threading.Lock()

    def ready(self) -> None:
        with self._lock:
            self._ensure_state_dir()
            self._load_records()

    def record_observation(self, observation: Mapping[str, Any], receipt_ref: str | None) -> dict[str, Any]:
        with self._lock:
            next_observation = _parse_observation(observation)
            receipt = _parse_receipt_ref(receipt_ref)
            records = self._load_records()
            previous = self._last_observation(records, next_observation["target_alias"])
            if previous is not None and _observation_fingerprint(previous) == _observation_fingerprint(
                next_observation
            ):
                return previous
            recorded = dict(next_observation)
            recorded["changed"] = previous is not None
            records.append(
                {
                    "version": LEDGER_VERSION,
                    "kind": "observation",
                    "recorded_at": recorded["observed_at"],
                    "receipt_ref": receipt,
                    "observation": recorded,
                }
            )
            self._persist(records)
            return recorded

    def last_observation(self, alias: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(alias)
            return self._last_observation(self._load_records(), key)

    def record_readiness(self, readiness: Mapping[str, Any], receipt_ref: str | None) -> dict[str, Any]:
        with self._lock:
            recorded = _parse_readiness(readiness)
            receipt = _parse_receipt_ref(receipt_ref)
            records = self._load_records()
            records.append(
                {
                    "version": LEDGER_VERSION,
                    "kind": "readiness",
                    "recorded_at": recorded["observed_at"],
                    "receipt_ref": receipt,
                    "readiness": recorded,
                }
            )
            self._persist(records)
            return recorded

    def last_readiness(self, alias: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(alias)
            for record in reversed(self._load_records()):
                if record["kind"] == "readiness" and record["readiness"]["target_alias"] == key:
                    return dict(record["readiness"])
            return None

    def record_finding(self, finding: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            recorded = _parse_finding(finding)
            records = self._load_records()
            records.append(
                {
                    "version": LEDGER_VERSION,
                    "kind": "finding",
                    "recorded_at": recorded["observed_at"],
                    "finding": recorded,
                    "revision": backup_finding_revision(recorded),
                }
            )
            self._persist(records)
            return recorded

    def last_finding(self, finding_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_identifier(finding_id)
            record = self._last_finding(self._load_records(), key)
            return None if record is None else dict(record["finding"])

    def latest_finding_revision(self, finding_id: str) -> str | None:
        with self._lock:
            key = parse_identifier(finding_id)
            record = self._last_finding(self._load_records(), key)
            return None if record is None else record["revision"]

    def replace_findings(self, scope: str, findings: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            requested = parse_identifier(scope)
            if not isinstance(findings, list):
                _ledger_invalid()
            incoming = []
            for entry in findings:
                recorded = _parse_finding(entry)
                if not _belongs_to_scope(recorded, requested):
                    _ledger_invalid()
                incoming.append(recorded)
            records = [
                record
                for record in self._load_records()
                if record["kind"] != "finding" or not _belongs_to_scope(record["finding"], requested)
            ]
            for recorded in incoming:
                records.append(
                    {
                        "version": LEDGER_VERSION,
                        "kind": "finding",
                        "recorded_at": recorded["observed_at"],
                        "finding": recorded,
                        "revision": backup_finding_revision(recorded),
                    }
                )
            self._persist(records)

    def _last_observation(self, records: list[dict[str, Any]], alias: str) -> dict[str, Any] | None:
        for record in reversed(records):
            if record["kind"] == "observation" and record["observation"]["target_alias"] == alias:
                return dict(record["observation"])
        return None

    def _last_finding(self, records: list[dict[str, Any]], finding_id: str) -> dict[str, Any] | None:
        for record in reversed(records):
            if record["kind"] == "finding" and record["finding"]["finding_id"] == finding_id:
                return record
        return None

    def _ensure_state_dir(self) -> None:
        directory = self._path.parent
        try:
            info = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(parents=True, mode=0o700)
                os.chmod(directory, 0o700)
            except OSError as exc:
                raise BackupError("unavailable", "Backup ledger write failed") from exc
            info = directory.lstat()
        except OSError:
            _ledger_invalid()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            _ledger_invalid()

    def _load_records(self) -> list[dict[str, Any]]:
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            _ledger_invalid()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            _ledger_invalid()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            _ledger_invalid()
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        records: list[dict[str, Any]] = []
        malformed_tail = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    malformed_tail = True
                    break
                _ledger_invalid()
            try:
                record = _parse_record(parsed)
            except BackupError:
                if index == len(lines) - 1:
                    malformed_tail = True
                    break
                raise
            if record is None:
                if index == len(lines) - 1:
                    malformed_tail = True
                    break
                _ledger_invalid()
            records.append(record)
        if len(records) > MAX_RECORDS:
            _ledger_invalid()
        if malformed_tail:
            self._persist(records)
        return records

    def _persist(self, records: list[dict[str, Any]]) -> None:
        retained = records if len(records) <= MAX_RECORDS else records[-MAX_RECORDS:]
        self._ensure_state_dir()
        body = "" if not retained else "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in retained)
        temp = Path(f"{self._path}.tmp.{os.getpid()}.{threading.get_ident()}")
        handle = None
        try:
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.fchmod(handle, 0o600)
            _write_all(handle, body.encode("utf-8"))
            os.fsync(handle)
            os.close(handle)
            handle = None
            os.replace(temp, self._path)
            os.chmod(self._path, 0o600)
        except BackupError:
            raise
        except OSError as exc:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            try:
                temp.unlink()
            except OSError:
                pass
            raise BackupError("unavailable", "Backup ledger write failed") from exc
