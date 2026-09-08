"""Signed public-verifier approval integration coverage."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from brigade import (
    agent_change,
    agent_change_approval_verify,
    agent_change_verify,
    approval,
    approval_v2,
    attestation,
    cli,
    localio,
    run_journal,
)
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
    evidence = (
        approval_v2.collect_test_result_evidence(target, run_dir.name, "1" * 40)
        if decision == "allow"
        else approval_v2.empty_evidence()
    )
    statement = approval_v2.build_statement(
        run_id=run_dir.name,
        journal_chain_head=report.events[-1].event_digest,
        tree_fingerprint="1" * 40,
        evidence=evidence,
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
            "producer_keyids": sorted(evidence.producer_keyids),
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


def _approval_reason_commitment(path: Path) -> str:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    statement, _statement_bytes = approval._decode_statement(envelope)
    assert statement is not None
    predicate = statement["predicate"]
    assert isinstance(predicate, dict)
    commitment = predicate.get("reason") or predicate.get("reasonSha256")
    assert isinstance(commitment, str)
    return commitment


@pytest.mark.parametrize("version", [1, 2])
def test_group_a_current_signed_approval_is_complete_for_each_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, approval_version=version)
    (target / "README").write_text("fixture\n", encoding="utf-8")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "Test User"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "add", "README"],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=target, check=True, timeout=10)
    live_tree = localio.tree_fingerprint(target)

    assert live_tree is not None
    assert live_tree != "1" * 40

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


@pytest.mark.parametrize("decision", ["allow", "deny", "hold"])
def test_group_c_absent_recorded_request_keeps_allow_incomplete_and_refusals_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], decision: str
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, include_request=False)
    if decision != "allow":
        _append_v2_decision(target, run_dir, decision=decision, nonce=("05" if decision == "deny" else "06") * 16)

    result, output = _verify(target, run_dir, capsys)

    assert result == 1
    assert output["status"] == ("INCOMPLETE" if decision == "allow" else "POLICY-FAIL")


def test_group_c_absent_request_still_rejects_current_reason_drift_before_classification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, include_request=False)
    run_json = run_dir / "run.json"
    projection = json.loads(run_json.read_text(encoding="utf-8"))
    projection["approval"]["reason"] = "changed after signing"
    run_json.write_text(json.dumps(projection, indent=2, sort_keys=True), encoding="utf-8")

    result, output = _verify(target, run_dir, capsys)

    approval = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    assert result == 1
    assert output["status"] == "INVALID"
    assert approval["binding"] == "conflicted"


def test_group_c_expired_signed_allow_is_policy_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(
        tmp_path, approval_expires_at="2025-09-06T13:00:00.000000Z"
    )

    result, output = _verify(target, run_dir, capsys)

    assert result == 1
    assert output["status"] == "POLICY-FAIL"


def test_group_c_absent_request_does_not_mask_an_expired_signed_allow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(
        tmp_path,
        include_request=False,
        approval_expires_at="2025-09-06T13:00:00.000000Z",
    )

    result, output = _verify(target, run_dir, capsys)

    assert result == 1
    assert output["status"] == "POLICY-FAIL"


@pytest.mark.parametrize("version", [1, 2])
def test_group_b_signed_requester_collision_stays_policy_fail_despite_projection_replacement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(
        tmp_path, approval_version=version, approver_is_requester=True
    )
    run_json = run_dir / "run.json"
    projection = json.loads(run_json.read_text(encoding="utf-8"))
    projection["requester_principal"] = "replacement"
    projection["requester_keyid"] = "SHA256:replacement"
    run_json.write_text(json.dumps(projection, indent=2, sort_keys=True), encoding="utf-8")

    result, output = _verify(target, run_dir, capsys)

    approval = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    assert result == 1
    assert output["status"] == "POLICY-FAIL"
    assert approval["cryptographic"] == "valid"
    assert approval["trust"] == "trusted"
    assert approval["binding"] == "bound"


def test_group_b_signed_producer_only_collision_is_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, request = fixtures._build_full_run(
        tmp_path,
        approver_is_producer=True,
        producer_is_requester=False,
    )

    result, output = _verify(target, run_dir, capsys)

    approval_ref = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    producer_keyid = fixtures._key_fingerprint(target / ".brigade" / "attestation" / "test-producer-key")
    assert request["requester_keyid"] != producer_keyid
    assert result == 1
    assert output["status"] == "POLICY-FAIL"
    assert approval_ref["cryptographic"] == "valid"
    assert approval_ref["trust"] == "trusted"
    assert approval_ref["binding"] == "bound"


def test_group_c_corrupt_recorded_request_is_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, run_dir, key, request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    request_path = run_dir / request["attestation_path"]
    request_path.write_text("{}", encoding="utf-8")

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "INVALID"


def test_group_c_absent_request_rejects_changed_current_patch_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, include_request=False)
    patch = target / ".brigade" / "work" / "verify-runs" / "verify-001" / "changes.patch"
    patch.write_text("changed patch", encoding="utf-8")

    result, output = _verify(target, run_dir, capsys)

    assert result == 1
    assert output["status"] == "INVALID"


def test_group_d_historical_signed_allow_is_unevaluated_and_cannot_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    original_path = run_dir / "approvals" / f"{'02' * 16}.json"
    _append_v2_decision(target, run_dir, decision="deny", nonce="03" * 16)

    result, output = _verify(target, run_dir, capsys)

    approvals = [ref for ref in output["references"] if ref["kind"] == "human-approval"]
    assert result == 1
    assert any(ref["approval_state"] == "historical" and ref["policy_outcome"] == "unevaluated" for ref in approvals)
    assert any(ref["approval_state"] == "current" and ref["policy_outcome"] == "fail" for ref in approvals)
    assert _approval_reason_commitment(original_path) != _approval_reason_commitment(
        run_dir / "approvals" / f"{'03' * 16}.json"
    )
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["approval"]["reason"] == "deny decision"


def test_group_d_historical_signed_deny_does_not_poison_a_later_allow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path, approval_decision="deny")
    original_path = run_dir / "approvals" / f"{'02' * 16}.json"
    _append_v2_decision(target, run_dir, decision="allow", nonce="07" * 16)

    result, output = _verify(target, run_dir, capsys)

    approvals = [ref for ref in output["references"] if ref["kind"] == "human-approval"]
    assert result == 0
    assert output["status"] == "COMPLETE-OK"
    assert any(ref["approval_state"] == "historical" and ref["policy_outcome"] == "unevaluated" for ref in approvals)
    assert any(ref["approval_state"] == "current" and ref["policy_outcome"] == "pass" for ref in approvals)
    assert _approval_reason_commitment(original_path) != _approval_reason_commitment(
        run_dir / "approvals" / f"{'07' * 16}.json"
    )
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["approval"]["reason"] == "allow decision"


def test_group_e_omitted_latest_event_cannot_borrow_an_older_allowance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="deny", nonce="08" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    approvals = [ref for ref in statement["predicate"]["references"] if ref["kind"] == "human-approval"]
    latest = next(ref for ref in approvals if ref["locator"].endswith(f"/{'08' * 16}.json"))
    statement["predicate"]["references"].remove(latest)

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    observed = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    assert result == 1
    assert observed["approval_state"] == "historical"
    assert observed["policy_outcome"] == "unevaluated"
    assert output["status"] == "INCOMPLETE"


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


def test_group_e_signed_approval_reference_orphaned_from_valid_journal_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.write_bytes(b"\n".join(journal.read_bytes().splitlines()[:-1]) + b"\n")

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


def test_group_f_static_events_symlink_is_refused_before_any_outside_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    _verify(target, run_dir, capsys)
    outside = tmp_path / "outside-events"
    shutil.move(str(run_dir / "events"), outside)
    (outside / "lifecycle.jsonl.lock").unlink()
    (run_dir / "events").symlink_to(outside, target_is_directory=True)

    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])

    assert result == 1
    assert not (outside / "lifecycle.jsonl.lock").exists()
    output = json.loads(capsys.readouterr().out)
    approval = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    assert output["status"] == "INVALID"
    assert approval["binding"] == "conflicted"


def test_group_f_approval_envelope_replacement_after_generic_check_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    _verify(target, run_dir, capsys)
    approval_path = run_dir / "approvals" / f"{'02' * 16}.json"
    original = agent_change_verify._load_envelope
    replaced = False

    def replace_after_generic_check(path: Path) -> tuple[dict[str, object] | None, str]:
        nonlocal replaced
        result = original(path)
        if path == approval_path and not replaced:
            replaced = True
            approval_path.write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr("brigade.agent_change_verify._load_envelope", replace_after_generic_check)
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])

    assert replaced
    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "INVALID"
    approval = next(ref for ref in output["references"] if ref["kind"] == "human-approval")
    assert approval["binding"] == "conflicted"


def test_group_f_selected_envelope_replacement_between_batch_reads_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="deny", nonce="09" * 16)
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    selected_path = run_dir / "approvals" / f"{'02' * 16}.json"
    replacement = (run_dir / "approvals" / f"{'09' * 16}.json").read_text(encoding="utf-8")
    original = agent_change_approval_verify._load_selected_approval
    replaced = False

    def replace_selected(path: Path, nonce: str) -> object:
        nonlocal replaced
        loaded = original(path, nonce)
        if path == selected_path and not replaced:
            replaced = True
            selected_path.write_text(replacement, encoding="utf-8")
        return loaded

    monkeypatch.setattr("brigade.agent_change_approval_verify._load_selected_approval", replace_selected)
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])

    assert replaced
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID"


def test_group_f_valid_journal_mutation_between_batch_reads_is_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, _key, _request = fixtures._build_full_run(tmp_path)
    journal = run_dir / "events" / "lifecycle.jsonl"
    original_journal = journal.read_bytes()
    run_json = run_dir / "run.json"
    original_projection = run_json.read_text(encoding="utf-8")
    cli.main(["receipts", "export", "agent-change", "--target", str(target), "--run-id", run_dir.name, "--force"])
    capsys.readouterr()
    _append_v2_decision(target, run_dir, decision="deny", nonce="0a" * 16)
    mutated_journal = journal.read_bytes()
    journal.write_bytes(original_journal)
    run_json.write_text(original_projection, encoding="utf-8")
    original = run_journal.read_journal_bounded
    reads = 0

    def read_then_mutate(path: Path) -> run_journal.JournalReport:
        nonlocal reads
        report = original(path)
        reads += 1
        if reads == 2:
            journal.write_bytes(mutated_journal)
        return report

    monkeypatch.setattr("brigade.agent_change_approval_verify.run_journal.read_journal_bounded", read_then_mutate)
    result = cli.main(["receipts", "verify-agent-change", str(run_dir), "--target", str(target), "--json"])

    assert reads == 3
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID"


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


def test_group_g_historical_disallowed_profile_is_policy_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path, approval_decision="deny")
    _append_v2_decision(target, run_dir, decision="allow", nonce="0b" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    historical = next(
        ref
        for ref in statement["predicate"]["references"]
        if ref["kind"] == "human-approval" and ref["locator"].endswith(f"/{'02' * 16}.json")
    )
    historical["profile"] = "disallowed.profile"

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "POLICY-FAIL"


@pytest.mark.parametrize("reverse", [False, True])
def test_group_g_integrity_failure_precedes_clean_refusal_in_both_reference_orders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reverse: bool
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path)
    _append_v2_decision(target, run_dir, decision="deny", nonce="0c" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    approvals = [ref for ref in statement["predicate"]["references"] if ref["kind"] == "human-approval"]
    historical = next(ref for ref in approvals if ref["locator"].endswith(f"/{'02' * 16}.json"))
    historical["payloadSha256"] = "0" * 64
    if reverse:
        references = statement["predicate"]["references"]
        first, second = (references.index(approvals[0]), references.index(approvals[1]))
        references[first], references[second] = references[second], references[first]

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert result == 1
    assert output["status"] == "INVALID"


def test_group_h_unresolved_optional_reference_cannot_be_complete_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, key, _request = fixtures._build_full_run(tmp_path, approval_decision="deny")
    _append_v2_decision(target, run_dir, decision="allow", nonce="0d" * 16)
    statement = fixtures.agent_change.build_statement(target, run_dir.name)
    historical_path = run_dir / "approvals" / f"{'02' * 16}.json"
    historical_envelope = json.loads(historical_path.read_text(encoding="utf-8"))
    original = agent_change_verify._verify_envelope_signature

    def unavailable_optional_signature(
        envelope: dict[str, object],
        target: Path,
        expected_predicate_type: str,
        receipt: dict[str, object] | None = None,
    ) -> attestation.AttestationVerifyResult:
        result = original(envelope, target, expected_predicate_type, receipt)
        if envelope == historical_envelope:
            result.status = attestation.STATUS_UNVERIFIABLE_SIGNATURE
        return result

    monkeypatch.setattr("brigade.agent_change_verify._verify_envelope_signature", unavailable_optional_signature)

    result, output = _verify_statement(target, run_dir, key, statement, capsys)

    assert all(item["status"] == "satisfied" for item in output["required_set"])
    historical = next(ref for ref in output["references"] if ref["locator"].endswith(f"/{'02' * 16}.json"))
    assert historical["approval_state"] == "historical"
    assert historical["syntax"] == "wellformed"
    assert historical["binding"] == "bound"
    assert historical["cryptographic"] == "unverifiable"
    assert historical["trust"] == "unknown"
    assert historical["rederivation"] == "not-applicable"
    assert result == 1
    assert output["status"] == "INCOMPLETE"


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
        subprocess.run(argv, cwd=target, check=True, timeout=10)
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
