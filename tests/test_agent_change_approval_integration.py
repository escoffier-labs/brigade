"""Signed public-verifier approval integration coverage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brigade import agent_change, approval_v2, attestation, cli, localio, run_journal
from brigade.work_cmd import verification as work_cmd

from tests import test_agent_change as fixtures


def _verify(target: Path, run_dir: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])
    return result, json.loads(capsys.readouterr().out)


def _append_v2_decision(target: Path, run_dir: Path, *, decision: str, nonce: str) -> None:
    """Append a real, signed decision to the fixture journal."""
    journal = run_dir / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    requester = approval_v2.verify_recorded_request(target, run_dir, report.events)
    key = target / ".brigade" / "attestation" / "test-approver-key"
    keyid = fixtures._key_fingerprint(key)
    decided_at = "2026-09-07T13:00:00.000000Z"
    reason = f"{decision} decision"
    statement = approval_v2.build_statement(
        run_id=run_dir.name,
        journal_chain_head=report.events[-1].event_digest,
        tree_fingerprint="1" * 40,
        evidence=approval_v2.empty_evidence(),
        requester=requester,
        decision=decision,
        scope="run",
        principal="test-approver",
        keyid=keyid,
        decided_at=decided_at,
        expires_at="2027-09-07T13:00:00.000000Z",
        nonce=nonce,
        reason_code="reviewed-tests",
        reason=reason,
    )
    envelope = attestation.create_envelope(statement, key)
    approval_path = run_dir / "approvals" / f"{nonce}.json"
    approval_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="approval",
        payload={
            "decision": decision,
            "scope": "run",
            "approver_principal": "test-approver",
            "approver_keyid": keyid,
            "subject_tree": "1" * 40,
            "nonce": nonce,
            "decided_at": decided_at,
            "expires_at": "2027-09-07T13:00:00.000000Z",
            "statement_sha256": fixtures.hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest(),
            "attestation_path": f"approvals/{nonce}.json",
            "producer_keyids": [],
        },
        idempotency_key=f"approval:{nonce}",
        expected_previous_sequence=report.events[-1].sequence,
        recorded_at=decided_at,
    )
    metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    metadata["approval"] = {"reason": reason}
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")


def _verify_statement(
    target: Path,
    run_dir: Path,
    key: Path,
    statement: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    envelope = attestation.create_envelope(statement, key)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])
    return result, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("version", [1, 2])
def test_group_a_current_signed_approval_is_complete_for_each_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, approval_version=version)

    result, output = _verify(target, run_dir, capsys)

    assert result == 0
    assert output["status"] == "COMPLETE-OK"


@pytest.mark.parametrize("decision", ["deny", "hold"])
def test_group_c_current_signed_refusal_is_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], decision: str
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision=decision, nonce=("03" if decision == "deny" else "04") * 16)

    result, output = _verify(target, run_dir, capsys)

    assert result == 1
    assert output["status"] == "POLICY-FAIL"


def test_group_d_historical_signed_allow_is_unevaluated_and_cannot_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="deny", nonce="03" * 16)

    result, output = _verify(target, run_dir, capsys)

    approvals = [ref for ref in output["references"] if ref["kind"] == "human-approval"]
    assert result == 1
    assert any(ref["approval_state"] == "historical" and ref["policy_outcome"] == "unevaluated" for ref in approvals)
    assert any(ref["approval_state"] == "current" and ref["policy_outcome"] == "fail" for ref in approvals)


def test_group_e_duplicate_signed_reference_to_one_event_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    references = statement["predicate"]["references"]
    approval = next(reference for reference in references if reference["kind"] == "human-approval")
    references.append(dict(approval))

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "INVALID"


def test_group_f_approval_nonce_traversal_is_an_invalid_observation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    approval = next(
        reference for reference in statement["predicate"]["references"] if reference["kind"] == "human-approval"
    )
    approval["locator"] = f".brigade/runs/{run_dir.name}/approvals/../{'02' * 16}.json"

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "INVALID"
    observed = next(reference for reference in output["references"] if reference["kind"] == "human-approval")
    assert observed["syntax"] == "malformed"


def test_group_g_disallowed_approval_profile_remains_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    approval = next(
        reference for reference in statement["predicate"]["references"] if reference["kind"] == "human-approval"
    )
    approval["profile"] = "disallowed.profile"

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "POLICY-FAIL"
    observed = next(reference for reference in output["references"] if reference["kind"] == "human-approval")
    assert observed["policy_outcome"] == "fail"


def test_group_h_real_failing_brigade_receipt_rederives_to_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    (target / "README").write_text("fixture\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "Test User"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=target, check=True)
    tree = localio.tree_fingerprint(target)
    assert tree is not None
    agent_change.init_policy(target)
    key = fixtures._keygen(target)
    run_dir = fixtures._run_dir(target, "run-001", tree)
    monkeypatch.setenv("BRIGADE_RUN_ID", run_dir.name)
    assert (
        work_cmd.verify_run(
            target=target,
            commands=[["python3", "-c", "raise SystemExit(1)"]],
            reuse=False,
        )
        == 1
    )
    capsys.readouterr()
    receipt_paths = sorted((target / ".brigade" / "work" / "verify-runs").glob("*/receipt.json"))
    failed_receipt = next(
        path for path in receipt_paths if json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"
    )
    receipt = json.loads(failed_receipt.read_text(encoding="utf-8"))
    metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    metadata["tree_fingerprint"] = receipt["tree_fingerprint"]
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    envelope = attestation.create_envelope(attestation.build_statement(receipt), key)
    (failed_receipt.parent / "attestation.json").write_text(
        json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8"
    )

    result, output = _verify(target, run_dir, capsys)

    failed = next(
        reference
        for reference in output["references"]
        if reference["locator"].startswith(str(failed_receipt.parent.relative_to(target)))
    )
    assert result == 1
    assert output["status"] == "POLICY-FAIL"
    assert failed["rederivation"] == "reproduced"
    assert failed["policy_outcome"] == "fail"


def test_verifier_preserves_malformed_non_object_approval_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed list member is observed in place instead of crashing the verifier."""
    target, run_dir, key_path, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    statement["predicate"]["references"].insert(0, ["not", "an", "object"])
    envelope = attestation.create_envelope(statement, key_path)
    index_path = run_dir / "agent-change.json"
    index_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")

    result = cli.main(["receipts", "verify-agent-change", str(index_path), "--target", str(target), "--json"])

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["references"][0]["syntax"] == "malformed"
    assert output["references"][0]["binding"] == "conflicted"
