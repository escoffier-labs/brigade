"""Relay Grok Bot findings through a durable opaque Fleet Hub outbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import fleet_client, grokbot_findings, grokbot_jobs
from .grokbot_findings import DEFAULT_LIMIT, FindingsError

RELAY_HARNESS = "grokbot"
RELAY_SEAT = "findings-relay"
OUTBOX_DIRNAME = "outbox"
OUTBOX_SCHEMA = "brigade.grokbot.findings-outbox.v1"
OUTBOX_KEYS = frozenset({"schema", "relay_id", "binding", "status", "event"})
EVENT_KEYS = frozenset({"run_id", "seat", "harness", "state", "ts", "sequence", "digest"})
OUTBOX_STATUSES = frozenset({"pending", "ready", "reported"})
RELAY_STATES = frozenset(f"finding.{name}" for name in grokbot_findings.SEVERITIES)
OUTBOX_MAX_BYTES = 4096
OUTBOX_MAX_RECORDS = 10_000
OUTBOX_TEMP_RE = re.compile(r"^\.[0-9a-f]{64}\.json\.[0-9a-f]{24}\.tmp$")


def relay_preview(
    records: object,
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Validate records and return counts plus irreversible relay IDs."""
    owner_path, bound, entries = grokbot_findings._preflight_entries(owner, records, limit)
    eligible, known = grokbot_findings._classify(target, owner_path, entries)
    selected = eligible[:bound]
    return {
        "eligible": len(eligible),
        "known": len(known),
        "created": 0,
        "limit": bound,
        "relays": [_relay_id(entry) for entry in selected],
    }


def relay_apply(
    records: object,
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Spool opaque outbox records, deliver review drafts, then report events."""
    owner_path, bound, entries = grokbot_findings._preflight_entries(owner, records, limit)
    delivered = _spool_and_deliver(target, owner_path, entries, bound, now)
    reported, pending, relays = _reconcile_outbox(target)
    return {
        "eligible": delivered["eligible"],
        "known": delivered["known"],
        "created": delivered["created"],
        "skipped": delivered["skipped"],
        "limit": delivered["limit"],
        "pending": pending,
        "reported": reported,
        "relays": relays,
    }


def _relay_id(entry: dict[str, str]) -> str:
    return grokbot_findings.identity_digest(entry["producer"], entry["finding_id"], entry["revision"])


def _entry_binding(entry: dict[str, str]) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _spool_and_deliver(
    target: Path,
    owner: Path,
    entries: list[dict[str, str]],
    bound: int,
    now: datetime | None,
) -> dict[str, Any]:
    """Select, spool, and deliver one bounded batch under the queue lock."""
    if not grokbot_findings.SECURE_OWNER_WRITE_AVAILABLE:  # pragma: no cover - exercised on Windows.
        raise FindingsError("secure-owner-write-unavailable")
    created = 0
    recovered = 0
    eligible: list[dict[str, str]] = []
    known: list[dict[str, str]] = []
    try:
        with grokbot_jobs._storage_paths(target) as storage, grokbot_jobs._queue_lock(storage):
            findings, findings_fd = grokbot_findings._open_findings(storage)
            outbox, outbox_fd = grokbot_findings._open_queue_child(storage, OUTBOX_DIRNAME)
            try:
                eligible, known, recoverable = grokbot_findings._classify_locked(findings, owner, entries)
                for entry in known:
                    existing = _load_outbox_file(outbox, _relay_id(entry))
                    if existing is None:
                        continue
                    if existing["binding"] != _entry_binding(entry):
                        raise FindingsError("outbox-conflict")
                    _mark_outbox_status(outbox, _relay_id(entry), "ready")
                deliverable = {(entry["producer"], entry["finding_id"]) for entry in [*eligible, *recoverable]}
                for entry in entries:
                    identity = (entry["producer"], entry["finding_id"])
                    if identity not in deliverable:
                        continue
                    if created + recovered >= bound:
                        break
                    _ensure_pending_record(outbox, entry, now)
                    _handle, status = grokbot_findings._deliver_one(findings, owner, entry, now)
                    _mark_outbox_status(outbox, _relay_id(entry), "ready")
                    if status == "created":
                        created += 1
                    else:
                        recovered += 1
            finally:
                if outbox_fd is not None:
                    os.close(outbox_fd)
                if findings_fd is not None:
                    os.close(findings_fd)
    except grokbot_jobs.GrokbotJobError as exc:
        raise FindingsError(exc.reason) from exc
    return {
        "eligible": len(eligible),
        "known": len(known),
        "created": created,
        "skipped": len(known) + recovered,
        "limit": bound,
    }


def _ensure_pending_record(
    directory: grokbot_jobs._Directory,
    entry: dict[str, str],
    now: datetime | None,
) -> None:
    existing = _load_outbox_file(directory, _relay_id(entry))
    if existing is not None:
        if existing["binding"] != _entry_binding(entry):
            raise FindingsError("outbox-conflict")
        return
    relay_id = _relay_id(entry)
    grokbot_jobs._write_json_file(
        directory,
        f"{relay_id}.json",
        {
            "schema": OUTBOX_SCHEMA,
            "relay_id": relay_id,
            "binding": _entry_binding(entry),
            "status": "pending",
            "event": _build_event(entry, now),
        },
    )


def _build_event(entry: dict[str, str], now: datetime | None) -> dict[str, Any]:
    severity = entry["severity"]
    state = f"finding.{severity}"
    if state not in RELAY_STATES:
        raise FindingsError("invalid-severity")
    relay_id = _relay_id(entry)
    canonical = {
        "harness": RELAY_HARNESS,
        "run_id": relay_id,
        "seat": RELAY_SEAT,
        "sequence": 1,
        "state": state,
        "ts": grokbot_jobs._now_iso(now),
    }
    digest = _event_digest(canonical)
    return {**canonical, "digest": digest}


def _event_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reconcile_outbox(target: Path) -> tuple[int, int, list[str]]:
    reported = 0
    pending = 0
    relays: list[str] = []
    for record in _read_outbox_records(target):
        if record["status"] == "reported":
            continue
        relays.append(record["relay_id"])
        if record["status"] == "pending":
            pending += 1
            continue
        delivered = fleet_client.report_event(dict(record["event"]))
        if delivered:
            _mark_outbox_record(target, record, "reported")
            reported += 1
        else:
            pending += 1
    return reported, pending, relays


def _read_outbox_records(target: Path) -> list[dict[str, Any]]:
    directory = grokbot_findings._open_queue_child_readonly(target, OUTBOX_DIRNAME)
    try:
        if directory is None:
            return []
        location: int | Path = directory.descriptor if directory.descriptor is not None else directory.path
        try:
            names = sorted(os.listdir(location))
        except OSError as exc:
            raise FindingsError("unsafe-storage") from exc
        if len(names) > OUTBOX_MAX_RECORDS:
            raise FindingsError("outbox-capacity")
        records: list[dict[str, Any]] = []
        for name in names:
            if OUTBOX_TEMP_RE.fullmatch(name):
                continue
            if not name.endswith(".json") or not grokbot_jobs.LOWER_HEX_64_RE.fullmatch(name[:-5]):
                raise FindingsError("corrupt-storage")
            record = _load_outbox_file(directory, name[:-5])
            if record is None:  # pragma: no cover - list/open race.
                continue
            records.append(record)
        return records
    finally:
        if directory is not None and directory.descriptor is not None:
            os.close(directory.descriptor)


def _load_outbox_file(directory: grokbot_jobs._Directory, relay_id: str) -> dict[str, Any] | None:
    try:
        data = grokbot_jobs._read_bytes_file(
            directory,
            f"{relay_id}.json",
            maximum=OUTBOX_MAX_BYTES,
            missing_reason="outbox-missing",
        )
    except grokbot_jobs.GrokbotJobError as exc:
        if exc.reason == "outbox-missing":
            return None
        raise FindingsError(exc.reason) from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FindingsError("corrupt-storage") from exc
    return _validate_outbox(payload, relay_id)


def _validate_outbox(payload: object, relay_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != OUTBOX_KEYS or payload.get("schema") != OUTBOX_SCHEMA:
        raise FindingsError("corrupt-storage")
    if payload.get("relay_id") != relay_id or payload.get("status") not in OUTBOX_STATUSES:
        raise FindingsError("corrupt-storage")
    binding = payload.get("binding")
    if not isinstance(binding, str) or not grokbot_jobs.LOWER_HEX_64_RE.fullmatch(binding):
        raise FindingsError("corrupt-storage")
    event = payload.get("event")
    if not isinstance(event, dict) or set(event) != EVENT_KEYS:
        raise FindingsError("corrupt-storage")
    if event.get("run_id") != relay_id or event.get("seat") != RELAY_SEAT or event.get("harness") != RELAY_HARNESS:
        raise FindingsError("corrupt-storage")
    if event.get("state") not in RELAY_STATES or event.get("sequence") != 1:
        raise FindingsError("corrupt-storage")
    if not isinstance(event.get("ts"), str) or not isinstance(event.get("digest"), str):
        raise FindingsError("corrupt-storage")
    if not grokbot_jobs.LOWER_HEX_64_RE.fullmatch(event["digest"]):
        raise FindingsError("corrupt-storage")
    grokbot_findings._validate_aware_timestamp(event["ts"], reason="corrupt-storage")
    canonical_event = {key: event[key] for key in EVENT_KEYS - {"digest"}}
    if event["digest"] != _event_digest(canonical_event):
        raise FindingsError("corrupt-storage")
    return {
        "schema": OUTBOX_SCHEMA,
        "relay_id": relay_id,
        "binding": binding,
        "status": payload["status"],
        "event": {key: event[key] for key in sorted(EVENT_KEYS)},
    }


def _mark_outbox_status(
    directory: grokbot_jobs._Directory,
    relay_id: str,
    status: str,
) -> None:
    if status not in OUTBOX_STATUSES:
        raise FindingsError("invalid-outbox-status")
    existing = _load_outbox_file(directory, relay_id)
    if existing is None:
        raise FindingsError("corrupt-storage")
    if existing["status"] == "reported" or existing["status"] == status:
        return
    if existing["status"] == "ready" and status == "pending":
        raise FindingsError("invalid-outbox-transition")
    grokbot_jobs._write_json_file(
        directory,
        f"{relay_id}.json",
        {**existing, "status": status},
    )


def _mark_outbox_record(target: Path, record: dict[str, Any], status: str) -> None:
    try:
        with grokbot_jobs._storage_paths(target) as storage, grokbot_jobs._queue_lock(storage):
            directory, extra_fd = grokbot_findings._open_queue_child(storage, OUTBOX_DIRNAME)
            try:
                existing = _load_outbox_file(directory, record["relay_id"])
                if existing is None or existing["status"] == status:
                    return
                if existing["event"] != record["event"] or existing["binding"] != record["binding"]:
                    raise FindingsError("corrupt-storage")
                _mark_outbox_status(directory, record["relay_id"], status)
            finally:
                if extra_fd is not None:
                    os.close(extra_fd)
    except grokbot_jobs.GrokbotJobError as exc:
        raise FindingsError(exc.reason) from exc
