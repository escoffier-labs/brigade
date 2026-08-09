"""Tracked verifier and fixture manifest contract for scoreable verify runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import localio, outcome_cmd, runguard, verification_contract
from .router import PATHS

VERIFY_MANIFEST_SCHEMA = "brigade.verify_manifest.v1"
VERIFY_MANIFEST_SCHEMA_VERSION = 1

CHECK_ROLES = frozenset({"effectiveness", "utility_guardrail"})
BINDING_MODES = frozenset({"patch_backed", "fixture_eval"})
PATCH_SOURCES = frozenset({"worktree", "generated"})
_WHOLE_REPO_SCOPE_GLOBS = frozenset({"**", "**/*", "*", "/*"})
_ROUTE_CLASS_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")

_BUILTIN_MANIFESTS_DIR = Path(__file__).resolve().parent / "templates" / "verify" / "manifests"
_WORKSPACE_MANIFESTS_REL = Path("verify") / "manifests"


@dataclass(frozen=True)
class VerifyCheckSpec:
    check_id: str
    check_role: str
    command: str | list[str]
    obligation_id: str | None = None


@dataclass(frozen=True)
class VerifyManifest:
    manifest_id: str
    binding_mode: str
    artifact_kind: str
    artifact_id: str
    verifier_id: str
    checks: tuple[VerifyCheckSpec, ...]
    required_utility_check_ids: tuple[str, ...] = ()
    scope_globs: tuple[str, ...] = ()
    route_paths: tuple[str, ...] = ()
    route_classes: tuple[str, ...] = ()
    subject_path: str | None = None
    content_fingerprint: str | None = None
    fixture_manifest_id: str | None = None
    fixture_case_id: str | None = None
    fixture_check_id: str | None = None
    patch_source: str = "worktree"
    path: Path | None = None
    consequential: bool = False
    verification_contract: dict[str, Any] | None = None

    @property
    def planned_commands(self) -> list[str | list[str]]:
        return [check.command for check in self.checks]

    def contract_plan_view(self) -> dict[str, Any]:
        """Plan-time VerificationContract completeness for this work template."""
        payload = self.verification_contract
        if payload is None and self.consequential:
            # Consequential manifests may omit nested verifier and inherit checks.
            return verification_contract.contract_plan_view(
                None,
                consequential=True,
                allow_manifest_checks=True,
            )
        if payload is None:
            return verification_contract.contract_plan_view(
                None,
                consequential=False,
                allow_manifest_checks=True,
            )
        # Allow omitting verifier on manifests: fill manifest_checks for validation.
        enriched = dict(payload)
        verifier = enriched.get("verifier")
        if verifier is None:
            enriched["verifier"] = {"source": "manifest_checks"}
        elif (
            isinstance(verifier, dict)
            and verifier.get("source") is None
            and not any(key in verifier for key in ("command", "argv", "manifest_id"))
        ):
            enriched["verifier"] = {**verifier, "source": "manifest_checks"}
        return verification_contract.contract_plan_view(
            enriched,
            consequential=self.consequential,
            allow_manifest_checks=True,
        )


def validate_route_paths(route_paths: tuple[str, ...] | list[str]) -> str | None:
    """Return a stable rejection reason when route_paths are outside router.PATHS."""
    for item in route_paths:
        normalized = str(item).strip()
        if not normalized:
            return "invalid_route_path"
        if normalized not in PATHS:
            return "invalid_route_path"
    return None


def validate_route_classes(route_classes: tuple[str, ...] | list[str]) -> str | None:
    """Return a stable rejection reason when route_classes are not fingerprint-shaped."""
    for item in route_classes:
        normalized = str(item).strip()
        if not normalized or _ROUTE_CLASS_FINGERPRINT.fullmatch(normalized) is None:
            return "invalid_route_class"
    return None


def validate_scope_globs(globs: tuple[str, ...] | list[str]) -> str | None:
    """Return a stable rejection reason when scope_globs are unsafe or unusable."""
    if not globs:
        return "missing_scope_globs"
    for item in globs:
        normalized = str(item).strip()
        if not normalized:
            return "invalid_scope_glob"
        if normalized.startswith("/") or normalized.startswith("\\"):
            return "invalid_scope_glob"
        parts = normalized.replace("\\", "/").split("/")
        if any(part == ".." for part in parts):
            return "invalid_scope_glob"
        if normalized in _WHOLE_REPO_SCOPE_GLOBS:
            return "invalid_scope_glob"
    return None


def _workspace_manifests_root(target: Path) -> Path:
    return target / _WORKSPACE_MANIFESTS_REL


def _is_builtin_manifest(path: Path) -> bool:
    try:
        path.resolve().relative_to(_BUILTIN_MANIFESTS_DIR.resolve())
        return True
    except ValueError:
        return False


def _is_git_tracked(target: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(target.resolve())
    except ValueError:
        return False
    result = runguard._git(target, "ls-files", "--error-unmatch", "--", relative.as_posix())
    return result.code == 0


def _load_manifest_file(path: Path) -> dict[str, Any] | None:
    payload = localio.read_json_dict(path)
    return payload


def _parse_check(raw: object, *, index: int) -> tuple[VerifyCheckSpec | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"checks[{index}] must be an object"
    check_id = raw.get("check_id")
    check_role = raw.get("check_role")
    command = raw.get("command")
    if not isinstance(check_id, str) or not check_id.strip():
        return None, f"checks[{index}].check_id is required"
    if not isinstance(check_role, str) or check_role not in CHECK_ROLES:
        return None, f"checks[{index}].check_role must be one of {sorted(CHECK_ROLES)}"
    if isinstance(command, str):
        if not command.strip():
            return None, f"checks[{index}].command is required"
    elif isinstance(command, list):
        if not command or not all(isinstance(item, str) for item in command):
            return None, f"checks[{index}].command must be a non-empty string array"
    else:
        return None, f"checks[{index}].command is required"
    obligation_id = raw.get("obligation_id")
    if obligation_id is not None and (not isinstance(obligation_id, str) or not obligation_id.strip()):
        return None, f"checks[{index}].obligation_id must be a non-empty string when set"
    return (
        VerifyCheckSpec(
            check_id=check_id,
            check_role=check_role,
            command=command,
            obligation_id=obligation_id if isinstance(obligation_id, str) else None,
        ),
        None,
    )


def validate_manifest_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != VERIFY_MANIFEST_SCHEMA:
        errors.append(f"schema must be {VERIFY_MANIFEST_SCHEMA!r}")
    if payload.get("schema_version") != VERIFY_MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must be {VERIFY_MANIFEST_SCHEMA_VERSION}")
    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        errors.append("manifest_id is required")
    binding_mode = payload.get("binding_mode")
    if not isinstance(binding_mode, str) or binding_mode not in BINDING_MODES:
        errors.append(f"binding_mode must be one of {sorted(BINDING_MODES)}")
    verifier_id = payload.get("verifier_id")
    if not isinstance(verifier_id, str) or not verifier_id.strip():
        errors.append("verifier_id is required")
    subject = payload.get("subject")
    if not isinstance(subject, dict):
        errors.append("subject object is required")
        subject = {}
    artifact_kind = subject.get("artifact_kind")
    artifact_id = subject.get("artifact_id")
    if not isinstance(artifact_kind, str) or artifact_kind not in {"skill", "card"}:
        errors.append("subject.artifact_kind must be 'skill' or 'card'")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        errors.append("subject.artifact_id is required")
    subject_path = subject.get("subject_path")
    if binding_mode == "patch_backed":
        if not isinstance(subject_path, str) or not subject_path.strip():
            errors.append("subject.subject_path is required for patch_backed manifests")
    content_fingerprint = subject.get("content_fingerprint")
    if content_fingerprint is not None and (
        not isinstance(content_fingerprint, str) or not content_fingerprint.strip()
    ):
        errors.append("subject.content_fingerprint must be a non-empty string when set")
    patch_source = payload.get("patch_source", "worktree")
    if patch_source not in PATCH_SOURCES:
        errors.append(f"patch_source must be one of {sorted(PATCH_SOURCES)}")
    checks = payload.get("checks")
    parsed_checks: list[VerifyCheckSpec] = []
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty array")
    else:
        for index, item in enumerate(checks):
            parsed, error = _parse_check(item, index=index)
            if error:
                errors.append(error)
            elif parsed is not None:
                parsed_checks.append(parsed)
    required_utility = payload.get("required_utility_check_ids", [])
    if required_utility is None:
        errors.append("required_utility_check_ids must be an array")
        required_utility = []
    elif not isinstance(required_utility, list):
        errors.append("required_utility_check_ids must be an array")
        required_utility = []
    else:
        utility_check_ids = {check.check_id for check in parsed_checks if check.check_role == "utility_guardrail"}
        for index, check_id in enumerate(required_utility):
            if not isinstance(check_id, str) or not check_id.strip():
                errors.append(f"required_utility_check_ids[{index}] must be a non-empty string")
                continue
            if check_id not in utility_check_ids:
                errors.append(f"required_utility_check_ids[{index}] must name a utility_guardrail check in checks")
    scope_globs = payload.get("scope_globs")
    if scope_globs is not None:
        if not isinstance(scope_globs, list):
            errors.append("scope_globs must be an array when set")
        else:
            for index, item in enumerate(scope_globs):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"scope_globs[{index}] must be a non-empty string")
                elif validate_scope_globs([item]) == "invalid_scope_glob":
                    errors.append(f"scope_globs[{index}] must be a relative, non-traversing glob")
    route_paths = payload.get("route_paths")
    if route_paths is not None:
        if not isinstance(route_paths, list):
            errors.append("route_paths must be an array when set")
        else:
            for index, item in enumerate(route_paths):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"route_paths[{index}] must be a non-empty string")
                elif item.strip() not in PATHS:
                    errors.append(f"route_paths[{index}] must be one of: {', '.join(PATHS)}")
    route_classes = payload.get("route_classes")
    if route_classes is not None:
        if not isinstance(route_classes, list):
            errors.append("route_classes must be an array when set")
        else:
            for index, item in enumerate(route_classes):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"route_classes[{index}] must be a non-empty string")
                elif _ROUTE_CLASS_FINGERPRINT.fullmatch(item.strip()) is None:
                    errors.append(f"route_classes[{index}] must be a 64-character lowercase hex fingerprint")
    if binding_mode == "fixture_eval":
        fixture = payload.get("fixture")
        if not isinstance(fixture, dict):
            errors.append("fixture object is required for fixture_eval manifests")
        else:
            for key in ("manifest_id", "case_id", "check_id"):
                value = fixture.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"fixture.{key} is required for fixture_eval manifests")
    consequential = payload.get("consequential", False)
    if consequential is not False and consequential is not True:
        errors.append("consequential must be a boolean when set")
    contract_raw = payload.get("verification_contract")
    if contract_raw is not None:
        if not isinstance(contract_raw, dict):
            errors.append("verification_contract must be an object when set")
        else:
            enriched = dict(contract_raw)
            verifier = enriched.get("verifier")
            if verifier is None:
                enriched["verifier"] = {"source": "manifest_checks"}
            elif (
                isinstance(verifier, dict)
                and verifier.get("source") is None
                and not any(key in verifier for key in ("command", "argv", "manifest_id"))
            ):
                enriched["verifier"] = {**verifier, "source": "manifest_checks"}
            for error in verification_contract.validate_contract_payload(
                enriched,
                allow_manifest_checks=True,
            ):
                errors.append(f"verification_contract: {error}")
    elif consequential is True:
        errors.append(
            "consequential manifests require verification_contract "
            "(rollback and budget; verifier defaults to manifest checks)"
        )
    return errors


def manifest_from_payload(payload: dict[str, Any], *, path: Path | None = None) -> VerifyManifest:
    errors = validate_manifest_payload(payload)
    if errors:
        raise ValueError("; ".join(errors))
    subject = payload["subject"]
    checks: list[VerifyCheckSpec] = []
    for index, item in enumerate(payload["checks"]):
        parsed, error = _parse_check(item, index=index)
        if error or parsed is None:
            raise ValueError(error or f"checks[{index}] is invalid")
        checks.append(parsed)
    fixture = payload.get("fixture") if payload.get("binding_mode") == "fixture_eval" else None
    required_utility_raw = payload.get("required_utility_check_ids", [])
    required_utility_check_ids = (
        tuple(str(item) for item in required_utility_raw) if isinstance(required_utility_raw, list) else ()
    )
    scope_globs_raw = payload.get("scope_globs", [])
    scope_globs = tuple(str(item) for item in scope_globs_raw) if isinstance(scope_globs_raw, list) else ()
    route_paths_raw = payload.get("route_paths", [])
    route_paths = tuple(str(item) for item in route_paths_raw) if isinstance(route_paths_raw, list) else ()
    route_classes_raw = payload.get("route_classes", [])
    route_classes = tuple(str(item) for item in route_classes_raw) if isinstance(route_classes_raw, list) else ()
    contract_raw = payload.get("verification_contract")
    contract_payload = dict(contract_raw) if isinstance(contract_raw, dict) else None
    if contract_payload is not None:
        verifier = contract_payload.get("verifier")
        if verifier is None:
            contract_payload["verifier"] = {"source": "manifest_checks"}
        elif (
            isinstance(verifier, dict)
            and verifier.get("source") is None
            and not any(key in verifier for key in ("command", "argv", "manifest_id"))
        ):
            contract_payload["verifier"] = {**verifier, "source": "manifest_checks"}
    return VerifyManifest(
        manifest_id=str(payload["manifest_id"]),
        binding_mode=str(payload["binding_mode"]),
        artifact_kind=str(subject["artifact_kind"]),
        artifact_id=str(subject["artifact_id"]),
        verifier_id=str(payload["verifier_id"]),
        checks=tuple(checks),
        required_utility_check_ids=required_utility_check_ids,
        scope_globs=scope_globs,
        route_paths=route_paths,
        route_classes=route_classes,
        subject_path=subject.get("subject_path") if isinstance(subject.get("subject_path"), str) else None,
        content_fingerprint=subject.get("content_fingerprint")
        if isinstance(subject.get("content_fingerprint"), str)
        else None,
        fixture_manifest_id=fixture.get("manifest_id") if isinstance(fixture, dict) else None,
        fixture_case_id=fixture.get("case_id") if isinstance(fixture, dict) else None,
        fixture_check_id=fixture.get("check_id") if isinstance(fixture, dict) else None,
        patch_source=str(payload.get("patch_source", "worktree")),
        path=path,
        consequential=bool(payload.get("consequential", False)),
        verification_contract=contract_payload,
    )


def _discover_builtin_manifest_files() -> list[Path]:
    if _BUILTIN_MANIFESTS_DIR.is_dir():
        return sorted(_BUILTIN_MANIFESTS_DIR.glob("*.json"))
    return []


def _discover_workspace_manifest_files(target: Path, *, tracked_only: bool = False) -> list[Path]:
    workspace_root = _workspace_manifests_root(target)
    if not workspace_root.is_dir():
        return []
    paths = sorted(workspace_root.glob("*.json"))
    if tracked_only:
        paths = [path for path in paths if _is_git_tracked(target, path)]
    return paths


def _discover_manifest_files(target: Path, *, tracked_only: bool = False) -> list[Path]:
    return _discover_builtin_manifest_files() + _discover_workspace_manifest_files(
        target,
        tracked_only=tracked_only,
    )


def registered_manifest_ids(target: Path) -> list[str]:
    ids: list[str] = []
    for path in _discover_manifest_files(target, tracked_only=True):
        payload = _load_manifest_file(path)
        if not isinstance(payload, dict):
            continue
        manifest_id = payload.get("manifest_id")
        if isinstance(manifest_id, str) and manifest_id:
            ids.append(manifest_id)
    return sorted(set(ids))


def resolve_manifest(target: Path, manifest_id: str) -> tuple[VerifyManifest | None, str | None]:
    target = target.expanduser().resolve()
    matches: list[VerifyManifest] = []
    untracked_workspace_match = False
    for path in _discover_manifest_files(target):
        payload = _load_manifest_file(path)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("manifest_id") or "") != manifest_id:
            continue
        if not _is_builtin_manifest(path) and not _is_git_tracked(target, path):
            untracked_workspace_match = True
            continue
        try:
            matches.append(manifest_from_payload(payload, path=path))
        except ValueError as exc:
            return None, str(exc)
    if not matches:
        if untracked_workspace_match:
            return None, f"verify manifest not tracked: {manifest_id}"
        return None, f"verify manifest not found: {manifest_id}"
    if len(matches) > 1:
        return None, f"verify manifest id is ambiguous: {manifest_id}"
    return matches[0], None


def resolve_subject_fingerprint(target: Path, manifest: VerifyManifest) -> str | None:
    if manifest.content_fingerprint:
        return manifest.content_fingerprint
    fingerprint = outcome_cmd.artifact_fingerprint(target, manifest.artifact_id, manifest.artifact_kind)
    if fingerprint is not None:
        return fingerprint
    if manifest.subject_path:
        from . import verify_trial

        return verify_trial.subject_hash_for_path(target, manifest.subject_path)
    return None


def manifest_source_path(target: Path, manifest: VerifyManifest) -> str | None:
    if manifest.path is None:
        return None
    if _is_builtin_manifest(manifest.path):
        try:
            return manifest.path.resolve().relative_to(_BUILTIN_MANIFESTS_DIR.resolve()).as_posix()
        except ValueError:
            return manifest.path.as_posix()
    try:
        return manifest.path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return manifest.path.as_posix()


def manifest_payload_sha256(manifest: VerifyManifest) -> str | None:
    if manifest.path is None or not manifest.path.is_file():
        return None
    return f"sha256:{localio.file_sha256(manifest.path)}"


def build_manifest_binding(target: Path, manifest: VerifyManifest) -> dict[str, Any]:
    payload_sha256 = manifest_payload_sha256(manifest)
    if payload_sha256 is None:
        raise ValueError(f"verify manifest payload is unreadable: {manifest.manifest_id}")
    binding: dict[str, Any] = {
        "manifest_id": manifest.manifest_id,
        "payload_sha256": payload_sha256,
    }
    source_path = manifest_source_path(target, manifest)
    if source_path:
        binding["source_path"] = source_path
    return binding


def write_workspace_manifest(target: Path, payload: dict[str, Any]) -> Path:
    """Write a workspace-local manifest after validation (tests and operators)."""
    manifest = manifest_from_payload(payload)
    root = _workspace_manifests_root(target)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{manifest.manifest_id}.json"
    localio.write_json(path, payload)
    return path
