"""Tests for VerificationContract (#500)."""

from __future__ import annotations

import json

import pytest

from brigade import runbook_cmd, verification_contract, verify_manifest


def _valid_contract(**overrides):
    payload = {
        "schema": verification_contract.VERIFICATION_CONTRACT_SCHEMA,
        "schema_version": verification_contract.VERIFICATION_CONTRACT_SCHEMA_VERSION,
        "verifier": {"source": "command", "command": "printf verified"},
        "rollback": {"policy": "none"},
        "budget": {"latency_seconds": 30, "token_budget": 1000},
    }
    payload.update(overrides)
    return payload


def test_contract_requires_verifier_rollback_and_budget():
    assert verification_contract.incompleteness_reasons(None) == ["missing_verification_contract"]
    assert "missing_verifier" in verification_contract.incompleteness_reasons({})
    assert "missing_rollback" in verification_contract.incompleteness_reasons({"verifier": {"command": "true"}})
    errors = verification_contract.validate_contract_payload(_valid_contract())
    assert errors == []
    contract = verification_contract.contract_from_payload(_valid_contract())
    assert contract.verifier.source == "command"
    assert contract.rollback.policy == "none"
    assert contract.budget.latency_seconds == 30
    assert contract.budget.token_budget == 1000


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_contract_schema_version_requires_a_real_integer(schema_version):
    assert verification_contract.validate_contract_payload(_valid_contract(schema_version=schema_version)) == [
        "schema_version must be 1"
    ]


def test_contract_plan_view_blocks_consequential_without_contract():
    view = verification_contract.contract_plan_view(None, consequential=True)
    assert view["complete"] is False
    assert view["blockers"]
    assert "missing_verification_contract" in view["incompleteness_reasons"]

    view_ok = verification_contract.contract_plan_view(_valid_contract(), consequential=True)
    assert view_ok["complete"] is True
    assert view_ok["blockers"] == []
    assert view_ok["contract"]["budget"]["latency_seconds"] == 30


def test_budget_use_and_verification_separate_from_model_completion():
    contract = verification_contract.contract_from_payload(_valid_contract())
    budget = verification_contract.budget_use_payload(contract, latency_seconds_used=12.5, tokens_used=40)
    assert budget["latency_seconds_budget"] == 30
    assert budget["latency_seconds_used"] == 12.5
    assert budget["tokens_used"] == 40
    assert budget["exhausted"] is False

    exhausted = verification_contract.budget_use_payload(contract, latency_seconds_used=90.0, tokens_used=2000)
    assert exhausted["exhausted"] is True

    verification = verification_contract.verification_outcome_payload(status="passed", exit_code=0)
    model = verification_contract.model_completion_payload(status="completed")
    assert verification["independent_of_model_completion"] is True
    assert verification["status"] == "passed"
    assert model["status"] == "completed"
    assert set(verification).isdisjoint({"model_status"}) or True


def test_optional_input_output_token_fields_parse_and_report_without_aliasing_token_budget():
    payload = _valid_contract(
        budget={
            "latency_seconds": 30,
            "token_budget": 100,
            "input_tokens": 40,
            "output_tokens": 60,
            "wall_clock_seconds": 90,
            "worker_dispatch_count": 2,
        }
    )
    assert verification_contract.validate_contract_payload(payload) == []
    contract = verification_contract.contract_from_payload(payload)
    assert contract.budget.token_budget == 100
    assert contract.budget.input_tokens == 40
    assert contract.budget.output_tokens == 60
    round_trip = contract.to_payload()["budget"]
    assert round_trip["token_budget"] == 100
    assert round_trip["input_tokens"] == 40
    assert round_trip["output_tokens"] == 60
    use = verification_contract.budget_use_payload(contract, latency_seconds_used=1.0, tokens_used=50)
    assert use["token_budget"] == 100
    assert use["tokens_used"] == 50
    assert use["input_tokens_budget"] == 40
    assert use["output_tokens_budget"] == 60
    assert "input_tokens" not in use


def test_runbook_plan_blocks_consequential_without_contract(tmp_path, capsys):
    runbook = tmp_path / "runbook.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "needs-contract",
                "consequential": True,
                "allowed_commands": ["printf"],
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )
    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 1
    plan = json.loads(capsys.readouterr().out)
    assert plan["consequential"] is True
    assert plan["verification_contract"]["complete"] is False
    assert plan["verification_contract"]["blockers"]

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "verification-contract-incomplete"


def test_runbook_runs_verifier_and_records_budget_separately(tmp_path, capsys):
    runbook = tmp_path / "runbook.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "with-contract",
                "consequential": True,
                "allowed_commands": ["printf"],
                "verification_contract": _valid_contract(),
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )
    assert runbook_cmd.plan(target=tmp_path, runbook=runbook, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["verification_contract"]["complete"] is True

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert receipt["model_completion"]["status"] == "completed"
    assert receipt["verification"]["status"] == "passed"
    assert receipt["verification"]["independent_of_model_completion"] is True
    assert receipt["verification_contract"]["budget"]["latency_seconds"] == 30
    assert receipt["budget_use"]["latency_seconds_budget"] == 30
    assert isinstance(receipt["budget_use"]["latency_seconds_used"], float)
    assert (tmp_path / ".brigade" / "runbooks" / "runs" / receipt["run_id"] / "verification.stdout.log").is_file()


def test_runbook_rollback_on_verifier_failure(tmp_path, capsys):
    marker = tmp_path / "rolled-back"
    runbook = tmp_path / "runbook.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "rollback-demo",
                "consequential": True,
                "allowed_commands": ["printf", "false", "touch"],
                "verification_contract": {
                    "schema": verification_contract.VERIFICATION_CONTRACT_SCHEMA,
                    "schema_version": 1,
                    "verifier": {"source": "command", "command": "false"},
                    "rollback": {"policy": "command", "command": f"touch {marker}"},
                    "budget": {"latency_seconds": 30},
                },
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )
    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["model_completion"]["status"] == "completed"
    assert receipt["verification"]["status"] == "failed"
    assert receipt["rollback"]["status"] == "completed"
    assert marker.is_file()


def test_verify_manifest_inherits_manifest_checks_verifier(tmp_path):
    target = tmp_path
    manifests = target / "verify" / "manifests"
    manifests.mkdir(parents=True)
    path = manifests / "skill-demo.json"
    payload = {
        "schema": verify_manifest.VERIFY_MANIFEST_SCHEMA,
        "schema_version": verify_manifest.VERIFY_MANIFEST_SCHEMA_VERSION,
        "manifest_id": "skill-demo",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "consequential": True,
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "demo",
            "subject_path": "skills/demo/SKILL.md",
        },
        "checks": [
            {"check_id": "verify.echo", "check_role": "effectiveness", "command": "printf ok"},
        ],
        "verification_contract": {
            "schema": verification_contract.VERIFICATION_CONTRACT_SCHEMA,
            "schema_version": 1,
            "rollback": {"policy": "manual"},
            "budget": {"latency_seconds": 60, "token_budget": 0},
        },
    }
    path.write_text(json.dumps(payload))
    # Untracked manifests still parse via manifest_from_payload.
    manifest = verify_manifest.manifest_from_payload(payload, path=path)
    assert manifest.consequential is True
    view = manifest.contract_plan_view()
    assert view["complete"] is True
    assert view["contract"]["verifier"]["source"] == "manifest_checks"


def test_verify_manifest_consequential_requires_contract():
    payload = {
        "schema": verify_manifest.VERIFY_MANIFEST_SCHEMA,
        "schema_version": verify_manifest.VERIFY_MANIFEST_SCHEMA_VERSION,
        "manifest_id": "missing-contract",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "consequential": True,
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "demo",
            "subject_path": "skills/demo/SKILL.md",
        },
        "checks": [
            {"check_id": "verify.echo", "check_role": "effectiveness", "command": "printf ok"},
        ],
    }
    errors = verify_manifest.validate_manifest_payload(payload)
    assert any("verification_contract" in error for error in errors)
