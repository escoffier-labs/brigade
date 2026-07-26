"""Fail-closed eligibility projection for scoreable verify receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import localio, verify_manifest

VERIFY_INFRASTRUCTURE_FAILURE_CLASSES = frozenset(
    {
        "infrastructure",
    }
)

VERIFY_SKILL_FAILURE_CLASSES = frozenset(
    {
        "verification",
    }
)

KNOWN_VERIFY_FAILURE_CLASSES = VERIFY_INFRASTRUCTURE_FAILURE_CLASSES | VERIFY_SKILL_FAILURE_CLASSES


@dataclass(frozen=True)
class TrialProjection:
    eligible: bool
    reason: str
    attributed: bool
    infrastructure_excluded: bool = False


def classify_verify_failure(status: object, exit_code: object) -> tuple[str, str]:
    """Map a verify command outcome to the #474-style failure taxonomy."""
    normalized_status = str(status or "")
    if normalized_status == "timed_out":
        return "infrastructure", "timeout"
    if normalized_status == "interrupted":
        return "infrastructure", "interrupted"
    if normalized_status == "rejected":
        return "infrastructure", "rejected"
    if normalized_status == "failed":
        if exit_code == 127:
            return "infrastructure", "command_not_found"
        return "verification", "nonzero_exit"
    if normalized_status == "completed" and exit_code not in (0, None):
        return "verification", "nonzero_exit"
    return "infrastructure", "unknown"


def stamp_verify_command_failure_taxonomy(command: dict[str, Any]) -> None:
    status = command.get("status")
    exit_code = command.get("exit_code")
    if status == "completed" and exit_code == 0:
        command.pop("failure_class", None)
        command.pop("failure_kind", None)
        return
    failure_class, failure_kind = classify_verify_failure(status, exit_code)
    command["failure_class"] = failure_class
    command["failure_kind"] = failure_kind


def stamp_verify_receipt_failure_taxonomy(receipt: dict[str, Any]) -> None:
    status = receipt.get("status")
    if status in {"completed", "running"}:
        receipt.pop("failure_class", None)
        receipt.pop("failure_kind", None)
        return
    if status == "canceled":
        receipt["failure_class"] = "infrastructure"
        receipt["failure_kind"] = "canceled"
        return
    commands = receipt.get("commands")
    if isinstance(commands, list):
        for command in commands:
            if isinstance(command, dict):
                stamp_verify_command_failure_taxonomy(command)
    if status == "failed":
        receipt["failure_class"] = "verification"
        receipt["failure_kind"] = "receipt_failed"
        return
    if status == "rejected":
        receipt["failure_class"] = "infrastructure"
        receipt["failure_kind"] = "rejected"


def _sha256_prefixed(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _patch_delta_bytes_for_path(patch_bytes: bytes, subject_path: str) -> bytes | None:
    if not patch_bytes.strip():
        return None
    text = patch_bytes.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    chunks: list[str] = []
    active = False
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            active = False
            parts = line.split()
            if len(parts) >= 4:
                for token in (parts[2], parts[3]):
                    candidate = token[2:] if token.startswith(("a/", "b/")) else token
                    if candidate == subject_path or candidate.endswith(f"/{subject_path}"):
                        active = True
                        break
            if active:
                chunks.append(line)
            continue
        if active:
            chunks.append(line)
    if not chunks:
        return None
    return "".join(chunks).encode("utf-8")


def owned_delta_sha256(patch_bytes: bytes, subject_path: str) -> str | None:
    delta = _patch_delta_bytes_for_path(patch_bytes, subject_path)
    if delta is None:
        return None
    return hashlib.sha256(delta).hexdigest()


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def subject_path_dirty_in_git_status(subject_path: str, dirty_files: object) -> bool:
    if not isinstance(dirty_files, list):
        return False
    normalized = _normalize_repo_path(subject_path)
    for line in dirty_files:
        if not isinstance(line, str) or len(line) < 3:
            continue
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        candidate = _normalize_repo_path(path_part)
        if candidate == normalized or candidate.endswith(f"/{normalized}"):
            return True
    return False


def start_git_dirty_files_fingerprint(dirty_files: object) -> str | None:
    if not isinstance(dirty_files, list):
        return None
    lines = sorted(str(line) for line in dirty_files)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_work_session_payload(target: Path, session_id: str) -> dict[str, Any] | None:
    session_json = target / ".brigade" / "work" / session_id / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_producer_binding(
    *,
    session_id: str | None,
    session_payload: dict[str, Any] | None,
    subject_path: str,
    owned_delta: str | None,
) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "work_session_id": session_id,
        "owned_delta_sha256": owned_delta,
    }
    if session_payload is None or not session_id:
        binding["subject_clean_at_start"] = False
        return binding
    start = session_payload.get("start")
    if not isinstance(start, dict):
        binding["subject_clean_at_start"] = False
        return binding
    git = start.get("git")
    if not isinstance(git, dict):
        binding["subject_clean_at_start"] = False
        return binding
    dirty_files = git.get("dirty_files")
    fingerprint = start_git_dirty_files_fingerprint(dirty_files)
    if fingerprint is not None:
        binding["start_git"] = {"dirty_files_sha256": fingerprint}
    binding["subject_clean_at_start"] = not subject_path_dirty_in_git_status(subject_path, dirty_files)
    return binding


def _validate_producer_binding(
    producer: dict[str, Any],
    *,
    subject_path: str,
    patch_bytes: bytes,
    target: Path | None,
) -> str | None:
    session_id = producer.get("work_session_id")
    if not isinstance(session_id, str) or not session_id:
        return "producer_session_missing"

    owned_delta = producer.get("owned_delta_sha256")
    if not isinstance(owned_delta, str) or not owned_delta:
        return "no_owned_work"
    expected_owned = owned_delta_sha256(patch_bytes, subject_path)
    if expected_owned is None or expected_owned != owned_delta:
        return "no_owned_work"

    subject_clean_at_start = producer.get("subject_clean_at_start")
    if subject_clean_at_start is False:
        return "subject_pre_existing_dirty"

    if target is None:
        if subject_clean_at_start is not True:
            return "producer_session_missing"
        start_git = producer.get("start_git")
        if not isinstance(start_git, dict) or not isinstance(start_git.get("dirty_files_sha256"), str):
            return "producer_session_missing"
        return None

    session_payload = load_work_session_payload(target, session_id)
    if session_payload is None:
        return "producer_session_missing"
    if session_payload.get("id") != session_id:
        return "producer_session_missing"

    start = session_payload.get("start")
    if not isinstance(start, dict):
        return "producer_session_missing"
    git = start.get("git")
    if not isinstance(git, dict):
        return "producer_session_missing"

    dirty_files = git.get("dirty_files")
    if subject_path_dirty_in_git_status(subject_path, dirty_files):
        if producer.get("subject_clean_at_start") is True:
            return "concurrent_session_ownership"
        return "subject_pre_existing_dirty"

    start_git = producer.get("start_git")
    computed_fp = start_git_dirty_files_fingerprint(dirty_files)
    if not isinstance(start_git, dict):
        return "producer_session_missing"
    stored_fp = start_git.get("dirty_files_sha256")
    if not isinstance(stored_fp, str) or not stored_fp or computed_fp is None or stored_fp != computed_fp:
        return "producer_session_mismatch"

    if subject_clean_at_start is not True:
        return "producer_session_missing"
    return None


def _missing_required_utility_checks(receipt: dict[str, Any]) -> bool:
    required = receipt.get("required_utility_check_ids")
    if required is None:
        return False
    if not isinstance(required, list):
        return True
    if not required:
        return False
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return True
    utility_ids = {
        command.get("check_id")
        for command in commands
        if isinstance(command, dict) and command.get("check_role") == "utility_guardrail"
    }
    return any(not isinstance(check_id, str) or check_id not in utility_ids for check_id in required)


def subject_hash_for_path(target: Path, subject_path: str) -> str | None:
    path = target / subject_path
    try:
        return _sha256_prefixed(path.read_bytes())
    except OSError:
        return None


def verifier_session_id() -> str | None:
    claude_session = os.environ.get("BRIGADE_CLAUDE_SESSION")
    if claude_session and re.fullmatch(r"[0-9a-f]{16}", claude_session):
        return claude_session
    override = os.environ.get("BRIGADE_VERIFY_SESSION_ID", "").strip()
    return override or None


def _verifier_session_id() -> str | None:
    return verifier_session_id()


def _binding_dict(receipt: dict[str, Any]) -> dict[str, Any] | None:
    binding = receipt.get("subject_binding")
    return binding if isinstance(binding, dict) else None


def _commands_missing_check_role(receipt: dict[str, Any]) -> bool:
    commands = receipt.get("commands")
    if not isinstance(commands, list) or not commands:
        return True
    for command in commands:
        if not isinstance(command, dict):
            return True
        role = command.get("check_role")
        if role not in {"effectiveness", "utility_guardrail"}:
            return True
        check_id = command.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            return True
    return False


def _receipt_has_unclassified_failure(receipt: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Return (has_failure, infrastructure, missing_or_unknown_taxonomy)."""
    has_failure = receipt.get("status") not in {None, "completed", "running"}
    infrastructure = False
    missing_or_unknown = False
    if receipt.get("status") not in {None, "completed", "running"}:
        failure_class = receipt.get("failure_class")
        if not isinstance(failure_class, str) or failure_class not in KNOWN_VERIFY_FAILURE_CLASSES:
            missing_or_unknown = True
        elif failure_class in VERIFY_INFRASTRUCTURE_FAILURE_CLASSES:
            infrastructure = True
    commands = receipt.get("commands")
    if isinstance(commands, list):
        for command in commands:
            if not isinstance(command, dict):
                continue
            status = command.get("status")
            exit_code = command.get("exit_code")
            if status == "completed" and exit_code == 0:
                continue
            has_failure = True
            failure_class = command.get("failure_class")
            if not isinstance(failure_class, str) or failure_class not in KNOWN_VERIFY_FAILURE_CLASSES:
                missing_or_unknown = True
            elif failure_class in VERIFY_INFRASTRUCTURE_FAILURE_CLASSES:
                infrastructure = True
    return has_failure, infrastructure, missing_or_unknown


def _receipt_digest_reason(receipt: dict[str, Any]) -> str | None:
    digests = receipt.get("digests")
    if not isinstance(digests, dict):
        return "receipt_digest_missing"
    expected = digests.get("receipt_sha256")
    if not isinstance(expected, str) or not expected:
        return "receipt_digest_missing"
    actual = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    if expected != actual:
        return "receipt_digest_mismatch"
    return None


def _manifest_binding_dict(binding: dict[str, Any]) -> dict[str, Any] | None:
    manifest_binding = binding.get("manifest_binding")
    return manifest_binding if isinstance(manifest_binding, dict) else None


def _manifest_binding_structure_complete(manifest_binding: dict[str, Any]) -> bool:
    manifest_id = manifest_binding.get("manifest_id")
    payload_sha256 = manifest_binding.get("payload_sha256")
    return (
        isinstance(manifest_id, str) and bool(manifest_id) and isinstance(payload_sha256, str) and bool(payload_sha256)
    )


def _verifier_identity_complete(binding: dict[str, Any]) -> bool:
    verifier = binding.get("verifier_identity")
    if not isinstance(verifier, dict):
        return False
    verifier_id = verifier.get("verifier_id")
    session_id = verifier.get("session_id")
    return isinstance(verifier_id, str) and bool(verifier_id) and isinstance(session_id, str) and bool(session_id)


def _normalize_command_identity(command: dict[str, Any]) -> str | None:
    argv = command.get("argv")
    if isinstance(argv, list) and argv:
        return shlex.join(str(item) for item in argv)
    value = command.get("command")
    if isinstance(value, str) and value:
        try:
            return shlex.join(shlex.split(value))
        except ValueError:
            return value
    return None


def _normalize_manifest_command(command: str | list[str]) -> str:
    if isinstance(command, list):
        return shlex.join(command)
    try:
        return shlex.join(shlex.split(command))
    except ValueError:
        return command


def _manifest_command_plan(manifest: verify_manifest.VerifyManifest) -> list[tuple[str, str, str | None, str]]:
    plan: list[tuple[str, str, str | None, str]] = []
    for check in manifest.checks:
        plan.append(
            (
                check.check_id,
                check.check_role,
                check.obligation_id,
                _normalize_manifest_command(check.command),
            )
        )
    return plan


def _receipt_command_plan(receipt: dict[str, Any]) -> list[tuple[str, str, str | None, str]] | None:
    commands = receipt.get("commands")
    if not isinstance(commands, list) or not commands:
        return None
    plan: list[tuple[str, str, str | None, str]] = []
    for command in commands:
        if not isinstance(command, dict):
            return None
        check_id = command.get("check_id")
        check_role = command.get("check_role")
        if not isinstance(check_id, str) or not check_id:
            return None
        if check_role not in {"effectiveness", "utility_guardrail"}:
            return None
        normalized = _normalize_command_identity(command)
        if normalized is None:
            return None
        obligation_id = command.get("obligation_id")
        if obligation_id is not None and (not isinstance(obligation_id, str) or not obligation_id):
            return None
        plan.append((check_id, check_role, obligation_id if isinstance(obligation_id, str) else None, normalized))
    return plan


def _required_utility_ids_match(receipt: dict[str, Any], manifest: verify_manifest.VerifyManifest) -> bool:
    receipt_required = receipt.get("required_utility_check_ids")
    if not manifest.required_utility_check_ids:
        return receipt_required is None or receipt_required == []
    if not isinstance(receipt_required, list):
        return False
    return tuple(str(item) for item in receipt_required) == manifest.required_utility_check_ids


def _manifest_binding_matches_resolved(
    target: Path,
    manifest_binding: dict[str, Any],
    manifest: verify_manifest.VerifyManifest,
) -> bool:
    if manifest_binding.get("manifest_id") != manifest.manifest_id:
        return False
    expected_payload = verify_manifest.manifest_payload_sha256(manifest)
    if expected_payload is None or manifest_binding.get("payload_sha256") != expected_payload:
        return False
    source_path = manifest_binding.get("source_path")
    if source_path is not None:
        expected_source = verify_manifest.manifest_source_path(target, manifest)
        if not isinstance(source_path, str) or source_path != expected_source:
            return False
    return True


def _binding_matches_manifest(binding: dict[str, Any], manifest: verify_manifest.VerifyManifest) -> bool:
    if binding.get("artifact_kind") != manifest.artifact_kind:
        return False
    if binding.get("artifact_id") != manifest.artifact_id:
        return False
    verifier = binding.get("verifier_identity")
    if not isinstance(verifier, dict) or verifier.get("verifier_id") != manifest.verifier_id:
        return False
    if manifest.binding_mode == "patch_backed":
        if binding.get("patch_source") != manifest.patch_source:
            return False
    if manifest.binding_mode == "fixture_eval":
        fixture_binding = binding.get("fixture_binding")
        if not isinstance(fixture_binding, dict):
            return False
        if fixture_binding.get("manifest_id") != manifest.fixture_manifest_id:
            return False
        if fixture_binding.get("case_id") != manifest.fixture_case_id:
            return False
        if fixture_binding.get("check_id") != manifest.fixture_check_id:
            return False
    return True


def _validate_manifest_contract(
    receipt: dict[str, Any],
    binding: dict[str, Any],
    *,
    target: Path | None,
) -> str | None:
    manifest_binding = _manifest_binding_dict(binding)
    if manifest_binding is None or not _manifest_binding_structure_complete(manifest_binding):
        return "verifier_manifest_missing"

    binding_mode = binding.get("binding_mode")
    if binding_mode in {"patch_backed", "fixture_eval"} and not _verifier_identity_complete(binding):
        return "verifier_identity_missing"

    if target is None:
        return None

    verify_manifest_id = receipt.get("verify_manifest_id")
    if not isinstance(verify_manifest_id, str) or not verify_manifest_id:
        return "verifier_manifest_missing"
    if verify_manifest_id != manifest_binding.get("manifest_id"):
        return "verifier_manifest_mismatch"

    manifest, error = verify_manifest.resolve_manifest(target, verify_manifest_id)
    if manifest is None:
        return "verifier_manifest_missing" if error and "not found" in error else "verifier_manifest_mismatch"

    if not _manifest_binding_matches_resolved(target, manifest_binding, manifest):
        return "verifier_manifest_mismatch"
    if not _binding_matches_manifest(binding, manifest):
        return "verifier_manifest_mismatch"

    receipt_plan = _receipt_command_plan(receipt)
    manifest_plan = _manifest_command_plan(manifest)
    if receipt_plan is None or receipt_plan != manifest_plan:
        return "verifier_manifest_mismatch"
    if not _required_utility_ids_match(receipt, manifest):
        return "verifier_manifest_mismatch"
    return None


def _patch_identity_complete(receipt: dict[str, Any]) -> bool:
    for key in ("baseline_commit", "tree_fingerprint", "changes_patch_sha256"):
        value = receipt.get(key)
        if not isinstance(value, str) or not value:
            return False
    return True


def project_trial(
    receipt: dict[str, Any], *, target: Path | None = None, patch_path: Path | None = None
) -> TrialProjection:
    binding = _binding_dict(receipt)
    if binding is None:
        return TrialProjection(eligible=False, reason="unattributed", attributed=False)
    if _commands_missing_check_role(receipt):
        return TrialProjection(eligible=False, reason="missing_check_role", attributed=True)

    binding_mode = binding.get("binding_mode")
    if binding_mode == "patch_backed":
        patch_binding = binding.get("patch_binding")
        if not isinstance(patch_binding, dict):
            return TrialProjection(eligible=False, reason="patch_binding_incomplete", attributed=True)
        subject_path = patch_binding.get("subject_path")
        subject_hash = patch_binding.get("subject_hash")
        if not isinstance(subject_path, str) or not subject_path:
            return TrialProjection(eligible=False, reason="patch_binding_incomplete", attributed=True)
        if not isinstance(subject_hash, str) or not subject_hash:
            return TrialProjection(eligible=False, reason="patch_binding_incomplete", attributed=True)
        if not _patch_identity_complete(receipt):
            return TrialProjection(eligible=False, reason="incomplete_patch_identity", attributed=True)
        resolved_target = target
        if resolved_target is None:
            target_value = receipt.get("target")
            if isinstance(target_value, str) and target_value:
                resolved_target = Path(target_value)
        patch_file = patch_path
        if patch_file is None:
            receipt_path = receipt.get("path")
            if isinstance(receipt_path, str) and receipt_path:
                patch_file = Path(receipt_path) / "changes.patch"
        patch_bytes = b""
        if patch_file is not None and patch_file.is_file():
            try:
                patch_bytes = patch_file.read_bytes()
            except OSError:
                patch_bytes = b""
        if not patch_bytes.strip():
            return TrialProjection(eligible=False, reason="empty_patch", attributed=True)
        patch_hash = receipt.get("changes_patch_sha256")
        if not isinstance(patch_hash, str) or hashlib.sha256(patch_bytes).hexdigest() != patch_hash:
            return TrialProjection(eligible=False, reason="patch_digest_mismatch", attributed=True)
        producer = binding.get("producer_binding")
        if not isinstance(producer, dict):
            return TrialProjection(eligible=False, reason="producer_session_missing", attributed=True)
        producer_reason = _validate_producer_binding(
            producer,
            subject_path=subject_path,
            patch_bytes=patch_bytes,
            target=resolved_target,
        )
        if producer_reason is not None:
            return TrialProjection(eligible=False, reason=producer_reason, attributed=True)
        if binding.get("patch_source") == "generated":
            verifier = binding.get("verifier_identity")
            producer_session = producer.get("work_session_id")
            if not isinstance(verifier, dict):
                return TrialProjection(eligible=False, reason="verifier_not_independent", attributed=True)
            verifier_session = verifier.get("session_id")
            if (
                not isinstance(verifier_session, str)
                or not verifier_session
                or not isinstance(producer_session, str)
                or not producer_session
                or verifier_session == producer_session
            ):
                return TrialProjection(eligible=False, reason="verifier_not_independent", attributed=True)
        if resolved_target is not None:
            live_hash = subject_hash_for_path(resolved_target, subject_path)
            if live_hash is None or live_hash != subject_hash:
                return TrialProjection(eligible=False, reason="subject_hash_mismatch", attributed=True)
        patch_tuple = patch_binding
        for key in ("baseline_commit", "tree_fingerprint", "changes_patch_sha256"):
            if patch_tuple.get(key) != receipt.get(key):
                return TrialProjection(eligible=False, reason="patch_binding_incomplete", attributed=True)
    elif binding_mode == "fixture_eval":
        fixture_binding = binding.get("fixture_binding")
        if not isinstance(fixture_binding, dict):
            return TrialProjection(eligible=False, reason="fixture_binding_incomplete", attributed=True)
        for key in ("manifest_id", "case_id", "check_id"):
            value = fixture_binding.get(key)
            if not isinstance(value, str) or not value:
                return TrialProjection(eligible=False, reason="fixture_binding_incomplete", attributed=True)
        fingerprint = binding.get("content_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            return TrialProjection(eligible=False, reason="fixture_binding_incomplete", attributed=True)
    else:
        return TrialProjection(eligible=False, reason="missing_subject_binding", attributed=True)

    manifest_reason = _validate_manifest_contract(receipt, binding, target=target)
    if manifest_reason is not None:
        return TrialProjection(eligible=False, reason=manifest_reason, attributed=True)

    for key in ("artifact_kind", "artifact_id", "content_fingerprint"):
        value = binding.get(key)
        if not isinstance(value, str) or not value:
            return TrialProjection(eligible=False, reason="missing_subject_binding", attributed=True)

    if _missing_required_utility_checks(receipt):
        return TrialProjection(eligible=False, reason="missing_required_utility_check", attributed=True)

    has_failure, infrastructure, missing_or_unknown = _receipt_has_unclassified_failure(receipt)
    if has_failure:
        if missing_or_unknown:
            return TrialProjection(eligible=False, reason="failure_taxonomy_missing", attributed=True)
        if infrastructure:
            return TrialProjection(
                eligible=False,
                reason="failure_taxonomy_infrastructure",
                attributed=True,
                infrastructure_excluded=True,
            )

    digest_reason = _receipt_digest_reason(receipt)
    if digest_reason is not None:
        return TrialProjection(eligible=False, reason=digest_reason, attributed=True)

    return TrialProjection(eligible=True, reason="eligible", attributed=True)
