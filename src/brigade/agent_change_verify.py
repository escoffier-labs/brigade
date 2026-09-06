"""Agent-change evidence index verifier (issue #1404, slice 2)."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    agent_change as agent_change_mod,
    agent_request,
    approval,
    approval_v2,
    attestation,
    attestation_input,
    localio,
)

AGENT_CHANGE_VERIFICATION_SCHEMA = "brigade.agent_change_verification.v1"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_APPROVAL_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_VERIFY_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX40_OR_64_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class AgentChangeVerifyError(RuntimeError):
    """Agent-change verification failed."""


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _krl_path(target: Path) -> Path | None:
    path = attestation.default_revoked_keys_path(target)
    return path if path.is_file() else None


def _allowed_signers_path(target: Path) -> Path:
    return attestation.default_allowed_signers_path(target)


def _load_envelope(path: Path) -> tuple[dict[str, Any] | None, str]:
    if path.is_symlink() or not path.is_file():
        return None, "missing"
    try:
        envelope = attestation_input.read_json_object(path)
    except (OSError, attestation_input.AttestationInputError):
        return None, "unreadable"
    if not isinstance(envelope, dict):
        return None, "malformed"
    if not all(isinstance(envelope.get(k), str) for k in ("payloadType", "payload")) or not isinstance(
        envelope.get("signatures"), list
    ):
        return None, "malformed"
    return envelope, "ok"


def _verify_envelope_signature(
    envelope: Mapping[str, Any],
    target: Path,
    expected_predicate_type: str,
    receipt: Mapping[str, Any] | None = None,
) -> attestation.AttestationVerifyResult:
    krl = _krl_path(target)
    return attestation.verify_attestation(
        envelope,
        allowed_signers_path=_allowed_signers_path(target),
        target=target,
        krl_path=krl,
        expected_predicate_type=expected_predicate_type,
        require_receipt=receipt is not None,
        receipt=receipt,
    )


def _classify_signature_status(status: str | None) -> str:
    if status == attestation.STATUS_SIGNED_OK:
        return "valid"
    if status in {
        attestation.STATUS_SIGNATURE_MISMATCH,
        attestation.STATUS_SUBJECT_MISMATCH,
        attestation.STATUS_EVIDENCE_MISSING,
    }:
        return "invalid"
    return "unverifiable"


def _classify_trust_status(status: str | None, target: Path) -> str:
    if status == attestation.STATUS_SIGNED_OK:
        return "trusted"
    if not _allowed_signers_path(target).is_file():
        return "unknown"
    if status == attestation.STATUS_UNTRUSTED_KEY:
        return "untrusted"
    return "unknown"


def _freshness(target: Path) -> str:
    if _krl_path(target) is not None:
        return "revocation-checked"
    return "revocation-absent"


def _decode_payload_bytes(envelope: Mapping[str, Any]) -> tuple[bytes | None, dict[str, Any] | None]:
    payload_b64 = envelope.get("payload")
    if not isinstance(payload_b64, str):
        return None, None
    try:
        payload_bytes = attestation_input.decode_dsse_base64(
            payload_b64,
            label="attestation payload",
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return None, None
    try:
        statement = attestation_input.strict_json_loads(
            payload_bytes,
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return payload_bytes, None
    return payload_bytes, statement if isinstance(statement, dict) else None


def _extract_index_tree(statement: Mapping[str, Any]) -> str | None:
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict) or subject.get("name") != "git:tree":
            continue
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            continue
        value = digest.get("gitTree")
        if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
            return value
    return None


def _extract_tree(statement: Mapping[str, Any] | None) -> str | None:
    if statement is None:
        return None
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        name = subject.get("name")
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            continue
        if name == "git:tree":
            value = digest.get("gitTree")
            if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
                return value
        elif name == "git:baseline":
            value = digest.get("gitCommit")
            if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
                return value
    return None


def _extract_baseline(statement: Mapping[str, Any] | None) -> str | None:
    if statement is None:
        return None
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict) or subject.get("name") != "git:baseline":
            continue
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            continue
        value = digest.get("gitCommit")
        if isinstance(value, str) and _HEX40_OR_64_RE.fullmatch(value):
            return value
    return None


def _policy_digest(policy: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_policy(target: Path, policy_path: Path | None) -> tuple[dict[str, Any] | None, str, str]:
    if policy_path is None:
        policy_path = agent_change_mod.default_policy_path(target)
    else:
        policy_path = policy_path.expanduser().resolve()
    if policy_path.is_symlink() or not policy_path.is_file():
        return None, str(policy_path), "unavailable"
    try:
        policy = attestation_input.read_json_object(policy_path)
    except (OSError, attestation_input.AttestationInputError):
        return None, str(policy_path), "unavailable"
    try:
        policy = agent_change_mod._validate_policy(policy)
    except agent_change_mod.AgentChangeError:
        return None, str(policy_path), "unavailable"
    return policy, str(policy_path), "present"


def _run_request_nonce(target: Path, run_id: str | None) -> str | None:
    if run_id is None:
        return None
    run_dir = (target / ".brigade" / "runs" / run_id).resolve()
    if run_dir.parent != (target / ".brigade" / "runs").resolve() or not run_dir.is_dir() or run_dir.is_symlink():
        return None
    try:
        run_meta = attestation_input.read_json_object(run_dir / "run.json")
    except (OSError, attestation_input.AttestationInputError):
        return None
    request = run_meta.get("request")
    if not isinstance(request, dict):
        return None
    nonce = request.get("nonce")
    if isinstance(nonce, str) and agent_change_mod._APPROVAL_NONCE_RE.fullmatch(nonce):
        return nonce
    return None


def _index_binding(
    statement: Mapping[str, Any] | None,
    target: Path,
    run_id: str | None,
) -> tuple[str, str | None]:
    if statement is None or run_id is None:
        return "unavailable", None
    tree = _extract_index_tree(statement)
    if tree is None:
        return "unavailable", None
    runs_root = (target / ".brigade" / "runs").resolve()
    run_dir = runs_root / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        return "unavailable", None
    try:
        run_meta = attestation_input.read_json_object(run_dir / "run.json")
    except (OSError, attestation_input.AttestationInputError):
        return "unavailable", None
    local_tree = run_meta.get("tree_fingerprint")
    if not isinstance(local_tree, str) or not _HEX40_OR_64_RE.fullmatch(local_tree):
        return "unavailable", None
    if local_tree == tree:
        return "bound", tree
    return "conflicted", tree


def _verify_index_envelope(
    envelope: Mapping[str, Any],
    target: Path,
) -> dict[str, Any]:
    payload_bytes, statement = _decode_payload_bytes(envelope)
    syntax = "wellformed" if statement is not None and payload_bytes is not None else "malformed"

    if syntax == "malformed":
        return {
            "syntax": "malformed",
            "signature": "unverifiable",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "unavailable",
            "policy": "unavailable",
        }

    result = _verify_envelope_signature(envelope, target, agent_change_mod.AGENT_CHANGE_PREDICATE_TYPE)
    signature = _classify_signature_status(result.status)
    trust = _classify_trust_status(result.status, target)
    run_id = None
    if isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            run_ref = predicate.get("run")
            if isinstance(run_ref, dict):
                run_id = run_ref.get("id")
    binding, _tree = _index_binding(statement, target, run_id if isinstance(run_id, str) else None)

    return {
        "syntax": syntax,
        "signature": signature,
        "trust": trust,
        "freshness": f"{_freshness(target)}, timestamp-absent",
        "binding": binding,
        "policy": "unavailable",
    }


def _reference_locator_to_path(
    locator: str,
    target: Path,
    run_id: str | None,
) -> Path | None:
    if ".." in locator.split("/"):
        return None
    if locator.startswith("/"):
        return None
    if locator.startswith(".brigade/runs/") and run_id is not None:
        prefix = f".brigade/runs/{run_id}/"
        if not locator.startswith(prefix):
            return None
        rest = locator[len(prefix) :]
        return (target / ".brigade" / "runs" / run_id / rest).resolve()
    if locator.startswith(".brigade/work/verify-runs/"):
        return (target / locator).resolve()
    return None


def _classify_availability(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return "missing"
    return "present"


def _verify_reference(
    ref: Mapping[str, Any],
    target: Path,
    run_id: str | None,
    index_tree: str | None,
    policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    locator = ref.get("locator")
    if not isinstance(locator, str):
        return {
            "kind": ref.get("kind"),
            "locator": None,
            "availability": "missing",
            "syntax": "malformed",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "not-applicable" if ref.get("kind") != "test-result" else "unevaluated",
        }

    path = _reference_locator_to_path(locator, target, run_id)
    if path is None:
        return {
            "kind": ref.get("kind"),
            "locator": locator,
            "availability": "partial",
            "syntax": "malformed",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "not-applicable" if ref.get("kind") != "test-result" else "unevaluated",
        }

    availability = _classify_availability(path)
    if availability != "present":
        return {
            "kind": ref.get("kind"),
            "locator": locator,
            "availability": availability,
            "syntax": "unchecked",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "not-applicable" if ref.get("kind") != "test-result" else "unevaluated",
        }

    envelope, load_status = _load_envelope(path)
    if envelope is None:
        return {
            "kind": ref.get("kind"),
            "locator": locator,
            "availability": "partial",
            "syntax": "malformed",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "not-applicable" if ref.get("kind") != "test-result" else "unevaluated",
        }

    payload_bytes, statement = _decode_payload_bytes(envelope)
    syntax = "wellformed" if statement is not None and payload_bytes is not None else "malformed"
    if syntax == "malformed":
        return {
            "kind": ref.get("kind"),
            "locator": locator,
            "availability": "present",
            "syntax": "malformed",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "not-applicable" if ref.get("kind") != "test-result" else "unevaluated",
        }

    expected_predicate = ref.get("predicateType")
    if expected_predicate not in {
        agent_request.AGENT_REQUEST_PREDICATE_TYPE,
        attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        approval.HUMAN_APPROVAL_PREDICATE_TYPE,
        approval_v2.HUMAN_APPROVAL_PREDICATE_TYPE,
    }:
        return {
            "kind": ref.get("kind"),
            "locator": locator,
            "availability": "present",
            "syntax": "wellformed",
            "cryptographic": "unchecked",
            "trust": "unknown",
            "freshness": f"{_freshness(target)}, timestamp-absent",
            "binding": "conflicted",
            "rederivation": "not-applicable",
            "policy_outcome": "unevaluated",
        }

    require_receipt = ref.get("kind") == "test-result"
    receipt: Mapping[str, Any] | None = None
    if require_receipt:
        # Locate the receipt.json sibling for re-derivation.
        verify_dir = path.parent
        receipt_path = verify_dir / "receipt.json"
        if receipt_path.is_file() and not receipt_path.is_symlink():
            try:
                receipt = attestation_input.read_json_object(receipt_path)
            except (OSError, attestation_input.AttestationInputError):
                receipt = None

    result = _verify_envelope_signature(envelope, target, expected_predicate, receipt=receipt)
    signature = _classify_signature_status(result.status)
    trust = _classify_trust_status(result.status, target)

    # Binding: local artifact digests must match reference, and tree/baseline must match index.
    local_payload_sha256 = hashlib.sha256(payload_bytes).hexdigest() if payload_bytes is not None else None
    local_envelope_sha256 = localio.canonical_json_digest(envelope)
    ref_payload = ref.get("payloadSha256")
    ref_envelope = ref.get("envelopeSha256")
    digests_match = (
        isinstance(ref_payload, str)
        and local_payload_sha256 == ref_payload
        and isinstance(ref_envelope, str)
        and local_envelope_sha256 == ref_envelope
    )

    binding = "conflicted"
    if ref.get("kind") == "agent-request":
        local_baseline = _extract_baseline(statement)
        ref_baseline = ref.get("subjectBaseline")
        expected_nonce = _run_request_nonce(target, run_id)
        ref_nonce = ref.get("nonce")
        if (
            digests_match
            and isinstance(local_baseline, str)
            and local_baseline == ref_baseline
            and isinstance(ref_nonce, str)
            and ref_nonce == expected_nonce
        ):
            binding = "bound"
    else:
        local_tree = _extract_tree(statement)
        if digests_match and isinstance(local_tree, str) and local_tree == index_tree:
            binding = "bound"

    rederivation = "not-applicable"
    if ref.get("kind") == "test-result":
        if result.rederived:
            rederivation = "reproduced"
        else:
            rederivation = "failed"

    policy_outcome = "not-applicable"
    if ref.get("kind") == "test-result":
        predicate = statement.get("predicate") if isinstance(statement, dict) else None
        result_value = predicate.get("result") if isinstance(predicate, dict) else None
        allowed_profiles = set(policy.get("allowed_profiles", [])) if policy is not None else set()
        profile_ok = ref.get("profile") in allowed_profiles
        policy_outcome = "unevaluated"
        if signature == "valid" and trust == "trusted" and binding == "bound" and rederivation == "reproduced":
            if result_value == "PASSED" and profile_ok:
                policy_outcome = "pass"
            else:
                policy_outcome = "fail"
        elif signature == "invalid" or trust == "untrusted":
            policy_outcome = "fail"

    return {
        "kind": ref.get("kind"),
        "locator": locator,
        "availability": "present",
        "syntax": syntax,
        "cryptographic": signature,
        "trust": trust,
        "freshness": f"{_freshness(target)}, timestamp-absent",
        "binding": binding,
        "rederivation": rederivation,
        "policy_outcome": policy_outcome,
    }


def _evaluate_required_set(
    policy: Mapping[str, Any] | None,
    references: Sequence[Mapping[str, Any]],
    verified_refs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if policy is None:
        return []
    required = policy.get("required_references", [])
    if not isinstance(required, list):
        return []
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for vref in verified_refs:
        kind = vref.get("kind", "")
        if isinstance(kind, str):
            by_kind.setdefault(kind, []).append(vref)
    observations: list[dict[str, Any]] = []
    for item in required:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str):
            continue
        min_count = item.get("min_count", 1)
        if not isinstance(min_count, int) or isinstance(min_count, bool) or min_count < 1:
            min_count = 1
        satisfied = [
            vref
            for vref in by_kind.get(kind, [])
            if vref.get("availability") == "present"
            and vref.get("syntax") == "wellformed"
            and vref.get("cryptographic") == "valid"
            and vref.get("trust") == "trusted"
            and vref.get("binding") == "bound"
            and (vref.get("rederivation") in {"reproduced", "not-applicable"})
            and vref.get("policy_outcome") in {"pass", "not-applicable"}
        ]
        if len(satisfied) >= min_count:
            observations.append(
                {
                    "kind": kind,
                    "status": "satisfied",
                    "count": len(satisfied),
                }
            )
        else:
            observations.append(
                {
                    "kind": kind,
                    "status": "missing",
                    "reason": f"required {min_count} verified {kind} reference(s), found {len(satisfied)}",
                    "count": len(satisfied),
                }
            )
    return observations


def _overall_status(
    index: Mapping[str, Any],
    required_set: Sequence[dict[str, Any]],
    references: Sequence[dict[str, Any]],
    policy_status: str,
    project_status: str,
) -> str:
    if index.get("syntax") == "malformed" or index.get("signature") == "unverifiable" or policy_status == "unavailable":
        return "UNVERIFIABLE"
    for req in required_set:
        if req.get("status") != "satisfied":
            return "INCOMPLETE"
    if index.get("signature") == "invalid" or policy_status == "mismatch" or project_status == "mismatch":
        return "INVALID"
    if index.get("trust") != "trusted" or index.get("binding") != "bound":
        return "INVALID"
    if index.get("policy") != "match":
        return "INVALID"
    for ref in references:
        if ref.get("kind") == "test-result" and ref.get("policy_outcome") == "fail":
            return "INVALID"
    return "COMPLETE-OK"


def verify_agent_change(
    path: Path,
    target: Path,
    *,
    policy: Path | None = None,
    json_output: bool = False,
) -> int:
    """CLI handler for 'brigade receipts verify-agent-change'."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    input_path = path.expanduser().resolve()
    envelope: dict[str, Any] | None = None
    run_id: str | None = None
    if input_path.is_dir():
        envelope_path = input_path / "agent-change.json"
        run_id = input_path.name
    else:
        envelope_path = input_path

    envelope, load_status = _load_envelope(envelope_path)
    if envelope is None:
        print(f"error: cannot read agent-change envelope: {envelope_path} ({load_status})", file=sys.stderr)
        return 2

    policy_obj, policy_path_str, policy_file_status = _load_policy(target, policy)
    policy_digest = _policy_digest(policy_obj) if policy_obj is not None else None
    policy_path_rel = policy_path_str
    try:
        policy_path_rel = str(Path(policy_path_str).relative_to(target))
    except ValueError:
        pass

    index_obs = _verify_index_envelope(envelope, target)

    # Determine run_id from the statement if we read a file directly.
    payload_bytes, statement = _decode_payload_bytes(envelope)
    if run_id is None and isinstance(statement, dict):
        predicate = statement.get("predicate")
        if isinstance(predicate, dict):
            run_ref = predicate.get("run")
            if isinstance(run_ref, dict):
                run_id = run_ref.get("id")
    index_tree = _extract_index_tree(statement) if statement is not None else None

    # Policy digest comparison.
    policy_status = "unavailable"
    if policy_obj is not None and isinstance(statement, dict):
        pred = statement.get("predicate")
        if isinstance(pred, dict):
            policy_field = pred.get("policy")
            if isinstance(policy_field, dict):
                policy_digest_field = policy_field.get("digest")
                if isinstance(policy_digest_field, dict):
                    stated_digest = policy_digest_field.get("sha256")
                    if stated_digest == policy_digest:
                        policy_status = "match"
                    elif isinstance(stated_digest, str):
                        policy_status = "mismatch"
    index_obs["policy"] = policy_status

    # Project scope comparison.
    project_status = "mismatch"
    if policy_obj is not None and isinstance(statement, dict):
        pred = statement.get("predicate")
        if isinstance(pred, dict):
            project_field = pred.get("project")
            if isinstance(project_field, dict):
                stated_scope = project_field.get("scope")
                if stated_scope == policy_obj.get("project_scope"):
                    project_status = "match"

    references = []
    if isinstance(statement, dict):
        pred = statement.get("predicate")
        if isinstance(pred, dict):
            raw_refs = pred.get("references")
            if isinstance(raw_refs, list):
                references = raw_refs

    verified_refs = [_verify_reference(ref, target, run_id, index_tree, policy_obj) for ref in references]
    required_set = _evaluate_required_set(policy_obj, references, verified_refs)
    status = _overall_status(index_obs, required_set, verified_refs, policy_status, project_status)

    evaluated_at = _utc_now_iso_z()
    output: dict[str, Any] = {
        "schema": AGENT_CHANGE_VERIFICATION_SCHEMA,
        "status": status,
        "evaluatedAt": evaluated_at,
        "policy": {
            "path": policy_path_rel,
            "status": policy_status,
            "digest": {"sha256": policy_digest} if policy_digest is not None else None,
        },
        "project": {
            "scope": policy_obj.get("project_scope") if policy_obj is not None else None,
            "status": project_status,
        },
        "index": index_obs,
        "references": verified_refs,
        "required_set": required_set,
        "disclaimer": "This is an audit observation, not an approval or release decision.",
    }
    if output["policy"]["digest"] is None:
        del output["policy"]["digest"]
    if output["project"]["scope"] is None:
        del output["project"]["scope"]

    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"status: {status}")
        print(f"policy: {policy_status} ({policy_path_rel})")
        print(f"project: {project_status}")
        print(f"index syntax: {index_obs['syntax']}")
        print(f"index signature: {index_obs['signature']}")
        print(f"index trust: {index_obs['trust']}")
        print(f"index binding: {index_obs['binding']}")
        print(f"references: {len(verified_refs)}")
        for req in required_set:
            print(f"required {req['kind']}: {req['status']}")
        print(output["disclaimer"])

    return 0 if status == "COMPLETE-OK" else 1
