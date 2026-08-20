"""Receipt-only outcome backfill scorecard audit and operator surfaces (#574)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from brigade import cli, outcome_cmd, scorecard, verify_trial, work_cmd
from brigade.work_cmd.session import briefing

from tests.test_operator_checkup import _patch_all_doctors
from tests.test_scorecard import (
    _effectiveness_command,
    _fixture_binding,
    _write_verify_receipt,
)
from tests.test_verify_trial import _write_skill_subject
from tests.work_cmd_test_helpers import _init_git_repo

pytest_plugins = ["tests.test_scorecard"]


def _records_path(target: Path) -> Path:
    return target / "memory" / "outcome" / "records.jsonl"


def _records_mtime(target: Path) -> float | None:
    path = _records_path(target)
    return path.stat().st_mtime if path.is_file() else None


def test_backfill_scorecard_eligible_fixture_receipt(scoreable_target, capsys):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(
        scoreable_target,
        "fixture-ok",
        binding=binding,
        commands=[_effectiveness_command()],
    )

    assert outcome_cmd.backfill_scorecard(target=scoreable_target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["total_receipts"] == 1
    assert payload["eligible"] == 1
    assert payload["unattributed"] == 0
    assert payload["ineligible"] == 0
    assert payload["ineligibility_rate"] == 0.0
    assert payload["legacy_records_audit_only"] is True
    assert "records.jsonl" in payload["legacy_records_note"]


def test_backfill_scorecard_eligible_patch_backed_receipt(scoreable_target, monkeypatch):
    from tests.test_verify_trial import _run_manifest_verify, _write_patch_manifest

    _write_patch_manifest(scoreable_target)
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npatched\n")
    import subprocess

    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    _run_manifest_verify(scoreable_target, "test-skill-patch", monkeypatch=monkeypatch, start_session=False)

    audit = scorecard.receipt_scorecard_audit(scoreable_target)
    assert audit["total_receipts"] == 1
    assert audit["eligible"] == 1


def test_backfill_scorecard_missing_binding_is_unattributed(scoreable_target, capsys):
    _write_verify_receipt(
        scoreable_target,
        "no-binding",
        binding=None,
        commands=[_effectiveness_command()],
    )

    assert outcome_cmd.backfill_scorecard(target=scoreable_target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["unattributed"] == 1
    assert payload["ineligible"] == 1
    assert payload["eligible"] + payload["ineligible"] == payload["total_receipts"]
    assert sum(payload["ineligible_by_reason"].values()) == payload["ineligible"]
    assert payload["ineligible_by_reason"]["unattributed"] == 1


def test_backfill_scorecard_invalid_receipt_json_is_ineligible_unattributed(scoreable_target):
    runs_root = scoreable_target / ".brigade" / "work" / "verify-runs"
    (runs_root / "bad-json").mkdir(parents=True)
    (runs_root / "bad-json" / "receipt.json").write_text("{not json\n", encoding="utf-8")
    (runs_root / "not-object").mkdir(parents=True)
    (runs_root / "not-object" / "receipt.json").write_text("[1, 2, 3]\n", encoding="utf-8")

    audit = scorecard.receipt_scorecard_audit(scoreable_target)

    assert audit["total_receipts"] == 2
    assert audit["eligible"] == 0
    assert audit["ineligible"] == 2
    assert audit["unattributed"] == 2
    assert audit["attributed_ineligible"] == 0
    assert audit["eligible"] + audit["ineligible"] == audit["total_receipts"]
    assert sum(audit["ineligible_by_reason"].values()) == audit["ineligible"]
    assert audit["ineligible_by_reason"]["invalid_receipt_json"] == 2


def test_backfill_scorecard_patch_mismatch_is_ineligible(scoreable_target, monkeypatch):
    from tests.test_verify_trial import _run_manifest_verify, _write_patch_manifest

    _write_patch_manifest(scoreable_target)
    assert work_cmd.start(target=scoreable_target, title="verify-trial") == 0
    skill = _write_skill_subject(scoreable_target, content="# brigade-work\npatched\n")
    import subprocess

    subprocess.run(["git", "add", str(skill.relative_to(scoreable_target))], cwd=scoreable_target, check=True)
    receipt = _run_manifest_verify(
        scoreable_target,
        "test-skill-patch",
        monkeypatch=monkeypatch,
        start_session=False,
    )
    patch_path = Path(receipt["path"]) / "changes.patch"
    patch_path.write_bytes(b"diff --git a/x b/x\n")

    audit = scorecard.receipt_scorecard_audit(scoreable_target)
    assert audit["ineligible"] == 1
    assert audit["ineligible_by_reason"]["patch_digest_mismatch"] == 1


def test_backfill_scorecard_infrastructure_failure_is_ineligible(scoreable_target):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    command = _effectiveness_command()
    command["status"] = "timed_out"
    command["exit_code"] = None
    command["failure_class"] = "infrastructure"
    command["failure_kind"] = "timeout"
    _write_verify_receipt(
        scoreable_target,
        "infra-fail",
        binding=binding,
        status="failed",
        commands=[command],
        extra={
            "failure_class": "infrastructure",
            "failure_kind": "timeout",
        },
    )

    audit = scorecard.receipt_scorecard_audit(scoreable_target)
    assert audit["ineligible"] == 1
    assert audit["ineligible_by_reason"]["failure_taxonomy_infrastructure"] == 1


def test_backfill_scorecard_does_not_mutate_records_jsonl(scoreable_target, capsys):
    records = _records_path(scoreable_target)
    records.parent.mkdir(parents=True, exist_ok=True)
    records.write_text(
        json.dumps(
            {
                "artifact_id": "brigade-work",
                "artifact_kind": "skill",
                "source": "verify",
                "signal_value": 1,
            }
        )
        + "\n"
    )
    before_mtime = _records_mtime(scoreable_target)
    before_text = records.read_text()

    _write_verify_receipt(scoreable_target, "audit-only", binding=None)
    assert outcome_cmd.backfill_scorecard(target=scoreable_target, json_output=False) == 0
    capsys.readouterr()

    assert records.read_text() == before_text
    assert _records_mtime(scoreable_target) == before_mtime


def test_health_and_work_brief_expose_receipt_scorecard_fields(scoreable_target, capsys):
    skill = _write_skill_subject(scoreable_target)
    fingerprint = verify_trial.subject_hash_for_path(scoreable_target, str(skill.relative_to(scoreable_target)))
    binding = _fixture_binding(fingerprint=fingerprint, target=scoreable_target)
    _write_verify_receipt(
        scoreable_target,
        "brief-fixture",
        binding=binding,
        commands=[_effectiveness_command()],
    )

    health = outcome_cmd.health(scoreable_target)
    assert health["attributed_receipt_count"] >= 1
    assert health["eligible_receipt_count"] >= 1
    assert sum(health["exploration_bands"].values()) >= 1
    assert health["legacy_records_audit_only"] is True

    payload = briefing._brief_payload(scoreable_target, limit=3)
    loop = payload["outcome_loop"]
    assert loop["eligible_receipt_count"] == health["eligible_receipt_count"]
    assert loop["exploration_bands"] == health["exploration_bands"]
    assert loop["leading_ineligibility_reason"] == health["leading_ineligibility_reason"]

    assert work_cmd.brief(target=scoreable_target, json_output=False) == 0
    out = capsys.readouterr().out
    assert "attributed_receipts=" in out
    assert "eligible=" in out
    assert "ineligible=" in out
    assert "ineligibility_rate=" in out
    assert "outcome_exploration_bands:" in out


def test_operator_checkup_outcome_surface_warns_on_half_fed_and_high_ineligibility(scoreable_target, capsys):
    from brigade.operator_cmd import lifecycle

    for index in range(51):
        _write_verify_receipt(
            scoreable_target,
            f"missing-binding-{index:02d}",
            binding=None,
            started_at=f"2026-07-26T{index % 24:02d}:00:00+00:00",
        )

    rc = lifecycle.checkup(target=scoreable_target, surfaces=["outcome"], json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["selected_ready"] is False
    issue_names = {issue["name"] for issue in payload["surfaces"][0]["details"]["issues"]}
    assert "outcome_loop_half_fed" in issue_names
    assert "outcome_receipt_ineligibility_high" in issue_names
    latest = payload["surfaces"][0]["details"]["latest_receipt_window"]
    assert latest["limit"] == scorecard.LATEST_RECEIPT_WINDOW
    assert latest["count"] == scorecard.LATEST_RECEIPT_WINDOW
    assert latest["ineligibility_rate"] > 0.5


def test_operator_checkup_default_skips_outcome_surface(monkeypatch, capsys):
    from brigade.operator_cmd import lifecycle

    def unexpected(**kwargs):
        raise AssertionError("outcome surface should not run in default checkup")

    _patch_all_doctors(monkeypatch, skills_rc=0)
    monkeypatch.setattr(lifecycle, "_checkup_outcome", unexpected)

    rc = lifecycle.checkup(target=Path("."), json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert "outcome" in payload["skipped_surfaces"]
    assert payload["selected_surfaces"] == list(lifecycle.CHECKUP_DEFAULT_SURFACES)


def test_operator_checkup_lists_outcome_surface(tmp_path, capsys):
    assert cli.main(["operator", "checkup", "--target", str(tmp_path), "--list-surfaces", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert "outcome" in payload["surface_names"]
    assert "outcome" not in payload["default_surfaces"]


def test_backfill_scorecard_human_output(scoreable_target, capsys):
    _write_verify_receipt(scoreable_target, "human", binding=None)
    assert outcome_cmd.backfill_scorecard(target=scoreable_target, json_output=False) == 0
    out = capsys.readouterr().out

    assert "outcome backfill scorecard:" in out
    assert "total=1" in out
    assert "records.jsonl" in out


def test_backfill_scorecard_cli_dispatch(scoreable_target, capsys):
    _write_verify_receipt(scoreable_target, "cli", binding=None)
    assert (
        cli.main(
            [
                "outcome",
                "backfill",
                "scorecard",
                "--target",
                str(scoreable_target),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {
        "total_receipts",
        "eligible",
        "unattributed",
        "ineligible",
        "attributed_ineligible",
        "ineligibility_rate",
        "ineligible_by_reason",
        "exploration_bands",
    }


def test_health_half_fed_uses_receipt_only_advice(tmp_path):
    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=[f"{sys.executable} -c \"print('ok')\""]) == 0
    health = outcome_cmd.health(tmp_path)

    assert health["verify_run_count"] >= 1
    assert health["eligible_receipt_count"] == 0
    assert health["top_issue"]["name"] == "outcome_loop_half_fed"
    assert "subject_binding" in health["top_issue"]["detail"]
    assert "--manifest" in health["top_issue"]["detail"]
    assert "verify/manifests" in health["top_issue"]["detail"]
    assert "registered verifier" not in health["top_issue"]["detail"]
    assert "outcome capture" not in health["top_issue"]["detail"].lower()


def test_latest_receipt_window_sort_is_stable(scoreable_target):
    for run_id, started_at in (
        ("b-run", "2026-07-26T12:00:00+00:00"),
        ("a-run", "2026-07-26T12:00:00+00:00"),
        ("c-run", "2026-07-25T12:00:00+00:00"),
    ):
        _write_verify_receipt(scoreable_target, run_id, binding=None, started_at=started_at)

    audits = scorecard.audit_all_verify_receipts(scoreable_target)
    latest = scorecard.latest_receipt_audits(audits, limit=2)
    assert [audit.run_id for audit in latest] == ["b-run", "a-run"]
