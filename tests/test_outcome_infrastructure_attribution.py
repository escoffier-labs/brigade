"""Outcome and friction attribution for worker infrastructure failures (#580)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import friction_cmd, localio, outcome, outcome_cmd, verify_trial

from tests.test_outcome_cmd import _write_run_receipt, _write_verify_receipt


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
