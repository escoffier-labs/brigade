import json

from brigade import cli, outcome, outcome_cmd, work_cmd

from tests.work_cmd_test_helpers import _init_git_repo


def _seed(target, records):
    outcome_cmd.append_records(target, records)


def test_records_persist_under_git_tracked_memory_dir(tmp_path):
    _seed(tmp_path, [outcome.OutcomeRecord("c", "card", "t", "verify", 1, "r", "2026-06-20T00:00:00+00:00")])
    assert (tmp_path / "memory" / "outcome" / "records.jsonl").is_file()
    # durable ledger must NOT live under the gitignored .brigade/ dir
    assert not (tmp_path / ".brigade" / "outcome" / "records.jsonl").exists()


def test_score_reports_wilson_for_seeded_records(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00"),
            outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref2", "2026-06-20T01:00:00+00:00"),
        ],
    )
    assert outcome_cmd.score(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = {s["artifact_id"]: s for s in payload["scores"]}["skill-x"]
    assert entry["helped"] == 2 and entry["hurt"] == 0
    assert entry["score"] == outcome.wilson_lower_bound(2, 2)


def test_score_can_filter_to_one_artifact(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00"),
            outcome.OutcomeRecord("skill-y", "skill", "t2", "verify", -1, "ref2", "2026-06-20T01:00:00+00:00"),
        ],
    )
    assert outcome_cmd.score(target=tmp_path, artifact_id="skill-y", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [s["artifact_id"] for s in payload["scores"]]
    assert ids == ["skill-y"]


def test_explain_lists_the_signal_trail_in_time_order(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", -1, "ref-b", "2026-06-20T02:00:00+00:00"),
            outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref-a", "2026-06-20T00:00:00+00:00"),
        ],
    )
    assert outcome_cmd.explain(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact_id"] == "skill-x"
    assert [t["evidence_ref"] for t in payload["trail"]] == ["ref-a", "ref-b"]
    assert payload["score"]["helped"] == 1 and payload["score"]["hurt"] == 1


def test_explain_unknown_artifact_is_empty_not_error(tmp_path, capsys):
    assert outcome_cmd.explain(target=tmp_path, artifact_id="nope", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trail"] == []
    assert payload["score"]["score"] == 0.0


def test_cli_outcome_score_and_explain_dispatch(tmp_path, capsys):
    _seed(tmp_path, [outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00")])
    assert cli.main(["outcome", "score", "--target", str(tmp_path), "--json"]) == 0
    assert "skill-x" in capsys.readouterr().out
    assert cli.main(["outcome", "explain", "skill-x", "--target", str(tmp_path), "--json"]) == 0
    assert "skill-x" in capsys.readouterr().out


def test_capture_records_a_passing_verify_run_as_helped(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], timeout=30) == 0
    capsys.readouterr()
    assert (
        outcome_cmd.capture(
            target=tmp_path, artifact_id="skill-x", artifact_kind="skill", task_id="t1", json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["signal_value"] == 1
    assert payload["record"]["source"] == "verify"
    assert payload["record"]["artifact_id"] == "skill-x"
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1 and records[0].evidence_ref.endswith("receipt.json")


def test_capture_records_a_failing_verify_run_as_hurt(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=['python3 -c "raise SystemExit(3)"'], timeout=30) == 3
    capsys.readouterr()
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["signal_value"] == -1


def test_capture_errors_when_no_verify_run_exists(tmp_path):
    _init_git_repo(tmp_path)
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x") == 1


def test_cli_outcome_capture_dispatch(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], timeout=30) == 0
    capsys.readouterr()
    assert cli.main(["outcome", "capture", "skill-x", "--target", str(tmp_path), "--kind", "skill", "--json"]) == 0
    assert "skill-x" in capsys.readouterr().out


def _helped(artifact_id, n, start_hour=0):
    return [
        outcome.OutcomeRecord(
            artifact_id, "skill", f"t{i}", "verify", 1, f"ref{i}", f"2026-06-20T0{start_hour + i}:00:00+00:00"
        )
        for i in range(n)
    ]


def _status_file(target):
    return target / "memory" / "outcome" / "status.json"


def _decisions_dir(target):
    return target / "memory" / "outcome" / "decisions"


def test_reconcile_dry_run_reports_install_without_writing(tmp_path, capsys):
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    actions = {d["artifact_id"]: d["action"] for d in payload["decisions"]}
    assert actions["skill-x"] == "install"
    # dry-run writes nothing
    assert not _status_file(tmp_path).exists()
    assert not _decisions_dir(tmp_path).exists()


def _stub_execute(monkeypatch, *, install="installed"):
    """Isolate the status state machine from the physical skills side effect."""

    def _fake(target, artifact_id, action):
        return install if action == "install" else "reverted:claude:uninstall"

    monkeypatch.setattr(outcome_cmd, "_execute_skill_decision", _fake)


def test_reconcile_apply_installs_and_persists_status_and_receipt(tmp_path, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == ["skill-x"]
    status = json.loads(_status_file(tmp_path).read_text())
    assert status["artifacts"]["skill-x"]["status"] == "promoted"
    assert list(_decisions_dir(tmp_path).glob("*.json"))


def test_reconcile_apply_does_not_promote_when_install_fails(tmp_path, capsys, monkeypatch):
    # A skill that crosses the threshold but cannot physically install must NOT be
    # marked promoted (the forward-only ratchet would hide the failure forever).
    _stub_execute(monkeypatch, install="install-skipped: not in registry")
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    # not counted as applied, and the surfaced status stays candidate
    assert payload["applied"] == []
    decision = {d["artifact_id"]: d for d in payload["decisions"]}["skill-x"]
    assert decision["new_status"] == "candidate"
    assert decision["decided_status"] == "promoted"
    assert decision["execution"] == "install-skipped: not in registry"
    status = json.loads(_status_file(tmp_path).read_text())
    assert status["artifacts"]["skill-x"]["status"] == "candidate"


def test_reconcile_holds_inside_cooldown_after_apply(tmp_path, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions"] == []


def test_reconcile_rolls_back_promoted_artifact_on_regression(tmp_path, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    cfg = outcome.ReconcileConfig(cooldown_seconds=0)
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=cfg, json_output=True) == 0
    capsys.readouterr()
    _seed(
        tmp_path,
        [outcome.OutcomeRecord("skill-x", "skill", "t9", "verify", -1, "regress", "2026-06-20T09:00:00+00:00")],
    )
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, config=cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    actions = {d["artifact_id"]: d for d in payload["decisions"]}
    assert actions["skill-x"]["action"] == "rollback"
    status = json.loads(_status_file(tmp_path).read_text())
    assert status["artifacts"]["skill-x"]["status"] == "demoted"


def test_cli_outcome_reconcile_dispatch(tmp_path, capsys):
    _seed(tmp_path, _helped("skill-x", 2))
    assert cli.main(["outcome", "reconcile", "--target", str(tmp_path), "--json"]) == 0
    assert "skill-x" in capsys.readouterr().out


def test_rank_orders_artifacts_by_verified_score(tmp_path, capsys):
    _seed(tmp_path, _helped("skill-a", 4))  # cleanly verified, higher Wilson bound
    _seed(
        tmp_path,
        [
            *_helped("skill-b", 2),
            outcome.OutcomeRecord("skill-b", "skill", "tb", "verify", -1, "rb", "2026-06-20T09:00:00+00:00"),
        ],
    )
    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [item["artifact_id"] for item in payload["ranking"]]
    assert ids[0] == "skill-a"
    assert ids.index("skill-a") < ids.index("skill-b")


def test_record_appends_an_explicit_friction_cleared_signal(tmp_path, capsys):
    assert (
        outcome_cmd.record(
            target=tmp_path,
            artifact_id="skill-x",
            source="friction",
            status="cleared",
            evidence_ref="friction-scan#42",
            json_output=True,
        )
        == 0
    )
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1
    assert records[0].source == "friction" and records[0].signal_value == 1


def test_record_friction_recurred_is_a_hurt_signal(tmp_path, capsys):
    assert (
        outcome_cmd.record(
            target=tmp_path,
            artifact_id="skill-x",
            source="friction",
            status="recurred",
            evidence_ref="f",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert outcome_cmd.load_records(tmp_path)[0].signal_value == -1


def test_cli_outcome_rank_and_record_dispatch(tmp_path, capsys):
    assert (
        cli.main(
            [
                "outcome",
                "record",
                "skill-x",
                "--source",
                "friction",
                "--status",
                "cleared",
                "--evidence",
                "f",
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["outcome", "rank", "--target", str(tmp_path), "--json"]) == 0
    assert "skill-x" in capsys.readouterr().out
