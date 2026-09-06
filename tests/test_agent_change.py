"""Tests for agent-change evidence index emitter and verifier (issue #1404, slice 2)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from brigade import (
    agent_change,
    agent_change_verify,
    approval,
    approval_v2,
    attestation,
    attestation_input,
    attestation_receipt,
    cli,
    localio,
    run_journal,
)

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for agent-change tests", allow_module_level=True)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _keygen(target: Path, principal: str = "test-signer") -> Path:
    key_path, _ = attestation.keygen(target, principal=principal)
    return key_path


def _key_fingerprint(key_path: Path) -> str:
    return attestation.get_key_fingerprint(key_path)


def _run_dir(target: Path, run_id: str, tree: str, *, request: dict[str, Any] | None = None) -> Path:
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    run_json = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "task": "test task",
        "orchestrator": "cursor_worker",
        "worker": "codex_coder",
        "tree_fingerprint": tree,
        "started_at": "2026-09-06T00:00:00.000000Z",
        "status_started_at": "2026-09-06T00:00:00.000000Z",
        "status": "running",
        "dry_run": False,
        "read_only": False,
        "suspected_noop": False,
    }
    if request is not None:
        run_json["requester_principal"] = request["requester_principal"]
        run_json["requester_keyid"] = request["requester_keyid"]
        run_json["request"] = {
            "nonce": request["nonce"],
            "attestation_path": request["attestation_path"],
            "task_sha256": request["task_sha256"],
            "statement_sha256": request["statement_sha256"],
            "baseline_commit": request["baseline_commit"],
        }
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2, sort_keys=True), encoding="utf-8")
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
    (run_dir / "roster.json").write_text(json.dumps(roster, indent=2, sort_keys=True), encoding="utf-8")
    return run_dir


def _signed_request(
    target: Path,
    run_dir: Path,
    key_path: Path,
    baseline_commit: str,
    task: str = "test task",
) -> dict[str, Any]:
    run_id = run_dir.name
    nonce = "01" * 16
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:baseline", "digest": {"gitCommit": baseline_commit}}],
        "predicateType": "https://brigade.dev/attestation/agent-request/v1",
        "predicate": {
            "schemaVersion": 1,
            "run": {"id": run_id},
            "taskSha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
            "requestedAt": "2026-09-06T00:00:00.000000Z",
            "nonce": nonce,
        },
    }
    envelope = attestation.create_envelope(statement, key_path)
    requests_dir = run_dir / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    request_path = requests_dir / f"{nonce}.json"
    request_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    principal = "test-signer"
    keyid = _key_fingerprint(key_path)
    statement_sha256 = hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest()
    task_sha256 = hashlib.sha256(task.encode("utf-8")).hexdigest()
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type="request.signed",
        payload={
            "requester_principal": principal,
            "requester_keyid": keyid,
            "baseline_commit": baseline_commit,
            "task_sha256": task_sha256,
            "nonce": nonce,
            "statement_sha256": statement_sha256,
            "attestation_path": f"requests/{nonce}.json",
        },
        idempotency_key=f"request:{nonce}",
        expected_previous_sequence=0,
        recorded_at="2026-09-06T00:00:00.000000Z",
    )
    return {
        "requester_principal": principal,
        "requester_keyid": keyid,
        "baseline_commit": baseline_commit,
        "task_sha256": task_sha256,
        "nonce": nonce,
        "statement_sha256": statement_sha256,
        "attestation_path": f"requests/{nonce}.json",
    }


def _test_result_envelope(
    target: Path,
    run_dir: Path,
    key_path: Path,
    verify_run_id: str,
    tree: str,
    baseline_commit: str,
    patch_sha256: str,
    result: str = "PASSED",
) -> Path:
    receipt = {
        "schema_version": 2,
        "run_id": verify_run_id,
        "target": str(target / "workspace"),
        "status": "completed",
        "started_at": "2026-09-06T12:00:00.000000Z",
        "completed_at": "2026-09-06T12:00:05.000000Z",
        "duration_seconds": 5.0,
        "path": str(target / ".brigade" / "work" / "verify-runs" / verify_run_id),
        "baseline_commit": baseline_commit,
        "tree_fingerprint": tree,
        "changes_patch_sha256": patch_sha256,
        "producer_run_id": run_dir.name,
        "commands": [
            {
                "command": "pytest -q tests/test_unit.py",
                "check_id": "unit-tests",
                "status": "completed",
                "exit_code": 0,
                "started_at": "2026-09-06T12:00:01.000000Z",
                "completed_at": "2026-09-06T12:00:04.000000Z",
                "stdout_summary": "1 passed in 0.1s",
            }
        ],
        "digests": {
            "algorithm": "sha256",
            "receipt_sha256": "",
        },
    }
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    verify_dir = target / ".brigade" / "work" / "verify-runs" / verify_run_id
    verify_dir.mkdir(parents=True)
    (verify_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    (verify_dir / "changes.patch").write_text("patch", encoding="utf-8")
    statement = attestation.build_statement(receipt)
    statement["predicate"]["result"] = result
    envelope = attestation.create_envelope(statement, key_path)
    (verify_dir / "attestation.json").write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return verify_dir


def _approval_envelope(
    run_dir: Path,
    key_path: Path,
    tree: str,
    nonce: str,
    journal_head: str,
    version: int = 2,
) -> Path:
    statement: dict[str, Any] = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:tree", "digest": {"gitTree": tree}}],
    }
    if version == 1:
        statement["predicateType"] = approval.HUMAN_APPROVAL_PREDICATE_TYPE
        statement["predicate"] = {
            "schemaVersion": 1,
            "run": {"id": run_dir.name, "journalChainHead": {"sha256": journal_head}},
            "decision": "allow",
            "scope": "run",
            "approver": {"principal": "test-signer", "keyid": _key_fingerprint(key_path), "kind": "human"},
            "decidedAt": "2026-09-06T13:00:00.000000Z",
            "expiresAt": "2027-09-06T13:00:00.000000Z",
            "nonce": nonce,
            "reasonCode": "reviewed-tests",
            "reason": "looks good",
            "policy": {"name": "brigade.sod.v1", "digest": {"sha256": "a" * 64}},
            "producers": [],
        }
    else:
        statement["predicateType"] = approval_v2.HUMAN_APPROVAL_PREDICATE_TYPE
        statement["predicate"] = {
            "schemaVersion": 2,
            "run": {"id": run_dir.name, "journalChainHead": {"sha256": journal_head}},
            "decision": "allow",
            "scope": "run",
            "approver": {"principal": "test-signer", "keyid": _key_fingerprint(key_path), "kind": "human"},
            "requester": {"status": "unknown"},
            "decidedAt": "2026-09-06T13:00:00.000000Z",
            "expiresAt": "2027-09-06T13:00:00.000000Z",
            "nonce": nonce,
            "reasonCode": "reviewed-tests",
            "reasonSha256": "b" * 64,
            "evidence": {"testResultPayloadSha256": [], "envelopes": []},
            "policy": {"name": "brigade.sod.v2", "digest": {"sha256": "c" * 64}},
        }
    envelope = attestation.create_envelope(statement, key_path)
    approvals_dir = run_dir / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    approval_path = approvals_dir / f"{nonce}.json"
    approval_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return approval_path


def _approval_envelope_baseline(
    run_dir: Path,
    key_path: Path,
    baseline_commit: str,
    nonce: str,
    journal_head: str,
) -> Path:
    statement: dict[str, Any] = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:baseline", "digest": {"gitCommit": baseline_commit}}],
        "predicateType": approval_v2.HUMAN_APPROVAL_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 2,
            "run": {"id": run_dir.name, "journalChainHead": {"sha256": journal_head}},
            "decision": "allow",
            "scope": "run",
            "approver": {"principal": "test-signer", "keyid": _key_fingerprint(key_path), "kind": "human"},
            "requester": {"status": "unknown"},
            "decidedAt": "2026-09-06T13:00:00.000000Z",
            "expiresAt": "2027-09-06T13:00:00.000000Z",
            "nonce": nonce,
            "reasonCode": "reviewed-tests",
            "reasonSha256": "b" * 64,
            "evidence": {"testResultPayloadSha256": [], "envelopes": []},
            "policy": {"name": "brigade.sod.v2", "digest": {"sha256": "c" * 64}},
        },
    }
    envelope = attestation.create_envelope(statement, key_path)
    approvals_dir = run_dir / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    approval_path = approvals_dir / f"{nonce}.json"
    approval_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return approval_path


def _append_approval_event(
    run_dir: Path, nonce: str, statement_sha256: str, expected_previous_sequence: int = 0
) -> None:
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "test-signer",
            "approver_keyid": "dummy",
            "subject_tree": "1111111111111111111111111111111111111111",
            "nonce": nonce,
            "decided_at": "2026-09-06T13:00:00.000000Z",
            "expires_at": "2027-09-06T13:00:00.000000Z",
            "statement_sha256": statement_sha256,
            "attestation_path": f"approvals/{nonce}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{nonce}",
        expected_previous_sequence=expected_previous_sequence,
        recorded_at="2026-09-06T13:00:00.000000Z",
    )


def test_policy_init_writes_default_policy(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    policy_path = agent_change.init_policy(target)
    assert policy_path.is_file()
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert policy["schema"] == agent_change.AGENT_CHANGE_POLICY_SCHEMA
    assert policy["schema_version"] == 1
    assert policy["required_references"] == [
        {"kind": "agent-request"},
        {"kind": "test-result", "min_count": 1},
        {"kind": "human-approval"},
    ]
    assert policy["allowed_profiles"] == [attestation.ATTESTATION_PROFILE]


def test_build_statement_refuses_without_policy_file(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    run_dir = _run_dir(target, "run-001", "1111111111111111111111111111111111111111")
    with pytest.raises(agent_change.AgentChangeError, match="agent-change policy file is missing"):
        agent_change.build_statement(target, run_dir.name)


def test_build_statement_refuses_without_terminal_tree(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    agent_change.init_policy(target)
    run_dir = target / ".brigade" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    run_json = {"schema": "brigade.run.v1", "schema_version": 1, "tree_fingerprint": None}
    (run_dir / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    with pytest.raises(agent_change.AgentChangeError, match="run.json has no final tree_fingerprint"):
        agent_change.build_statement(target, "run-001")


def _build_full_run(
    tmp_path: Path,
    *,
    tree: str = "1111111111111111111111111111111111111111",
    baseline_commit: str = "2222222222222222222222222222222222222222",
    patch_sha256: str = "3333333333333333333333333333333333333333333333333333333333333333",
    approval_version: int = 2,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    target = tmp_path / "ws"
    target.mkdir()
    agent_change.init_policy(target)
    key_path = _keygen(target)
    run_id = "run-001"
    run_dir = _run_dir(target, run_id, tree)
    request_info = _signed_request(target, run_dir, key_path, baseline_commit)
    # Update run.json with the request fields without recreating the directory.
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_json["requester_principal"] = request_info["requester_principal"]
    run_json["requester_keyid"] = request_info["requester_keyid"]
    run_json["request"] = {
        "nonce": request_info["nonce"],
        "attestation_path": request_info["attestation_path"],
        "task_sha256": request_info["task_sha256"],
        "statement_sha256": request_info["statement_sha256"],
        "baseline_commit": request_info["baseline_commit"],
    }
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2, sort_keys=True), encoding="utf-8")
    _test_result_envelope(target, run_dir, key_path, "verify-001", tree, baseline_commit, patch_sha256)
    approval_nonce = "02" * 16
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal_path,
        run_id=run_id,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "test-signer",
            "approver_keyid": _key_fingerprint(key_path),
            "subject_tree": tree,
            "nonce": approval_nonce,
            "decided_at": "2026-09-06T13:00:00.000000Z",
            "expires_at": "2027-09-06T13:00:00.000000Z",
            "statement_sha256": "a" * 64,
            "attestation_path": f"approvals/{approval_nonce}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{approval_nonce}",
        expected_previous_sequence=1,
        recorded_at="2026-09-06T13:00:00.000000Z",
    )
    _ = _approval_envelope(run_dir, key_path, tree, approval_nonce, "a" * 64, version=approval_version)
    return target, run_dir, key_path, request_info


def test_statement_determinism_with_fixed_inputs(tmp_path: Path) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    statement1 = agent_change.build_statement(target, run_dir.name)
    statement2 = agent_change.build_statement(target, run_dir.name)
    # Fields that are intentionally non-deterministic (nonce, emittedAt) are ignored.
    for field in ("nonce", "emittedAt"):
        statement1["predicate"][field] = "fixed"
        statement2["predicate"][field] = "fixed"
    b1 = attestation.canonical_statement_bytes(statement1)
    b2 = attestation.canonical_statement_bytes(statement2)
    assert b1 == b2


def test_statement_lists_references_with_correct_digests_and_sorted_order(tmp_path: Path) -> None:
    target, run_dir, key_path, request_info = _build_full_run(tmp_path)
    statement = agent_change.build_statement(target, run_dir.name)
    refs = statement["predicate"]["references"]
    kinds = [r["kind"] for r in refs]
    assert kinds == ["agent-request", "human-approval", "test-result"]
    req_ref = refs[0]
    assert req_ref["predicateType"] == "https://brigade.dev/attestation/agent-request/v1"
    assert req_ref["predicateVersion"] == "1"
    assert req_ref["subjectBaseline"] == request_info["baseline_commit"]
    assert req_ref["verified"] is True
    assert req_ref["locator"] == f".brigade/runs/{run_dir.name}/requests/{request_info['nonce']}.json"
    assert req_ref["nonce"] == request_info["nonce"]
    assert req_ref["taskSha256"] == request_info["task_sha256"]
    assert _HEX64_RE.match(req_ref["payloadSha256"])
    assert _HEX64_RE.match(req_ref["envelopeSha256"])
    assert req_ref["signerKeyids"] == [_key_fingerprint(key_path)]
    assert statement["predicate"]["request"]["nonce"] == request_info["nonce"]
    assert statement["predicate"]["request"]["taskSha256"] == request_info["task_sha256"]
    test_ref = refs[2]
    assert test_ref["predicateType"] == attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE
    assert test_ref["predicateVersion"] == "v0.1"
    assert test_ref["subjectTree"] == "1111111111111111111111111111111111111111"
    assert test_ref["result"] == "PASSED"
    assert test_ref["rederived"] is True
    assert test_ref["locator"] == ".brigade/work/verify-runs/verify-001/attestation.json"
    assert _HEX64_RE.match(test_ref["payloadSha256"])
    assert _HEX64_RE.match(test_ref["envelopeSha256"])
    assert test_ref["signerKeyids"] == [_key_fingerprint(key_path)]


def test_duplicate_payload_references_deduplicated_with_both_locators(tmp_path: Path) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    # Add a second approval envelope with the same canonical payload bytes but different file name.
    approval_path1 = run_dir / "approvals" / "02020202020202020202020202020202.json"
    approval_nonce2 = "03" * 16
    approval_path2 = run_dir / "approvals" / f"{approval_nonce2}.json"
    approval_path2.write_bytes(approval_path1.read_bytes())
    # Append a second approval event so both are considered non-latest and grouped by payload.
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "test-signer",
            "approver_keyid": _key_fingerprint(key_path),
            "subject_tree": "1111111111111111111111111111111111111111",
            "nonce": approval_nonce2,
            "decided_at": "2026-09-06T13:00:00.000000Z",
            "expires_at": "2027-09-06T13:00:00.000000Z",
            "statement_sha256": "a" * 64,
            "attestation_path": f"approvals/{approval_nonce2}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{approval_nonce2}",
        expected_previous_sequence=2,
        recorded_at="2026-09-06T13:00:01.000000Z",
    )
    statement = agent_change.build_statement(target, run_dir.name)
    approval_refs = [r for r in statement["predicate"]["references"] if r["kind"] == "human-approval"]
    # The two identical payloads should be collapsed to one reference with two locators.
    assert len(approval_refs) == 1
    locators = approval_refs[0].get("locators", [])
    assert sorted(locators) == sorted(
        [str(approval_path1.relative_to(target)), str(approval_path2.relative_to(target))]
    )


def test_unverifiable_reference_is_listed_with_verified_false(tmp_path: Path) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    # Corrupt the test-result attestation signature so verification fails.
    attestation_path = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "attestation.json"
    env = json.loads(attestation_path.read_text(encoding="utf-8"))
    env["signatures"][0]["sig"] = base64.b64encode(b"invalid").decode("ascii")
    attestation_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    statement = agent_change.build_statement(target, run_dir.name)
    test_ref = [r for r in statement["predicate"]["references"] if r["kind"] == "test-result"][0]
    assert test_ref["verified"] is False
    assert test_ref["signerKeyids"] == []
    assert isinstance(test_ref.get("reason"), str) and test_ref["reason"]


def test_missing_required_reference_recorded_complete_false_and_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    agent_change.init_policy(target)
    _ = _keygen(target)
    run_id = "run-001"
    tree = "1111111111111111111111111111111111111111"
    run_dir = _run_dir(target, run_id, tree)
    # No request, no test-result, no approval -> all required references missing.
    result = cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_id])
    assert result != 0
    out = capsys.readouterr().out
    assert "incomplete" in out.lower()
    index_path = run_dir / "agent-change.json"
    assert index_path.is_file()
    envelope = json.loads(index_path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(envelope["payload"]))
    assert payload["predicate"]["complete"] is False
    missing_kinds = {m["kind"] for m in payload["predicate"]["missing"]}
    assert missing_kinds == {"agent-request", "test-result", "human-approval"}


def test_roster_command_and_env_never_appear_in_statement(tmp_path: Path) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    statement = agent_change.build_statement(target, run_dir.name)
    statement_json = json.dumps(statement)
    assert '"command"' not in statement_json
    assert '"env"' not in statement_json
    assert "argv" not in statement_json


def test_symlinked_approvals_dir_is_refused(tmp_path: Path) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    approvals_dir = run_dir / "approvals"
    real_dir = tmp_path / "real-approvals"
    real_dir.mkdir()
    shutil.rmtree(approvals_dir)
    approvals_dir.symlink_to(real_dir)
    with pytest.raises(agent_change.AgentChangeError, match="approval directory must not contain symlinks"):
        agent_change.build_statement(target, run_dir.name)


def test_symlinked_verify_run_receipt_dir_is_refused(tmp_path: Path) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    verify_dir = target / ".brigade" / "work" / "verify-runs" / "verify-001"
    real_dir = tmp_path / "real-verify"
    real_dir.mkdir()
    shutil.rmtree(verify_dir)
    verify_dir.symlink_to(real_dir)
    with pytest.raises(agent_change.AgentChangeError, match="verify receipt directory must not be a symlink"):
        agent_change.build_statement(target, run_dir.name)


def test_approval_file_name_outside_pattern_is_skipped(tmp_path: Path) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    bad_path = run_dir / "approvals" / "bad-name.json"
    bad_path.write_text("{}", encoding="utf-8")
    statement = agent_change.build_statement(target, run_dir.name)
    approval_refs = [r for r in statement["predicate"]["references"] if r["kind"] == "human-approval"]
    assert len(approval_refs) == 1


def test_verifier_rejects_locator_with_dotdot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    # Manually inject a bad locator into the emitted statement.
    index_path = run_dir / "agent-change.json"
    env = json.loads(index_path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["references"][0]["locator"] = ".brigade/runs/../evil.json"
    env["payload"] = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = capsys.readouterr().out
    output = json.loads(out)
    assert output["schema"] == agent_change_verify.AGENT_CHANGE_VERIFICATION_SCHEMA
    assert output["references"][0]["availability"] == "partial"


def test_verifier_binding_conflicted_on_tampered_reference_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    # Tamper the request envelope bytes.
    request_path = run_dir / "requests" / "01010101010101010101010101010101.json"
    env = json.loads(request_path.read_text(encoding="utf-8"))
    env["signatures"][0]["sig"] = base64.b64encode(b"tampered").decode("ascii")
    request_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    req_ref = [r for r in out["references"] if r["kind"] == "agent-request"][0]
    assert req_ref["binding"] == "conflicted"


def test_verifier_binding_conflicted_for_test_result_on_other_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    # Re-sign the test-result attestation with a different subject tree so the reference is for another tree.
    att_path = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "attestation.json"
    att_env = json.loads(att_path.read_text(encoding="utf-8"))
    att_statement = json.loads(base64.b64decode(att_env["payload"]))
    att_statement["subject"][0]["digest"]["gitTree"] = "4444444444444444444444444444444444444444"
    att_path.write_text(
        json.dumps(attestation.create_envelope(att_statement, key_path), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    test_ref = [r for r in out["references"] if r["kind"] == "test-result"][0]
    assert out["index"]["binding"] == "bound"
    assert test_ref["binding"] == "conflicted"


def test_verifier_untrusted_signer_reports_untrusted_and_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    # Generate a second key outside the target's allowed_signers and re-sign the index.
    other_key_path = tmp_path / "other-signer-key"
    attestation.keygen(
        tmp_path,
        principal="other-signer",
        key_file=other_key_path,
        allowed_signers_file=tmp_path / "other-allowed-signers",
    )
    statement = agent_change.build_statement(target, run_dir.name)
    env = attestation.create_envelope(statement, other_key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["signature"] == "valid"
    assert out["index"]["trust"] == "untrusted"
    assert out["status"] == "INVALID"


def test_verifier_absent_allowed_signers_reports_trust_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    # Create a fresh target without the allowed_signers file.
    fresh_target = tmp_path / "fresh"
    fresh_target.mkdir()
    # Copy the policy and keys? No, we only need to verify the envelope signature; trust should be unknown.
    # We can still use the original policy file via --policy.
    policy_path = agent_change.default_policy_path(target)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(
        [
            "receipts",
            "verify-agent-change",
            str(index_path),
            "--target",
            str(fresh_target),
            "--policy",
            str(policy_path),
            "--json",
        ]
    )
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["trust"] == "unknown"


def test_verifier_reports_revocation_absent_when_no_krl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result == 0
    out = json.loads(capsys.readouterr().out)
    assert "revocation-absent" in out["index"]["freshness"]


def test_verifier_policy_digest_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    # Mutate the policy file so the digest no longer matches.
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["policy_version"] = 999
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["policy"] == "mismatch"


def test_verifier_project_scope_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["project_scope"] = "different-scope"
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"]["status"] == "mismatch"


def test_verifier_tampered_reference_takes_priority_over_missing_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    # Tamper the request envelope signature so the reference fails policy.
    request_path = run_dir / "requests" / "01010101010101010101010101010101.json"
    req_env = json.loads(request_path.read_text(encoding="utf-8"))
    req_env["signatures"][0]["sig"] = base64.b64encode(b"tampered").decode("ascii")
    request_path.write_text(json.dumps(req_env, indent=2, sort_keys=True), encoding="utf-8")
    # Require a second test-result so the required set is unmet.
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["required_references"] = [
        {"kind": "agent-request"},
        {"kind": "test-result", "min_count": 2},
        {"kind": "human-approval"},
    ]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "INVALID"
    req_ref = [r for r in out["references"] if r["kind"] == "agent-request"][0]
    assert req_ref["policy_outcome"] == "fail"


def test_verifier_project_scope_mismatch_takes_priority_over_missing_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["project_scope"] = "different-scope"
    policy["required_references"] = [
        {"kind": "agent-request"},
        {"kind": "test-result", "min_count": 2},
        {"kind": "human-approval"},
    ]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"]["status"] == "mismatch"
    assert out["status"] == "INVALID"


def test_verifier_missing_required_reports_incomplete_before_policy_digest_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["policy_version"] = 999
    policy["required_references"] = [
        {"kind": "agent-request"},
        {"kind": "test-result", "min_count": 2},
        {"kind": "human-approval"},
    ]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["policy"] == "mismatch"
    assert out["status"] == "INCOMPLETE"


def test_verifier_binding_unavailable_when_run_dir_absent_but_signature_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = tmp_path / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    # Remove the run directory so binding is unavailable but the signature still verifies.
    import shutil

    shutil.rmtree(run_dir)
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["signature"] == "valid"
    assert out["index"]["binding"] == "unavailable"


def test_verifier_rederivation_failed_when_receipt_digest_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    receipt_path = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["baseline_commit"] = "4444444444444444444444444444444444444444"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    test_ref = [r for r in out["references"] if r["kind"] == "test-result"][0]
    assert test_ref["rederivation"] == "failed"
    assert test_ref["reason"] == "receipt-digest-invalid"


def test_verifier_request_nonce_conflict_with_run_request_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    env = attestation.create_envelope(agent_change.build_statement(target, run_dir.name), key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    # Mutate the journal request.signed event nonce so it differs from the request reference.
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        event = json.loads(line)
        if event.get("event_type") == "request.signed":
            event["payload"]["nonce"] = "05050505050505050505050505050505"
        new_lines.append(json.dumps(event, sort_keys=True))
    journal_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    req_ref = [r for r in out["references"] if r["kind"] == "agent-request"][0]
    assert req_ref["binding"] == "conflicted"


def test_verifier_ignores_statement_complete_when_policy_requires_more(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    # Mutate the policy to require two test results.
    policy_path = agent_change.default_policy_path(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["required_references"] = [
        {"kind": "agent-request"},
        {"kind": "test-result", "min_count": 2},
        {"kind": "human-approval"},
    ]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "INCOMPLETE"


def test_complete_ok_round_trip_with_request_test_result_and_v2_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path, approval_version=2)
    result = cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name])
    assert result == 0
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == agent_change_verify.AGENT_CHANGE_VERIFICATION_SCHEMA
    assert out["status"] == "COMPLETE-OK"
    assert out["index"]["signature"] == "valid"
    assert out["index"]["trust"] == "trusted"
    assert out["index"]["binding"] == "bound"
    assert out["index"]["policy"] == "match"
    assert out["project"]["status"] == "match"


def test_no_absolute_path_env_argv_or_task_text_in_statement_or_verifier_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    statement = agent_change.build_statement(target, run_dir.name)
    statement_json = json.dumps(statement)
    assert str(tmp_path) not in statement_json
    assert str(target) not in statement_json
    assert "/workspace" not in statement_json
    assert "SECRET" not in statement_json
    assert "argv" not in statement_json
    assert "test task" not in statement_json

    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    out = capsys.readouterr().out
    assert str(tmp_path) not in out
    assert str(target) not in out
    assert "/workspace" not in out
    assert "SECRET" not in out
    assert "argv" not in out
    assert "test task" not in out


def test_cli_agent_change_policy_init_idempotent_with_force(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    assert cli.main(["receipts", "agent-change-policy", "init", "--target", str(target)]) == 0
    assert cli.main(["receipts", "agent-change-policy", "init", "--target", str(target)]) == 1
    assert cli.main(["receipts", "agent-change-policy", "init", "--target", str(target), "--force"]) == 0


def test_verifier_index_binding_conflicted_when_run_json_tree_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_json["tree_fingerprint"] = "4444444444444444444444444444444444444444"
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["binding"] == "conflicted"


def test_verifier_json_output_is_sorted_and_uses_documented_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result == 0
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert out["schema"] == agent_change_verify.AGENT_CHANGE_VERIFICATION_SCHEMA
    assert raw.strip() == json.dumps(out, indent=2, sort_keys=True)


def test_verifier_untrusted_reference_signer_reports_trust_untrusted_and_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    # Re-sign the test-result attestation with a key that is not in the target's allowed_signers.
    other_key_path = tmp_path / "other-signer-key"
    attestation.keygen(
        tmp_path,
        principal="other-signer",
        key_file=other_key_path,
        allowed_signers_file=tmp_path / "other-allowed-signers",
    )
    att_path = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "attestation.json"
    att_env = json.loads(att_path.read_text(encoding="utf-8"))
    att_statement = json.loads(base64.b64decode(att_env["payload"]))
    att_path.write_text(
        json.dumps(attestation.create_envelope(att_statement, other_key_path), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    test_ref = [r for r in out["references"] if r["kind"] == "test-result"][0]
    assert test_ref["trust"] == "untrusted"
    assert test_ref["policy_outcome"] == "fail"


def test_verifier_request_reference_nonce_differs_from_journal_nonce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    # Mutate the request envelope's nonce to a different value and re-sign.
    request_path = run_dir / "requests" / "01010101010101010101010101010101.json"
    env = json.loads(request_path.read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(env["payload"]))
    statement["predicate"]["nonce"] = "06060606060606060606060606060606"
    request_path.write_text(
        json.dumps(attestation.create_envelope(statement, key_path), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    # Re-export so the reference nonce reflects the mutated envelope.
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    req_ref = [r for r in out["references"] if r["kind"] == "agent-request"][0]
    assert req_ref["binding"] == "conflicted"


def test_verifier_rejects_traversal_run_id_and_does_not_read_outside_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    agent_change.init_policy(target)
    key_path = _keygen(target)
    run_id = "run-001"
    tree = "1111111111111111111111111111111111111111"
    run_dir = _run_dir(target, run_id, tree)
    # Canary file outside the target workspace; reading it as JSON would fail.
    canary = tmp_path / "canary"
    canary.write_text("not valid json {", encoding="utf-8")
    statement = agent_change.build_statement(target, run_id)
    statement["predicate"]["run"]["id"] = "../../../etc"
    env = attestation.create_envelope(statement, key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    read_paths: list[Path] = []
    original_read = attestation_input.read_json_object

    def _recording_read(path: Path, *, max_bytes: int = attestation_input.MAX_JSON_BYTES) -> dict[str, Any]:
        read_paths.append(Path(path).expanduser().resolve())
        return original_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(attestation_input, "read_json_object", _recording_read)
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["binding"] == "unavailable"
    target_resolved = target.resolve()
    for p in read_paths:
        assert p.resolve().is_relative_to(target_resolved), f"read outside target: {p}"


def test_verifier_refuses_symlinked_verify_run_attestation_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    attestation_path = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "attestation.json"
    real_path = tmp_path / "real-attestation.json"
    real_path.write_text(attestation_path.read_text(encoding="utf-8"), encoding="utf-8")
    attestation_path.unlink()
    attestation_path.symlink_to(real_path)
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    test_ref = [r for r in out["references"] if r["kind"] == "test-result"][0]
    assert test_ref["availability"] == "partial"
    assert test_ref["reason"] == "symlink-refused"


def test_verifier_refuses_symlinked_verify_run_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    root = target / ".brigade" / "work" / "verify-runs"
    verify_dir = root / "verify-001"
    real_dir = tmp_path / "real-verify"
    real_dir.mkdir()
    for child in list(verify_dir.iterdir()):
        shutil.move(str(child), str(real_dir / child.name))
    verify_dir.rmdir()
    verify_dir.symlink_to(real_dir)
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    test_ref = [r for r in out["references"] if r["kind"] == "test-result"][0]
    assert test_ref["availability"] == "partial"
    assert test_ref["reason"] == "symlink-refused"


def test_policy_without_agent_request_no_request_file_complete_true(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    policy_path = agent_change.init_policy(target)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["required_references"] = [
        {"kind": "test-result", "min_count": 1},
        {"kind": "human-approval"},
    ]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
    key_path = _keygen(target)
    run_id = "run-001"
    tree = "1111111111111111111111111111111111111111"
    baseline = "2222222222222222222222222222222222222222"
    patch = "3" * 64
    run_dir = _run_dir(target, run_id, tree, request=None)
    _test_result_envelope(target, run_dir, key_path, "verify-001", tree, baseline, patch)
    approval_nonce = "02" * 16
    _approval_envelope(run_dir, key_path, tree, approval_nonce, "a" * 64, version=2)
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "test-signer",
            "approver_keyid": _key_fingerprint(key_path),
            "subject_tree": tree,
            "nonce": approval_nonce,
            "decided_at": "2026-09-06T13:00:00.000000Z",
            "expires_at": "2027-09-06T13:00:00.000000Z",
            "statement_sha256": "a" * 64,
            "attestation_path": f"approvals/{approval_nonce}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{approval_nonce}",
        expected_previous_sequence=0,
        recorded_at="2026-09-06T13:00:00.000000Z",
    )
    result = cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_id, "--force"])
    assert result == 0
    out = capsys.readouterr().out
    assert "incomplete" not in out.lower()
    index_path = run_dir / "agent-change.json"
    envelope = json.loads(index_path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(envelope["payload"]))
    assert payload["predicate"]["complete"] is True
    assert not any(m["kind"] == "agent-request" for m in payload["predicate"]["missing"])
    assert payload["predicate"]["request"]["status"] == "absent"


def test_verify_reference_envelope_rederives_test_result_with_snapshot_receipt_even_if_receipt_json_missing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    key_path = _keygen(target)
    tree = "1111111111111111111111111111111111111111"
    run_dir = _run_dir(target, "run-001", tree)
    baseline = "2222222222222222222222222222222222222222"
    patch = "3" * 64
    verify_dir = _test_result_envelope(target, run_dir, key_path, "verify-001", tree, baseline, patch)
    receipt = json.loads((verify_dir / "receipt.json").read_text(encoding="utf-8"))
    snapshot = attestation_receipt.snapshot_stored_receipt(receipt)
    (verify_dir / "receipt.json").unlink()
    envelope = json.loads((verify_dir / "attestation.json").read_text(encoding="utf-8"))
    result, statement, payload_bytes = agent_change._verify_reference_envelope(
        envelope,
        target,
        attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        "Test Result",
        require_receipt=True,
        receipt=snapshot.receipt,
    )
    assert result.status == attestation.STATUS_SIGNED_OK
    assert result.rederived is True


def test_verify_reference_envelope_reports_rederivation_failed_when_snapshot_digest_differs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    key_path = _keygen(target)
    tree = "1111111111111111111111111111111111111111"
    run_dir = _run_dir(target, "run-001", tree)
    baseline = "2222222222222222222222222222222222222222"
    patch = "3" * 64
    verify_dir = _test_result_envelope(target, run_dir, key_path, "verify-001", tree, baseline, patch)
    receipt = json.loads((verify_dir / "receipt.json").read_text(encoding="utf-8"))
    receipt["baseline_commit"] = "4444444444444444444444444444444444444444"
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    snapshot = attestation_receipt.snapshot_stored_receipt(receipt)
    envelope = json.loads((verify_dir / "attestation.json").read_text(encoding="utf-8"))
    result, statement, payload_bytes = agent_change._verify_reference_envelope(
        envelope,
        target,
        attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        "Test Result",
        require_receipt=True,
        receipt=snapshot.receipt,
    )
    assert result.status == attestation.STATUS_SUBJECT_MISMATCH
    assert result.rederived is False


def test_complete_ok_round_trip_with_v1_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path, approval_version=1)
    result = cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name])
    assert result == 0
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "COMPLETE-OK"


def test_emitter_records_unsupported_predicate_approval_reference(
    tmp_path: Path,
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    bad_statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:tree", "digest": {"gitTree": "1111111111111111111111111111111111111111"}}],
        "predicateType": agent_change.AGENT_CHANGE_PREDICATE_TYPE,
        "predicate": {"schemaVersion": 1},
    }
    bad_nonce = "07070707070707070707070707070707"
    bad_path = run_dir / "approvals" / f"{bad_nonce}.json"
    bad_path.write_text(
        json.dumps(attestation.create_envelope(bad_statement, key_path), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    statement = agent_change.build_statement(target, run_dir.name)
    bad_refs = [
        r for r in statement["predicate"]["references"] if r["kind"] == "human-approval" and r.get("nonce") == bad_nonce
    ]
    assert len(bad_refs) == 1
    assert bad_refs[0]["verified"] is False
    assert bad_refs[0]["reason"] == "unsupported-predicate"


def test_emitter_records_malformed_payload_approval_reference(
    tmp_path: Path,
) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    bad_nonce = "08080808080808080808080808080808"
    bad_path = run_dir / "approvals" / f"{bad_nonce}.json"
    bad_path.write_text(
        json.dumps({"payload": "not-base64!!!", "payloadType": "application/vnd.in-toto+json"}), encoding="utf-8"
    )
    statement = agent_change.build_statement(target, run_dir.name)
    bad_refs = [
        r for r in statement["predicate"]["references"] if r["kind"] == "human-approval" and r.get("nonce") == bad_nonce
    ]
    assert len(bad_refs) == 1
    assert bad_refs[0]["verified"] is False
    assert bad_refs[0]["reason"] == "malformed-payload"


def test_other_tree_receipts_recorded(
    tmp_path: Path,
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    other_tree = "4444444444444444444444444444444444444444"
    baseline = "2222222222222222222222222222222222222222"
    patch = "3" * 64
    _test_result_envelope(target, run_dir, key_path, "verify-002", other_tree, baseline, patch)
    statement = agent_change.build_statement(target, run_dir.name)
    other_trees = statement["predicate"]["otherTreeReceipts"]
    assert any(item.get("verifyRunId") == "verify-002" for item in other_trees)


def test_emitter_records_receipt_digest_invalid(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    key_path = _keygen(target)
    agent_change.init_policy(target)
    run_dir = _run_dir(target, "run-001", "1111111111111111111111111111111111111111")
    verify_dir = _test_result_envelope(
        target,
        run_dir,
        key_path,
        "verify-001",
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
        "3" * 64,
    )
    # Corrupt the receipt digest so snapshotting fails, leaving the attestation envelope intact.
    bad_receipt = json.loads((verify_dir / "receipt.json").read_text(encoding="utf-8"))
    bad_receipt["digests"] = {"algorithm": "sha256", "receipt_sha256": "bad"}
    (verify_dir / "receipt.json").write_text(json.dumps(bad_receipt, indent=2, sort_keys=True), encoding="utf-8")
    statement = agent_change.build_statement(target, run_dir.name)
    test_refs = [r for r in statement["predicate"]["references"] if r["kind"] == "test-result"]
    assert any(r.get("reason") == "receipt-digest-invalid" for r in test_refs)


def test_verifier_refuses_absolute_path_locator(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    index_path = run_dir / "agent-change.json"
    env = json.loads(index_path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["references"][0]["locator"] = "/tmp/evil.json"
    env["payload"] = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    ref = out["references"][0]
    assert ref["availability"] == "partial"
    assert ref["reason"] == "malformed-locator"


def test_verifier_untrusted_index_signer_reports_trust_untrusted_and_status_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    other_key_path = tmp_path / "other-signer-key"
    attestation.keygen(
        tmp_path,
        principal="other-signer",
        key_file=other_key_path,
        allowed_signers_file=tmp_path / "other-allowed-signers",
    )
    statement = agent_change.build_statement(target, run_dir.name)
    env = attestation.create_envelope(statement, other_key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(env, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["trust"] == "untrusted"
    assert out["status"] == "INVALID"


def test_verifier_no_cycle_rule_for_approval_with_non_tree_subject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    # Inject an approval whose subject names an agent-change payload digest instead of a git tree.
    bad_nonce = "09090909090909090909090909090909"
    bad_statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "https://brigade.dev/attestation/agent-change/v1", "digest": {"sha256": "a" * 64}}],
        "predicateType": approval_v2.HUMAN_APPROVAL_PREDICATE_TYPE,
        "predicate": {
            "schemaVersion": 2,
            "run": {"id": run_dir.name, "journalChainHead": {"sha256": "a" * 64}},
            "decision": "allow",
            "scope": "run",
            "approver": {"principal": "test-signer", "keyid": _key_fingerprint(key_path), "kind": "human"},
            "requester": {"status": "unknown"},
            "decidedAt": "2026-09-06T13:00:00.000000Z",
            "expiresAt": "2027-09-06T13:00:00.000000Z",
            "nonce": bad_nonce,
            "reasonCode": "reviewed-tests",
            "reasonSha256": "b" * 64,
            "evidence": {"testResultPayloadSha256": [], "envelopes": []},
            "policy": {"name": "brigade.sod.v2", "digest": {"sha256": "c" * 64}},
        },
    }
    bad_path = run_dir / "approvals" / f"{bad_nonce}.json"
    bad_path.write_text(
        json.dumps(attestation.create_envelope(bad_statement, key_path), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    # Re-export so the bad approval is included in the index references.
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    bad_ref = [
        r
        for r in out["references"]
        if r.get("kind") == "human-approval" and r.get("locator", "").endswith(f"{bad_nonce}.json")
    ][0]
    assert bad_ref["binding"] == "conflicted"
    assert bad_ref["policy_outcome"] == "fail"


def test_verifier_approval_baseline_only_subject_reports_binding_conflicted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    baseline_nonce = "04040404040404040404040404040404"
    _approval_envelope_baseline(run_dir, key_path, "2222222222222222222222222222222222222222", baseline_nonce, "a" * 64)
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_dir.name,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "test-signer",
            "approver_keyid": _key_fingerprint(key_path),
            "subject_tree": "1111111111111111111111111111111111111111",
            "nonce": baseline_nonce,
            "decided_at": "2026-09-06T13:00:00.000000Z",
            "expires_at": "2027-09-06T13:00:00.000000Z",
            "statement_sha256": "a" * 64,
            "attestation_path": f"approvals/{baseline_nonce}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{baseline_nonce}",
        expected_previous_sequence=2,
        recorded_at="2026-09-06T13:00:00.000000Z",
    )
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    assert result != 0
    out = json.loads(capsys.readouterr().out)
    baseline_ref = [
        r
        for r in out["references"]
        if r.get("kind") == "human-approval" and r.get("locator", "").endswith(f"{baseline_nonce}.json")
    ][0]
    assert baseline_ref["binding"] == "conflicted"


def test_emitter_requests_directory_symlink_refused(
    tmp_path: Path,
) -> None:
    target, run_dir, key_path, _req = _build_full_run(tmp_path)
    requests_dir = run_dir / "requests"
    real_dir = tmp_path / "real-requests"
    real_dir.mkdir()
    for child in list(requests_dir.iterdir()):
        shutil.move(str(child), str(real_dir / child.name))
    requests_dir.rmdir()
    requests_dir.symlink_to(real_dir)
    statement = agent_change.build_statement(target, run_dir.name)
    req_refs = [r for r in statement["predicate"]["references"] if r["kind"] == "agent-request"]
    assert not req_refs
    assert any(
        m["kind"] == "agent-request" and m["reason"] == "signed request envelope is missing"
        for m in statement["predicate"]["missing"]
    )


def test_worker_seats_excludes_orchestrator(
    tmp_path: Path,
) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    statement = agent_change.build_statement(target, run_dir.name)
    worker_seats = statement["predicate"]["run"]["workerSeats"]
    assert "cursor_worker" not in worker_seats
    assert "codex_coder" in worker_seats


def test_emitted_at_second_precision(
    tmp_path: Path,
) -> None:
    target, run_dir, _key, _req = _build_full_run(tmp_path)
    statement = agent_change.build_statement(target, run_dir.name)
    emitted_at = statement["predicate"]["emittedAt"]
    assert "." not in emitted_at.split("Z")[0]
