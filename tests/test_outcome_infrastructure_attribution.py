"""Outcome and friction attribution for worker infrastructure failures (#580)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import friction_cmd, localio, outcome, outcome_cmd, verify_trial, worker_failure

from tests.test_outcome_cmd import _write_run_receipt, _write_verify_receipt


def _write_run_receipt_with_failure(
    target: Path,
    run_id: str,
    *,
    status: str = "failed",
    failure_kind: str | None = None,
    failure_phase: str | None = None,
    failure: dict | None = None,
    started_at: str = "2026-06-20T00:00:00+00:00",
) -> Path:
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt: dict = {
        "task": "fixture task",
        "cwd": str(target),
        "status": status,
        "dry_run": False,
        "read_only": False,
        "started_at": started_at,
        "completed_at": started_at,
        "artifacts": str(run_dir),
    }
    if failure_kind is not None:
        receipt["failure_kind"] = failure_kind
    if failure_phase is not None:
        receipt["failure_phase"] = failure_phase
    if failure is not None:
        receipt["failure"] = failure
    path = run_dir / "run.json"
    localio.write_json(path, receipt)
    return path


def _ok_worker(*, worker: str = "implementer") -> dict:
    return {
        "worker": worker,
        "task": "fixture task",
        "ok": True,
        "detail": "done",
        "text": "done",
    }


def _capture_signal(tmp_path, run_id: str) -> tuple[int, dict]:
    import io
    import sys

    buffer = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buffer
    try:
        rc = outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt=run_id, json_output=True)
    finally:
        sys.stdout = old_stdout
    payload = json.loads(buffer.getvalue())
    return rc, payload


def _write_worker_results(
    target: Path,
    run_id: str,
    *,
    results: list[dict],
    ground_truth: dict | None = None,
) -> Path:
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"results": results}
    if ground_truth is not None:
        payload["ground_truth"] = ground_truth
    path = run_dir / "worker-results.json"
    localio.write_json(path, payload)
    return path


def _infra_worker(
    *,
    worker: str = "implementer",
    failure_class: str = "transport-hang",
    cause_code: str = "initialize-no-progress",
    phase: str = "preflight",
) -> dict:
    if failure_class == "timeout":
        phase = "dispatch"
    return {
        "worker": worker,
        "task": "fixture task",
        "ok": False,
        "detail": "transport made no progress",
        "transport": "app-server",
        "failure": {
            "schema": "brigade.worker_failure.v1",
            "class": failure_class,
            "domain": "infrastructure",
            "phase": phase,
            "retry": "fallback",
            "detected_by": "fixture",
            "detail": "transport made no progress",
            "cause_code": cause_code,
        },
    }


def test_infrastructure_only_failed_run_is_neutral_with_evidence_ref(tmp_path, capsys):
    run_json = _write_run_receipt(tmp_path, run_id="infra-failed", status="failed")
    _write_worker_results(tmp_path, "infra-failed", results=[_infra_worker()])

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="infra-failed", json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["source"] == "run"
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_infrastructure_only_incomplete_run_is_neutral_with_evidence_ref(tmp_path, capsys):
    run_json = _write_run_receipt(tmp_path, run_id="infra-incomplete", status="incomplete")
    _write_worker_results(tmp_path, "infra-incomplete", results=[_infra_worker(failure_class="timeout")])

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="infra-incomplete", json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_verify_failure_remains_negative_with_verify_source(tmp_path, capsys):
    _write_verify_receipt(tmp_path, run_id="verify-failed", status="failed")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_id="verify-failed", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["source"] == "verify"


def test_verify_success_remains_positive(tmp_path, capsys):
    _write_verify_receipt(tmp_path, run_id="verify-ok", status="completed")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_id="verify-ok", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 1
    assert payload["record"]["source"] == "verify"


def test_mixed_infra_failure_without_completed_verifier_is_neutral(tmp_path, capsys):
    run_json = _write_run_receipt(
        tmp_path,
        run_id="mixed-no-verify",
        status="incomplete",
        started_at="2026-06-20T00:00:00+00:00",
    )
    _write_worker_results(
        tmp_path,
        "mixed-no-verify",
        results=[_infra_worker()],
        ground_truth={"verify_receipts": [], "latest_verify": None},
    )

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="mixed-no-verify", json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_mixed_infra_failure_with_verifier_failure_remains_negative(tmp_path, capsys):
    verify_path = _write_verify_receipt(
        tmp_path,
        run_id="verify-mixed-fail",
        status="failed",
        started_at="2026-06-20T01:00:00+00:00",
    )
    verify_payload = json.loads(verify_path.read_text())
    from brigade.verify_trial import stamp_verify_receipt_failure_taxonomy

    stamp_verify_receipt_failure_taxonomy(verify_payload)
    localio.write_json(verify_path, verify_payload)

    run_json = _write_run_receipt(
        tmp_path,
        run_id="mixed-verify-fail",
        status="incomplete",
        started_at="2026-06-20T00:00:00+00:00",
    )
    _write_worker_results(
        tmp_path,
        "mixed-verify-fail",
        results=[_infra_worker()],
        ground_truth={
            "verify_receipts": [json.loads(verify_path.read_text())],
            "latest_verify": {
                "run_id": "verify-mixed-fail",
                "status": "failed",
                "failure_class": "verification",
            },
        },
    )

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="mixed-verify-fail", json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_successful_run_capture_unchanged(tmp_path, capsys):
    run_json = _write_run_receipt(tmp_path, run_id="ok-run", status="ok")
    _write_worker_results(
        tmp_path,
        "ok-run",
        results=[{"worker": "implementer", "task": "fixture", "ok": True, "detail": "done", "text": "done"}],
    )

    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="ok-run", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_legacy_worker_result_maps_unknown_kind_at_read_time_without_mutation(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "legacy"
    run_dir.mkdir(parents=True)
    fixture = {
        "results": [
            {
                "worker": "implementer",
                "ok": False,
                "detail": "mystery adapter failure",
                "failure_phase": "dispatch",
                "failure_kind": "future-adapter-code",
            }
        ]
    }
    path = run_dir / "worker-results.json"
    original_bytes = (json.dumps(fixture, indent=2) + "\n").encode()
    path.write_bytes(original_bytes)

    failure = outcome_cmd.worker_result_failure(fixture["results"][0])

    assert failure is not None
    assert failure.failure_class.value == "unclassified"
    assert path.read_bytes() == original_bytes


def test_run_capture_signal_value_unit_cases():
    assert outcome.run_capture_signal_value("failed", infrastructure_only=True, verifier_failed=False) == 0
    assert outcome.run_capture_signal_value("incomplete", infrastructure_only=True, verifier_failed=False) == 0
    assert outcome.run_capture_signal_value("failed", infrastructure_only=True, verifier_failed=True) == -1
    assert outcome.run_capture_signal_value("ok", infrastructure_only=False, verifier_failed=False) == 1
    assert outcome.run_capture_signal_value("error", infrastructure_only=False, verifier_failed=False) == -1


def test_friction_adapter_failure_uses_failure_class_and_cause_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    run = tmp_path / ".brigade" / "runs" / "r-timeout"
    run.mkdir(parents=True)
    (run / "worker-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "worker": "composer",
                        "ok": False,
                        "detail": "deadline exceeded",
                        "transport": "direct",
                        "timed_out": True,
                        "failure_phase": "dispatch",
                        "failure_kind": "timeout",
                        "failure": {
                            "schema": "brigade.worker_failure.v1",
                            "class": "timeout",
                            "domain": "infrastructure",
                            "phase": "dispatch",
                            "retry": "fallback",
                            "detected_by": "legacy-failure-kind",
                            "detail": "deadline exceeded",
                            "cause_code": "timeout",
                        },
                    }
                ]
            }
        )
    )
    run2 = tmp_path / ".brigade" / "runs" / "r-adapter"
    run2.mkdir(parents=True)
    (run2 / "worker-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "worker": "composer",
                        "ok": False,
                        "detail": "decode failure",
                        "transport": "cursor-acpx",
                        "failure_phase": "dispatch",
                        "failure_kind": "decode-failure",
                        "failure": {
                            "schema": "brigade.worker_failure.v1",
                            "class": "transport-unavailable",
                            "domain": "infrastructure",
                            "phase": "dispatch",
                            "retry": "same-seat-once",
                            "detected_by": "legacy-failure-kind",
                            "detail": "decode failure",
                            "cause_code": "decode-failure",
                        },
                    }
                ]
            }
        )
    )

    families = friction_cmd._structured_families(tmp_path, None, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
    run_candidates = families["run"]
    by_class = {item["error_class"]: item for item in run_candidates}

    assert by_class["timeout"]["cause_code"] == "timeout"
    assert by_class["transport-unavailable"]["cause_code"] == "decode-failure"


def test_infra_run_ignores_unrelated_later_verify_receipt(tmp_path, capsys):
    """Ground truth saying "no verification" must not be overridden by a later verify run."""

    run_json = _write_run_receipt(
        tmp_path,
        run_id="infra-no-verify",
        status="failed",
        started_at="2026-06-20T00:00:00+00:00",
    )
    _write_worker_results(
        tmp_path,
        "infra-no-verify",
        results=[_infra_worker()],
        ground_truth={"verify_receipts": [], "latest_verify": None},
    )
    verify_path = _write_verify_receipt(
        tmp_path,
        run_id="verify-later-unrelated",
        status="failed",
        started_at="2026-06-20T02:00:00+00:00",
    )
    verify_payload = json.loads(verify_path.read_text())
    verify_payload["failure_class"] = "verification"
    localio.write_json(verify_path, verify_payload)

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="infra-no-verify", json_output=True)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_non_infrastructure_domain_is_not_neutralized(tmp_path, capsys):
    run_json = _write_run_receipt(tmp_path, run_id="product-failed", status="failed")
    worker = _infra_worker()
    worker["failure"]["domain"] = "product"
    worker["failure_kind"] = "timeout"
    worker["failure_phase"] = "dispatch"
    _write_worker_results(tmp_path, "product-failed", results=[worker])

    assert (
        outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="product-failed", json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_verify_summary_with_empty_commands_falls_back_to_status():
    assert outcome_cmd._verify_summary_failed_verification({"status": "failed", "commands": []}) is True
    assert (
        outcome_cmd._verify_summary_failed_verification(
            {"status": "failed", "failure_class": "infrastructure", "commands": []}
        )
        is False
    )
    assert (
        outcome_cmd._verify_summary_failed_verification(
            {"status": "failed", "commands": [{"failure_class": "infrastructure"}]}
        )
        is False
    )


def test_verify_failure_classes_are_read_from_verify_trial(monkeypatch):
    """The vocabulary must come from verify_trial, not from literals copied into outcome_cmd.

    verify_trial stamps these receipts, so it owns the class names. If a future class is
    added there and this module still matches "verification"/"infrastructure" literally,
    runs get silently misclassified. Extending the frozensets must change behaviour here.
    """
    monkeypatch.setattr(verify_trial, "VERIFY_SKILL_FAILURE_CLASSES", frozenset({"assertion"}))
    monkeypatch.setattr(verify_trial, "VERIFY_INFRASTRUCTURE_FAILURE_CLASSES", frozenset({"harness"}))

    # A class that is only "skill-blaming" under the patched vocabulary.
    assert outcome_cmd._verify_summary_failed_verification({"failure_class": "assertion"}) is True
    # The real literal is no longer a skill class, so it must stop counting as one.
    assert outcome_cmd._verify_summary_failed_verification({"failure_class": "verification"}) is False
    # The status fallback must consult the patched infrastructure set.
    assert outcome_cmd._verify_summary_failed_verification({"status": "failed", "failure_class": "harness"}) is False


def test_nested_only_infrastructure_kind_is_neutral_with_evidence_ref(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "nested-branch-drift",
        status="failed",
        failure={"kind": "branch-head-drift", "phase": "run-isolation"},
    )
    _write_worker_results(tmp_path, "nested-branch-drift", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "nested-branch-drift")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_legacy_top_level_failure_kind_fallback_still_works(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "legacy-worker-failure",
        status="failed",
        failure_kind="worker-failure",
    )
    _write_worker_results(tmp_path, "legacy-worker-failure", results=[_infra_worker()])

    rc, payload = _capture_signal(tmp_path, "legacy-worker-failure")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_legacy_top_level_only_infrastructure_kind_is_neutral(tmp_path):
    # A receipt written before nested failure taxonomy existed: top-level kind only,
    # no nested failure block. The fallback must still neutralize it.
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "legacy-branch-drift",
        status="failed",
        failure_kind="branch-head-drift",
    )
    _write_worker_results(tmp_path, "legacy-branch-drift", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "legacy-branch-drift")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_legacy_top_level_catch_all_uses_top_level_phase(tmp_path):
    # Top-level kind + top-level phase, no nested block: catch-all in a model phase
    # must stay negative rather than fall through to the legacy neutral path.
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "legacy-agent-error-planning",
        status="failed",
        failure_kind="agent-error",
        failure_phase="planning",
    )
    _write_worker_results(tmp_path, "legacy-agent-error-planning", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "legacy-agent-error-planning")

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_nested_failure_kind_wins_when_both_present(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "nested-wins",
        status="failed",
        failure_kind="orchestrator-error",
        failure_phase="planning",
        failure={"kind": "branch-head-drift", "phase": "run-isolation"},
    )
    _write_worker_results(tmp_path, "nested-wins", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "nested-wins")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_catch_all_in_infrastructure_phase_is_neutral(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "agent-error-dispatch",
        status="failed",
        failure={"kind": "agent-error", "phase": "dispatch"},
    )
    _write_worker_results(tmp_path, "agent-error-dispatch", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "agent-error-dispatch")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_catch_all_in_model_phase_stays_negative(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "orchestrator-planning",
        status="failed",
        failure={"kind": "orchestrator-error", "phase": "planning"},
    )
    _write_worker_results(tmp_path, "orchestrator-planning", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "orchestrator-planning")

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_catch_all_with_missing_phase_stays_negative(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "agent-error-no-phase",
        status="failed",
        failure={"kind": "agent-error"},
    )
    _write_worker_results(tmp_path, "agent-error-no-phase", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "agent-error-no-phase")

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_catch_all_with_unknown_phase_stays_negative(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "unexpected-unknown-phase",
        status="failed",
        failure={"kind": "unexpected-error", "phase": "artifact-collection"},
    )
    _write_worker_results(tmp_path, "unexpected-unknown-phase", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "unexpected-unknown-phase")

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


@pytest.mark.parametrize(
    ("kind", "phase"),
    [
        ("invalid-plan", "dispatch"),
        ("non-final-output", "run-isolation"),
    ],
)
def test_model_contract_failures_stay_negative_even_in_infrastructure_phase(tmp_path, kind, phase):
    run_id = f"model-contract-{kind}"
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        run_id,
        status="failed",
        failure={"kind": kind, "phase": phase},
    )
    _write_worker_results(tmp_path, run_id, results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, run_id)

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_neither_failure_field_present_behavior_unchanged(tmp_path):
    run_json = _write_run_receipt_with_failure(tmp_path, "no-failure-fields", status="failed")
    _write_worker_results(tmp_path, "no-failure-fields", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "no-failure-fields")

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_run_580_regression_nested_branch_head_drift_all_workers_ok(tmp_path):
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "run-580-regression",
        status="failed",
        failure={"kind": "branch-head-drift", "phase": "run-isolation"},
    )
    _write_worker_results(
        tmp_path,
        "run-580-regression",
        results=[_ok_worker(worker="planner"), _ok_worker(worker="implementer")],
    )

    rc, payload = _capture_signal(tmp_path, "run-580-regression")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)


def test_run_failure_phase_sets_are_disjoint():
    """The two phase sets must not overlap, or the catch-all rule is ambiguous."""
    assert not (worker_failure.RUN_INFRASTRUCTURE_FAILURE_PHASES & worker_failure.RUN_MODEL_FAILURE_PHASES)


@pytest.mark.parametrize("phase", sorted(worker_failure.RUN_MODEL_FAILURE_PHASES))
def test_catch_all_in_every_declared_model_phase_stays_negative(tmp_path, phase):
    """Pins the enumerated policy: no model phase may neutralize a catch-all kind.

    The rule is implemented as "not an infrastructure phase", so this locks the
    declared model phases against someone later adding one to the infra set.
    """
    run_id = f"catch-all-{phase}"
    run_json = _write_run_receipt_with_failure(
        tmp_path,
        run_id,
        status="failed",
        failure={"kind": "agent-error", "phase": phase},
    )
    _write_worker_results(tmp_path, run_id, results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, run_id)

    assert rc == 0
    assert payload["record"]["signal_value"] == -1
    assert payload["record"]["evidence_ref"] == str(run_json)


@pytest.mark.parametrize("failure_class", list(worker_failure.FailureClass))
def test_failure_class_kind_is_infrastructure_at_read_time(failure_class):
    """Typed abort receipts write FailureClass values; all are infrastructure-neutral."""

    assert worker_failure.run_failure_is_infrastructure_at_read_time(failure_class.value, "preflight") is True


def test_isolation_breach_failure_class_is_infrastructure_at_read_time():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("isolation-breach", "postflight") is True


def test_model_contract_kind_wins_over_legacy_failure_class_map():
    # ``non-final-output`` maps to ``output-contract-violation`` in LEGACY_FAILURE_KIND_MAP,
    # but RUN_MODEL_CONTRACT_FAILURE_KINDS must win before the FailureClass check.
    assert worker_failure.run_failure_is_infrastructure_at_read_time("non-final-output", "run-isolation") is False
    assert worker_failure.run_failure_is_infrastructure_at_read_time("invalid-plan", "dispatch") is False


def test_catch_all_classifier_in_model_phase_stays_negative():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("agent-error", "planning") is False


def test_catch_all_classifier_missing_phase_fails_closed():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("agent-error", None) is False


def test_catch_all_classifier_unknown_phase_fails_closed():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("unexpected-error", "artifact-collection") is False


def test_slice_b_typed_failure_class_run_receipt_is_neutral(tmp_path):
    """Slice-B abort writes FailureClass values into nested failure.kind; capture stays neutral."""

    run_json = _write_run_receipt_with_failure(
        tmp_path,
        "slice-b-auth-required",
        status="failed",
        failure={"kind": "auth-required", "phase": "preflight"},
    )
    _write_worker_results(tmp_path, "slice-b-auth-required", results=[_ok_worker()])

    rc, payload = _capture_signal(tmp_path, "slice-b-auth-required")

    assert rc == 0
    assert payload["record"]["signal_value"] == 0
    assert payload["record"]["evidence_ref"] == str(run_json)
