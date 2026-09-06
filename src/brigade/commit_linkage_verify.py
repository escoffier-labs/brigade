"""Post-commit linkage statement verifier (issue #1404, slice 3)."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    agent_change,
    agent_change_refs,
    agent_change_verify,
    attestation,
    attestation_input,
    commit_linkage,
    localio,
)

COMMIT_LINKAGE_VERIFICATION_SCHEMA = "brigade.commit_linkage_verification.v1"

_STATUS_LINKED_EXACT = "LINKED-EXACT"
_STATUS_LINKED_NORMALIZED = "LINKED-NORMALIZED"
_STATUS_NOT_EQUIVALENT = "NOT-EQUIVALENT"
_STATUS_INVALID = "INVALID"
_STATUS_UNVERIFIABLE = "UNVERIFIABLE"


class CommitLinkageVerifyError(RuntimeError):
    """Commit-linkage verification failed."""


def _extract_commit_sha(statement: Mapping[str, Any]) -> str | None:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict) or subject.get("name") != "git:commit":
            continue
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            continue
        value = digest.get("gitCommit")
        if isinstance(value, str) and commit_linkage._HEX40_OR_64_RE.fullmatch(value):
            return value
    return None


def _extract_attested_tree(statement: Mapping[str, Any]) -> str | None:
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        return None
    attested = predicate.get("attestedTree")
    if not isinstance(attested, dict):
        return None
    value = attested.get("gitTree")
    if isinstance(value, str) and commit_linkage._HEX40_OR_64_RE.fullmatch(value):
        return value
    return None


def _extract_predicate_field(statement: Mapping[str, Any], *path: str) -> Any:
    value: Any = statement.get("predicate")
    if not isinstance(value, dict):
        return None
    for key in path:
        value = value.get(key)
        if not isinstance(value, dict):
            return None
    return value


def _find_linkage_envelope(
    input_path: Path,
    requested_commit: str | None,
) -> tuple[Path | None, str | None, str | None]:
    """Locate a single linkage envelope under *input_path* (or return the file).

    Returns (envelope_path, commit_sha, error_message).
    """
    if input_path.is_file():
        return input_path, requested_commit, None
    if not input_path.is_dir():
        return None, None, f"not a file or directory: {input_path}"

    linkage_dir = input_path / "linkage"
    if not linkage_dir.is_dir() or linkage_dir.is_symlink():
        return None, None, f"linkage directory not found: {linkage_dir}"

    files = sorted(
        p for p in linkage_dir.iterdir() if p.is_file() and not p.is_symlink() and p.suffix == ".json"
    )
    if not files:
        return None, None, f"no linkage envelopes found in {linkage_dir}"

    if requested_commit is not None:
        candidate = linkage_dir / f"{requested_commit}.json"
        if candidate not in files:
            return None, None, f"no linkage envelope for commit {requested_commit} in {linkage_dir}"
        return candidate, requested_commit, None

    if len(files) > 1:
        return None, None, "--commit is required when more than one linkage envelope exists in the run directory"

    return files[0], None, None


def _load_linkage_envelope(path: Path) -> tuple[dict[str, Any] | None, str]:
    return agent_change_verify._load_envelope(path)


def _verify_index_reference(
    target: Path,
    run_id: str,
    reference: Mapping[str, Any],
) -> str:
    """Check the local agent-change envelope against the statement reference."""
    if reference.get("status") == "absent":
        return "absent"
    try:
        run_dir = agent_change._resolve_run_dir(target, run_id)
    except agent_change.AgentChangeError:
        return "conflicted"
    path = run_dir / "agent-change.json"
    if path.is_symlink() or not path.is_file():
        return "conflicted"
    try:
        envelope = attestation_input.read_json_object(path)
    except (OSError, attestation_input.AttestationInputError):
        return "conflicted"

    payload_b64 = envelope.get("payload")
    if not isinstance(payload_b64, str):
        return "conflicted"
    try:
        payload_bytes = attestation_input.decode_dsse_base64(
            payload_b64,
            label="agent-change payload",
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return "conflicted"
    statement = attestation_input.strict_json_loads(
        payload_bytes,
        max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
    )
    if not isinstance(statement, dict):
        return "conflicted"

    local_payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    local_envelope_sha256 = localio.canonical_json_digest(envelope)
    local_subject_tree = agent_change_refs._extract_tree(statement)

    ref_payload = reference.get("payloadSha256")
    ref_envelope = reference.get("envelopeSha256")
    ref_subject_tree = reference.get("subjectTree")

    if (
        isinstance(ref_payload, str)
        and local_payload_sha256 == ref_payload
        and isinstance(ref_envelope, str)
        and local_envelope_sha256 == ref_envelope
        and isinstance(ref_subject_tree, str)
        and ref_subject_tree == local_subject_tree
    ):
        return "bound"
    return "conflicted"


def _recompute_equivalence(
    target: Path,
    statement: Mapping[str, Any],
    commit_sha: str,
    attested_tree: str,
) -> tuple[str, str | None, str | None, bool]:
    """Recompute commitTree, normalizedTree, equivalence, and ruleDrift.

    Returns (commitTree, normalizedTree, equivalence, ruleDrift).
    """
    local_commit_tree = commit_linkage._commit_tree(target, commit_sha)
    if local_commit_tree is None:
        return None, None, None, False

    comparison = _extract_predicate_field(statement, "comparison")
    if not isinstance(comparison, dict):
        return local_commit_tree, None, None, False

    exclusions = comparison.get("exclusions")
    if not isinstance(exclusions, list) or not all(isinstance(e, str) for e in exclusions):
        return local_commit_tree, None, None, False
    tuple_exclusions = tuple(exclusions)
    rule_drift = tuple_exclusions != localio.TREE_FINGERPRINT_EVIDENCE_PATHS

    if local_commit_tree == attested_tree:
        return local_commit_tree, None, "exact", rule_drift

    normalization_base = comparison.get("normalizationBase")
    if not isinstance(normalization_base, dict):
        return local_commit_tree, None, None, rule_drift
    base_sha = normalization_base.get("gitCommit")
    if not isinstance(base_sha, str) or not commit_linkage._HEX40_OR_64_RE.fullmatch(base_sha):
        return local_commit_tree, None, None, rule_drift

    local_normalized_tree = commit_linkage._normalize_commit_tree(
        target, commit_sha, base_sha, tuple_exclusions
    )
    if local_normalized_tree is None:
        return local_commit_tree, None, None, rule_drift

    if local_normalized_tree == attested_tree:
        equivalence = "normalized"
    else:
        equivalence = "none"

    return local_commit_tree, local_normalized_tree, equivalence, rule_drift


def _verify_baseline_relation(
    target: Path,
    statement: Mapping[str, Any],
    commit_sha: str,
) -> str:
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        return "unavailable"
    baseline = predicate.get("baseline")
    if not isinstance(baseline, dict):
        return "unavailable"
    baseline_commit = baseline.get("gitCommit")
    stated_relation = baseline.get("baselineRelation")
    if not isinstance(baseline_commit, str) or not isinstance(stated_relation, str):
        return "unavailable"
    parents = commit_linkage._commit_parents(target, commit_sha)
    if parents is None:
        return "unavailable"
    first_parent = parents[0] if parents else None
    relation, _moved = commit_linkage._baseline_relation(target, baseline_commit, first_parent)
    if relation is None:
        return "unavailable"
    if relation == stated_relation:
        return "confirmed"
    return "contradicted"


def _evaluate_status(
    envelope: Mapping[str, Any],
    policy_status: str,
    project_status: str,
    run_binding: str,
    equivalence: str | None,
    equivalence_obs: str,
    normalization_base: Mapping[str, Any] | None,
) -> str:
    if envelope.get("syntax") == "malformed" or envelope.get("signature") == "unverifiable" or policy_status == "unavailable":
        return _STATUS_UNVERIFIABLE
    if (
        envelope.get("signature") == "invalid"
        or envelope.get("trust") != "trusted"
        or policy_status == "mismatch"
        or project_status == "mismatch"
    ):
        return _STATUS_INVALID
    if run_binding != "bound":
        return _STATUS_INVALID
    if equivalence is None or equivalence_obs in {"contradicted", "unavailable"}:
        return _STATUS_NOT_EQUIVALENT
    if equivalence == "exact":
        return _STATUS_LINKED_EXACT
    if equivalence == "normalized":
        if isinstance(normalization_base, dict) and normalization_base.get("source") == "run-baseline":
            return _STATUS_LINKED_NORMALIZED
        return _STATUS_NOT_EQUIVALENT
    return _STATUS_NOT_EQUIVALENT


def verify_commit_linkage(
    path: Path,
    target: Path,
    *,
    commit: str | None = None,
    json_output: bool = False,
) -> int:
    """CLI handler for 'brigade receipts verify-commit-linkage'."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if not (target / ".git").exists() and not (target / ".git").is_file():
        print(f"error: --target is not a git work tree: {target}", file=sys.stderr)
        return 2

    input_path = path.expanduser().resolve()
    envelope_path, requested_commit, error = _find_linkage_envelope(input_path, commit)
    if envelope_path is None:
        print(f"error: {error}", file=sys.stderr)
        return 2

    envelope, load_status = _load_linkage_envelope(envelope_path)
    if envelope is None:
        print(f"error: cannot read commit-linkage envelope: {envelope_path} ({load_status})", file=sys.stderr)
        return 2

    policy_obj, policy_path_str, policy_file_status = agent_change_verify._load_policy(target, None)
    policy_digest = agent_change_verify._policy_digest(policy_obj) if policy_obj is not None else None
    policy_path_rel = policy_path_str
    try:
        policy_path_rel = str(Path(policy_path_str).relative_to(target))
    except ValueError:
        pass

    payload_bytes, statement = agent_change_verify._decode_payload_bytes(envelope)
    syntax = "wellformed" if statement is not None and payload_bytes is not None else "malformed"

    if syntax == "malformed":
        output: dict[str, Any] = {
            "schema": COMMIT_LINKAGE_VERIFICATION_SCHEMA,
            "status": _STATUS_UNVERIFIABLE,
            "evaluatedAt": agent_change_verify._utc_now_iso_z(),
            "policy": {
                "path": policy_path_rel,
                "status": "unavailable",
                "digest": {"sha256": policy_digest} if policy_digest is not None else None,
            },
            "project": {
                "scope": policy_obj.get("project_scope") if policy_obj is not None else None,
                "status": "mismatch",
            },
            "envelope": {
                "syntax": "malformed",
                "signature": "unverifiable",
                "trust": "unknown",
                "freshness": f"{agent_change_verify._freshness(target)}, timestamp-absent",
                "policy": "unavailable",
                "project": "mismatch",
            },
            "commitAvailable": "missing",
            "objectFormat": "mismatch",
            "attestedTreeObject": "missing",
            "commitTree": "unavailable",
            "normalizedTree": "unavailable",
            "ruleDrift": False,
            "equivalence": "unavailable",
            "runBinding": "unavailable",
            "indexBinding": "absent",
            "baselineRelation": "unavailable",
            "disclaimer": "This confirms tree equivalence for one commit object and nothing about merge order, branch state, or review.",
        }
        if output["policy"]["digest"] is None:
            del output["policy"]["digest"]
        if output["project"]["scope"] is None:
            del output["project"]["scope"]
        if json_output:
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            print(f"status: {output['status']}")
            print(f"envelope syntax: malformed")
        return 1

    result = agent_change_verify._verify_envelope_signature(
        envelope, target, commit_linkage.COMMIT_LINKAGE_PREDICATE_TYPE
    )
    signature = agent_change_verify._classify_signature_status(result.status)
    trust = agent_change_verify._classify_trust_status(result.status, target)

    envelope_obs = {
        "syntax": syntax,
        "signature": signature,
        "trust": trust,
        "freshness": f"{agent_change_verify._freshness(target)}, timestamp-absent",
        "policy": "unavailable",
        "project": "mismatch",
    }

    stated_policy_digest = None
    if isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            policy_field = predicate.get("policy")
            if isinstance(policy_field, dict):
                policy_digest_field = policy_field.get("digest")
                if isinstance(policy_digest_field, dict):
                    stated_policy_digest = policy_digest_field.get("sha256")
    if policy_digest is not None and stated_policy_digest == policy_digest:
        envelope_obs["policy"] = "match"
    elif isinstance(stated_policy_digest, str):
        envelope_obs["policy"] = "mismatch"

    project_status = "mismatch"
    if policy_obj is not None and isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            project_field = predicate.get("project")
            if isinstance(project_field, dict):
                stated_scope = project_field.get("scope")
                if stated_scope == policy_obj.get("project_scope"):
                    project_status = "match"
                    envelope_obs["project"] = "match"

    commit_sha = _extract_commit_sha(statement)
    attested_tree = _extract_attested_tree(statement)

    if requested_commit is not None and commit_sha != requested_commit:
        print("error: envelope subject does not match --commit", file=sys.stderr)
        return 2

    local_object_format = commit_linkage._git_object_format(target)
    stated_object_format = None
    if isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            git_field = predicate.get("git")
            if isinstance(git_field, dict):
                stated_object_format = git_field.get("objectFormat")
    object_format_status = "match"
    if local_object_format is None or stated_object_format != local_object_format:
        object_format_status = "mismatch"
    if commit_sha is not None and local_object_format is not None:
        expected_len = 64 if local_object_format == "sha256" else 40 if local_object_format == "sha1" else None
        if expected_len is not None and len(commit_sha) != expected_len:
            object_format_status = "mismatch"

    commit_available = "missing"
    if commit_sha is not None:
        result_cat = commit_linkage._git(target, "cat-file", "-e", "--", f"{commit_sha}^{{commit}}")
        if result_cat is not None and result_cat.returncode == 0:
            commit_available = "present"

    attested_tree_object = "missing"
    if attested_tree is not None:
        result_tree = commit_linkage._git(target, "cat-file", "-e", "--", f"{attested_tree}^{{tree}}")
        if result_tree is not None and result_tree.returncode == 0:
            attested_tree_object = "present"

    commit_tree_status = "unavailable"
    normalized_tree_status = "unavailable"
    equivalence_obs = "unavailable"
    rule_drift = False
    recomputed_commit_tree: str | None = None
    recomputed_normalized_tree: str | None = None
    recomputed_equivalence: str | None = None
    if commit_sha is not None and attested_tree is not None:
        recomputed_commit_tree, recomputed_normalized_tree, recomputed_equivalence, rule_drift = _recompute_equivalence(
            target, statement, commit_sha, attested_tree
        )
        if recomputed_commit_tree is not None:
            commit_tree_status = "match" if recomputed_commit_tree == attested_tree else "mismatch"
        if recomputed_normalized_tree is not None:
            normalized_tree_status = "match" if recomputed_normalized_tree == attested_tree else "mismatch"
        if recomputed_equivalence is not None:
            stated_equivalence = None
            if isinstance(statement, dict):
                predicate = statement.get("predicate")
                if isinstance(predicate, dict):
                    stated_equivalence = predicate.get("equivalence")
            if stated_equivalence == recomputed_equivalence:
                equivalence_obs = "confirmed"
            else:
                equivalence_obs = "contradicted"

    run_binding = "unavailable"
    if isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            run_ref = predicate.get("run")
            if isinstance(run_ref, dict):
                run_id = run_ref.get("id")
                if isinstance(run_id, str) and agent_change._RUN_ID_RE.fullmatch(run_id) and run_id not in {".", ".."}:
                    try:
                        run_dir = agent_change._resolve_run_dir(target, run_id)
                        run_meta = agent_change._read_run_json(run_dir)
                        local_tree = run_meta.get("tree_fingerprint")
                        if isinstance(local_tree, str) and commit_linkage._HEX40_OR_64_RE.fullmatch(local_tree):
                            if attested_tree == local_tree:
                                run_binding = "bound"
                            else:
                                run_binding = "conflicted"
                    except agent_change.AgentChangeError:
                        pass

    index_binding = "absent"
    if isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            reference = predicate.get("references")
            if isinstance(reference, dict):
                if reference.get("status") == "absent":
                    index_binding = "absent"
                else:
                    run_id = None
                    run_ref = predicate.get("run")
                    if isinstance(run_ref, dict):
                        run_id = run_ref.get("id")
                    if isinstance(run_id, str) and agent_change._RUN_ID_RE.fullmatch(run_id) and run_id not in {".", ".."}:
                        index_binding = _verify_index_reference(target, run_id, reference)
                    else:
                        index_binding = "conflicted"

    baseline_relation = "unavailable"
    if commit_sha is not None and isinstance(statement, dict):
        baseline_relation = _verify_baseline_relation(target, statement, commit_sha)

    normalization_base = _extract_predicate_field(statement, "comparison", "normalizationBase")

    status = _evaluate_status(
        envelope_obs,
        envelope_obs["policy"],
        project_status,
        run_binding,
        recomputed_equivalence,
        equivalence_obs,
        normalization_base,
    )

    evaluated_at = agent_change_verify._utc_now_iso_z()
    output = {
        "schema": COMMIT_LINKAGE_VERIFICATION_SCHEMA,
        "status": status,
        "evaluatedAt": evaluated_at,
        "policy": {
            "path": policy_path_rel,
            "status": envelope_obs["policy"],
            "digest": {"sha256": policy_digest} if policy_digest is not None else None,
        },
        "project": {
            "scope": policy_obj.get("project_scope") if policy_obj is not None else None,
            "status": project_status,
        },
        "envelope": envelope_obs,
        "commitAvailable": commit_available,
        "objectFormat": object_format_status,
        "attestedTreeObject": attested_tree_object,
        "commitTree": commit_tree_status,
        "normalizedTree": normalized_tree_status,
        "ruleDrift": rule_drift,
        "equivalence": equivalence_obs,
        "runBinding": run_binding,
        "indexBinding": index_binding,
        "baselineRelation": baseline_relation,
        "disclaimer": "This confirms tree equivalence for one commit object and nothing about merge order, branch state, or review.",
    }
    if output["policy"]["digest"] is None:
        del output["policy"]["digest"]
    if output["project"]["scope"] is None:
        del output["project"]["scope"]

    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"status: {status}")
        print(f"policy: {envelope_obs['policy']} ({policy_path_rel})")
        print(f"project: {project_status}")
        print(f"envelope syntax: {envelope_obs['syntax']}")
        print(f"envelope signature: {envelope_obs['signature']}")
        print(f"envelope trust: {envelope_obs['trust']}")
        print(f"commit available: {commit_available}")
        print(f"object format: {object_format_status}")
        print(f"attested tree object: {attested_tree_object}")
        print(f"commit tree: {commit_tree_status}")
        print(f"normalized tree: {normalized_tree_status}")
        print(f"equivalence: {equivalence_obs}")
        print(f"run binding: {run_binding}")
        print(f"index binding: {index_binding}")
        print(f"baseline relation: {baseline_relation}")
        print(output["disclaimer"])

    return 0 if status in {_STATUS_LINKED_EXACT, _STATUS_LINKED_NORMALIZED} else 1
