"""Dual-criterion scorecard promotion gate for outcome reconcile/fork (#503)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from brigade import cli, localio, outcome, outcome_cmd, scorecard, verify_manifest, verify_trial

from tests.test_scorecard import (
    _SCORECARD_ALT_CHECK_MANIFEST_ID,
    _SCORECARD_UTILITY_MANIFEST_ID,
    _SCORECARD_UTILITY_PASSING_MANIFEST_ID,
    _effectiveness_command,
    _ensure_scorecard_manifest,
    _fixture_binding,
    _utility_command,
    _write_verify_receipt,
)
from tests.test_verify_trial import _write_skill_subject

pytest_plugins = ["tests.test_scorecard"]

_REQUIRED_UTILITY = ["guardrail.tests-green", "guardrail.lint-clean"]


def _ensure_git_repo(target: Path) -> None:
    if (target / ".git").is_dir():
        return
    subprocess.run(["git", "init"], cwd=target, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=target,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _status_file(target: Path) -> Path:
    return target / "memory" / "outcome" / "status.json"


def _decisions_dir(target: Path) -> Path:
    return target / "memory" / "outcome" / "decisions"


def _stub_execute(monkeypatch, *, install: str = "installed") -> None:
    def _fake(target, artifact_id, action):
        return install if action == "install" else "reverted:claude:uninstall"

    monkeypatch.setattr(outcome_cmd, "_execute_skill_decision", _fake)


def _skill_fingerprint(target: Path, artifact_id: str = "brigade-work") -> str:
    skill = target / "skills" / artifact_id / "SKILL.md"
    return verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))


def _utility_pass_commands(*, lint_ok: bool = True) -> list[dict]:
    return [
        _effectiveness_command(),
        _utility_command(ok=True, check_id="guardrail.tests-green"),
        _utility_command(ok=lint_ok, check_id="guardrail.lint-clean"),
    ]


def _write_independent_utility_pass(
    target: Path,
    run_id: str,
    *,
    fingerprint: str,
    case_id: str,
    manifest_id: str = _SCORECARD_UTILITY_PASSING_MANIFEST_ID,
    started_at: str = "2026-06-20T00:00:00+00:00",
    lint_ok: bool = True,
    reused_from: str | None = None,
) -> None:
    _ensure_scorecard_manifest(target, manifest_id=manifest_id)
    binding = _fixture_binding(
        fingerprint=fingerprint,
        target=target,
        manifest_id=manifest_id,
        case_id=case_id,
    )
    _write_verify_receipt(
        target,
        run_id,
        binding=binding,
        commands=_utility_pass_commands(lint_ok=lint_ok),
        required_utility_check_ids=_REQUIRED_UTILITY,
        started_at=started_at,
        reused_from=reused_from,
    )


def _write_registry_skill_utility_manifest(
    target: Path,
    skill_id: str,
    *,
    manifest_id: str,
    case_id: str,
) -> None:
    from tests.test_verify_trial import _track_workspace_manifest

    subject_path = f".brigade/skills/registry/{skill_id}/SKILL.md"
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": manifest_id,
        "binding_mode": "fixture_eval",
        "verifier_id": "brigade.verify.fixture",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": skill_id,
            "subject_path": subject_path,
        },
        "fixture": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": case_id,
            "check_id": "fixture.echo-ok",
        },
        "required_utility_check_ids": _REQUIRED_UTILITY,
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


def _registry_binding(
    target: Path,
    skill_id: str,
    fingerprint: str,
    *,
    manifest_id: str,
    case_id: str,
) -> dict:
    manifest, error = verify_manifest.resolve_manifest(target, manifest_id)
    assert manifest is not None, error
    return {
        "binding_mode": "fixture_eval",
        "artifact_kind": "skill",
        "artifact_id": skill_id,
        "content_fingerprint": fingerprint,
        "fixture_binding": {
            "manifest_id": "adversarial-failed-verification",
            "case_id": case_id,
            "check_id": "fixture.echo-ok",
        },
        "manifest_binding": verify_manifest.build_manifest_binding(target, manifest),
        "verifier_identity": {"verifier_id": "brigade.verify.fixture", "session_id": "verifier-1"},
    }


def seed_registry_skill_scorecard_promotion(target: Path, skill_id: str) -> str:
    """Write dual-gate scorecard receipts for a registry skill under test."""
    _ensure_git_repo(target)
    passing_id = f"{skill_id}-utility-passing"
    utility_id = f"{skill_id}-utility"
    for manifest_id, case_id in (
        (passing_id, "report-passing-tests"),
        (utility_id, "report-failing-tests"),
    ):
        if manifest_id not in verify_manifest.registered_manifest_ids(target):
            _write_registry_skill_utility_manifest(
                target,
                skill_id,
                manifest_id=manifest_id,
                case_id=case_id,
            )
    fingerprint = outcome_cmd.artifact_fingerprint(target, skill_id, "skill")
    assert fingerprint is not None
    for run_id, manifest_id, case_id, started_at in (
        (f"{skill_id}-pass-a", passing_id, "report-passing-tests", "2026-06-20T00:00:00+00:00"),
        (f"{skill_id}-pass-b", utility_id, "report-failing-tests", "2026-06-20T01:00:00+00:00"),
    ):
        binding = _registry_binding(
            target,
            skill_id,
            fingerprint,
            manifest_id=manifest_id,
            case_id=case_id,
        )
        _write_verify_receipt(
            target,
            run_id,
            binding=binding,
            commands=_utility_pass_commands(),
            required_utility_check_ids=_REQUIRED_UTILITY,
            started_at=started_at,
        )
    return fingerprint


def seed_registry_skill_scorecard_hurt(target: Path, skill_id: str) -> None:
    _ensure_git_repo(target)
    hurt_id = f"{skill_id}-utility-alt"
    if hurt_id not in verify_manifest.registered_manifest_ids(target):
        from tests.test_verify_trial import _track_workspace_manifest

        subject_path = f".brigade/skills/registry/{skill_id}/SKILL.md"
        payload = {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": hurt_id,
            "binding_mode": "fixture_eval",
            "verifier_id": "brigade.verify.fixture",
            "subject": {
                "artifact_kind": "skill",
                "artifact_id": skill_id,
                "subject_path": subject_path,
            },
            "fixture": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-passing-tests",
                "check_id": "fixture.echo-alt",
            },
            "checks": [
                {
                    "check_id": "fixture.echo-alt",
                    "check_role": "effectiveness",
                    "command": f"{sys.executable} -c \"print('alt')\"",
                }
            ],
        }
        verify_manifest.write_workspace_manifest(target, payload)
        _track_workspace_manifest(target, hurt_id)
    fingerprint = outcome_cmd.artifact_fingerprint(target, skill_id, "skill")
    assert fingerprint is not None
    binding = _registry_binding(
        target,
        skill_id,
        fingerprint,
        manifest_id=hurt_id,
        case_id="report-passing-tests",
    )
    binding["fixture_binding"]["check_id"] = "fixture.echo-alt"
    hurt_command = {
        "command": f"{sys.executable} -c \"print('alt')\"",
        "check_role": "effectiveness",
        "check_id": "fixture.echo-alt",
        "status": "failed",
        "exit_code": 1,
    }
    verify_trial.stamp_verify_command_failure_taxonomy(hurt_command)
    _write_verify_receipt(
        target,
        f"{skill_id}-hurt",
        binding=binding,
        commands=[hurt_command],
        started_at="2026-06-20T02:00:00+00:00",
    )


def _seed_dual_gate_promotion_ready(target: Path) -> str:
    skill = _write_skill_subject(target)
    fingerprint = verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))
    _write_independent_utility_pass(
        target,
        "pass-a",
        fingerprint=fingerprint,
        case_id="report-passing-tests",
        manifest_id=_SCORECARD_UTILITY_PASSING_MANIFEST_ID,
    )
    _write_independent_utility_pass(
        target,
        "pass-b",
        fingerprint=fingerprint,
        case_id="report-failing-tests",
        manifest_id=_SCORECARD_UTILITY_MANIFEST_ID,
        started_at="2026-06-20T01:00:00+00:00",
    )
    return fingerprint


def test_effectiveness_pass_utility_fail_holds_promotion(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    _ensure_scorecard_manifest(scoreable_target, manifest_id=_SCORECARD_UTILITY_PASSING_MANIFEST_ID)
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    _write_independent_utility_pass(
        scoreable_target,
        "eff-only-a",
        fingerprint=fingerprint,
        case_id="report-passing-tests",
        lint_ok=False,
    )
    _write_independent_utility_pass(
        scoreable_target,
        "eff-only-b",
        fingerprint=fingerprint,
        case_id="report-failing-tests",
        manifest_id=_SCORECARD_UTILITY_MANIFEST_ID,
        started_at="2026-06-20T01:00:00+00:00",
        lint_ok=True,
    )

    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "hold"
    assert decision["reason"] == "withheld: utility_guardrail guardrail.lint-clean"
    assert not _status_file(scoreable_target).exists()

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.dimensions["effectiveness"]["helped"] == 2
    decision = scorecard.decide_scorecard(
        card,
        artifact_id="brigade-work",
        current_status="candidate",
        last_action_ts=None,
        now=localio.utc_now(),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "hold"
    assert decision.reason == "withheld: utility_guardrail guardrail.lint-clean"


def test_duplicate_utility_evidence_does_not_satisfy_gate(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    _ensure_scorecard_manifest(scoreable_target, manifest_id=_SCORECARD_UTILITY_PASSING_MANIFEST_ID)
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    _write_independent_utility_pass(
        scoreable_target,
        "orig",
        fingerprint=fingerprint,
        case_id="report-passing-tests",
    )
    _write_independent_utility_pass(
        scoreable_target,
        "reuse",
        fingerprint=fingerprint,
        case_id="report-passing-tests",
        started_at="2026-06-20T01:00:00+00:00",
        reused_from="orig",
    )

    card = scorecard.scorecard_for_artifact(scoreable_target, "brigade-work")
    assert card is not None
    assert card.utility_guardrails["per_check"]["guardrail.tests-green"]["passing_units"] == 1

    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "hold"
    assert decision["reason"] in {
        "insufficient verified evidence",
        "withheld: utility_guardrail guardrail.tests-green",
        "withheld: utility_guardrail guardrail.lint-clean",
    }


def test_dual_gate_promotion_persists_route_policy_marker(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    _seed_dual_gate_promotion_ready(scoreable_target)

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == ["brigade-work"]
    decision = payload["decisions"][0]
    assert decision["action"] == "install"
    assert decision["policy_version"] == scorecard.SCORECARD_POLICY_VERSION
    assert decision["new_status"] == "promoted"

    status = json.loads(_status_file(scoreable_target).read_text())
    entry = status["artifacts"]["brigade-work"]
    assert entry["status"] == "promoted"
    assert entry["route_policy"]["policy_version"] == scorecard.SCORECARD_POLICY_VERSION

    receipts = list(_decisions_dir(scoreable_target).glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["route_policy"]["policy_version"] == scorecard.SCORECARD_POLICY_VERSION


def test_missing_scorecard_holds_skill_promotion(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    from tests.test_outcome_cmd import _helped, _seed, _write_registry_skill

    _write_registry_skill(scoreable_target, "skill-x")
    _seed(scoreable_target, _helped("skill-x", 2))

    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["artifact_id"] == "skill-x"
    assert decision["action"] == "hold"
    assert decision["reason"] == "withheld: missing scorecard"
    assert not _status_file(scoreable_target).exists()


def test_promoted_regression_demotes_inside_cooldown(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch, install="installed")
    _seed_dual_gate_promotion_ready(scoreable_target)
    cfg = outcome.ReconcileConfig(cooldown_seconds=86_400, revert_min_hurt=99)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    capsys.readouterr()

    fingerprint = _skill_fingerprint(scoreable_target)
    binding = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_ALT_CHECK_MANIFEST_ID,
        case_id="report-failing-tests",
        check_id="fixture.echo-alt",
    )
    _write_verify_receipt(
        scoreable_target,
        "hurt",
        binding=binding,
        commands=[_effectiveness_command(ok=False, check_id="fixture.echo-alt")],
        started_at="2026-06-20T02:00:00+00:00",
    )

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "rollback"
    assert decision["new_status"] == "demoted"
    assert decision["reason"] == "verified regression measured"

    status = json.loads(_status_file(scoreable_target).read_text())
    assert status["artifacts"]["brigade-work"]["status"] == "demoted"


def test_promoted_regression_demotes_even_when_rollback_fails(scoreable_target, capsys, monkeypatch):
    _stub_execute(monkeypatch, install="installed")

    def _rollback_always_fails(target, artifact_id, action):
        return "reverted:claude:noop" if action == "rollback" else "installed"

    monkeypatch.setattr(outcome_cmd, "_execute_skill_decision", _rollback_always_fails)

    _seed_dual_gate_promotion_ready(scoreable_target)
    cfg = outcome.ReconcileConfig(cooldown_seconds=0)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    capsys.readouterr()

    fingerprint = _skill_fingerprint(scoreable_target)
    binding = _fixture_binding(
        fingerprint=fingerprint,
        target=scoreable_target,
        manifest_id=_SCORECARD_ALT_CHECK_MANIFEST_ID,
        case_id="report-failing-tests",
        check_id="fixture.echo-alt",
    )
    _write_verify_receipt(
        scoreable_target,
        "hurt",
        binding=binding,
        commands=[_effectiveness_command(ok=False, check_id="fixture.echo-alt")],
        started_at="2026-06-20T02:00:00+00:00",
    )

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "rollback"
    assert decision["new_status"] == "demoted"
    assert "noop" in decision["execution"]

    status = json.loads(_status_file(scoreable_target).read_text())
    entry = status["artifacts"]["brigade-work"]
    assert entry["status"] == "demoted"
    assert "route_policy" not in entry


def test_fork_parity_with_reconcile_dual_gate(scoreable_target, capsys, tmp_path):
    _seed_dual_gate_promotion_ready(scoreable_target)

    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    reconcile_payload = json.loads(capsys.readouterr().out)
    reconcile_decision = reconcile_payload["decisions"][0]

    fork_out = tmp_path / "fork.json"
    assert outcome_cmd.fork(target=scoreable_target, out=fork_out, json_output=True) == 0
    capsys.readouterr()
    fork_entry = json.loads(fork_out.read_text())["artifacts"]["brigade-work"]

    assert fork_entry["action"] == reconcile_decision["action"]
    assert fork_entry["new_status"] == reconcile_decision["new_status"]
    assert fork_entry["policy_version"] == scorecard.SCORECARD_POLICY_VERSION


def test_fork_utility_threshold_override(scoreable_target, capsys, tmp_path):
    _seed_dual_gate_promotion_ready(scoreable_target)

    fork_strict = tmp_path / "strict.json"
    assert (
        outcome_cmd.fork(
            target=scoreable_target,
            out=fork_strict,
            config=outcome.ReconcileConfig(utility_min_passing_units=3),
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    strict = json.loads(fork_strict.read_text())["artifacts"]["brigade-work"]
    assert strict["new_status"] != "promoted"

    fork_loose = tmp_path / "loose.json"
    assert (
        outcome_cmd.fork(
            target=scoreable_target,
            out=fork_loose,
            config=outcome.ReconcileConfig(utility_min_passing_units=1, install_min_helped=1),
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    loose = json.loads(fork_loose.read_text())["artifacts"]["brigade-work"]
    assert loose["new_status"] == "promoted"


def test_cli_fork_utility_check_min_passing_units_alias(scoreable_target, capsys, tmp_path):
    _seed_dual_gate_promotion_ready(scoreable_target)
    fork_out = tmp_path / "alias.json"
    assert (
        cli.main(
            [
                "outcome",
                "fork",
                "--target",
                str(scoreable_target),
                "--out",
                str(fork_out),
                "--utility-check-min-passing-units",
                "3",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    entry = json.loads(fork_out.read_text())["artifacts"]["brigade-work"]
    assert entry["new_status"] != "promoted"


def test_cli_fork_effective_wilson_override(scoreable_target, capsys, tmp_path):
    _seed_dual_gate_promotion_ready(scoreable_target)
    fork_out = tmp_path / "wilson.json"
    assert (
        cli.main(
            [
                "outcome",
                "fork",
                "--target",
                str(scoreable_target),
                "--out",
                str(fork_out),
                "--effective-wilson-min",
                "0.99",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    entry = json.loads(fork_out.read_text())["artifacts"]["brigade-work"]
    assert entry["new_status"] != "promoted"
    assert "wilson" in entry["reason"].lower() or entry["helped"] < 2


def test_fork_z_override_controls_effectiveness_gate(scoreable_target, capsys, tmp_path):
    _seed_dual_gate_promotion_ready(scoreable_target)
    high_z_out = tmp_path / "high-z.json"
    low_z_out = tmp_path / "low-z.json"
    assert (
        cli.main(
            [
                "outcome",
                "fork",
                "--target",
                str(scoreable_target),
                "--out",
                str(high_z_out),
                "--z",
                "5.0",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "outcome",
                "fork",
                "--target",
                str(scoreable_target),
                "--out",
                str(low_z_out),
                "--z",
                "0.5",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    high_z_entry = json.loads(high_z_out.read_text())["artifacts"]["brigade-work"]
    low_z_entry = json.loads(low_z_out.read_text())["artifacts"]["brigade-work"]
    assert high_z_entry["new_status"] != "promoted"
    assert "wilson" in high_z_entry["reason"].lower()
    assert low_z_entry["new_status"] == "promoted"
