"""Regression decisions become inbox review requests with a behavioral contract (#2590, criterion 1)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import outcome, outcome_cmd, outcome_ratchet_report, outcome_skill_proposals as proposals, skills_cmd
from brigade.skills_cmd import registry
from tests.test_scorecard_reconcile import seed_registry_skill_scorecard_hurt
from tests.test_skills_local_edit_guard import SKILL, _write_skill

BUNDLED = "note"
CFG = outcome.ReconcileConfig(cooldown_seconds=0)


def _install_bundled(tmp_path: Path, capsys) -> Path:
    assert skills_cmd.install(workspace=tmp_path, skill=f"bundled:{BUNDLED}", harness="claude", json_output=True) == 0
    capsys.readouterr()
    return tmp_path / ".claude" / "skills" / BUNDLED / "SKILL.md"


def _reconcile(tmp_path: Path, capsys, *, proposals_flag: bool = True, **kwargs) -> tuple[dict, str]:
    rc = outcome_cmd.reconcile(target=tmp_path, config=CFG, json_output=True, proposals=proposals_flag, **kwargs)
    assert rc == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def _inbox(tmp_path: Path, capsys) -> list[dict]:
    assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
    return json.loads(capsys.readouterr().out)["proposals"]


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def bundled_regression(tmp_path, capsys):
    installed = _install_bundled(tmp_path, capsys)
    seed_registry_skill_scorecard_hurt(tmp_path, BUNDLED)
    return tmp_path, installed


def test_hold_files_one_proposal_with_a_behavioral_contract(bundled_regression, capsys):
    tmp_path, installed = bundled_regression
    text_before = installed.read_bytes()
    skill_files_before = _tree(installed.parent)

    payload, _err = _reconcile(tmp_path, capsys)

    block = payload["outcome_proposals"]
    assert (block["filed"], block["existing"], block["flagged"], block["skipped"]) == (1, 0, 0, 0)
    row = block["proposals"][0]
    assert (row["artifact_id"], row["reason_code"], row["status"]) == (BUNDLED, "regression_hold", "filed")

    (proposal,) = _inbox(tmp_path, capsys)
    assert proposal["proposal_id"] == row["proposal_id"]
    assert proposal["status"] == "pending"
    assert proposal["requires_revision"] is True
    assert proposal["unrevised_identity"] == proposal["candidate_identity"]
    assert proposal["source"] == f"brigade://outcome-regression/{BUNDLED}"
    assert proposal["dedupe_key"].startswith(f"outcome:{BUNDLED}:regression_hold:")
    fingerprint = proposal["dedupe_key"].rsplit(":", 1)[1]
    assert len(fingerprint) == 64 and not fingerprint.startswith("sha256")
    assert proposal["contract_status"] == "complete"

    contract = proposal["behavioral_contract"]
    assert set(contract) == {
        "schema_version",
        "reason",
        "addressed_failure",
        "change_category",
        "baseline_fingerprint",
        "expected_observable_behavior",
    }
    assert contract["baseline_fingerprint"] is None  # a bundled skill never imported into the registry
    assert proposal["baseline_identity"] is None
    assert BUNDLED in contract["reason"] and "regression_hold" in contract["reason"]
    assert "helped=0 hurt=1" in contract["reason"]
    assert f"{BUNDLED}-hurt" in contract["addressed_failure"]
    assert "fixture.echo-alt" in contract["addressed_failure"]
    (assertion,) = contract["expected_observable_behavior"]
    assert assertion["id"] == f"{BUNDLED}-hurt:fixture.echo-alt"
    assert "print('alt')" in assertion["description"]
    assert f".brigade/work/verify-runs/{BUNDLED}-hurt/receipt.json" in assertion["description"]

    # The staged text is the installed text, unchanged; nothing else on disk moved.
    staged = Path(proposal["skill_path"]) / "SKILL.md"
    assert staged.read_bytes() == text_before
    assert installed.read_bytes() == text_before
    assert _tree(installed.parent) == skill_files_before
    assert not (tmp_path / ".brigade" / "skills" / "registry" / BUNDLED).exists()


def test_second_run_returns_the_same_proposal(bundled_regression, capsys):
    tmp_path, _installed = bundled_regression
    first, _ = _reconcile(tmp_path, capsys)
    second, _ = _reconcile(tmp_path, capsys)

    filed = first["outcome_proposals"]["proposals"][0]
    again = second["outcome_proposals"]["proposals"][0]
    assert (again["status"], again["proposal_id"]) == ("existing", filed["proposal_id"])
    assert second["outcome_proposals"]["filed"] == 0 and second["outcome_proposals"]["existing"] == 1
    assert len(_inbox(tmp_path, capsys)) == 1


def test_rejected_proposal_stays_muted(bundled_regression, capsys):
    tmp_path, _installed = bundled_regression
    first, _ = _reconcile(tmp_path, capsys)
    proposal_id = first["outcome_proposals"]["proposals"][0]["proposal_id"]
    assert skills_cmd.inbox_reject(target=tmp_path, proposal_id=proposal_id, reason="not now", json_output=True) == 0
    capsys.readouterr()

    again, _ = _reconcile(tmp_path, capsys)

    row = again["outcome_proposals"]["proposals"][0]
    assert (row["status"], row["detail"], row["proposal_id"]) == ("existing", "rejected", proposal_id)
    assert len(_inbox(tmp_path, capsys)) == 1


def test_registry_skill_is_flagged_without_a_proposal(tmp_path, capsys):
    assert skills_cmd.import_skill(target=tmp_path, source=_write_skill(tmp_path / "src"), json_output=True) == 0
    assert skills_cmd.install(workspace=tmp_path, skill=f"registry:{SKILL}", harness="claude", json_output=True) == 0
    capsys.readouterr()
    seed_registry_skill_scorecard_hurt(tmp_path, SKILL)

    payload, _err = _reconcile(tmp_path, capsys)

    row = payload["outcome_proposals"]["proposals"][0]
    assert (row["artifact_id"], row["status"], row["detail"]) == (SKILL, "flagged", "registry")
    assert payload["outcome_proposals"]["flagged"] == 1
    assert _inbox(tmp_path, capsys) == []


def test_path_skill_is_skipped_as_unclassified(tmp_path, capsys):
    from tests.test_outcome_cmd import _write_registry_skill

    _write_registry_skill(tmp_path, "local-only")
    seed_registry_skill_scorecard_hurt(tmp_path, "local-only")

    payload, _err = _reconcile(tmp_path, capsys)

    row = payload["outcome_proposals"]["proposals"][0]
    assert (row["artifact_id"], row["status"], row["detail"]) == ("local-only", "skipped", "unclassified")
    assert _inbox(tmp_path, capsys) == []


def test_missing_receipt_says_command_unavailable(tmp_path):
    gone = tmp_path / ".brigade" / "work" / "verify-runs" / "run-gone" / "receipt.json"
    audit = SimpleNamespace(receipt_path=str(gone), run_id="run-gone", eligible=True, reused=False, effectiveness=-1)
    card = SimpleNamespace(
        dimensions={"effectiveness": {"helped": 0, "hurt": 1}},
        receipt_trail=[audit],
    )
    item = SimpleNamespace(
        decision=outcome.Decision("note", "hold", "candidate", "withheld", code=outcome.REGRESSION_HOLD),
        scorecard=card,
    )

    contract = proposals.build_contract(tmp_path, item, baseline=None)

    (assertion,) = contract["expected_observable_behavior"]
    assert assertion["id"] == "run-gone:receipt-unavailable"
    assert "command unavailable" in assertion["description"]
    assert ".brigade/work/verify-runs/run-gone/receipt.json" in assertion["description"]
    assert "command unavailable" in contract["addressed_failure"]
    assert "echo" not in json.dumps(contract)


def test_observed_regression_hold_files_nothing(tmp_path):
    item = SimpleNamespace(
        decision=outcome.Decision(
            "note", "hold", "promoted", "withheld: observed", code=outcome.OBSERVED_REGRESSION_HOLD
        ),
        artifact_kind="skill",
        scorecard=None,
    )
    assert proposals.file_regression_proposals(tmp_path, [item]) == []
    assert proposals.TRIGGER_CODES == {outcome.REGRESSION_HOLD, outcome.REGRESSION_ROLLBACK}


def test_cards_never_file_proposals(tmp_path):
    item = SimpleNamespace(
        decision=outcome.Decision("a-card", "hold", "candidate", "withheld", code=outcome.REGRESSION_HOLD),
        artifact_kind="card",
        scorecard=None,
    )
    assert proposals.file_regression_proposals(tmp_path, [item]) == []


def test_filing_failure_warns_and_keeps_the_exit_code(bundled_regression, capsys, monkeypatch):
    tmp_path, _installed = bundled_regression

    def boom(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(proposals, "inbox_add", boom)

    payload, err = _reconcile(tmp_path, capsys)

    row = payload["outcome_proposals"]["proposals"][0]
    assert row["status"] == "skipped" and "disk full" in row["detail"]
    assert f"warning: outcome proposal for {BUNDLED} not filed" in err
    assert payload["decisions"]  # the ratchet decisions are unchanged
    assert _inbox(tmp_path, capsys) == []


def test_dry_run_files_only_with_the_flag_and_says_so(bundled_regression, capsys):
    tmp_path, _installed = bundled_regression

    plain, _ = _reconcile(tmp_path, capsys, proposals_flag=False)
    assert "outcome_proposals" not in plain
    assert _inbox(tmp_path, capsys) == []

    assert outcome_cmd.reconcile(target=tmp_path, config=CFG, proposals=True) == 0
    text = capsys.readouterr().out
    assert "proposals: filed=1 existing=0 flagged=0 skipped=0" in text
    assert "filed in a dry run because --proposals was passed" in text
    assert f"- filed: {BUNDLED} [regression_hold]" in text
    assert len(_inbox(tmp_path, capsys)) == 1


def test_report_persists_the_counts(bundled_regression, capsys):
    tmp_path, _installed = bundled_regression
    _reconcile(tmp_path, capsys, report=True)

    summary = outcome_ratchet_report.load_report(tmp_path)["summary"]
    assert summary["outcome_proposals"] == {"filed": 1, "existing": 0, "flagged": 0, "skipped": 0}
    assert outcome_ratchet_report.brief_payload(outcome_ratchet_report.load_report(tmp_path))["outcome_proposals"] == {
        "filed": 1,
        "existing": 0,
        "flagged": 0,
        "skipped": 0,
    }

    _reconcile(tmp_path, capsys, proposals_flag=False, report=True)
    assert "outcome_proposals" not in outcome_ratchet_report.load_report(tmp_path)["summary"]


def test_filing_never_writes_the_registry_or_a_skill_file(bundled_regression, capsys):
    tmp_path, installed = bundled_regression
    watched = [installed.parent, tmp_path / ".brigade" / "skills" / "registry"]
    before = [_tree(root) if root.exists() else None for root in watched]

    _reconcile(tmp_path, capsys)

    assert [_tree(root) if root.exists() else None for root in watched] == before
    assert registry._slug(BUNDLED) == BUNDLED
