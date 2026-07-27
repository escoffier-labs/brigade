"""Receipt-only skill scorecard projection (#572)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from brigade import cli, localio, outcome, outcome_cmd, scorecard, verify_manifest, verify_trial

from tests.test_verify_trial import (
    _track_workspace_manifest,
    _write_fixture_manifest,
    _write_skill_subject,
)

pytest_plugins = ["tests.test_verify_trial"]

_SCORECARD_MANIFEST_ID = "scorecard-skill-fixture"
_SCORECARD_PASSING_MANIFEST_ID = "scorecard-skill-fixture-passing"
_SCORECARD_ALT_CHECK_MANIFEST_ID = "scorecard-skill-fixture-alt"
_SCORECARD_UTILITY_MANIFEST_ID = "scorecard-skill-fixture-utility"
_SCORECARD_UTILITY_PASSING_MANIFEST_ID = "scorecard-skill-fixture-utility-passing"


def _write_scorecard_manifest(target: Path, *, manifest_id: str, case_id: str, check_id: str) -> dict:
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": manifest_id,
        "binding_mode": "fixture_eval",
        "verifier_id": "brigade.verify.fixture",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "skills/brigade-work/SKILL.md",
        },
        "fixture": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": case_id,
            "check_id": check_id,
        },
        "checks": [
            {
                "check_id": check_id,
                "check_role": "effectiveness",
                "command": f"{sys.executable} -c \"print('ok')\"",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(target, payload)
    _track_workspace_manifest(target, manifest_id)
    return payload


def _ensure_scorecard_manifest(target: Path, *, manifest_id: str = _SCORECARD_MANIFEST_ID) -> dict:
    if manifest_id not in verify_manifest.registered_manifest_ids(target):
        if manifest_id == _SCORECARD_PASSING_MANIFEST_ID:
            _write_scorecard_manifest(
                target,
                manifest_id=manifest_id,
                case_id="report-passing-tests",
                check_id="fixture.echo-ok",
            )
        elif manifest_id == _SCORECARD_ALT_CHECK_MANIFEST_ID:
            _write_scorecard_manifest(
                target,
                manifest_id=manifest_id,
                case_id="report-failing-tests",
                check_id="fixture.echo-alt",
            )
        elif manifest_id in {_SCORECARD_UTILITY_MANIFEST_ID, _SCORECARD_UTILITY_PASSING_MANIFEST_ID}:
            case_id = (
                "report-passing-tests"
                if manifest_id == _SCORECARD_UTILITY_PASSING_MANIFEST_ID
                else "report-failing-tests"
            )
            payload = {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": manifest_id,
                "binding_mode": "fixture_eval",
                "verifier_id": "brigade.verify.fixture",
                "subject": {
                    "artifact_kind": "skill",
                    "artifact_id": "brigade-work",
                    "subject_path": "skills/brigade-work/SKILL.md",
                },
                "fixture": {
                    "manifest_id": "adversarial-failed-verification",
                    "case_id": case_id,
                    "check_id": "fixture.echo-ok",
                },
                "required_utility_check_ids": ["guardrail.tests-green", "guardrail.lint-clean"],
                "checks": [
                    {
                        "check_id": "fixture.echo-ok",
                        "check_role": "effectiveness",
                        "command": f"{sys.executable} -c \"print('ok')\"",
                    },
                    {
                        "check_id": "guardrail.tests-green",
                        "check_role": "utility_guardrail",
                        "command": f"{sys.executable} -c \"print('ok')\"",
                    },
                    {
                        "check_id": "guardrail.lint-clean",
                        "check_role": "utility_guardrail",
                        "command": f"{sys.executable} -c \"print('ok')\"",
                    },
                ],
            }
            verify_manifest.write_workspace_manifest(target, payload)
            _track_workspace_manifest(target, manifest_id)
        else:
            _write_fixture_manifest(target, manifest_id=manifest_id)
    manifest, _error = verify_manifest.resolve_manifest(target, manifest_id)
    assert manifest is not None
    return verify_manifest.build_manifest_binding(target, manifest)


def _fixture_binding(
    *,
    artifact_id: str = "brigade-work",
    fingerprint: str | None = None,
    target: Path,
    manifest_id: str = _SCORECARD_MANIFEST_ID,
    case_id: str = "report-failing-tests",
    check_id: str = "fixture.echo-ok",
) -> dict:
    manifest_binding = _ensure_scorecard_manifest(target, manifest_id=manifest_id)
    return {
        "binding_mode": "fixture_eval",
        "artifact_kind": "skill",
        "artifact_id": artifact_id,
        "content_fingerprint": fingerprint or "sha256:" + "a" * 64,
        "fixture_binding": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": case_id,
            "check_id": check_id,
        },
        "manifest_binding": manifest_binding,
        "verifier_identity": {"verifier_id": "brigade.verify.fixture", "session_id": "verifier-1"},
    }


def _write_verify_receipt(
    target,
    run_id: str,
    *,
    binding: dict | None = None,
    status: str = "completed",
    commands: list[dict] | None = None,
    started_at: str = "2026-06-20T00:00:00+00:00",
    duration_seconds: float = 10.0,
    reused_from: str | None = None,
    required_utility_check_ids: list[str] | None = None,
    extra: dict | None = None,
) -> str:
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2,
        "run_id": run_id,
        "target": str(target),
        "status": status,
        "started_at": started_at,
        "completed_at": started_at,
        "duration_seconds": duration_seconds,
        "path": str(run_dir),
        "commands": commands or [],
    }
    if binding is not None:
        receipt["subject_binding"] = binding
        manifest_binding = binding.get("manifest_binding")
        if isinstance(manifest_binding, dict) and isinstance(manifest_binding.get("manifest_id"), str):
            receipt["verify_manifest_id"] = manifest_binding["manifest_id"]
    if reused_from is not None:
        receipt["reused_from"] = reused_from
    if required_utility_check_ids is not None:
        receipt["required_utility_check_ids"] = required_utility_check_ids
    if extra:
        receipt.update(extra)
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    for command in receipt["commands"]:
        if isinstance(command, dict):
            verify_trial.stamp_verify_command_failure_taxonomy(command)
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    localio.write_json(run_dir / "receipt.json", receipt)
    return str(run_dir / "receipt.json")


def _effectiveness_command(
    *,
    ok: bool = True,
    check_id: str = "fixture.echo-ok",
    duration_seconds: float | None = None,
) -> dict:
    command = {
        "command": f"{sys.executable} -c \"print('ok')\"",
        "check_role": "effectiveness",
        "check_id": check_id,
        "status": "completed" if ok else "failed",
        "exit_code": 0 if ok else 1,
    }
    if duration_seconds is not None:
        command["duration_seconds"] = duration_seconds
    verify_trial.stamp_verify_command_failure_taxonomy(command)
    return command


def _utility_command(
    *,
    ok: bool = True,
    check_id: str = "guardrail.tests-green",
    duration_seconds: float | None = None,
) -> dict:
    command = {
        "command": f"{sys.executable} -c \"print('ok')\"",
        "check_role": "utility_guardrail",
        "check_id": check_id,
        "status": "completed" if ok else "failed",
        "exit_code": 0 if ok else 1,
    }
    if duration_seconds is not None:
        command["duration_seconds"] = duration_seconds
    verify_trial.stamp_verify_command_failure_taxonomy(command)
    return command


def test_build_scorecard_projects_all_dimensions(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding_a = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    binding_b = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_PASSING_MANIFEST_ID,
        case_id="report-passing-tests",
    )
    _write_verify_receipt(
        scoreable_target,
        "run-a",
        binding=binding_a,
        commands=[_effectiveness_command()],
        duration_seconds=12.0,
    )
    _write_verify_receipt(
        scoreable_target,
        "run-b",
        binding=binding_b,
        commands=[_effectiveness_command()],
        started_at="2026-06-20T01:00:00+00:00",
        duration_seconds=20.0,
    )

    cards = scorecard.build_scorecards(scoreable_target)
    assert len(cards) == 1
    card = cards[0]
    assert card.subject.artifact_id == "brigade-work"
    assert card.dimensions["effectiveness"]["helped"] == 2
    assert card.dimensions["effectiveness"]["hurt"] == 0
    assert card.dimensions["effectiveness"]["trials"] == 2
    assert card.dimensions["verifier_cost"]["median_s"] == 16.0
    assert card.dimensions["verifier_cost"]["receipt_median_s"] == 16.0
    assert card.dimensions["verifier_cost"]["receipt_samples"] == 2
    assert card.dimensions["evidence_integrity"]["eligible"] == 2
    assert card.dimensions["evidence_integrity"]["audit"] == 2


def test_utility_only_failure_does_not_hurt_effectiveness(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_UTILITY_MANIFEST_ID,
    )
    _write_verify_receipt(
        scoreable_target,
        "utility-fail",
        binding=binding,
        status="failed",
        commands=[
            _effectiveness_command(),
            _utility_command(ok=True, check_id="guardrail.tests-green"),
            _utility_command(ok=False, check_id="guardrail.lint-clean"),
        ],
        required_utility_check_ids=["guardrail.tests-green", "guardrail.lint-clean"],
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["helped"] == 1
    assert card.dimensions["effectiveness"]["hurt"] == 0


def test_reused_and_duplicate_evidence_units_do_not_inflate_effectiveness(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    commands = [_effectiveness_command()]
    _write_verify_receipt(scoreable_target, "run-1", binding=binding, commands=commands)
    _write_verify_receipt(
        scoreable_target,
        "run-2",
        binding=binding,
        commands=commands,
        started_at="2026-06-20T01:00:00+00:00",
    )
    _write_verify_receipt(
        scoreable_target,
        "run-3",
        binding=binding,
        commands=commands,
        started_at="2026-06-20T02:00:00+00:00",
        reused_from="run-1",
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["trials"] == 1
    assert card.dimensions["effectiveness"]["helped"] == 1


def test_ineligible_only_subject_stays_visible_with_reason_counts(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(
        scoreable_target,
        "missing-role",
        binding=binding,
        commands=[{"status": "completed", "exit_code": 0, "check_id": "fixture.echo-ok"}],
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["trials"] == 0
    assert card.ineligible_summary["missing_check_role"] == 1


def test_rank_json_emits_scorecard_dimensions(scoreable_target, capsys):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(scoreable_target, "run-a", binding=binding, commands=[_effectiveness_command()])

    assert outcome_cmd.rank(target=scoreable_target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["ranking"][0]
    assert entry["artifact_id"] == "brigade-work"
    assert entry["policy_version"] == scorecard.SCORECARD_POLICY_VERSION
    assert "effectiveness" in entry["dimensions"]
    assert "utility_guardrails" in entry


def test_explain_json_includes_receipt_trail_keyed_by_subject_binding(scoreable_target, capsys):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(scoreable_target, "run-a", binding=binding, commands=[_effectiveness_command()])

    assert outcome_cmd.explain(target=scoreable_target, artifact_id="brigade-work", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "scorecard" in payload
    trail = payload["scorecard"]["receipt_trail"]
    assert len(trail) == 1
    assert trail[0]["subject_binding"]["artifact_id"] == "brigade-work"
    assert trail[0]["eligible"] is True


def test_poisoned_non_verifier_inputs_do_not_change_dimensions(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(scoreable_target, "run-a", binding=binding, commands=[_effectiveness_command()])

    baseline = scorecard.subject_scorecard_to_dict(scorecard.scorecard_for_artifact(scoreable_target, "brigade-work"))

    # Ledger rows with a different caller artifact id must not affect scorecard values.
    outcome_cmd.append_records(
        scoreable_target,
        [
            outcome.OutcomeRecord(
                "poison-skill",
                "skill",
                "t1",
                "verify",
                1,
                "fake",
                "2026-06-20T00:00:00+00:00",
            )
        ],
    )
    run_dir = scoreable_target / ".brigade" / "runs" / "poison-run"
    run_dir.mkdir(parents=True)
    localio.write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "suspected_noop": False,
            "context_eval": {"brief_hit_rate": 1.0},
            "duration_seconds": 9999,
        },
    )
    closeout_dir = scoreable_target / ".brigade" / "work" / "closeouts" / "poison-closeout"
    closeout_dir.mkdir(parents=True)
    localio.write_json(closeout_dir / "closeout.json", {"status": "ready", "ready": True})

    after = scorecard.subject_scorecard_to_dict(scorecard.scorecard_for_artifact(scoreable_target, "brigade-work"))
    assert after == baseline


def test_cli_outcome_explain_dispatch(scoreable_target, capsys):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(scoreable_target, "run-a", binding=binding, commands=[_effectiveness_command()])

    assert cli.main(["outcome", "explain", "brigade-work", "--target", str(scoreable_target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scorecard"]["subject"]["artifact_id"] == "brigade-work"


def test_different_check_ids_with_same_command_do_not_dedupe(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding_a = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    binding_b = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_ALT_CHECK_MANIFEST_ID,
        check_id="fixture.echo-alt",
    )
    shared_command = f"{sys.executable} -c \"print('ok')\""
    cmd_a = {
        "command": shared_command,
        "check_role": "effectiveness",
        "check_id": "fixture.echo-ok",
        "status": "completed",
        "exit_code": 0,
    }
    cmd_b = {
        "command": shared_command,
        "check_role": "effectiveness",
        "check_id": "fixture.echo-alt",
        "status": "completed",
        "exit_code": 0,
    }
    verify_trial.stamp_verify_command_failure_taxonomy(cmd_a)
    verify_trial.stamp_verify_command_failure_taxonomy(cmd_b)
    _write_verify_receipt(scoreable_target, "check-a", binding=binding_a, commands=[cmd_a])
    _write_verify_receipt(
        scoreable_target,
        "check-b",
        binding=binding_b,
        commands=[cmd_b],
        started_at="2026-06-20T01:00:00+00:00",
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["trials"] == 2
    assert card.dimensions["effectiveness"]["helped"] == 2


def test_same_check_retry_sequence_records_transitions_and_flips(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    commands_fail = [_effectiveness_command(ok=False)]
    commands_pass = [_effectiveness_command(ok=True)]
    _write_verify_receipt(scoreable_target, "retry-1", binding=binding, commands=commands_fail)
    _write_verify_receipt(
        scoreable_target,
        "retry-2",
        binding=binding,
        commands=commands_pass,
        started_at="2026-06-20T01:00:00+00:00",
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    stability = card.dimensions["retry_stability"]
    assert stability["sequences"] == 1
    assert stability["fail_to_pass"] == 1
    assert stability["flips"] == 1
    assert stability["first_pass_yield"] == 0.0


def test_digest_missing_is_ineligible_not_hurt(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    run_dir = scoreable_target / ".brigade" / "work" / "verify-runs" / "digest-missing"
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2,
        "run_id": "digest-missing",
        "target": str(scoreable_target),
        "status": "completed",
        "started_at": "2026-06-20T00:00:00+00:00",
        "completed_at": "2026-06-20T00:00:00+00:00",
        "duration_seconds": 10.0,
        "path": str(run_dir),
        "commands": [_effectiveness_command()],
        "verify_manifest_id": _SCORECARD_MANIFEST_ID,
        "subject_binding": binding,
    }
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    localio.write_json(run_dir / "receipt.json", receipt)

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["hurt"] == 0
    assert card.dimensions["effectiveness"]["trials"] == 0
    assert card.ineligible_summary["receipt_digest_missing"] == 1


def test_digest_mismatch_is_ineligible_not_hurt(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    run_dir = scoreable_target / ".brigade" / "work" / "verify-runs" / "digest-bad"
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 2,
        "run_id": "digest-bad",
        "target": str(scoreable_target),
        "status": "completed",
        "started_at": "2026-06-20T00:00:00+00:00",
        "completed_at": "2026-06-20T00:00:00+00:00",
        "duration_seconds": 10.0,
        "path": str(run_dir),
        "commands": [_effectiveness_command()],
        "verify_manifest_id": _SCORECARD_MANIFEST_ID,
        "subject_binding": binding,
        "digests": {
            "algorithm": "sha256",
            "logs": {},
            "receipt_sha256": "c" * 64,
        },
    }
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    localio.write_json(run_dir / "receipt.json", receipt)

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["hurt"] == 0
    assert card.dimensions["effectiveness"]["trials"] == 0
    assert card.ineligible_summary["receipt_digest_mismatch"] == 1


def test_verifier_cost_includes_command_duration_stats(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding_a = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    binding_b = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_PASSING_MANIFEST_ID,
        case_id="report-passing-tests",
    )
    _write_verify_receipt(
        scoreable_target,
        "cmd-cost-a",
        binding=binding_a,
        commands=[_effectiveness_command(duration_seconds=4.0)],
        duration_seconds=12.0,
    )
    _write_verify_receipt(
        scoreable_target,
        "cmd-cost-b",
        binding=binding_b,
        commands=[_effectiveness_command(duration_seconds=8.0)],
        started_at="2026-06-20T01:00:00+00:00",
        duration_seconds=20.0,
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    cost = card.dimensions["verifier_cost"]
    assert cost["receipt_median_s"] == 16.0
    assert cost["receipt_samples"] == 2
    assert cost["command_median_s"] == 6.0
    assert cost["command_samples"] == 2
    assert cost["median_s"] == cost["receipt_median_s"]


def test_utility_guardrails_track_per_check_passing_and_failing_units(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding_a = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_UTILITY_MANIFEST_ID,
    )
    binding_b = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_UTILITY_PASSING_MANIFEST_ID,
        case_id="report-passing-tests",
    )
    _write_verify_receipt(
        scoreable_target,
        "utility-pass",
        binding=binding_a,
        commands=[
            _effectiveness_command(),
            _utility_command(ok=True, check_id="guardrail.tests-green"),
            _utility_command(ok=True, check_id="guardrail.lint-clean"),
        ],
        required_utility_check_ids=["guardrail.tests-green", "guardrail.lint-clean"],
    )
    _write_verify_receipt(
        scoreable_target,
        "utility-fail",
        binding=binding_b,
        commands=[
            _effectiveness_command(),
            _utility_command(ok=True, check_id="guardrail.tests-green"),
            _utility_command(ok=False, check_id="guardrail.lint-clean"),
        ],
        status="failed",
        started_at="2026-06-20T01:00:00+00:00",
        required_utility_check_ids=["guardrail.tests-green", "guardrail.lint-clean"],
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    guardrails = card.utility_guardrails
    assert guardrails["passing_trials"] == 1
    per_check = guardrails["per_check"]
    assert per_check["guardrail.tests-green"]["passing_units"] == 2
    assert per_check["guardrail.tests-green"]["failing_units"] == 0
    assert per_check["guardrail.lint-clean"]["passing_units"] == 1
    assert per_check["guardrail.lint-clean"]["failing_units"] == 1


def test_forged_receipt_is_audit_visible_but_ineligible(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    binding.pop("manifest_binding")
    _write_verify_receipt(scoreable_target, "forged", binding=binding, commands=[_effectiveness_command()])

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["trials"] == 0
    assert card.ineligible_summary["verifier_manifest_missing"] == 1
    assert len(card.receipt_trail) == 1
    assert card.receipt_trail[0].eligible is False


def test_utility_evidence_is_independent_of_effectiveness_plan(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    utility_manifest_b = "scorecard-skill-fixture-utility-alt-plan"
    if utility_manifest_b not in verify_manifest.registered_manifest_ids(scoreable_target):
        payload = {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": utility_manifest_b,
            "binding_mode": "fixture_eval",
            "verifier_id": "brigade.verify.fixture",
            "subject": {
                "artifact_kind": "skill",
                "artifact_id": "brigade-work",
                "subject_path": "skills/brigade-work/SKILL.md",
            },
            "fixture": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-failing-tests",
                "check_id": "fixture.echo-ok",
            },
            "required_utility_check_ids": ["guardrail.tests-green", "guardrail.lint-clean"],
            "checks": [
                {
                    "check_id": "fixture.echo-ok",
                    "check_role": "effectiveness",
                    "command": f"{sys.executable} -c \"print('alt')\"",
                },
                {
                    "check_id": "guardrail.tests-green",
                    "check_role": "utility_guardrail",
                    "command": f"{sys.executable} -c \"print('ok')\"",
                },
                {
                    "check_id": "guardrail.lint-clean",
                    "check_role": "utility_guardrail",
                    "command": f"{sys.executable} -c \"print('ok')\"",
                },
            ],
        }
        verify_manifest.write_workspace_manifest(scoreable_target, payload)
        _track_workspace_manifest(scoreable_target, utility_manifest_b)
    binding_a = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_UTILITY_MANIFEST_ID,
    )
    binding_b = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=utility_manifest_b,
    )
    _write_verify_receipt(
        scoreable_target,
        "utility-plan-a",
        binding=binding_a,
        commands=[
            _effectiveness_command(),
            _utility_command(ok=True, check_id="guardrail.tests-green"),
            _utility_command(ok=True, check_id="guardrail.lint-clean"),
        ],
        required_utility_check_ids=["guardrail.tests-green", "guardrail.lint-clean"],
    )
    _write_verify_receipt(
        scoreable_target,
        "utility-plan-b",
        binding=binding_b,
        commands=[
            _effectiveness_command(check_id="fixture.echo-ok"),
            _utility_command(ok=True, check_id="guardrail.tests-green"),
            _utility_command(ok=True, check_id="guardrail.lint-clean"),
        ],
        started_at="2026-06-20T01:00:00+00:00",
        required_utility_check_ids=["guardrail.tests-green", "guardrail.lint-clean"],
    )
    alt_effectiveness = _effectiveness_command(check_id="fixture.echo-ok")
    alt_effectiveness["command"] = f"{sys.executable} -c \"print('alt')\""
    receipt_path = scoreable_target / ".brigade" / "work" / "verify-runs" / "utility-plan-b" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["commands"][0] = alt_effectiveness
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    localio.write_json(receipt_path, receipt)

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    per_check = card.utility_guardrails["per_check"]
    assert per_check["guardrail.tests-green"]["passing_units"] == 1
    assert per_check["guardrail.lint-clean"]["passing_units"] == 1
