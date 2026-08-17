# src/brigade/research/registry.py
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade import aboyeur, localio, receipt_schema, runguard
from brigade.roster import Roster
from brigade.run_budget import RunBudgetDeclaration

from .types import (
    VALID_ORIGIN,
    VALID_TRUST,
    CitationAudit,
    CitationRecord,
    Finding,
    ReviewResult,
    SourceEnvelope,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

_SIDECAR_LOCK_TLS = threading.local()

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
RESEARCH_SIDECAR_LOCK = "research.json.lock"
DRAFT_REPORT_ARTIFACT = "report.draft.md"
REPORT_MD_ARTIFACT = "report.md"
REPORT_HTML_ARTIFACT = "report.html"
HANDOFF_ARTIFACT = "handoff.md"
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_RUN_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")


def validate_run_id(run_id: str) -> str:
    """Reject anything that is not a single relative path segment."""
    if not isinstance(run_id, str) or not _RUN_ID_SEGMENT_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    if run_id in {".", ".."}:
        raise ValueError(f"invalid run_id: {run_id!r}")
    return run_id


def _validate_run_id(run_id: str) -> str:
    return validate_run_id(run_id)


def standard_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "runs" / _validate_run_id(run_id)


def legacy_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "research" / _validate_run_id(run_id)


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


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_ref(path_name: str, digest: str) -> dict[str, str]:
    return {"path": path_name, "digest": digest}


def _sidecar_lock_path(target: Path, run_id: str) -> Path:
    return standard_run_dir(target, run_id) / RESEARCH_SIDECAR_LOCK


@contextmanager
def research_sidecar_lock(target: Path, run_id: str) -> Iterator[None]:
    """Cross-process lock scoped to research.json updates for one run."""
    # Re-entrancy: request_cancel may terminalize via update_research while
    # already holding the sidecar lock in the same thread.
    key = (str(standard_run_dir(target, run_id).resolve()), RESEARCH_SIDECAR_LOCK)
    depth = getattr(_SIDECAR_LOCK_TLS, "depth", None)
    if depth is None:
        depth = {}
        _SIDECAR_LOCK_TLS.depth = depth
    if key in depth and depth[key] > 0:
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return

    lock_path = _sidecar_lock_path(target, run_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth[key] -= 1
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _is_relative_contained(path_name: str) -> bool:
    if not path_name or path_name.startswith("/") or path_name.startswith("\\"):
        return False
    if ":" in path_name.split("/", 1)[0]:
        # Windows drive / UNC-style absolute paths
        return False
    parts = Path(path_name).parts
    if any(part in {"..", ""} for part in parts):
        return False
    if Path(path_name).is_absolute():
        return False
    return True


def _resolved_artifact_path(run_directory: Path, path_name: str) -> Path | None:
    if not _is_relative_contained(path_name):
        return None
    candidate = (run_directory / path_name).resolve()
    try:
        candidate.relative_to(run_directory.resolve())
    except ValueError:
        return None
    return candidate


def _valid_sha256_digest(digest: str | None) -> bool:
    return isinstance(digest, str) and bool(_SHA256_HEX_RE.fullmatch(digest))


def _bound_source_content(source: Any, max_content_chars: int) -> Any:
    """Return content truncated for pre-identity bounding only.

    Must not be used to mutate a SourceEnvelope after ``SourceEnvelope.build``:
    ``source_id`` / ``content_digest`` are derived from the stored content.
    """
    if max_content_chars < 0:
        raise ValueError("max_content_chars must be non-negative")
    if isinstance(source, str):
        return source[:max_content_chars]
    if isinstance(source, Mapping):
        payload = dict(source)
        content = payload.get("content")
        if isinstance(content, str) and len(content) > max_content_chars:
            payload["content"] = content[:max_content_chars]
        return payload
    return source


def bound_text(content: str, max_content_chars: int) -> str:
    if max_content_chars < 0:
        raise ValueError("max_content_chars must be non-negative")
    if len(content) <= max_content_chars:
        return content
    return content[:max_content_chars]


def _budget_payload_from_caps(caps: Mapping[str, Any]) -> dict[str, Any]:
    max_time = caps.get("max_time")
    max_dispatches = caps.get("max_dispatches", 24)
    return RunBudgetDeclaration(
        wall_clock_seconds=int(max_time) if max_time is not None else None,
        worker_dispatch_count=int(max_dispatches) if max_dispatches is not None else 24,
    ).to_payload()


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
        failure = run_payload.get("failure")
        if "failure_kind" not in record and isinstance(failure, dict) and isinstance(failure.get("kind"), str):
            record["failure_kind"] = failure["kind"]
        if "failure_phase" not in record and isinstance(failure, dict) and isinstance(failure.get("phase"), str):
            record["failure_phase"] = failure["phase"]
        if "question" not in record and isinstance(run_payload.get("task"), str):
            record["question"] = run_payload["task"]
        if "created_at" not in record and isinstance(run_payload.get("started_at"), str):
            record["created_at"] = run_payload["started_at"]
    if research_payload is not None:
        for key in ("failure_kind", "failure_phase", "detail", "status"):
            if key not in record and key in research_payload:
                record[key] = research_payload[key]
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
    budget_payload = _budget_payload_from_caps(caps)
    aboyeur.record_run_start(
        directory,
        task=question,
        cwd=target,
        roster=roster,
        read_only=True,
        kind="research",
        run_budget_payload=budget_payload,
    )
    sidecar = _initial_research_sidecar(run_id=run_id, question=question, profile=profile, caps=caps)
    sidecar["run_budget"] = budget_payload
    localio.write_json(directory / RESEARCH_ARTIFACT, sidecar)
    projected = _project_standard_record(target, run_id)
    if projected is None:
        raise FileNotFoundError(directory / RESEARCH_ARTIFACT)
    return projected


def read_research(target: Path, run_id: str) -> dict[str, Any]:
    path = standard_run_dir(target, run_id) / RESEARCH_ARTIFACT
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(path)
    return payload


def update_research(target: Path, run_id: str, **fields: Any) -> dict[str, Any]:
    path = standard_run_dir(target, run_id) / RESEARCH_ARTIFACT
    with research_sidecar_lock(target, run_id):
        # Initialize only when the sidecar is absent. A present but unreadable or
        # non-object research.json must fail closed rather than overwrite corruption.
        if path.is_file():
            current = _read_json(path)
            if current is None:
                raise ValueError(f"research artifact unreadable or not an object: {path}")
        else:
            # Absent sidecar: seed the canonical identity so category-only
            # (and other partial) writers cannot create a run_id-less artifact.
            current = {"run_id": run_id}
        fields.setdefault("updated_at", _now())
        current.update(fields)
        current.setdefault("run_id", run_id)
        # Explicit None clears cancel stamps (used when reopening a cancelled run).
        if fields.get("cancel_requested_at") is None and "cancel_requested_at" in fields:
            current.pop("cancel_requested_at", None)
            current.pop("cancel_requested_by", None)
        stamped = receipt_schema.stamp_research_sidecar(current)
        localio.write_json(path, stamped)
        return stamped


def write_sources(
    target: Path,
    run_id: str,
    sources: Sequence[Any],
    *,
    max_content_chars: int | None = None,
) -> dict[str, str]:
    """Persist source envelopes without mutating identity-bearing fields.

    ``max_content_chars`` is accepted for call-site compatibility but rejected
    when any envelope would need truncation after identity construction.
    Bound content before ``SourceEnvelope.build`` instead.
    """
    del max_content_chars  # identity must already match stored content
    path = standard_run_dir(target, run_id) / SOURCES_ARTIFACT
    bounded = list(sources)
    for item in bounded:
        if isinstance(item, SourceEnvelope):
            digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
            if digest != item.content_digest:
                raise ValueError("source content_digest does not match stored content")
    payload = receipt_schema.stamp_research_sources({"sources": _serialize_value(bounded)})
    localio.write_json(path, payload)
    return _artifact_ref(SOURCES_ARTIFACT, _file_digest(path))


def update_phase(
    target: Path,
    run_id: str,
    phase: str,
    *,
    status: str,
    artifact: Any | None = None,
    value: Any | None = None,
    extra: Mapping[str, Any] | None = None,
    reset_downstream: bool | Sequence[str] | None = None,
) -> dict[str, Any]:
    """Sidecar-locked read-modify-write for one phase entry.

    When a phase is restarted (``status == "running"``), later phase entries
    are reset to pending so a subsequent resume cannot pick up stale completed
    downstream work. Pass ``reset_downstream=False`` to keep later entries, or
    a sequence of later phase names to reset only those (for example
    ``["publishing"]`` after a completed repair so a second review does not
    erase ``phases.repair``).
    """
    if phase not in RESEARCH_PHASES:
        raise ValueError(f"unknown research phase: {phase}")
    index = RESEARCH_PHASES.index(phase)
    later_phases = RESEARCH_PHASES[index + 1 :]
    if reset_downstream is None:
        to_reset: tuple[str, ...] = later_phases if status == "running" else ()
    elif reset_downstream is False:
        to_reset = ()
    elif reset_downstream is True:
        to_reset = later_phases
    else:
        allowed = set(later_phases)
        to_reset = tuple(name for name in reset_downstream if name in allowed)
    path = standard_run_dir(target, run_id) / RESEARCH_ARTIFACT
    with research_sidecar_lock(target, run_id):
        if path.is_file():
            current = _read_json(path)
            if current is None:
                raise ValueError(f"research artifact unreadable or not an object: {path}")
        else:
            current = {"run_id": run_id}
        current.setdefault("run_id", run_id)
        phases = dict(current.get("phases") or {})
        for name in RESEARCH_PHASES:
            phases.setdefault(name, {"status": "pending"})
        entry: dict[str, Any] = {"status": status}
        if artifact is not None:
            entry["artifact"] = artifact
        if value is not None:
            entry["value"] = value
        if extra:
            entry.update(dict(extra))
        phases[phase] = entry
        for later in to_reset:
            phases[later] = {"status": "pending"}
        current["phases"] = phases
        current["current_phase"] = phase
        current["updated_at"] = _now()
        stamped = receipt_schema.stamp_research_sidecar(current)
        localio.write_json(path, stamped)
        return stamped


def consume_checkpoint(target: Path, run_id: str) -> bool:
    """Remove a legacy checkpoint after it has been migrated into resume state."""
    # Prefer the legacy path so a concurrent standard migration cannot hide it.
    candidates = (
        legacy_run_dir(target, run_id) / "checkpoint.json",
        standard_run_dir(target, run_id) / "checkpoint.json",
    )
    removed = False
    for path in candidates:
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def write_findings(target: Path, run_id: str, findings: Sequence[Any]) -> dict[str, str]:
    path = standard_run_dir(target, run_id) / FINDINGS_ARTIFACT
    payload = receipt_schema.stamp_research_findings({"findings": _serialize_value(list(findings))})
    localio.write_json(path, payload)
    return _artifact_ref(FINDINGS_ARTIFACT, _file_digest(path))


def write_citation_audit(target: Path, run_id: str, audit: Any) -> dict[str, str]:
    path = standard_run_dir(target, run_id) / CITATION_AUDIT_ARTIFACT
    serialized = _serialize_value(audit)
    if not isinstance(serialized, Mapping):
        raise TypeError("citation audit payload must serialize to a mapping")
    payload = receipt_schema.stamp_research_citation_audit(dict(serialized))
    localio.write_json(path, payload)
    return _artifact_ref(CITATION_AUDIT_ARTIFACT, _file_digest(path))


def write_text_artifact(target: Path, run_id: str, name: str, text: str) -> dict[str, str]:
    if not _is_relative_contained(name):
        raise ValueError(f"artifact name escapes run directory: {name}")
    path = standard_run_dir(target, run_id) / name
    localio.write_text_atomic(path, text)
    return _artifact_ref(name, _file_digest(path))


def request_cancel(target: Path, run_id: str, *, requested_by: str = "operator") -> dict[str, Any]:
    run_directory = standard_run_dir(target, run_id)
    if not run_directory.is_dir():
        raise FileNotFoundError(run_directory)
    now = _now()
    with research_sidecar_lock(target, run_id):
        path = run_directory / RESEARCH_ARTIFACT
        current = _read_json(path)
        if current is None:
            raise ValueError(f"research artifact unreadable or not an object: {path}")
        run_payload = _read_json(run_directory / "run.json") or {}
        terminal_statuses = {"completed", "failed", "cancelled"}
        research_status = str(current.get("status") or "")
        run_status = str(run_payload.get("status") or "")
        if research_status in terminal_statuses or run_status in terminal_statuses:
            # Refuse to split receipts: terminal runs are a truthful no-op.
            return current
        if current.get("cancel_requested_at"):
            # Idempotent: keep the original stamp and requester.
            sidecar = current
        else:
            current["cancel_requested_at"] = now
            current["cancel_requested_by"] = requested_by
            current["updated_at"] = now
            sidecar = receipt_schema.stamp_research_sidecar(current)
            localio.write_json(path, sidecar)
        if not runguard.has_active_run_owner(target, run_directory):
            if str(sidecar.get("status") or "") in terminal_statuses:
                return sidecar
            aboyeur.record_run_termination(
                run_directory,
                status="cancelled",
                failure_phase=str(sidecar.get("current_phase") or "run"),
                failure_kind="operator-cancelled",
                detail="cancel requested with no live owner",
            )
            return update_research(target, run_id, status="cancelled")
        return sidecar


def _artifact_path_and_digest(artifact: Any) -> tuple[str | None, str | None]:
    if artifact is None:
        return None, None
    if isinstance(artifact, str):
        return artifact, None
    if isinstance(artifact, Mapping):
        path = artifact.get("path")
        digest = artifact.get("digest")
        return (
            path if isinstance(path, str) else None,
            digest if isinstance(digest, str) else None,
        )
    return None, None


def _source_from_dict(payload: Mapping[str, Any]) -> SourceEnvelope:
    required = ("source_id", "origin", "provider", "uri", "content", "content_digest", "acquired_at", "trust")
    for key in required:
        if key not in payload:
            raise ValueError(f"source envelope missing {key}")
    parent_ids = payload.get("parent_source_ids") or ()
    if not isinstance(parent_ids, (list, tuple)):
        raise ValueError("source envelope parent_source_ids must be a list")
    origin = payload["origin"]
    trust = payload["trust"]
    if not isinstance(origin, str) or origin not in VALID_ORIGIN:
        raise ValueError(f"source envelope invalid origin: {origin!r}")
    if not isinstance(trust, str) or trust not in VALID_TRUST:
        raise ValueError(f"source envelope invalid trust: {trust!r}")
    return SourceEnvelope(
        source_id=str(payload["source_id"]),
        origin=origin,  # type: ignore[arg-type]
        provider=str(payload["provider"]),
        uri=str(payload["uri"]),
        content=str(payload["content"]),
        content_digest=str(payload["content_digest"]),
        acquired_at=str(payload["acquired_at"]),
        trust=trust,  # type: ignore[arg-type]
        producing_lane=payload.get("producing_lane") if isinstance(payload.get("producing_lane"), str) else None,
        requested_model=payload.get("requested_model") if isinstance(payload.get("requested_model"), str) else None,
        observed_model=payload.get("observed_model") if isinstance(payload.get("observed_model"), str) else None,
        parent_source_ids=tuple(str(item) for item in parent_ids),
    )


def _finding_from_dict(payload: Mapping[str, Any]) -> Finding:
    required = ("source_ids", "title", "summary", "evidence", "trust", "extraction_lane", "extracted_at")
    for key in required:
        if key not in payload:
            raise ValueError(f"finding missing {key}")
    source_ids = payload.get("source_ids") or ()
    parent_ids = payload.get("parent_source_ids") or ()
    if not isinstance(source_ids, (list, tuple)) or not isinstance(parent_ids, (list, tuple)):
        raise ValueError("finding id lists must be sequences")
    trust = payload.get("trust") or "web"
    if not isinstance(trust, str) or trust not in VALID_TRUST:
        raise ValueError(f"finding invalid trust: {trust!r}")
    return Finding(
        source_ids=tuple(str(item) for item in source_ids),
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        evidence=str(payload.get("evidence") or ""),
        trust=trust,  # type: ignore[arg-type]
        extraction_lane=str(payload.get("extraction_lane") or ""),
        extracted_at=str(payload.get("extracted_at") or ""),
        parent_source_ids=tuple(str(item) for item in parent_ids),
    )


def _citation_audit_from_dict(payload: Mapping[str, Any]) -> CitationAudit:
    if "accepted" not in payload:
        raise ValueError("citation audit missing accepted")
    citations_raw = payload.get("citations") or ()
    unresolved_raw = payload.get("unresolved") or ()
    if not isinstance(citations_raw, (list, tuple)) or not isinstance(unresolved_raw, (list, tuple)):
        raise ValueError("citation audit citations/unresolved must be sequences")
    citations = []
    for item in citations_raw:
        if not isinstance(item, Mapping):
            raise ValueError("citation audit citations must be objects")
        if "token" not in item or "source_ids" not in item or "status" not in item:
            raise ValueError("citation record missing required fields")
        source_ids = item.get("source_ids") or ()
        if not isinstance(source_ids, (list, tuple)):
            raise ValueError("citation record source_ids must be a sequence")
        citations.append(
            CitationRecord(
                token=str(item.get("token") or ""),
                source_ids=tuple(str(sid) for sid in source_ids),
                status=item.get("status") or "unresolved",  # type: ignore[arg-type]
            )
        )
    return CitationAudit(
        accepted=bool(payload.get("accepted")),
        citations=tuple(citations),
        unresolved=tuple(str(item) for item in unresolved_raw),
    )


def _review_from_dict(payload: Mapping[str, Any]) -> ReviewResult:
    required = ("accepted", "detail", "rejected_claims", "seat", "attempt_id")
    for key in required:
        if key not in payload:
            raise ValueError(f"review result missing {key}")
    rejected = payload.get("rejected_claims") or ()
    if not isinstance(rejected, (list, tuple)):
        raise ValueError("review rejected_claims must be a sequence")
    return ReviewResult(
        accepted=bool(payload["accepted"]),
        detail=str(payload.get("detail") or ""),
        rejected_claims=tuple(str(item) for item in rejected),
        seat=str(payload["seat"]),
        attempt_id=str(payload["attempt_id"]),
    )


def artifact_verification_state(target: Path, run_id: str, artifact: Any) -> str:
    """Classify an artifact reference for read-only projections.

    Returns ``"verified"`` when the recorded digest matches the file on disk,
    ``"digest-mismatch"`` when it does not, ``"missing"`` when the referenced
    file is absent, and ``"unrecorded"`` when the reference carries no usable
    path-plus-digest pair.
    """
    path_name, expected_digest = _artifact_path_and_digest(artifact)
    if path_name is None or not _valid_sha256_digest(expected_digest):
        return "unrecorded"
    run_directory = standard_run_dir(target, run_id)
    path = _resolved_artifact_path(run_directory, path_name)
    if path is None or not path.is_file():
        return "missing"
    return "verified" if _file_digest(path) == expected_digest else "digest-mismatch"


def read_verified_artifact(target: Path, run_id: str, artifact: Any) -> Any | None:
    path_name, expected_digest = _artifact_path_and_digest(artifact)
    if path_name is None:
        return None
    if not _valid_sha256_digest(expected_digest):
        return None
    run_directory = standard_run_dir(target, run_id)
    path = _resolved_artifact_path(run_directory, path_name)
    if path is None or not path.is_file():
        return None
    actual_digest = _file_digest(path)
    if actual_digest != expected_digest:
        return None
    if path_name.endswith(".json"):
        payload = _read_json(path)
        if payload is None:
            return None
        try:
            if path_name == SOURCES_ARTIFACT:
                sources = payload.get("sources")
                if not isinstance(sources, list):
                    return None
                return tuple(_source_from_dict(item) for item in sources)
            if path_name == FINDINGS_ARTIFACT:
                findings = payload.get("findings")
                if not isinstance(findings, list):
                    return None
                return tuple(_finding_from_dict(item) for item in findings)
            if path_name == CITATION_AUDIT_ARTIFACT:
                return _citation_audit_from_dict(payload)
            return payload
        except (KeyError, TypeError, ValueError):
            return None
    return path.read_text(encoding="utf-8")


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
            try:
                validate_run_id(child.name)
            except ValueError:
                continue
            record = _project_legacy_record(target, child.name)
            if record is not None:
                by_id[child.name] = record
    standard_root = _standard_root(target)
    if standard_root.is_dir():
        for child in standard_root.iterdir():
            if not child.is_dir():
                continue
            try:
                validate_run_id(child.name)
            except ValueError:
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
