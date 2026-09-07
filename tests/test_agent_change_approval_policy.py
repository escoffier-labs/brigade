"""Policy outcome precedence for agent-change approval observations."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agent_change_verify
from brigade import approval, approval_v2, approval_verification, attestation, run_journal

from tests import test_approval as approval_fixtures


def _index() -> dict[str, str]:
    return {"syntax": "wellformed", "signature": "valid", "trust": "trusted", "binding": "bound"}


def _approval(*, outcome: str, state: str = "current", binding: str = "bound") -> dict[str, str]:
    return {
        "kind": "human-approval",
        "availability": "present",
        "syntax": "wellformed",
        "cryptographic": "valid",
        "trust": "trusted",
        "binding": binding,
        "rederivation": "not-applicable",
        "policy_outcome": outcome,
        "approval_state": state,
    }


def test_clean_approval_policy_refusal_is_policy_fail() -> None:
    status = agent_change_verify._overall_status(_index(), [], [_approval(outcome="fail")], "match", "match")
    assert status == "POLICY-FAIL"


def test_integrity_failure_precedes_policy_failure_in_any_reference_order() -> None:
    invalid = _approval(outcome="unevaluated", binding="conflicted")
    refused = _approval(outcome="fail")
    for references in ([invalid, refused], [refused, invalid]):
        assert agent_change_verify._overall_status(_index(), [], references, "match", "match") == "INVALID"


def test_unknown_optional_reference_keeps_an_otherwise_satisfied_index_incomplete() -> None:
    unknown = _approval(outcome="unevaluated")
    unknown["trust"] = "unknown"
    required = [{"kind": "human-approval", "status": "satisfied", "count": 1}]
    assert (
        agent_change_verify._overall_status(_index(), required, [_approval(outcome="pass"), unknown], "match", "match")
        == "INCOMPLETE"
    )


def test_historical_approval_never_satisfies_the_required_set() -> None:
    required = agent_change_verify._evaluate_required_set(
        {"required_references": [{"kind": "human-approval"}]},
        [_approval(outcome="not-applicable", state="historical")],
    )
    assert required == [
        {
            "kind": "human-approval",
            "status": "missing",
            "reason": "required 1 verified human-approval reference(s), found 0",
            "count": 0,
        }
    ]


def _selected_artifact(
    target: Path, key: Path, *, version: int
) -> tuple[Path, run_journal.JournalReport, object, dict[str, object], dict[str, object], bytes]:
    if version == 1:
        approval_fixtures._record_v1_approval(target, key)
    else:
        assert approval_fixtures._approve(target, key) == 0
    run_dir = target / ".brigade" / "runs" / approval_fixtures.RUN_ID
    report = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl")
    event = report.events[-1]
    envelope, statement = approval_fixtures._statement(target)
    return run_dir, report, event, envelope, statement, attestation.canonical_statement_bytes(statement)


def _artifact_fixture(
    tmp_path: Path, *, version: int
) -> tuple[Path, Path, run_journal.JournalReport, object, dict[str, object], dict[str, object], bytes]:
    target, key, _signers = approval_fixtures._workspace(
        tmp_path, requester_principal=None if version == 1 else "requester"
    )
    run_dir, report, event, envelope, statement, statement_bytes = _selected_artifact(target, key, version=version)
    return target, run_dir, report, event, envelope, statement, statement_bytes


@pytest.mark.parametrize("version", [1, 2])
def test_validate_selected_signed_artifact_for_each_version(tmp_path: Path, version: int) -> None:
    target, run_dir, report, event, envelope, statement, statement_bytes = _artifact_fixture(tmp_path, version=version)

    validated = approval_verification.validate_approval_artifact(
        target=target,
        run_dir=run_dir,
        report=report,
        event=event,
        decision_value="allow",
        envelope=envelope,
        statement=statement,
        statement_bytes=statement_bytes,
    )

    assert validated.version == version
    assert validated.tree_fingerprint == approval_fixtures.TREE


def test_v2_selected_artifact_validation_does_not_read_current_reason_projection(tmp_path: Path) -> None:
    target, run_dir, report, event, envelope, statement, statement_bytes = _artifact_fixture(tmp_path, version=2)
    run_json = run_dir / "run.json"
    projection = json.loads(run_json.read_text(encoding="utf-8"))
    projection["approval"]["reason"] = "mutated after signing"
    run_json.write_text(json.dumps(projection), encoding="utf-8")

    validated = approval_verification.validate_approval_artifact(
        target=target,
        run_dir=run_dir,
        report=report,
        event=event,
        decision_value="allow",
        envelope=envelope,
        statement=statement,
        statement_bytes=statement_bytes,
    )

    assert validated.version == 2


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("mutation", ["digest", "previous_chain", "signer", "decision", "scope"])
def test_selected_artifact_rejects_each_conflicting_event_binding(tmp_path: Path, version: int, mutation: str) -> None:
    target, run_dir, report, event, envelope, statement, statement_bytes = _artifact_fixture(tmp_path, version=version)
    payload = dict(event.payload)
    if mutation == "digest":
        payload["statement_sha256"] = "0" * 64
    elif mutation == "signer":
        payload["approver_principal"] = "not-the-signer"
    elif mutation == "decision":
        payload["decision"] = "deny"
    elif mutation == "scope":
        payload["scope"] = "merge"
    conflicting = replace(
        event,
        payload=payload,
        previous_digest=("0" * 64 if mutation == "previous_chain" else event.previous_digest),
    )

    with pytest.raises((approval.ApprovalError, approval_v2.ApprovalV2Error)):
        approval_verification.validate_approval_artifact(
            target=target,
            run_dir=run_dir,
            report=report,
            event=conflicting,
            decision_value="allow",
            envelope=envelope,
            statement=statement,
            statement_bytes=statement_bytes,
        )


def test_strict_index_v1_rejects_extra_subject_digest_key(tmp_path: Path) -> None:
    target, run_dir, report, event, _envelope, statement, _statement_bytes = _artifact_fixture(tmp_path, version=1)
    altered_statement = copy.deepcopy(statement)
    altered_statement["subject"][0]["digest"]["unexpected"] = "not-allowed"
    key = tmp_path / "approver" / ".brigade" / "attestation" / "signing-key"
    altered_envelope = attestation.create_envelope(altered_statement, key)
    altered_bytes = attestation.canonical_statement_bytes(altered_statement)
    payload = dict(event.payload)
    payload["statement_sha256"] = hashlib.sha256(altered_bytes).hexdigest()
    selected_event = replace(event, payload=payload)

    compatibility = approval_verification.validate_approval_artifact(
        target=target,
        run_dir=run_dir,
        report=report,
        event=selected_event,
        decision_value="allow",
        envelope=altered_envelope,
        statement=altered_statement,
        statement_bytes=altered_bytes,
    )
    assert compatibility.version == 1
    with pytest.raises(approval.ApprovalError, match="subjects are invalid"):
        approval_verification.validate_approval_artifact(
            target=target,
            run_dir=run_dir,
            report=report,
            event=selected_event,
            decision_value="allow",
            envelope=altered_envelope,
            statement=altered_statement,
            statement_bytes=altered_bytes,
            strict_v1=True,
        )
