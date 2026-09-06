"""Post-commit linkage statement emitter (issue #1404, slice 3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    agent_change,
    agent_change_refs,
    attestation,
    attestation_input,
    causal_receipt,
    localio,
    receipts_trailer,
)

COMMIT_LINKAGE_PREDICATE_TYPE = "https://brigade.dev/attestation/commit-linkage/v1"

_HEX40_OR_64_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_REMOVE_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_LITERAL_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
}

_REMOVE_GIT_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


class CommitLinkageError(RuntimeError):
    """Commit-linkage statement construction or export failed."""


def _git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if k not in _REMOVE_GIT_ENV and not k.startswith(_REMOVE_GIT_ENV_PREFIXES)
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _git(
    target: Path,
    *args: str,
    env_extra: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run a bounded git command on *target* with a sanitized environment."""
    try:
        return subprocess.run(
            ["git", "-C", str(target), "--no-replace-objects", *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_git_env(env_extra),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_stdout_lines(result: subprocess.CompletedProcess[str] | None) -> list[str] | None:
    if result is None or result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _git_object_format(target: Path) -> str | None:
    result = _git(target, "rev-parse", "--show-object-format")
    lines = _git_stdout_lines(result)
    return lines[0] if lines else None


def _git_is_shallow(target: Path) -> bool | None:
    result = _git(target, "rev-parse", "--is-shallow-repository")
    lines = _git_stdout_lines(result)
    if lines is None:
        return None
    return lines[0].strip().lower() == "true"


def _validate_commit_sha(sha: str, object_format: str) -> None:
    if not isinstance(sha, str) or not sha:
        raise CommitLinkageError("commit sha must be a non-empty string")
    if object_format == "sha256":
        expected = 64
    elif object_format == "sha1":
        expected = 40
    else:
        raise CommitLinkageError(f"unsupported git object format: {object_format}")
    if len(sha) != expected or not _HEX40_OR_64_RE.fullmatch(sha):
        raise CommitLinkageError(f"commit sha must be {expected} lowercase hex characters for {object_format}")


def _commit_tree(target: Path, sha: str) -> str | None:
    result = _git(target, "rev-parse", f"{sha}^{{tree}}")
    lines = _git_stdout_lines(result)
    return lines[0] if lines else None


def _commit_parents(target: Path, sha: str, shallow: bool) -> list[str] | None:
    """Return ordered parent commit hashes, or None only at a shallow boundary.

    In a shallow repository, a commit whose parents are cut off by the
    shallow boundary is reported as ``None`` (unknown) rather than guessed.
    Any other failure in a non-shallow repository is refused so it is never
    mistaken for a shallow boundary.
    """
    result = _git(target, "rev-list", "--parents", "-1", sha)
    if result is None or result.returncode != 0:
        if shallow:
            return None
        raise CommitLinkageError("could not resolve commit parents")
    lines = _git_stdout_lines(result)
    if not lines:
        if shallow:
            return None
        raise CommitLinkageError("could not resolve commit parents")
    parts = lines[0].split()
    if not parts:
        if shallow:
            return None
        raise CommitLinkageError("could not resolve commit parents")
    parents = parts[1:]
    if shallow and not parents:
        return None
    return parents


def _commit_message(target: Path, sha: str) -> str | None:
    # Note: the commit sha is a revision argument, not a path, so it must
    # come before any "--" separator. The spec's literal "-- <sha>" form does
    # not work for git show / git log with a commit object.
    result = _git(target, "show", "-s", "--format=%B", sha)
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _commit_kind(parents: list[str]) -> str:
    if not parents:
        return "root"
    if len(parents) > 1:
        return "merge"
    return "linear"


def _normalize_commit_tree(
    target: Path,
    sha: str,
    base: str,
    exclusions: tuple[str, ...],
) -> str | None:
    """Compute the tree of *sha* with exclusion paths reset to *base*."""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="brigade-linkage-index-", suffix=".tmp")
        os.close(tmp_fd)
    except OSError:
        return None
    index_file = Path(tmp_path)
    env = {"GIT_INDEX_FILE": str(index_file)}
    try:
        read_tree = _git(target, "read-tree", "--", sha, env_extra=env)
        if read_tree is None or read_tree.returncode != 0:
            return None
        reset = _git(target, "reset", "-q", base, "--", *exclusions, env_extra=env)
        if reset is None or reset.returncode != 0:
            return None
        result = _git(target, "write-tree", env_extra=env)
        lines = _git_stdout_lines(result)
        return lines[0] if lines else None
    finally:
        index_file.unlink(missing_ok=True)


def _is_ancestor(target: Path, ancestor: str, descendant: str) -> bool | None:
    """Return True if *ancestor* is an ancestor of *descendant*, False if not, None on error."""
    result = _git(target, "merge-base", "--is-ancestor", "--", ancestor, descendant)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _baseline_relation(
    target: Path,
    baseline: str | None,
    first_parent: str | None,
    shallow: bool,
) -> tuple[str, bool | None]:
    """Return (baselineRelation, baselineMoved).

    Relation is one of: same-as-parent, ancestor-of-parent, unrelated,
    unknown. Any missing local information is recorded as ``unknown`` rather
    than guessed, especially at a shallow-repository boundary.
    """
    if baseline is None or first_parent is None:
        return "unknown", None
    if baseline == first_parent:
        return "same-as-parent", False
    is_ancestor = _is_ancestor(target, baseline, first_parent)
    if is_ancestor is True:
        return "ancestor-of-parent", True
    if is_ancestor is False:
        # In a shallow repository, exit 1 may mean the missing parent is not
        # an ancestor *or* is simply not available; never guess.
        if shallow:
            return "unknown", True
        return "unrelated", True
    return "unknown", True


def _load_run_json(run_dir: Path) -> dict[str, Any]:
    return agent_change._read_run_json(run_dir)


def _load_agent_change_index(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "agent-change.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return attestation_input.read_json_object(path)
    except (OSError, attestation_input.AttestationInputError):
        return None


def _index_reference(
    run_dir: Path,
    envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    if envelope is None:
        return {"status": "absent"}
    payload_b64 = envelope.get("payload")
    if not isinstance(payload_b64, str):
        return {"status": "absent"}
    try:
        payload_bytes = attestation_input.decode_dsse_base64(
            payload_b64,
            label="agent-change payload",
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError:
        return {"status": "absent"}
    statement = attestation_input.strict_json_loads(
        payload_bytes,
        max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
    )
    if not isinstance(statement, dict):
        return {"status": "absent"}
    subject_tree = agent_change_refs._extract_tree(statement)
    return {
        "kind": "agent-change",
        "payloadSha256": hashlib.sha256(payload_bytes).hexdigest(),
        "envelopeSha256": localio.canonical_json_digest(envelope),
        "subjectTree": subject_tree,
    }


def _trailer_observations(
    target: Path,
    run_dir: Path,
    run_id: str,
    sha: str,
) -> dict[str, Any]:
    message = _commit_message(target, sha)
    evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if message is None:
        return {
            "run": None,
            "receipt": None,
            "runMatches": None,
            "receiptResolves": None,
            "evaluatedAt": evaluated_at,
        }
    trailer_run_id, trailer_digest = receipts_trailer.parse_trailers(message)
    run_matches = None
    if trailer_run_id is not None:
        run_matches = trailer_run_id == run_id
    receipt_resolves = None
    if trailer_digest is not None:
        try:
            run_json = _load_run_json(run_dir)
            receipt_resolves = causal_receipt.receipt_digest(run_json) == trailer_digest
        except OSError:
            receipt_resolves = False
    return {
        "run": trailer_run_id,
        "receipt": trailer_digest,
        "runMatches": run_matches,
        "receiptResolves": receipt_resolves,
        "evaluatedAt": evaluated_at,
    }


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_statement(
    target: Path,
    run_id: str,
    commit_sha: str,
    *,
    policy_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the canonical commit-linkage statement for a run and a commit."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise CommitLinkageError(f"--target is not a directory: {target}")
    if not (target / ".git").exists() and not (target / ".git").is_file():
        raise CommitLinkageError("--target is not a git work tree")

    policy_path = (
        policy_path.expanduser().resolve() if policy_path is not None else agent_change.default_policy_path(target)
    )
    if not policy_path.is_file() or policy_path.is_symlink():
        raise CommitLinkageError("agent-change policy file is missing; run 'brigade receipts agent-change-policy init'")
    policy = agent_change._load_policy(policy_path)
    policy_digest = agent_change._policy_digest(policy)

    run_dir = agent_change._resolve_run_dir(target, run_id)
    run_meta = _load_run_json(run_dir)
    tree_fingerprint = run_meta.get("tree_fingerprint")
    if not isinstance(tree_fingerprint, str) or not _HEX40_OR_64_RE.fullmatch(tree_fingerprint):
        raise CommitLinkageError("run.json has no final tree_fingerprint")

    object_format = _git_object_format(target)
    if object_format is None:
        raise CommitLinkageError("could not determine git object format")
    _validate_commit_sha(commit_sha, object_format)

    shallow = _git_is_shallow(target)
    if shallow is None:
        shallow = True

    commit_object = _git(target, "cat-file", "-e", "--", f"{commit_sha}^{{commit}}")
    if commit_object is None or commit_object.returncode != 0:
        raise CommitLinkageError("commit not found or not a commit object")

    commit_tree = _commit_tree(target, commit_sha)
    if commit_tree is None:
        raise CommitLinkageError(f"could not resolve commit tree for {commit_sha}")
    parents = _commit_parents(target, commit_sha, shallow)
    if parents is None:
        # Shallow boundary: parents are unknown.
        commit_parents_obs: dict[str, Any] | list[dict[str, str]] = {"status": "unknown"}
        commit_kind = "unknown"
        first_parent = None
    else:
        commit_parents_obs = [{"gitCommit": p} for p in parents]
        commit_kind = _commit_kind(parents)
        first_parent = parents[0] if parents else None

    baseline_commit = run_meta.get("baseline_commit")
    baseline_commit = (
        baseline_commit if isinstance(baseline_commit, str) and _HEX40_OR_64_RE.fullmatch(baseline_commit) else None
    )
    baseline_relation, baseline_moved = _baseline_relation(target, baseline_commit, first_parent, shallow)

    if first_parent is None:
        normalization_base: dict[str, Any] = {"status": "unavailable"}
        normalized_tree: dict[str, Any] = {"status": "unavailable"}
    else:
        if baseline_commit is not None and baseline_commit == first_parent:
            normalization_base = {"gitCommit": first_parent, "source": "run-baseline"}
        else:
            normalization_base = {"gitCommit": first_parent, "source": "first-parent-assumed"}
        normalized_tree_value = _normalize_commit_tree(
            target,
            commit_sha,
            normalization_base["gitCommit"],
            localio.TREE_FINGERPRINT_EVIDENCE_PATHS,
        )
        if normalized_tree_value is None:
            normalized_tree = {"status": "unavailable"}
        else:
            normalized_tree = {"gitTree": normalized_tree_value}

    if commit_tree == tree_fingerprint:
        equivalence = "exact"
    elif isinstance(normalized_tree, dict) and normalized_tree.get("gitTree") == tree_fingerprint:
        equivalence = "normalized"
    else:
        equivalence = "none"

    report = agent_change._read_lifecycle_report(run_dir)
    events = report.events if report is not None else []
    journal_head = agent_change._journal_chain_head(events)

    agent_change_envelope = _load_agent_change_index(run_dir)
    index_ref = _index_reference(run_dir, agent_change_envelope)

    trailers = _trailer_observations(target, run_dir, run_id, commit_sha)

    emitted_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    emitted_at_str = emitted_at.isoformat().replace("+00:00", "Z")

    predicate: dict[str, Any] = {
        "schemaVersion": 1,
        "run": {
            "id": run_id,
            "journalChainHead": journal_head,
        },
        "attestedTree": {"gitTree": tree_fingerprint},
        "commitTree": {"gitTree": commit_tree},
        "commitParents": commit_parents_obs,
        "commitKind": commit_kind,
        "comparison": {
            "rule": "brigade.tree_fingerprint.v1",
            "exclusions": list(localio.TREE_FINGERPRINT_EVIDENCE_PATHS),
            "normalizationBase": normalization_base,
            "normalizedCommitTree": normalized_tree,
        },
        "equivalence": equivalence,
        "git": {
            "objectFormat": object_format,
            "shallow": shallow,
        },
        "references": index_ref,
        "trailers": trailers,
        "forgeEvidence": {"status": "not-included"},
        "emittedAt": emitted_at_str,
        "nonce": secrets.token_hex(16),
        "policy": {
            "name": agent_change.AGENT_CHANGE_POLICY_SCHEMA,
            "digest": {"sha256": policy_digest},
        },
        "project": {"scope": policy["project_scope"]},
        "signerIndependence": "shared-workspace-key",
    }
    if baseline_commit is not None:
        predicate["baseline"] = {
            "gitCommit": baseline_commit,
            "baselineRelation": baseline_relation,
            "baselineMoved": baseline_moved,
        }

    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {"name": "git:commit", "digest": {"gitCommit": commit_sha}},
        ],
        "predicateType": COMMIT_LINKAGE_PREDICATE_TYPE,
        "predicate": predicate,
    }


def _safe_linkage_path(run_dir: Path, commit_sha: str) -> Path | None:
    """Return the default linkage path only if it stays under the run directory.

    Refuses a symlinked run directory, linkage directory, or any intermediate
    component, using a per-component lstat check that does not follow links.
    """
    linkage_dir = run_dir / "linkage"
    out_path = linkage_dir / f"{commit_sha}.json"
    try:
        resolved_run_dir = run_dir.resolve()
        current = out_path
        while current != resolved_run_dir:
            if not current.is_relative_to(resolved_run_dir):
                return None
            if current.is_symlink():
                return None
            parent = current.parent
            if parent == current:
                return None
            current = parent
    except OSError:
        return None
    return out_path


def export_commit_linkage(
    target: Path,
    run_id: str,
    commit_sha: str,
    *,
    key: Path | None = None,
    policy: Path | None = None,
    out: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    """CLI handler for 'brigade receipts export commit-linkage'."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if not agent_change._RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        print(
            "error: run id must contain only letters, digits, dot, underscore, or hyphen",
            file=sys.stderr,
        )
        return 2

    key_path = attestation.resolve_signing_key_path(target, key_file=key)
    if not key_path.is_file():
        print(f"error: signing key not found: {key_path}", file=sys.stderr)
        return 1

    policy_path = policy.expanduser().resolve() if policy is not None else None

    try:
        statement = build_statement(target, run_id, commit_sha, policy_path=policy_path)
    except (CommitLinkageError, agent_change.AgentChangeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"error: {exc} (use --force to overwrite)", file=sys.stderr)
        return 1

    try:
        envelope = attestation.create_envelope(statement, key_path)
    except attestation.AttestationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if out == "-":
        print(json.dumps(envelope, indent=2, sort_keys=True))
        return 0 if statement["predicate"]["equivalence"] in {"exact", "normalized"} else 3

    out_path: Path | None
    if out is not None:
        out_path = Path(out).expanduser().resolve()
    else:
        try:
            run_dir = agent_change._resolve_run_dir(target, run_id)
        except agent_change.AgentChangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out_path = _safe_linkage_path(run_dir, commit_sha)
        if out_path is None:
            print("error: symlinked or out-of-run linkage path refused", file=sys.stderr)
            return 2

    assert out_path is not None
    try:
        attestation.write_attestation_file(envelope, out_path, force=force)
    except FileExistsError as exc:
        print(f"error: {exc} (use --force to overwrite)", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: failed to write commit-linkage envelope: {exc}", file=sys.stderr)
        return 1

    try:
        rel_path = str(out_path.relative_to(target))
    except ValueError:
        rel_path = str(out_path)

    if json_output:
        print(
            json.dumps(
                {
                    "schema": "brigade.commit_linkage_export_result.v1",
                    "status": statement["predicate"]["equivalence"],
                    "path": rel_path,
                    "run_id": run_id,
                    "commit": commit_sha,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"commit-linkage envelope: {rel_path}")
        if statement["predicate"]["equivalence"] == "none":
            print("warning: commit tree is not equivalent to the attested tree")

    return 0 if statement["predicate"]["equivalence"] in {"exact", "normalized"} else 3
