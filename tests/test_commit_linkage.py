"""Tests for post-commit linkage statement emitter and verifier (issue #1404, slice 3)."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import (
    agent_change,
    attestation,
    causal_receipt,
    commit_linkage,
    commit_linkage_verify,
    localio,
)

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for commit-linkage tests", allow_module_level=True)
if not shutil.which("git"):
    pytest.skip("git is required for commit-linkage tests", allow_module_level=True)

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_config(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)


def _git_commit(
    repo: Path,
    message: str,
    files: dict[str, str] | None = None,
    *,
    allow_empty: bool = False,
) -> str:
    if files:
        paths: list[str] = []
        for path, content in files.items():
            full_path = repo / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            paths.append(path)
        subprocess.run(["git", "-C", str(repo), "add", "--"] + paths, check=True)
    cmd = ["git", "-C", str(repo), "commit", "-q", "-m", message]
    if allow_empty or not files:
        cmd.append("--allow-empty")
    subprocess.run(cmd, check=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_tree(repo: Path, sha: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _workspace(
    tmp_path: Path,
    run_id: str = "run-001",
    *,
    tree: str | None = None,
    baseline_commit: str | None = None,
    initial_files: dict[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    _git_config(ws)
    if initial_files is not None:
        for path, content in initial_files.items():
            (ws / path).parent.mkdir(parents=True, exist_ok=True)
            (ws / path).write_text(content, encoding="utf-8")
    else:
        (ws / "src.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "baseline"], check=True)
    if baseline_commit is None:
        baseline_commit = subprocess.run(
            ["git", "-C", str(ws), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    if tree is None:
        tree = localio.tree_fingerprint(ws)

    key_path, _ = attestation.keygen(ws, principal="test-signer")
    key_path.chmod(0o600)
    agent_change.init_policy(ws)

    run_dir = ws / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    run_json = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "task": "test task",
        "orchestrator": "cursor_worker",
        "worker": "codex_coder",
        "tree_fingerprint": tree,
        "baseline_commit": baseline_commit,
        "started_at": "2026-09-06T00:00:00.000000Z",
        "status_started_at": "2026-09-06T00:00:00.000000Z",
        "status": "running",
        "dry_run": False,
        "read_only": False,
        "suspected_noop": False,
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    roster = {
        "schema": "brigade.roster_snapshot.v1",
        "schema_version": 1,
        "orchestrator": "cursor_worker",
        "max_workers": 1,
        "timeout_seconds": 300,
        "allow_models": ["gpt-4"],
        "sandbox": None,
        "agents": {
            "cursor_worker": {"cli": "cursor", "model": "cursor-unknown", "role": "orchestrator"},
            "codex_coder": {"cli": "codex", "model": "codex-coder", "role": "worker"},
        },
    }
    (run_dir / "roster.json").write_text(
        json.dumps(roster, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return ws, run_dir, key_path


def _decode_statement(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = base64.b64decode(envelope["payload"])
    return json.loads(payload)


def _decode_predicate(envelope: dict[str, Any]) -> dict[str, Any]:
    return _decode_statement(envelope)["predicate"]


def _export(
    ws: Path,
    run_id: str,
    sha: str,
    key_path: Path,
    *,
    out: str | None = None,
    json_output: bool = True,
) -> tuple[int, dict[str, Any] | None]:
    rc = commit_linkage.export_commit_linkage(
        ws,
        run_id,
        sha,
        key=key_path,
        out=out,
        json_output=json_output,
    )
    if rc == 0 or rc == 3:
        if out == "-":
            return rc, None
        path = ws / ".brigade" / "runs" / run_id / "linkage" / f"{sha}.json"
        if path.is_file():
            envelope = json.loads(path.read_text(encoding="utf-8"))
            return rc, envelope
    return rc, None


def _verify_json(
    ws: Path,
    envelope_path: Path,
    *,
    commit: str | None = None,
) -> tuple[int, dict[str, Any]]:
    import io
    import sys as _sys

    old_stdout = _sys.stdout
    _sys.stdout = buffer = io.StringIO()
    try:
        rc = commit_linkage_verify.verify_commit_linkage(
            envelope_path,
            ws,
            commit=commit,
            json_output=True,
        )
    finally:
        _sys.stdout = old_stdout
    return rc, json.loads(buffer.getvalue())


def test_normal_commit_of_attested_tree_is_exact(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    # Commit with the same tree as the attested tree (empty diff, different commit).
    sha = _git_commit(ws, "same tree", allow_empty=True)
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0
    assert envelope is not None
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "exact"
    assert predicate["commitKind"] == "linear"
    assert predicate["commitParents"] == [{"gitCommit": predicate["baseline"]["gitCommit"]}]
    assert predicate["baseline"]["baselineRelation"] == "same-as-parent"
    assert predicate["baseline"]["baselineMoved"] is False

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-EXACT"
    assert output["commitTree"] == "match"
    assert output["equivalence"] == "confirmed"


def test_amend_with_same_tree_is_exact_with_new_sha(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    original = _git_commit(ws, "original", allow_empty=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "--amend", "--allow-empty", "-q", "-m", "amended"],
        check=True,
    )
    amended = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert amended != original

    rc, envelope = _export(ws, "run-001", amended, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "exact"
    statement = _decode_statement(envelope)
    assert statement["subject"][0]["digest"]["gitCommit"] == amended

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{amended}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-EXACT"


def test_squash_result_tree_equal_to_attested_tree_is_exact(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    _git_commit(ws, "first", allow_empty=True)
    _git_commit(ws, "second", allow_empty=True)
    # Squash the last two commits into one whose tree equals the baseline tree.
    subprocess.run(
        ["git", "-C", str(ws), "reset", "--soft", "HEAD~2"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-q", "-m", "squashed", "--allow-empty"],
        check=True,
    )
    squashed = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc, envelope = _export(ws, "run-001", squashed, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "exact"

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{squashed}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-EXACT"


def test_cherry_pick_onto_same_base_is_exact(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git_commit(ws, "branch-point", {"src.txt": "world"})
    branch_commit = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    branch_tree = _git_tree(ws, branch_commit)
    # The attested tree is the branch commit tree.
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_json["tree_fingerprint"] = branch_tree
    (run_dir / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(ws), "checkout", "-q", baseline],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "cherry-pick", branch_commit],
        check=True,
    )
    picked = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc, envelope = _export(ws, "run-001", picked, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "exact"
    assert predicate["baseline"]["baselineRelation"] == "same-as-parent"

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{picked}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-EXACT"


def test_rebase_onto_moved_base_is_none_with_baseline_moved(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Branch commit adds a new file.
    _git_commit(ws, "branch-point", {"child.txt": "x"})
    branch_commit = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Move the base forward on main.
    subprocess.run(
        ["git", "-C", str(ws), "checkout", "-q", baseline],
        check=True,
    )
    _git_commit(ws, "moved base", {"base.txt": "moved"})
    new_base = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Rebase the branch onto the new base.
    subprocess.run(
        ["git", "-C", str(ws), "rebase", "-q", "--onto", new_base, baseline, branch_commit],
        check=True,
    )
    rebased = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Set attested tree to the original branch commit tree.
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_json["tree_fingerprint"] = _git_tree(ws, branch_commit)
    (run_dir / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    rc, envelope = _export(ws, "run-001", rebased, key_path)
    assert rc == 3
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "none"
    assert predicate["baseline"]["baselineMoved"] is True
    assert predicate["baseline"]["baselineRelation"] == "ancestor-of-parent"

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{rebased}.json")
    assert verify_rc == 1
    assert output["status"] == "NOT-EQUIVALENT"


def test_exclusion_change_with_run_baseline_is_normalized_and_linked_normalized(tmp_path: Path) -> None:
    exclusion_path = ".brigade/work/miseledger-export-cursor.json"
    ws, run_dir, key_path = _workspace(
        tmp_path,
        initial_files={
            "src.txt": "hello",
            exclusion_path: "baseline-content",
        },
    )
    baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Change only the exclusion path; the normalized tree should equal the baseline tree.
    child = _git_commit(ws, "exclusion-change", {exclusion_path: "changed-content"})

    rc, envelope = _export(ws, "run-001", child, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "normalized"
    assert predicate["comparison"]["normalizationBase"]["source"] == "run-baseline"
    assert predicate["comparison"]["normalizedCommitTree"]["gitTree"] == _git_tree(ws, baseline)
    assert predicate["baseline"]["baselineMoved"] is False

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{child}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-NORMALIZED"
    assert output["normalizedTree"] == "match"
    assert output["ruleDrift"] is False


def test_exclusion_change_with_assumed_base_is_normalized_but_not_equivalent(tmp_path: Path) -> None:
    exclusion_path = ".brigade/work/miseledger-export-cursor.json"
    ws, run_dir, key_path = _workspace(
        tmp_path,
        initial_files={
            "src.txt": "hello",
            exclusion_path: "baseline-content",
        },
    )
    _baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Add an intermediate commit on a different path so the exclusion-change parent is not the baseline,
    # but the exclusion file still matches the baseline content.
    _git_commit(ws, "intermediate", {"src.txt": "changed"})
    child = _git_commit(ws, "exclusion-change", {exclusion_path: "changed-content"})

    rc, envelope = _export(ws, "run-001", child, key_path)
    assert rc == 3
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "none"
    assert predicate["comparison"]["normalizationBase"]["source"] == "first-parent-assumed"
    assert predicate["baseline"]["baselineMoved"] is True

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{child}.json")
    assert verify_rc == 1
    assert output["status"] == "NOT-EQUIVALENT"
    assert output["normalizedTree"] == "mismatch"


def test_root_commit_records_normalized_tree_unavailable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    _git_config(ws)
    (ws / "src.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "root"], check=True)
    tree = localio.tree_fingerprint(ws)
    key_path, _ = attestation.keygen(ws, principal="test-signer")
    key_path.chmod(0o600)
    agent_change.init_policy(ws)
    run_dir = ws / ".brigade" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run_json = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "task": "test task",
        "orchestrator": "cursor_worker",
        "worker": "codex_coder",
        "tree_fingerprint": tree,
        "baseline_commit": None,
        "started_at": "2026-09-06T00:00:00.000000Z",
        "status_started_at": "2026-09-06T00:00:00.000000Z",
        "status": "running",
        "dry_run": False,
        "read_only": False,
        "suspected_noop": False,
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "roster.json").write_text(
        json.dumps(
            {
                "schema": "brigade.roster_snapshot.v1",
                "schema_version": 1,
                "orchestrator": "cursor_worker",
                "max_workers": 1,
                "timeout_seconds": 300,
                "allow_models": ["gpt-4"],
                "sandbox": None,
                "agents": {
                    "cursor_worker": {"cli": "cursor", "model": "cursor-unknown", "role": "orchestrator"},
                    "codex_coder": {"cli": "codex", "model": "codex-coder", "role": "worker"},
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["commitKind"] == "root"
    assert predicate["commitParents"] == []
    assert predicate["comparison"]["normalizationBase"] == {"status": "unavailable"}
    assert predicate["comparison"]["normalizedCommitTree"] == {"status": "unavailable"}
    assert predicate["equivalence"] == "exact"

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 0
    assert output["status"] == "LINKED-EXACT"
    assert output["baselineRelation"] == "unavailable"


def test_mismatched_object_format_sha_is_refused(tmp_path: Path) -> None:
    ws, _run_dir, key_path = _workspace(tmp_path)
    # Default git object format is sha1; pass a 64-character sha.
    sha64 = "a" * 64
    with pytest.raises(commit_linkage.CommitLinkageError, match="commit sha must be 40 lowercase hex"):
        commit_linkage.build_statement(ws, "run-001", sha64)
    rc = commit_linkage.export_commit_linkage(ws, "run-001", sha64, key=key_path)
    assert rc == 2


def test_missing_policy_refuses_export(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    _git_config(ws)
    (ws / "src.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "baseline"], check=True)
    key_path, _ = attestation.keygen(ws, principal="test-signer")
    key_path.chmod(0o600)
    # No policy file.
    sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with pytest.raises(commit_linkage.CommitLinkageError, match="agent-change policy file is missing"):
        commit_linkage.build_statement(ws, "run-001", sha)
    rc = commit_linkage.export_commit_linkage(ws, "run-001", sha, key=key_path)
    assert rc == 2


def test_none_equivalence_still_writes_envelope_and_exits_nonzero(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    _git_commit(ws, "different", {"src.txt": "world"})
    sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 3
    assert envelope is not None
    predicate = _decode_predicate(envelope)
    assert predicate["equivalence"] == "none"
    assert (run_dir / "linkage" / f"{sha}.json").is_file()


def test_merge_commit_records_merge_kind_and_parents(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git_commit(ws, "branch1", {"a.txt": "a"})
    parent1 = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(ws), "checkout", "-q", baseline],
        check=True,
    )
    _git_commit(ws, "branch2", {"b.txt": "b"})
    parent2 = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(ws), "merge", "-q", "--no-ff", "-m", "merge", parent1],
        check=True,
    )
    merge_sha = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc, envelope = _export(ws, "run-001", merge_sha, key_path)
    assert rc == 3
    predicate = _decode_predicate(envelope)
    assert predicate["commitKind"] == "merge"
    assert len(predicate["commitParents"]) == 2
    parent_shas = {p["gitCommit"] for p in predicate["commitParents"]}
    assert parent1 in parent_shas
    assert parent2 in parent_shas


def test_tampered_attested_tree_reports_run_binding_conflicted(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "same tree", allow_empty=True)
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0

    statement = _decode_statement(envelope)
    statement["predicate"]["attestedTree"]["gitTree"] = "0" * 40
    # Re-sign the tampered statement.
    tampered_envelope = attestation.create_envelope(statement, key_path)
    tampered_path = run_dir / "linkage" / "tampered.json"
    tampered_path.write_text(
        json.dumps(tampered_envelope, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    verify_rc, output = _verify_json(ws, tampered_path)
    assert verify_rc == 1
    assert output["status"] in {"INVALID", "UNVERIFIABLE"}
    assert output["runBinding"] == "conflicted"


def test_deleted_commit_object_reports_commit_available_missing(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "to-delete", allow_empty=True)
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0

    # Amend the commit away so the original sha object is unreachable and prune it.
    subprocess.run(
        ["git", "-C", str(ws), "commit", "--amend", "--allow-empty", "-q", "-m", "replaced"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "reflog", "expire", "--expire=now", "--all"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ws), "gc", "--prune=now", "-q"],
        check=True,
    )

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 1
    assert output["commitAvailable"] == "missing"


def test_foreign_exclusion_list_reports_rule_drift(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "same tree", allow_empty=True)
    statement = commit_linkage.build_statement(ws, "run-001", sha)
    # Mutate the exclusion list to a foreign value.
    statement["predicate"]["comparison"]["exclusions"] = ["foreign-path"]
    envelope = attestation.create_envelope(statement, key_path)
    foreign_path = run_dir / "linkage" / "foreign.json"
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_path.write_text(
        json.dumps(envelope, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    verify_rc, output = _verify_json(ws, foreign_path)
    assert output["ruleDrift"] is True
    assert output["status"] in {"LINKED-EXACT", "NOT-EQUIVALENT"}


def test_trailer_present_and_matching_reports_run_matches_true(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    receipt_digest = causal_receipt.receipt_digest(run_json)
    _git_commit(ws, "with trailers", allow_empty=True)
    msg = f"with trailers\n\nBrigade-Run: run-001\nBrigade-Receipt: sha256:{receipt_digest}\n"
    msg_file = ws / "commit-msg.txt"
    msg_file.write_text(msg, encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(ws),
            "commit",
            "--amend",
            "--allow-empty",
            "-q",
            "--file",
            str(msg_file),
        ],
        check=True,
    )
    amended = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    rc, envelope = _export(ws, "run-001", amended, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    trailers = predicate["trailers"]
    assert trailers["run"] == "run-001"
    assert trailers["runMatches"] is True
    assert trailers["receiptResolves"] is True


def test_verifier_refuses_non_git_target(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "same tree", allow_empty=True)
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0

    not_git = tmp_path / "not-git"
    not_git.mkdir()
    verify_rc = commit_linkage_verify.verify_commit_linkage(
        run_dir / "linkage" / f"{sha}.json",
        not_git,
        json_output=False,
    )
    assert verify_rc == 2


def test_no_private_paths_author_or_message_in_statement_or_output(
    tmp_path: Path,
) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(
        ws,
        "secret message from author Test User <test@example.com>",
        allow_empty=True,
    )
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0

    # The statement must not contain the commit message or author identity.
    payload_text = envelope["payload"]
    payload_decoded = base64.b64decode(payload_text).decode("utf-8")
    assert "secret message" not in payload_decoded
    assert "test@example.com" not in payload_decoded
    assert "Test User" not in payload_decoded
    assert str(ws) not in payload_decoded

    verify_rc, _output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 0


def test_json_sorted_and_uses_documented_schema_strings(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "same tree", allow_empty=True)
    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0
    statement = _decode_statement(envelope)
    assert statement["predicate"]["schemaVersion"] == 1
    assert statement["predicateType"] == commit_linkage.COMMIT_LINKAGE_PREDICATE_TYPE
    assert envelope["payloadType"] == "application/vnd.in-toto+json"

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 0
    assert output["schema"] == commit_linkage_verify.COMMIT_LINKAGE_VERIFICATION_SCHEMA
    keys = list(output.keys())
    assert keys == sorted(keys)


def test_verify_run_dir_with_two_linkages_without_commit_exits_with_message(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha1 = _git_commit(ws, "first", allow_empty=True)
    sha2 = _git_commit(ws, "second", allow_empty=True)
    rc1, _ = _export(ws, "run-001", sha1, key_path)
    rc2, _ = _export(ws, "run-001", sha2, key_path)
    assert rc1 == 0
    assert rc2 == 0

    import sys as _sys
    import io

    old_stderr = _sys.stderr
    _sys.stderr = buffer = io.StringIO()
    try:
        rc = commit_linkage_verify.verify_commit_linkage(run_dir, ws, json_output=False)
    finally:
        _sys.stderr = old_stderr
    stderr = buffer.getvalue()
    assert rc == 1
    assert "--commit is required when more than one linkage envelope exists" in stderr


def test_shallow_clone_marks_baseline_relation_unknown(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    # Add two commits so a depth-1 clone omits the baseline.
    _git_commit(ws, "first", {"src.txt": "one"})
    _git_commit(ws, "second", {"src.txt": "two"})
    head = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    baseline = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "HEAD~2"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{ws}", str(shallow)],
        check=True,
    )
    _git_config(shallow)
    # Copy the Brigade state into the shallow clone so the export can find the policy and key.
    shutil.copytree(ws / ".brigade", shallow / ".brigade", dirs_exist_ok=True)
    # Update the shallow clone's run.json baseline to the real baseline (omitted from history).
    run_json = json.loads((shallow / ".brigade" / "runs" / "run-001" / "run.json").read_text(encoding="utf-8"))
    run_json["baseline_commit"] = baseline
    (shallow / ".brigade" / "runs" / "run-001" / "run.json").write_text(
        json.dumps(run_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    rc, envelope = _export(shallow, "run-001", head, key_path)
    assert rc in {0, 3}
    predicate = _decode_predicate(envelope)
    assert predicate["git"]["shallow"] is True
    assert predicate["baseline"]["baselineRelation"] == "unknown"


def test_index_binding_bound_when_agent_change_envelope_present(tmp_path: Path) -> None:
    ws, run_dir, key_path = _workspace(tmp_path)
    sha = _git_commit(ws, "same tree", allow_empty=True)
    # Build an agent-change index and write it to the run dir.
    statement = agent_change.build_statement(ws, "run-001")
    agent_change_envelope = attestation.create_envelope(statement, key_path)
    (run_dir / "agent-change.json").write_text(
        json.dumps(agent_change_envelope, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    rc, envelope = _export(ws, "run-001", sha, key_path)
    assert rc == 0
    predicate = _decode_predicate(envelope)
    assert predicate["references"]["kind"] == "agent-change"
    assert predicate["references"].get("subjectTree") == predicate["attestedTree"]["gitTree"]

    verify_rc, output = _verify_json(ws, run_dir / "linkage" / f"{sha}.json")
    assert verify_rc == 0
    assert output["indexBinding"] == "bound"
