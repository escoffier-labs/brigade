# src/brigade/research/registry.py
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade import aboyeur, localio, receipt_schema
from brigade.roster import Roster

RESEARCH_PHASES: tuple[str, ...] = (
    "planning",
    "discovery",
    "extraction",
    "synthesis",
    "review",
    "repair",
    "publishing",
)

SOURCES_ARTIFACT = "sources.json"
FINDINGS_ARTIFACT = "findings.json"
CITATION_AUDIT_ARTIFACT = "citation-audit.json"
RESEARCH_ARTIFACT = "research.json"


def standard_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "runs" / run_id


def legacy_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "research" / run_id


def run_dir(target: Path, run_id: str) -> Path:
    standard = standard_run_dir(target, run_id)
    return standard if standard.is_dir() else legacy_run_dir(target, run_id)


def _legacy_root(target: Path) -> Path:
    return target / ".brigade" / "research"


def _standard_root(target: Path) -> Path:
    return target / ".brigade" / "runs"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "run"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    return localio.read_json_dict(path)


def _write_legacy_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize_value(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _serialize_value(value.to_dict())
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _serialize_value(value.as_dict())
    return value


def _initial_research_sidecar(
    *,
    run_id: str,
    question: str,
    profile: str,
    caps: Mapping[str, Any],
) -> dict[str, Any]:
    now = _now()
    return receipt_schema.stamp_research_sidecar(
        {
            "run_id": run_id,
            "question": question,
            "profile": profile,
            "caps": dict(caps),
            "created_at": now,
            "updated_at": now,
            "current_phase": "planning",
            "phases": {name: {"status": "pending"} for name in RESEARCH_PHASES},
            "artifacts": {},
            "stats": {},
            "blockers": [],
        }
    )


def _project_standard_record(target: Path, run_id: str) -> dict[str, Any] | None:
    directory = standard_run_dir(target, run_id)
    run_payload = _read_json(directory / "run.json")
    research_payload = _read_json(directory / RESEARCH_ARTIFACT)
    if run_payload is None and research_payload is None:
        return None
    record: dict[str, Any] = {}
    if research_payload is not None:
        record.update(research_payload)
    if run_payload is not None:
        for key in (
            "status",
            "kind",
            "started_at",
            "finished_at",
            "failure_kind",
            "failure_phase",
            "error",
            "run_budget",
        ):
            if key in run_payload:
                record[key] = run_payload[key]
        if "question" not in record and isinstance(run_payload.get("task"), str):
            record["question"] = run_payload["task"]
        if "created_at" not in record and isinstance(run_payload.get("started_at"), str):
            record["created_at"] = run_payload["started_at"]
    record["run_id"] = run_id
    record["legacy"] = False
    return record


def _project_legacy_record(target: Path, run_id: str) -> dict[str, Any] | None:
    payload = _read_json(legacy_run_dir(target, run_id) / "run.json")
    if payload is None:
        return None
    record = dict(payload)
    record.setdefault("run_id", run_id)
    record["legacy"] = True
    return record


def create_standard_run(
    target: Path,
    *,
    run_id: str,
    question: str,
    profile: str,
    caps: Mapping[str, Any],
    roster: Roster,
) -> dict[str, Any]:
    directory = standard_run_dir(target, run_id)
    aboyeur.record_run_start(
        directory,
        task=question,
        cwd=target,
        roster=roster,
        read_only=True,
        kind="research",
        run_budget_payload=caps,
    )
    sidecar = _initial_research_sidecar(run_id=run_id, question=question, profile=profile, caps=caps)
    localio.write_json(directory / RESEARCH_ARTIFACT, sidecar)
    projected = _project_standard_record(target, run_id)
    if projected is None:
        raise FileNotFoundError(directory / RESEARCH_ARTIFACT)
    return projected


def update_research(target: Path, run_id: str, **fields: Any) -> dict[str, Any]:
    path = standard_run_dir(target, run_id) / RESEARCH_ARTIFACT
    current = _read_json(path) or {}
    fields.setdefault("updated_at", _now())
    current.update(fields)
    stamped = receipt_schema.stamp_research_sidecar(current)
    localio.write_json(path, stamped)
    return stamped


def write_sources(target: Path, run_id: str, sources: Sequence[Any]) -> str:
    path = standard_run_dir(target, run_id) / SOURCES_ARTIFACT
    payload = receipt_schema.stamp_research_sources({"sources": _serialize_value(list(sources))})
    localio.write_json(path, payload)
    return SOURCES_ARTIFACT


def write_findings(target: Path, run_id: str, findings: Sequence[Any]) -> str:
    path = standard_run_dir(target, run_id) / FINDINGS_ARTIFACT
    payload = receipt_schema.stamp_research_findings({"findings": _serialize_value(list(findings))})
    localio.write_json(path, payload)
    return FINDINGS_ARTIFACT


def write_citation_audit(target: Path, run_id: str, audit: Any) -> str:
    path = standard_run_dir(target, run_id) / CITATION_AUDIT_ARTIFACT
    serialized = _serialize_value(audit)
    if not isinstance(serialized, Mapping):
        raise TypeError("citation audit payload must serialize to a mapping")
    payload = receipt_schema.stamp_research_citation_audit(dict(serialized))
    localio.write_json(path, payload)
    return CITATION_AUDIT_ARTIFACT


def create_run(
    target: Path, *, question: str, run_id: str, caps: dict[str, Any], manifest: dict[str, Any] | None = None
) -> str:
    d = legacy_run_dir(target, run_id)
    d.mkdir(parents=True, exist_ok=True)
    existing = _read_json(d / "run.json") or {}
    now = _now()
    _write_legacy_json(
        d / "run.json",
        {
            "run_id": run_id,
            "question": question,
            "status": "running",
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "caps": caps,
            "manifest": manifest or {},
            "stats": {},
            "artifacts": {},
            "blockers": [],
        },
    )
    return run_id


def show_run(target: Path, run_id: str) -> dict[str, Any] | None:
    standard = standard_run_dir(target, run_id)
    if standard.is_dir():
        return _project_standard_record(target, run_id)
    return _project_legacy_record(target, run_id)


def list_runs(target: Path) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    legacy_root = _legacy_root(target)
    if legacy_root.is_dir():
        for child in legacy_root.iterdir():
            if not child.is_dir():
                continue
            record = _project_legacy_record(target, child.name)
            if record is not None:
                by_id[child.name] = record
    standard_root = _standard_root(target)
    if standard_root.is_dir():
        for child in standard_root.iterdir():
            if not child.is_dir():
                continue
            research_path = child / RESEARCH_ARTIFACT
            run_path = child / "run.json"
            if not research_path.is_file() and not run_path.is_file():
                continue
            # Standard research runs carry research.json; plain work runs do not
            # participate in the research registry listing.
            if not research_path.is_file():
                run_payload = _read_json(run_path)
                if not isinstance(run_payload, dict) or run_payload.get("kind") != "research":
                    continue
            record = _project_standard_record(target, child.name)
            if record is not None:
                by_id[child.name] = record
    out = list(by_id.values())
    out.sort(
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("run_id") or "")),
        reverse=True,
    )
    return out


def _update_legacy(target: Path, run_id: str, **fields: Any) -> None:
    path = legacy_run_dir(target, run_id) / "run.json"
    rec = _read_json(path) or {}
    fields.setdefault("updated_at", _now())
    rec.update(fields)
    _write_legacy_json(path, rec)


def update_run(target: Path, run_id: str, **fields: Any) -> None:
    standard = standard_run_dir(target, run_id)
    if standard.is_dir() and (standard / RESEARCH_ARTIFACT).is_file():
        update_research(target, run_id, **fields)
        return
    _update_legacy(target, run_id, **fields)


def set_status(target: Path, run_id: str, status: str) -> None:
    standard = standard_run_dir(target, run_id)
    if standard.is_dir() and (standard / "run.json").is_file():
        aboyeur.update_run_receipt(standard, status=status)
        if (standard / RESEARCH_ARTIFACT).is_file():
            update_research(target, run_id, status=status)
        return
    _update_legacy(target, run_id, status=status)


def finish_run(
    target: Path,
    run_id: str,
    *,
    status: str,
    stats: dict[str, Any],
    artifacts: dict[str, Any],
    blockers: list[str] | None = None,
) -> None:
    payload = {
        "status": status,
        "stats": stats,
        "artifacts": artifacts,
        "blockers": blockers or [],
        "completed_at": _now(),
    }
    standard = standard_run_dir(target, run_id)
    if standard.is_dir() and (standard / RESEARCH_ARTIFACT).is_file():
        update_research(target, run_id, **payload)
        aboyeur.update_run_receipt(standard, status=status)
        return
    _update_legacy(target, run_id, **payload)


def save_checkpoint(target: Path, run_id: str, cp: dict[str, Any]) -> None:
    path = run_dir(target, run_id) / "checkpoint.json"
    if standard_run_dir(target, run_id).is_dir():
        localio.write_json(path, cp)
        return
    _write_legacy_json(path, cp)


def load_checkpoint(target: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(run_dir(target, run_id) / "checkpoint.json")


def append_event(target: Path, run_id: str, event: dict[str, Any]) -> None:
    path = run_dir(target, run_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
