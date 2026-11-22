"""Fail-closed eligibility projection for scoreable verify receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    agent_identity,
    generated_patch_quarantine,
    git_status_snapshot,
    localio,
    outcome_attribution,
    verify_manifest,
)
from .claude_hooks import producer_session as hook_producer

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
    # Every trial today is proven by local session state the scored agent can write,
    # so its ownership proof is local. A future trial with an independent proof may
    # carry "attested" here. That design is parked on #2591, so no registry exists.
    attestation: str = "local"


def classify_verify_failure(status: object, exit_code: object) -> tuple[str, str]:
    """Map a verify command outcome to the #474-style failure taxonomy."""
    normalized_status = str(status or "")
    if normalized_status == "busy":
        return "infrastructure", "busy"
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
    if status == "busy":
        receipt["failure_class"] = "infrastructure"
        receipt["failure_kind"] = "busy"
        return
    commands = receipt.get("commands")
    if isinstance(commands, list):
        for command in commands:
            if isinstance(command, dict):
                stamp_verify_command_failure_taxonomy(command)
    if status == "failed":
        planned = receipt.get("planned_commands")
        if receipt.get("bookkeeping_error") or (
            isinstance(planned, list) and isinstance(commands, list) and len(planned) != len(commands)
        ):
            receipt["failure_class"] = "infrastructure"
            receipt["failure_kind"] = "receipt_incomplete"
            return
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


_PATCH_HEADER_PATH_PREFIXES = ("--- ", "+++ ", "rename from ", "rename to ", "copy from ", "copy to ")


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote_patch_path(token: str) -> str:
    token = token.rstrip("\t")
    if not (len(token) >= 2 and token.startswith('"') and token.endswith('"')):
        return token
    # Git C-quotes paths; with core.quotePath=false bytes above 0x80 stay literal
    # UTF-8, so decode escapes to bytes and only then decode the whole path.
    body = token[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            out += char.encode("utf-8")
            index += 1
            continue
        escape = body[index + 1]
        octal = body[index + 1 : index + 4]
        if len(octal) == 3 and all(digit in "01234567" for digit in octal):
            out.append(int(octal, 8) & 0xFF)
            index += 4
        elif escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            index += 2
        else:
            out += char.encode("utf-8")
            index += 1
    return out.decode("utf-8", errors="replace")


def _header_path(line: str, prefix: str) -> str | None:
    token = _unquote_patch_path(line[len(prefix) :])
    if token == "/dev/null":
        return None
    if prefix in {"--- ", "+++ "} and token.startswith(("a/", "b/")):
        token = token[2:]
    return token or None


def _diff_git_symmetric_path(line: str) -> str | None:
    rest = line[len("diff --git ") :].rstrip("\n")
    if rest.startswith('"'):
        parts = rest.split('" "', 1)
        if len(parts) != 2:
            return None
        left, right = _unquote_patch_path(parts[0] + '"'), _unquote_patch_path('"' + parts[1])
    else:
        if len(rest) % 2 == 0:
            return None
        half = len(rest) // 2
        if rest[half] != " ":
            return None
        left, right = rest[:half], rest[half + 1 :]
    if not (left.startswith("a/") and right.startswith("b/")) or left[2:] != right[2:]:
        return None
    return left[2:] or None


def patch_changed_paths(patch_bytes: bytes) -> list[str] | None:
    """Return the repo-relative paths a git patch touches, or None when a file block is unparseable.

    Only file headers are read; hunk bodies are skipped so a removed line that
    looks like ``--- a/x`` is never mistaken for a path.
    """
    text = patch_bytes.decode("utf-8", errors="replace")
    paths: list[str] = []
    block: list[str] | None = None
    header_line = ""
    in_header = False

    def close_block() -> bool:
        if block is None:
            return True
        found = block or [p for p in [_diff_git_symmetric_path(header_line)] if p]
        if not found:
            return False
        for path in found:
            if path not in paths:
                paths.append(path)
        return True

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if not close_block():
                return None
            block, header_line, in_header = [], line, True
            continue
        if not in_header or block is None:
            continue
        if line.startswith(("@@", "GIT binary patch", "Binary files ")):
            in_header = False
            continue
        for prefix in _PATCH_HEADER_PATH_PREFIXES:
            if line.startswith(prefix):
                path = _header_path(line, prefix)
                if path is not None and path not in block:
                    block.append(path)
                break
    if not close_block():
        return None
    return paths


def _patch_outside_manifest_scope(patch_bytes: bytes, manifest: verify_manifest.VerifyManifest) -> bool:
    changed = patch_changed_paths(patch_bytes)
    if changed is None:
        return True
    # Git patch paths always use "/"; a backslash is part of a POSIX filename, so
    # never let path normalization turn it into a directory separator.
    return any("\\" in path or not verify_manifest.path_in_manifest_scope(manifest, path) for path in changed)


def owned_delta_sha256(patch_bytes: bytes, subject_path: str) -> str | None:
    delta = _patch_delta_bytes_for_path(patch_bytes, subject_path)
    if delta is None:
        return None
    return hashlib.sha256(delta).hexdigest()


def load_work_session_payload(target: Path, session_id: str, start_id: str | None = None) -> dict[str, Any] | None:
    if hook_producer.fingerprint_from_producer_id(session_id) is not None:
        return hook_producer.load_payload(target, session_id, start_id)
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
    target: Path | None = None,
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
    if hook_producer.fingerprint_from_producer_id(session_id) is not None:
        # A hook session's start snapshot cannot say who wrote the subject, so record the verdict
        # of the live ownership check for a projection that has no target to rerun it against, and
        # the start the receipt bound to so a later epoch's start cannot stand in for it.
        start_id = start.get("id")
        binding["hook_start_id"] = start_id
        binding["hook_ownership"] = (
            (hook_producer.ownership_reason(target, session_id, subject_path, start_id) or hook_producer.OWNED)
            if target is not None and isinstance(start_id, str)
            else "producer_session_missing"
        )
    git = start.get("git")
    if not isinstance(git, dict):
        binding["subject_clean_at_start"] = False
        return binding
    fingerprint = git_status_snapshot.fingerprint(git)
    if fingerprint is not None:
        binding["start_git"] = {"dirty_files_sha256": fingerprint}
    binding["subject_clean_at_start"] = not git_status_snapshot.subject_dirty(subject_path, git)
    return binding


_HOOK_OWNERSHIP_REASONS = frozenset({"concurrent_session_ownership", "no_owned_work"})


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

    hook_session = hook_producer.fingerprint_from_producer_id(session_id) is not None
    recorded_ownership = producer.get("hook_ownership")
    if hook_session and recorded_ownership != hook_producer.OWNED:
        return recorded_ownership if recorded_ownership in _HOOK_OWNERSHIP_REASONS else "producer_session_missing"

    if target is None:
        if subject_clean_at_start is not True:
            return "producer_session_missing"
        start_git = producer.get("start_git")
        if not isinstance(start_git, dict) or not isinstance(start_git.get("dirty_files_sha256"), str):
            return "producer_session_missing"
        return None

    hook_start_id = producer.get("hook_start_id")
    if hook_session and not isinstance(hook_start_id, str):
        return "producer_session_missing"
    session_payload = load_work_session_payload(target, session_id, hook_start_id if hook_session else None)
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

    computed_fp = git_status_snapshot.fingerprint(git)
    if computed_fp is None:
        # A start snapshot in an older or unreadable format cannot show the subject clean.
        return "producer_session_mismatch"
    if git_status_snapshot.subject_dirty(subject_path, git):
        if producer.get("subject_clean_at_start") is True:
            return "concurrent_session_ownership"
        return "subject_pre_existing_dirty"

    start_git = producer.get("start_git")
    if not isinstance(start_git, dict):
        return "producer_session_missing"
    stored_fp = start_git.get("dirty_files_sha256")
    if not isinstance(stored_fp, str) or not stored_fp or computed_fp is None or stored_fp != computed_fp:
        return "producer_session_mismatch"

    if subject_clean_at_start is not True:
        return "producer_session_missing"
    if hook_session:
        return hook_producer.ownership_reason(target, session_id, subject_path, hook_start_id)
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


def _historical_subject_hash(target: Path, baseline: str, patch_bytes: bytes, subject_path: str) -> str | None:
    """Reconstruct evidence in a private index, without reading live subject bytes."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="brigade-trial-index-") as staging:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(staging) / "index")}

            def git(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(
                    ["git", "-C", str(target), *args],
                    input=data,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=30,
                    check=False,
                )

            if git("read-tree", baseline).returncode != 0:
                return None
            if git("apply", "--cached", "--whitespace=nowarn", "-", data=patch_bytes).returncode != 0:
                return None
            result = git("show", ":" + subject_path)
            return _sha256_prefixed(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def subject_hash_for_path(target: Path, subject_path: str) -> str | None:
    path = target / subject_path
    try:
        return _sha256_prefixed(path.read_bytes())
    except OSError:
        return None


def verifier_session_id() -> str | None:
    """The verifying session: the hook-printed Claude session, else an explicit override, else the ambient one.

    One resolver names the Claude session for the verifier id and for the receipt's
    ``harness_session`` stamp (``agent_identity.claude_session_fingerprint``), so a
    typed verify that found its session through ``CLAUDE_CODE_SESSION_ID`` is the
    same session in both places and is never counted as an independent verifier of
    its own edit.
    """
    explicit = os.environ.get(outcome_attribution.CLAUDE_SESSION_ENV, "")
    if explicit and re.fullmatch(r"[0-9a-f]{16}", explicit):
        return explicit
    override = os.environ.get("BRIGADE_VERIFY_SESSION_ID", "").strip()
    return override or agent_identity.claude_session_fingerprint()


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
    base_source = manifest_binding.get("base_source")
    if base_source is not None and base_source not in {"default_branch", "no_remote"}:
        return False
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
    # The base commit moves with every merge to the default branch, so it is
    # recorded for audit and not compared. The payload hash above is what ties
    # a receipt to the manifest content that is trusted now.
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
    patch_bytes: bytes = b"",
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
        if error and "not found" in error:
            return "verifier_manifest_missing"
        if error:
            reason = error.split(":", 1)[0]
            if reason in {
                "unresolved_default_branch",
                "no_merge_base",
                "manifest_not_on_default_branch",
                "manifest_not_regular_file",
                "manifest_too_large",
                "manifest_invalid_utf8_json",
                "manifest_invalid_json",
                "manifest_blob_unreadable",
                "manifest_base_unreadable",
            }:
                return reason
        return "verifier_manifest_mismatch"

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
    if (
        manifest.binding_mode == "patch_backed"
        and manifest.scope_globs
        and _patch_outside_manifest_scope(patch_bytes, manifest)
    ):
        return "out_of_scope_changes"
    return None


def _patch_identity_complete(receipt: dict[str, Any]) -> bool:
    for key in ("baseline_commit", "tree_fingerprint", "changes_patch_sha256"):
        value = receipt.get(key)
        if not isinstance(value, str) or not value:
            return False
    return True


def project_trial(
    receipt: dict[str, Any],
    *,
    target: Path | None = None,
    patch_path: Path | None = None,
    require_live_subject: bool = True,
) -> TrialProjection:
    binding = _binding_dict(receipt)
    if binding is None:
        return TrialProjection(eligible=False, reason="unattributed", attributed=False)
    if _commands_missing_check_role(receipt):
        return TrialProjection(eligible=False, reason="missing_check_role", attributed=True)

    binding_mode = binding.get("binding_mode")
    patch_bytes = b""
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
                or hook_producer.same_session(producer_session, verifier_session)
            ):
                return TrialProjection(eligible=False, reason="verifier_not_independent", attributed=True)
            quarantine_reason = generated_patch_quarantine.generated_patch_eligibility_reason(binding, receipt)
            if quarantine_reason is not None:
                return TrialProjection(eligible=False, reason=quarantine_reason, attributed=True)
        if not require_live_subject:
            historical_hash = (
                _historical_subject_hash(resolved_target, receipt["baseline_commit"], patch_bytes, subject_path)
                if resolved_target is not None
                else None
            )
            if historical_hash is None or historical_hash != subject_hash:
                return TrialProjection(eligible=False, reason="subject_hash_mismatch", attributed=True)
        if resolved_target is not None and require_live_subject:
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

    manifest_reason = _validate_manifest_contract(receipt, binding, target=target, patch_bytes=patch_bytes)
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
