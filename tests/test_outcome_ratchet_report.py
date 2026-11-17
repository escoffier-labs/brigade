"""Advisory outcome ratchet report: write, load, cap, and reason codes (#2589)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade import cli, outcome, outcome_cmd
from brigade import outcome_ratchet_report as report_mod
from brigade import scorecard as scorecard_mod
from brigade.work_cmd.session import briefing

NOW = datetime(2026, 11, 17, 7, 30, tzinfo=timezone.utc)


@dataclass
class _Item:
    decision: outcome.Decision
    prior_status: str
    cohorts: Any = None
    scorecard: Any = None
    artifact_kind: str = "skill"


@dataclass
class _Current:
    helped: int
    hurt: int


@dataclass
class _Cohorts:
    current: _Current


def _hold(artifact_id: str = "skill-a", prior: str = "candidate") -> _Item:
    return _Item(
        decision=outcome.Decision(
            artifact_id,
            "hold",
            "candidate",
            "withheld: verified regression present",
            code=outcome.REGRESSION_HOLD,
        ),
        prior_status=prior,
        scorecard=_card(artifact_id, helped=1, hurt=2),
    )


def _card(artifact_id: str = "skill-a", *, helped: int = 0, hurt: int = 2) -> Any:
    return scorecard_mod.SubjectScorecard(
        subject=scorecard_mod.SubjectRef("skill", artifact_id, "fingerprint"),
        dimensions={"effectiveness": {"helped": helped, "hurt": hurt}},
        utility_guardrails={},
    )


def _write(tmp_path: Path, items: list[_Item]) -> Path | None:
    return report_mod.write_report(tmp_path, items, apply=False, generated_at=NOW)


def test_write_and_load_round_trip(tmp_path):
    other = _Item(
        decision=outcome.Decision("skill-b", "install", "promoted", "verified helped, no regressions"),
        prior_status="candidate",
        scorecard=_card("skill-b", helped=3, hurt=0),
    )
    rollback = _Item(
        decision=outcome.Decision(
            "skill-c",
            "rollback",
            "demoted",
            "verified regression measured",
            code=outcome.REGRESSION_ROLLBACK,
        ),
        prior_status="promoted",
        cohorts=_Cohorts(current=_Current(helped=1, hurt=1)),
    )
    path = _write(tmp_path, [_hold(), other, rollback])
    assert path == report_mod.report_path(tmp_path)
    assert path is not None and path.is_file()

    loaded = report_mod.load_report(tmp_path)
    assert loaded["version"] == report_mod.REPORT_VERSION
    assert loaded["apply"] is False
    assert loaded["generated_at"] == NOW.isoformat()
    assert loaded["summary"] == {
        "decisions": 3,
        "regression_holds": 1,
        "regression_rollbacks": 1,
        "regression_hold_ids": ["skill-a"],
    }
    assert loaded["decisions_by_action"] == {"hold": 1, "install": 1, "rollback": 1}
    assert [(row["artifact_id"], row["reason_code"]) for row in loaded["regressions"]] == [
        ("skill-a", "regression_hold"),
        ("skill-c", "regression_rollback"),
    ]
    hold = loaded["regressions"][0]
    assert hold["prior_state"] == "candidate"
    assert hold["reason"] == "withheld: verified regression present"
    assert (hold["helped"], hold["hurt"]) == (1, 2)
    rollback_row = loaded["regressions"][1]
    assert (rollback_row["helped"], rollback_row["hurt"]) == (1, 1)
    assert "ineligibility_rate" in loaded
    assert "leading_ineligibility_reason" in loaded


def test_report_is_bounded_and_truncates_regressions(tmp_path):
    items = [_hold(f"skill-{index:04d}") for index in range(2000)]
    path = _write(tmp_path, items)
    assert path is not None
    assert path.stat().st_size <= report_mod.REPORT_MAX_BYTES

    loaded = report_mod.load_report(tmp_path)
    assert loaded["summary"]["decisions"] == 2000
    assert loaded["summary"]["regression_holds"] == 2000
    assert loaded["truncated"] is True
    assert loaded["regressions_omitted"] + len(loaded["regressions"]) == 2000
    assert loaded["regressions"]


def test_loader_rejects_oversized_version_mismatch_and_garbage(tmp_path):
    path = report_mod.report_path(tmp_path)
    path.parent.mkdir(parents=True)

    path.write_text("x" * (report_mod.REPORT_MAX_BYTES + 1))
    assert report_mod.load_report(tmp_path) == {}

    path.write_text(json.dumps({"version": report_mod.REPORT_VERSION + 1, "summary": {}}))
    assert report_mod.load_report(tmp_path) == {}

    # True == 1 in Python, so a boolean version must not pass the version check.
    path.write_text(json.dumps({"version": True, "summary": {}}))
    assert report_mod.load_report(tmp_path) == {}

    path.write_text("{not json")
    assert report_mod.load_report(tmp_path) == {}

    path.write_text(json.dumps(["not", "an", "object"]))
    assert report_mod.load_report(tmp_path) == {}

    path.unlink()
    assert report_mod.load_report(tmp_path) == {}


def test_loader_and_writer_refuse_a_symlinked_report(tmp_path, capsys):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"version": report_mod.REPORT_VERSION, "generated_at": "planted"}))
    path = report_mod.report_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)

    assert report_mod.load_report(tmp_path) == {}
    assert _write(tmp_path, [_hold()]) is None
    # A skipped write leaves any older report in place, so it must say so.
    assert "outcome ratchet report not written: symlinked path refused" in capsys.readouterr().err
    # The symlink and its target are untouched.
    assert path.is_symlink()
    assert json.loads(outside.read_text())["generated_at"] == "planted"


def test_loader_and_writer_refuse_a_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "memory-care").symlink_to(outside, target_is_directory=True)

    assert report_mod.load_report(tmp_path) == {}
    assert _write(tmp_path, [_hold()]) is None
    assert not (outside / "outcome").exists()


def _snapshot(root: Path) -> dict[str, bytes]:
    files = (path for path in sorted(root.rglob("*")) if path.is_file())
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in files}


def test_reconcile_dry_run_without_report_writes_nothing(tmp_path, capsys):
    outcome_cmd.append_records(
        tmp_path,
        [outcome.OutcomeRecord("skill-a", "skill", "t0", "verify", 1, "ref-0", "2026-06-20T00:00:00+00:00")],
    )
    before = _snapshot(tmp_path)
    assert before
    assert outcome_cmd.reconcile(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert _snapshot(tmp_path) == before
    assert not report_mod.report_path(tmp_path).exists()


def test_reconcile_report_flag_writes_report_and_keeps_dry_run(tmp_path, monkeypatch, capsys):
    card = _card("skill-regressed", helped=0, hurt=2)

    def fake_scorecards(target: Path) -> dict[str, Any]:
        return {"skill-regressed": card}

    monkeypatch.setattr(outcome_cmd, "_scorecards_by_artifact", fake_scorecards)

    assert outcome_cmd.reconcile(target=tmp_path, json_output=True) == 0
    assert not report_mod.report_path(tmp_path).exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["applied"] == []
    assert payload["decisions"][0]["reason_code"] == "regression_hold"
    assert not outcome_cmd._status_path(tmp_path).exists()

    assert outcome_cmd.reconcile(target=tmp_path, json_output=True, report=True) == 0
    capsys.readouterr()
    report = report_mod.load_report(tmp_path)
    assert report["apply"] is False
    assert report["summary"] == {
        "decisions": 1,
        "regression_holds": 1,
        "regression_rollbacks": 0,
        "regression_hold_ids": ["skill-regressed"],
    }
    assert report["regressions"][0]["artifact_id"] == "skill-regressed"
    assert not outcome_cmd._status_path(tmp_path).exists()


def test_decide_sets_reason_codes_on_legacy_decisions():
    config = outcome.ReconcileConfig()
    score = outcome.OutcomeScore("skill-a", helped=0, hurt=2, neutral=0, score=0.0, last_signal_ts=None)

    held = outcome.decide(score, current_status="candidate", last_action_ts=None, now=NOW, config=config)
    assert (held.action, held.code) == ("hold", outcome.REGRESSION_HOLD)
    assert held.code == "regression_hold"

    promoted = outcome.OutcomeScore("skill-a", helped=0, hurt=2, neutral=0, score=0.0, last_signal_ts=None)
    rolled = outcome.decide(promoted, current_status="promoted", last_action_ts=None, now=NOW, config=config)
    assert (rolled.action, rolled.code) == ("rollback", outcome.REGRESSION_ROLLBACK)

    clean = outcome.OutcomeScore("skill-b", helped=3, hurt=0, neutral=0, score=0.5, last_signal_ts=None)
    installed = outcome.decide(clean, current_status="candidate", last_action_ts=None, now=NOW, config=config)
    assert installed.action == "install"
    assert installed.code is None


def test_decide_scorecard_sets_reason_codes():
    config = outcome.ReconcileConfig()
    regressed = _card("skill-a", helped=0, hurt=2)

    held = scorecard_mod.decide_scorecard(
        regressed, artifact_id="skill-a", current_status="candidate", last_action_ts=None, now=NOW, config=config
    )
    assert (held.action, held.code) == ("hold", outcome.REGRESSION_HOLD)

    rolled = scorecard_mod.decide_scorecard(
        regressed, artifact_id="skill-a", current_status="promoted", last_action_ts=None, now=NOW, config=config
    )
    assert (rolled.action, rolled.code) == ("rollback", outcome.REGRESSION_ROLLBACK)

    clean = _card("skill-b", helped=4, hurt=0)
    installed = scorecard_mod.decide_scorecard(
        clean, artifact_id="skill-b", current_status="candidate", last_action_ts=None, now=NOW, config=config
    )
    assert installed.action == "install"
    assert installed.code is None


def test_reason_code_is_never_derived_from_reason_text(tmp_path):
    decision = outcome.Decision("skill-a", "hold", "candidate", "withheld: verified regression present")
    assert decision.code is None

    item = _Item(decision=decision, prior_status="candidate", scorecard=_card("skill-a", hurt=2))
    report = report_mod.build_report([item], apply=False, generated_at=NOW, target=tmp_path)
    assert report["regressions"] == []
    assert report["summary"]["regression_holds"] == 0


def test_brief_payload_is_bounded(tmp_path):
    items = [_hold(f"skill-{index:02d}") for index in range(20)]
    _write(tmp_path, items)
    brief = report_mod.brief_payload(report_mod.load_report(tmp_path))

    assert brief["present"] is True
    assert brief["generated_at"] == NOW.isoformat()
    assert brief["regression_holds"] == 20
    assert len(brief["regression_hold_ids"]) <= report_mod._BRIEF_MAX_IDS
    assert brief["regression_hold_ids"] == [f"skill-{index:02d}" for index in range(report_mod._BRIEF_MAX_IDS)]
    assert report_mod.brief_payload({}) == {"present": False}


def test_skill_regression_row_counts_come_from_the_scorecard(tmp_path):
    # Reconcile decides a skill from its scorecard only. Legacy cohorts still
    # present on the same item must never make a regression hold read as helped.
    item = _hold()
    item.cohorts = _Cohorts(current=_Current(helped=2, hurt=0))
    assert _write(tmp_path, [item]) is not None

    row = report_mod.load_report(tmp_path)["regressions"][0]
    assert (row["helped"], row["hurt"]) == (1, 2)


def test_card_regression_row_counts_come_from_cohorts(tmp_path):
    # Cards are decided by the legacy scorer, so their cohorts win even when a
    # scorecard is also present.
    item = _Item(
        decision=outcome.Decision(
            "card-a",
            "rollback",
            "demoted",
            "verified regression measured",
            code=outcome.REGRESSION_ROLLBACK,
        ),
        prior_status="promoted",
        cohorts=_Cohorts(current=_Current(helped=2, hurt=1)),
        scorecard=_card("card-a", helped=0, hurt=5),
        artifact_kind="card",
    )
    assert _write(tmp_path, [item]) is not None

    row = report_mod.load_report(tmp_path)["regressions"][0]
    assert (row["helped"], row["hurt"]) == (2, 1)


def test_reconcile_report_write_failure_warns_and_keeps_apply_committed(tmp_path, monkeypatch, capsys):
    card = _card("skill-regressed", helped=0, hurt=2)
    monkeypatch.setattr(outcome_cmd, "_scorecards_by_artifact", lambda target: {"skill-regressed": card})
    status_path = tmp_path / "memory" / "outcome" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({"version": 1, "artifacts": {"skill-regressed": {"status": "promoted"}}}))

    # A directory in place of the report file makes the advisory write raise
    # IsADirectoryError after the run has already committed state.
    report_mod.report_path(tmp_path).mkdir(parents=True)

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, report=True) == 0
    captured = capsys.readouterr()
    assert "outcome reconcile:" in captured.out
    assert "skill-regressed" in captured.out
    assert "warning:" in captured.err
    assert "outcome ratchet report not written" in captured.err
    committed = json.loads(status_path.read_text())["artifacts"]["skill-regressed"]["status"]
    assert committed == "demoted"


def test_loader_refuses_a_symlink_the_pre_check_could_miss(tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"version": report_mod.REPORT_VERSION, "generated_at": "planted"}))
    path = report_mod.report_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)

    # Bypass the lexical symlink walk: the no-follow open must still refuse.
    monkeypatch.setattr(report_mod, "_has_symlinked_component", lambda path, boundary: False)
    assert report_mod.load_report(tmp_path) == {}


def test_brief_names_holds_the_size_cap_dropped(tmp_path):
    # Rollback rows are long and come first; the cap then drops the trailing
    # holds, so a brief built from the kept rows would print "(none)" beside a
    # nonzero hold count.
    items = [
        _Item(
            decision=outcome.Decision(
                f"rollback-{index:04d}",
                "rollback",
                "demoted",
                "verified regression measured " + ("x" * 120),
                code=outcome.REGRESSION_ROLLBACK,
            ),
            prior_status="promoted",
            cohorts=_Cohorts(current=_Current(helped=0, hurt=3)),
            artifact_kind="card",
        )
        for index in range(1500)
    ]
    items += [_hold(f"hold-{index:02d}") for index in range(20)]
    assert _write(tmp_path, items) is not None

    loaded = report_mod.load_report(tmp_path)
    assert loaded["truncated"] is True
    assert loaded["summary"]["regression_holds"] == 20
    brief = report_mod.brief_payload(loaded)
    assert brief["regression_holds"] == 20
    assert brief["regression_hold_ids"] == [f"hold-{index:02d}" for index in range(report_mod._BRIEF_MAX_IDS)]

    line = briefing._outcome_ratchet_line(brief)
    assert "regression_holds=20" in line
    assert "hold-00" in line
    assert f"+{20 - report_mod._BRIEF_MAX_IDS} more" in line
    assert "(none)" not in line


def test_cli_ratchet_prints_the_file_only(tmp_path, capsys):
    outcome_cmd.append_records(
        tmp_path,
        [outcome.OutcomeRecord("ranked-skill", "skill", "t0", "verify", 1, "ref-0", "2026-06-20T00:00:00+00:00")],
    )
    before = _snapshot(tmp_path)

    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}

    _write(tmp_path, [_hold()])
    after_write = _snapshot(tmp_path)
    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["regression_holds"] == 1
    assert payload["regressions"][0]["artifact_id"] == "skill-a"

    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "outcome ratchet:" in text
    assert "regression_holds=1" in text
    assert "skill-a" in text
    assert "mode=dry-run" in text
    # The report is printed as stored: no rank rows, and nothing was written.
    assert "outcome rank:" not in text
    assert " score=" not in text
    assert "ranked-skill score=" not in text
    assert _snapshot(tmp_path) == after_write
    assert set(after_write) - set(before) == {".brigade/memory-care/outcome/ratchet-report.json"}


def test_cli_ratchet_reports_absent_file_in_text(tmp_path, capsys):
    assert cli.main(["outcome", "ratchet", "--target", str(tmp_path)]) == 0
    assert "report: none" in capsys.readouterr().out


def test_cli_reconcile_report_flag_writes_advisory_file(tmp_path, capsys):
    assert cli.main(["outcome", "reconcile", "--target", str(tmp_path), "--report", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    report = report_mod.load_report(tmp_path)
    assert report["apply"] is False
    assert report["summary"] == {
        "decisions": 0,
        "regression_holds": 0,
        "regression_rollbacks": 0,
        "regression_hold_ids": [],
    }


def test_writer_warns_when_the_report_cannot_fit_the_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report_mod, "REPORT_MAX_BYTES", 10)

    assert _write(tmp_path, [_hold()]) is None
    assert "still over 10 bytes after truncation" in capsys.readouterr().err
    assert not report_mod.report_path(tmp_path).exists()
