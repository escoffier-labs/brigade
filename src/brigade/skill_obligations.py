"""Advisory audit of declared skill process obligations vs captured receipts (#499).

Compares skill.json ``obligations`` for skills bound on a Brigade run plan
(``selected_skill_ids``) against verify, review, and handoff ingest receipts.

Hard safety boundary:
- Read-only: never writes run dirs, receipts, or skill registries.
- Advisory: missing required evidence is a finding with exit 0.
- Exit 2 only when the run cannot be resolved or inputs are unusable.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import localio

AUDIT_SCHEMA = "brigade.skill_obligations_audit.v1"
AUDIT_SCHEMA_VERSION = 1

OBLIGATION_KINDS = frozenset({"check", "review", "handoff"})

KIND_CHECK = "check"
KIND_REVIEW = "review"
KIND_HANDOFF = "handoff"

RESULT_OK = "ok"
RESULT_WARN = "warn"
RESULT_NO_DECLARED_SKILLS = "no_declared_skills"
RESULT_NO_OBLIGATIONS = "no_obligations"

# Public audit output never prints host-private absolute paths. Paths under the
# audited target are repo-relative; everything else collapses to a collision-
# resistant external:<name>-<digest> label that cannot reveal the absolute path.
PUBLIC_TARGET = "."


def _external_public_label(stable_key: str, *, name_hint: str | None = None) -> str:
    """Deterministic collision-resistant public label for a path outside root."""
    name = Path(name_hint or stable_key).name or "path"
    digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:12]
    return f"external:{name}-{digest}"


def _public_path(root: Path, value: object) -> str | None:
    """Stable public path label: repo-relative under root, else external:<name>-<digest>."""
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        return None
    text = str(value).strip()
    if not text:
        return None
    root_resolved = root.expanduser().resolve()
    path = Path(text).expanduser()
    try:
        resolved = path.resolve() if path.is_absolute() else (root_resolved / path).resolve()
    except OSError:
        return _external_public_label(text, name_hint=Path(text).name or "path")
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError:
        return _external_public_label(resolved.as_posix(), name_hint=resolved.name or "path")
    rendered = relative.as_posix()
    return rendered if rendered else PUBLIC_TARGET


def _looks_like_filesystem_ref(value: str) -> bool:
    if not value:
        return False
    if value.startswith((".", "~", "/", "\\")):
        return True
    if "/" in value or "\\" in value:
        return True
    try:
        return Path(value).expanduser().is_absolute()
    except OSError:
        return False


def _public_skill_id(root: Path, skill_id: str) -> str:
    if not _looks_like_filesystem_ref(skill_id):
        return skill_id
    return _public_path(root, skill_id) or skill_id


def _public_source(root: Path, source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return source
    public = dict(source)
    identity = public.get("identity")
    if public.get("kind") == "path" and isinstance(identity, str):
        raw = identity.removeprefix("path:") if identity.startswith("path:") else identity
        public["identity"] = f"path:{_public_path(root, raw) or 'external:path'}"
    return public


@dataclass(frozen=True)
class SkillObligation:
    """One process obligation declared on a skill pack."""

    id: str
    kind: str
    required: bool = True
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "required": self.required,
        }
        if self.description:
            payload["description"] = self.description
        return payload


def parse_obligations(metadata: dict[str, Any] | None) -> tuple[list[SkillObligation], list[str]]:
    """Parse additive ``obligations`` from skill.json metadata.

    Missing or empty ``obligations`` is valid (no process contract). Malformed
    entries produce errors and are skipped so lint/audit can surface them.
    """
    if not isinstance(metadata, dict) or "obligations" not in metadata:
        return [], []
    raw = metadata.get("obligations")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["metadata obligations must be a list"]
    obligations: list[SkillObligation] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"obligations[{index}] must be an object")
            continue
        obligation_id = item.get("id")
        kind = item.get("kind")
        if not isinstance(obligation_id, str) or not obligation_id.strip():
            errors.append(f"obligations[{index}].id is required")
            continue
        obligation_id = obligation_id.strip()
        if obligation_id in seen_ids:
            errors.append(f"obligations[{index}].id is duplicated: {obligation_id}")
            continue
        if not isinstance(kind, str) or kind not in OBLIGATION_KINDS:
            errors.append(f"obligations[{index}].kind must be one of {sorted(OBLIGATION_KINDS)}")
            continue
        required = item.get("required", True)
        if not isinstance(required, bool):
            errors.append(f"obligations[{index}].required must be a boolean when set")
            continue
        description = item.get("description")
        if description is not None and (not isinstance(description, str) or not description.strip()):
            errors.append(f"obligations[{index}].description must be a non-empty string when set")
            continue
        seen_ids.add(obligation_id)
        obligations.append(
            SkillObligation(
                id=obligation_id,
                kind=kind,
                required=required,
                description=description.strip() if isinstance(description, str) else None,
            )
        )
    return obligations, errors


def declared_skill_ids_from_plan(plan: dict[str, Any] | None) -> list[str]:
    """Ordered unique skill ids from ``plan.json`` assignment bindings."""
    if not isinstance(plan, dict):
        return []
    assignments = plan.get("assignments")
    if not isinstance(assignments, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        raw = assignment.get("selected_skill_ids")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                continue
            skill_id = item.strip()
            if skill_id in seen:
                continue
            seen.add(skill_id)
            ordered.append(skill_id)
    return ordered


def _receipt_timestamp(receipt: dict[str, Any]) -> str:
    for key in ("completed_at", "started_at", "run_id"):
        value = receipt.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _verify_commands(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return []
    return [command for command in commands if isinstance(command, dict)]


def _command_succeeded(command: dict[str, Any]) -> bool:
    status = command.get("status")
    exit_code = command.get("exit_code")
    if status == "completed" and exit_code in (0, None):
        return True
    if status is None and exit_code == 0:
        return True
    return False


def _verify_receipt_succeeded(receipt: dict[str, Any]) -> bool:
    return receipt.get("status") == "completed"


def _review_receipt_succeeded(receipt: dict[str, Any]) -> bool:
    return receipt.get("status") == "completed" and receipt.get("exit_code") == 0


def _handoff_receipt_succeeded(receipt: dict[str, Any]) -> bool:
    processed = receipt.get("processed_handoff_paths")
    if isinstance(processed, list) and any(isinstance(path, str) and path for path in processed):
        return True
    return receipt.get("status") == "ingested"


def _evidence_ref(
    *,
    kind: str,
    receipt: dict[str, Any],
    target: Path,
    obligation_id: str | None = None,
    check_id: str | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "kind": kind,
        "run_id": receipt.get("run_id"),
        "path": _public_path(target, receipt.get("path")),
        "status": receipt.get("status"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
    }
    producer_run_id = receipt.get("producer_run_id")
    if isinstance(producer_run_id, str) and producer_run_id:
        ref["producer_run_id"] = producer_run_id
    if obligation_id is not None:
        ref["obligation_id"] = obligation_id
    if check_id is not None:
        ref["check_id"] = check_id
    return ref


def _receipt_producer_run_id(receipt: dict[str, Any]) -> str | None:
    value = receipt.get("producer_run_id")
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return None


def _receipt_attribution(
    receipt: dict[str, Any],
    *,
    audited_run_id: str,
) -> str:
    """Classify a receipt relative to the audited orchestrator run.

    Exact ``producer_run_id`` equality attributes the receipt. Missing
    ``producer_run_id`` is legacy/unattributed and never satisfies. A different
    stamped id is foreign (wrong-run); timestamp proximity must not attribute.
    """
    producer_run_id = _receipt_producer_run_id(receipt)
    if producer_run_id is None:
        return "unattributed"
    if producer_run_id == audited_run_id:
        return "attributed"
    return "foreign"


def _unattributed_ref(*, kind: str, receipt: dict[str, Any], target: Path) -> dict[str, Any]:
    ref = _evidence_ref(kind=kind, receipt=receipt, target=target)
    ref["attribution"] = "unattributed"
    return ref


def collect_receipt_evidence(
    target: Path,
    *,
    audited_run_id: str,
) -> dict[str, Any]:
    """Load verify / review / handoff receipts and partition by producer_run_id.

    Only receipts with ``producer_run_id`` equal to ``audited_run_id`` can
    satisfy per-run obligations. Legacy receipts without the field are listed
    as unattributed and remain visible without satisfying.
    """
    from . import scorecard
    from .handoff_cmd import drafts as handoff_drafts
    from .work_cmd import reviews as reviews_mod

    verify_all: list[dict[str, Any]] = []
    for path in scorecard.discover_verify_receipt_paths(target):
        receipt = localio.read_json_dict(path)
        if not isinstance(receipt, dict):
            continue
        receipt = dict(receipt)
        receipt.setdefault("path", str(path.parent))
        verify_all.append(receipt)
    verify_all.sort(key=_receipt_timestamp, reverse=True)

    review_all = list(reviews_mod._review_receipts(target))
    handoff_all = list(handoff_drafts._ingest_receipts(target))

    attributed: dict[str, list[dict[str, Any]]] = {
        KIND_CHECK: [],
        KIND_REVIEW: [],
        KIND_HANDOFF: [],
    }
    unattributed: list[dict[str, Any]] = []
    for kind, receipts in (
        (KIND_CHECK, verify_all),
        (KIND_REVIEW, review_all),
        (KIND_HANDOFF, handoff_all),
    ):
        for receipt in receipts:
            attribution = _receipt_attribution(receipt, audited_run_id=audited_run_id)
            if attribution == "attributed":
                attributed[kind].append(receipt)
            elif attribution == "unattributed":
                unattributed.append(_unattributed_ref(kind=kind, receipt=receipt, target=target))
    return {
        "attributed": attributed,
        "unattributed": unattributed,
    }


def match_obligation(
    obligation: SkillObligation,
    evidence: dict[str, list[dict[str, Any]]],
    *,
    target: Path,
) -> dict[str, Any] | None:
    """Return an evidence ref when captured receipts satisfy the obligation."""
    if obligation.kind == KIND_CHECK:
        verify_receipts = evidence.get(KIND_CHECK) or []
        stamped_match: dict[str, Any] | None = None
        stamped_failure: dict[str, Any] | None = None
        for receipt in verify_receipts:
            for command in _verify_commands(receipt):
                stamped = command.get("obligation_id")
                if stamped != obligation.id:
                    continue
                ref = _evidence_ref(
                    kind=KIND_CHECK,
                    receipt=receipt,
                    target=target,
                    obligation_id=obligation.id,
                    check_id=command.get("check_id") if isinstance(command.get("check_id"), str) else None,
                )
                if _command_succeeded(command) and _verify_receipt_succeeded(receipt):
                    stamped_match = ref
                    break
                stamped_failure = stamped_failure or ref
            if stamped_match is not None:
                break
        if stamped_match is not None:
            return stamped_match
        if stamped_failure is not None:
            # A stamped obligation_id that did not succeed does not fall back
            # to an unrelated verify receipt.
            return None
        for receipt in verify_receipts:
            if _verify_receipt_succeeded(receipt):
                return _evidence_ref(
                    kind=KIND_CHECK,
                    receipt=receipt,
                    target=target,
                    obligation_id=obligation.id,
                )
        return None

    if obligation.kind == KIND_REVIEW:
        for receipt in evidence.get(KIND_REVIEW) or []:
            if _review_receipt_succeeded(receipt):
                return _evidence_ref(
                    kind=KIND_REVIEW,
                    receipt=receipt,
                    target=target,
                    obligation_id=obligation.id,
                )
        return None

    if obligation.kind == KIND_HANDOFF:
        for receipt in evidence.get(KIND_HANDOFF) or []:
            if _handoff_receipt_succeeded(receipt):
                return _evidence_ref(
                    kind=KIND_HANDOFF,
                    receipt=receipt,
                    target=target,
                    obligation_id=obligation.id,
                )
        return None

    return None


def _finding(
    *,
    skill_id: str,
    obligation: SkillObligation,
    detail: str,
) -> dict[str, Any]:
    seed = f"{skill_id}:{obligation.id}:{obligation.kind}:{detail}"
    fingerprint = hashlib.sha256(seed.encode()).hexdigest()[:16]
    finding_id = hashlib.sha256(f"skill-obligation-{seed}".encode()).hexdigest()[:12]
    suggested = {
        KIND_CHECK: 'brigade work verify run --target . --command "<proving command>"',
        KIND_REVIEW: "brigade work review run --target .",
        KIND_HANDOFF: "brigade handoff receipt record --target .",
    }.get(obligation.kind)
    return {
        "id": f"skill-obligation-{finding_id}",
        "finding_id": f"skill-obligation-{finding_id}",
        "skill_id": skill_id,
        "obligation_id": obligation.id,
        "obligation_kind": obligation.kind,
        "required": obligation.required,
        "severity": "medium" if obligation.required else "low",
        "status": "warn",
        "title": f"Missing {obligation.kind} evidence for obligation {obligation.id}",
        "safe_summary": detail,
        "detail": detail,
        "source_fingerprint": fingerprint,
        "suggested_next_command": suggested,
    }


def _public_load_error_text(root: Path, skill_id: str, message: str) -> str:
    """Rewrite known private skill-selector paths out of load-error text."""
    public_id = _public_skill_id(root, skill_id)
    text = message
    candidates: list[str] = [skill_id]
    try:
        resolved = Path(skill_id).expanduser().resolve()
        candidates.append(str(resolved))
        candidates.append(resolved.as_posix())
    except OSError:
        pass
    # Replace longer spellings first so partial overlaps cannot leave a leak.
    for candidate in sorted({item for item in candidates if item}, key=len, reverse=True):
        if candidate in text:
            text = text.replace(candidate, public_id)
    return text


def _load_skill_metadata(target: Path, skill_id: str) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    from . import skills_cmd

    public_id = _public_skill_id(target, skill_id)
    try:
        skill_dir, metadata, source = skills_cmd._load_skill(target, skill_id)
    except Exception as exc:  # noqa: BLE001 - advisory path; surface load failure
        detail = _public_load_error_text(target, skill_id, f"skill not loadable: {exc}")
        return {}, None, detail
    if not (skill_dir / "SKILL.md").is_file() and not (skill_dir / "skill.json").is_file():
        return {}, None, f"skill not found: {public_id}"
    return metadata if isinstance(metadata, dict) else {}, source, None


def build_audit_payload(
    *,
    target: Path,
    run: str | Path,
    runs_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build the advisory obligations audit payload for one Brigade run."""
    from . import runs_cmd

    target = target.expanduser().resolve()
    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=target, runs_dir=runs_dir)
    if error is not None:
        return None, error
    assert run_dir is not None

    plan = localio.read_json_dict(run_dir / "plan.json")
    run_meta = localio.read_json_dict(run_dir / "run.json")
    declared_skill_ids = declared_skill_ids_from_plan(plan if isinstance(plan, dict) else None)
    audited_run_id = run_dir.name
    evidence_bundle = collect_receipt_evidence(target, audited_run_id=audited_run_id)
    attributed = evidence_bundle["attributed"]
    unattributed = evidence_bundle["unattributed"]

    skills: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    satisfied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    load_warnings: list[dict[str, Any]] = []

    public_declared = [_public_skill_id(target, skill_id) for skill_id in declared_skill_ids]
    for skill_id in declared_skill_ids:
        public_skill_id = _public_skill_id(target, skill_id)
        metadata, source, load_error = _load_skill_metadata(target, skill_id)
        if load_error is not None:
            load_warnings.append({"skill_id": public_skill_id, "detail": load_error})
            skills.append(
                {
                    "skill_id": public_skill_id,
                    "loaded": False,
                    "load_error": load_error,
                    "obligations": [],
                    "obligation_errors": [],
                }
            )
            continue
        obligations, obligation_errors = parse_obligations(metadata)
        skill_row: dict[str, Any] = {
            "skill_id": public_skill_id,
            "loaded": True,
            "source": _public_source(target, source),
            "obligations": [item.to_dict() for item in obligations],
            "obligation_errors": obligation_errors,
        }
        skills.append(skill_row)
        for message in obligation_errors:
            findings.append(
                {
                    "id": f"skill-obligation-parse-{hashlib.sha256(f'{skill_id}:{message}'.encode()).hexdigest()[:12]}",
                    "finding_id": f"skill-obligation-parse-{hashlib.sha256(f'{skill_id}:{message}'.encode()).hexdigest()[:12]}",
                    "skill_id": public_skill_id,
                    "obligation_id": None,
                    "obligation_kind": None,
                    "required": False,
                    "severity": "low",
                    "status": "warn",
                    "title": "Malformed skill obligation declaration",
                    "safe_summary": message,
                    "detail": message,
                    "source_fingerprint": hashlib.sha256(f"{skill_id}:{message}".encode()).hexdigest()[:16],
                    "suggested_next_command": f"brigade skills lint {public_skill_id}",
                }
            )
        for obligation in obligations:
            match = match_obligation(obligation, attributed, target=target)
            row = {
                "skill_id": public_skill_id,
                "obligation": obligation.to_dict(),
                "evidence": match,
            }
            if match is not None:
                satisfied.append(row)
                continue
            detail = (
                f"Skill {public_skill_id!r} declares required {obligation.kind} obligation "
                f"{obligation.id!r}, but no matching {obligation.kind} receipt was captured "
                f"with producer_run_id={audited_run_id!r}."
            )
            if obligation.required:
                findings.append(_finding(skill_id=public_skill_id, obligation=obligation, detail=detail))
            else:
                skipped.append({**row, "detail": detail.replace("required ", "optional ")})

    if not declared_skill_ids:
        result = RESULT_NO_DECLARED_SKILLS
    elif not any(skill.get("obligations") for skill in skills):
        result = RESULT_NO_OBLIGATIONS
    elif findings:
        result = RESULT_WARN
    else:
        result = RESULT_OK

    findings.sort(
        key=lambda item: (
            str(item.get("skill_id") or ""),
            str(item.get("obligation_id") or ""),
            str(item.get("id") or ""),
        )
    )
    public_run_dir = _public_path(target, run_dir) or f".brigade/runs/{audited_run_id}"
    payload: dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "target": PUBLIC_TARGET,
        "run_dir": public_run_dir,
        "run_id": audited_run_id,
        "result": result,
        "status": "warn" if findings else "ok",
        "declared_skill_ids": public_declared,
        "skills": skills,
        "satisfied_count": len(satisfied),
        "satisfied": satisfied,
        "skipped_optional": skipped,
        "finding_count": len(findings),
        "findings": findings,
        "load_warnings": load_warnings,
        "evidence_counts": {
            KIND_CHECK: len(attributed.get(KIND_CHECK) or []),
            KIND_REVIEW: len(attributed.get(KIND_REVIEW) or []),
            KIND_HANDOFF: len(attributed.get(KIND_HANDOFF) or []),
        },
        "unattributed_receipt_count": len(unattributed),
        "unattributed_receipts": unattributed,
        "advisory": True,
        "blocking": False,
        "matching": "producer_run_id",
    }
    if isinstance(run_meta, dict):
        if isinstance(run_meta.get("started_at"), str):
            payload["run_started_at"] = run_meta.get("started_at")
        if isinstance(run_meta.get("completed_at"), str):
            payload["run_completed_at"] = run_meta.get("completed_at")
        if isinstance(run_meta.get("status"), str):
            payload["run_status"] = run_meta.get("status")
    return payload, None


def audit(
    *,
    target: Path,
    run: str | Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """CLI entry: advisory skill-obligations audit. Exit 0 on success, 2 on resolve errors."""
    payload, error = build_audit_payload(target=target, run=run, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert payload is not None
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"skills audit: {payload['result']}")
    print(f"target: {payload['target']}")
    print(f"run: {payload['run_dir']}")
    print(f"declared_skills: {len(payload['declared_skill_ids'])}")
    print(f"findings: {payload['finding_count']}")
    print(f"satisfied: {payload['satisfied_count']}")
    print(f"unattributed_receipts: {payload['unattributed_receipt_count']}")
    if payload["result"] == RESULT_NO_DECLARED_SKILLS:
        print("note: plan.json has no selected_skill_ids; nothing to audit")
    elif payload["result"] == RESULT_NO_OBLIGATIONS:
        print("note: declared skills have no process obligations")
    for finding in payload["findings"]:
        print(f"- {finding['skill_id']}:{finding.get('obligation_id') or 'parse'} {finding['title']}")
    for item in payload["unattributed_receipts"]:
        print(
            f"- unattributed:{item.get('kind')} run_id={item.get('run_id')} "
            "(legacy receipt lacks producer_run_id; cannot satisfy per-run obligation)"
        )
    return 0
