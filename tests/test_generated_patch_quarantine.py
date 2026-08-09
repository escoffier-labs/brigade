"""GeneratedPatchQuarantine (#507) pure-module and outcome-source tests."""

from __future__ import annotations

import json

from brigade import generated_patch_quarantine as gpq
from brigade import outcome


def test_build_and_validate_quarantine_round_trip():
    payload = gpq.build_quarantine(
        candidate_count=4,
        model="gpt-test",
        model_version="2024-08",
        audit={"model_confidence": 0.91, "lexical_similarity": 0.8},
    )
    assert payload["schema"] == gpq.QUARANTINE_SCHEMA
    assert payload["status"] == "quarantined"
    assert payload["candidate_count"] == 4
    assert payload["model_confidence"] == 0.91
    assert gpq.validate_quarantine(payload) is None


def test_validate_quarantine_rejects_incomplete_and_zero_candidates():
    assert gpq.validate_quarantine(None) == "generated_patch_quarantine_incomplete"
    assert (
        gpq.validate_quarantine({"candidate_count": 0, "model": "m", "model_version": "1"})
        == "generated_patch_quarantine_incomplete"
    )
    assert (
        gpq.validate_quarantine({"candidate_count": 1, "model": " ", "model_version": "1"})
        == "generated_patch_quarantine_incomplete"
    )
    assert (
        gpq.validate_quarantine({"candidate_count": True, "model": "m", "model_version": "1"})
        == "generated_patch_quarantine_incomplete"
    )


def test_resolve_producer_metadata_prefers_session_file(tmp_path):
    session_id = "session-abc"
    patch_path = tmp_path / ".brigade" / "work" / session_id / "generated-patch.json"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        json.dumps(
            {
                "candidate_count": 5,
                "model": "file-model",
                "model_version": "file-v1",
                "lexical_similarity": 0.7,
            }
        )
    )
    metadata = gpq.resolve_producer_metadata(
        target=tmp_path,
        session_id=session_id,
        session_payload={"generated_patch": {"candidate_count": 1, "model": "session", "model_version": "s"}},
        environ={
            "BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT": "9",
            "BRIGADE_GENERATED_PATCH_MODEL": "env-model",
            "BRIGADE_GENERATED_PATCH_MODEL_VERSION": "env-v",
        },
    )
    assert metadata["candidate_count"] == 5
    assert metadata["model"] == "file-model"
    assert metadata["model_version"] == "file-v1"
    assert metadata["lexical_similarity"] == 0.7
    stamped = gpq.stamp_quarantine_from_metadata(metadata)
    assert stamped is not None
    assert stamped["candidate_count"] == 5
    assert stamped["lexical_similarity"] == 0.7


def test_resolve_producer_metadata_falls_back_to_context_model(tmp_path):
    metadata = gpq.resolve_producer_metadata(
        target=tmp_path,
        session_id=None,
        environ={
            "BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT": "2",
            "BRIGADE_CONTEXT_MODEL": "context-model",
            "BRIGADE_GENERATED_PATCH_MODEL_VERSION": "ctx-v",
        },
    )
    assert metadata == {
        "candidate_count": 2,
        "model": "context-model",
        "model_version": "ctx-v",
    }


def test_receipt_has_repository_tests_requires_effectiveness_check():
    assert gpq.receipt_has_repository_tests({"commands": []}) is False
    assert (
        gpq.receipt_has_repository_tests(
            {
                "commands": [
                    {"check_role": "utility_guardrail", "check_id": "guard.x"},
                ]
            }
        )
        is False
    )
    assert (
        gpq.receipt_has_repository_tests(
            {
                "commands": [
                    {"check_role": "effectiveness", "check_id": "verify.pytest"},
                ]
            }
        )
        is True
    )


def test_generated_patch_eligibility_ignores_non_promoting_audit_fields():
    binding = {
        "generated_patch_quarantine": gpq.build_quarantine(
            candidate_count=3,
            model="m",
            model_version="v",
            audit={"model_confidence": 0.99, "repeated_sampling": 12},
        )
    }
    receipt = {
        "commands": [
            {"check_role": "effectiveness", "check_id": "verify.pytest", "status": "completed", "exit_code": 0}
        ]
    }
    assert gpq.generated_patch_eligibility_reason(binding, receipt) is None
    receipt_no_tests = {"commands": [{"check_role": "utility_guardrail", "check_id": "g"}]}
    assert (
        gpq.generated_patch_eligibility_reason(binding, receipt_no_tests) == "generated_patch_missing_repository_tests"
    )


def test_non_promoting_outcome_sources_never_score():
    for source in sorted(outcome.NON_PROMOTING_SOURCES):
        assert outcome.signal_value(source, "completed") == 0
        assert outcome.signal_value(source, "cleared") == 0
        assert outcome.signal_value(source, "ok") == 0
        assert gpq.is_non_promoting_outcome_source(source) is True
    # Verified exit codes remain the promoting path.
    assert outcome.signal_value("verify", "completed") == 1
