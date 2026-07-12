import json

from brigade import cli, localio, outcome, outcome_cmd, receipts_cmd, work_cmd

from tests.work_cmd_test_helpers import _init_git_repo


def _seed(target, records):
    outcome_cmd.append_records(target, records)


def _write_verify_receipt(
    target,
    run_id="verify-run",
    *,
    status="completed",
    code_graph_delta=None,
    started_at="2026-06-20T00:00:00+00:00",
):
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "run_id": run_id,
        "target": str(target),
        "status": status,
        "started_at": started_at,
        "completed_at": started_at,
        "commands": [],
    }
    if code_graph_delta is not None:
        receipt["code_graph_delta"] = code_graph_delta
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    localio.write_json(run_dir / "receipt.json", receipt)
    return run_dir / "receipt.json"


def _write_run_receipt(
    target,
    run_id="brigade-run",
    *,
    status="ok",
    dry_run=False,
    read_only=False,
    code_graph_delta=None,
    context_eval=None,
    started_at="2026-06-20T00:00:00+00:00",
):
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "task": "fixture task",
        "cwd": str(target),
        "status": status,
        "dry_run": dry_run,
        "read_only": read_only,
        "started_at": started_at,
        "completed_at": started_at,
        "artifacts": str(run_dir),
    }
    if code_graph_delta is not None:
        receipt["code_graph_delta"] = code_graph_delta
    if context_eval is not None:
        receipt["context_eval"] = context_eval
    localio.write_json(run_dir / "run.json", receipt)
    return run_dir / "run.json"


def test_records_persist_under_git_tracked_memory_dir(tmp_path):
    _seed(tmp_path, [outcome.OutcomeRecord("c", "card", "t", "verify", 1, "r", "2026-06-20T00:00:00+00:00")])
    assert (tmp_path / "memory" / "outcome" / "records.jsonl").is_file()
    # durable ledger must NOT live under the gitignored .brigade/ dir
    assert not (tmp_path / ".brigade" / "outcome" / "records.jsonl").exists()


def test_appended_records_carry_tamper_evident_digest_chain(tmp_path):
    first = outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00")
    second = outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", -1, "ref2", "2026-06-20T01:00:00+00:00")

    outcome_cmd.append_records(tmp_path, [first])
    outcome_cmd.append_records(tmp_path, [second])

    rows = [
        json.loads(line)
        for line in (tmp_path / "memory" / "outcome" / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["prev_digest"] is None
    assert rows[0]["digest"] == localio.canonical_json_digest(rows[0], exclude_keys={"digest"})
    assert rows[1]["prev_digest"] == rows[0]["digest"]
    assert rows[1]["digest"] == localio.canonical_json_digest(rows[1], exclude_keys={"digest"})


def test_legacy_digestless_records_still_load_and_do_not_break_new_chain(tmp_path):
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    legacy = {
        "artifact_id": "skill-x",
        "artifact_kind": "skill",
        "task_id": "t0",
        "source": "verify",
        "signal_value": 1,
        "evidence_ref": "legacy",
        "ts": "2026-06-20T00:00:00+00:00",
    }
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n")

    outcome_cmd.append_records(
        tmp_path,
        [outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T01:00:00+00:00")],
    )

    loaded = outcome_cmd.load_records(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [record.evidence_ref for record in loaded] == ["legacy", "ref1"]
    assert loaded[0].code_graph_delta is None
    assert loaded[1].code_graph_delta is None
    assert loaded[0].context_eval is None
    assert loaded[1].context_eval is None
    assert "digest" not in rows[0]
    assert rows[1]["prev_digest"] is None
    assert rows[1]["digest"] == localio.canonical_json_digest(rows[1], exclude_keys={"digest"})


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


def test_capture_copies_compact_code_graph_delta_from_verify_receipt(tmp_path, capsys):
    delta = {
        "status": "ok",
        "summary": "changed_symbols=3 edge_churn=2",
        "changed_symbol_count": 3,
        "edge_churn": 2,
        "raw_counts": {"edges_added": 5, "edges_removed": 3},
        "sidecar_path": "/tmp/not-copied.json",
        "internal_debug": {"ignored": True},
    }
    _write_verify_receipt(tmp_path, run_id="with-delta", code_graph_delta=delta)

    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            artifact_kind="skill",
            task_id="t-delta",
            run_id="with-delta",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    compact = {
        "status": "ok",
        "summary": "changed_symbols=3 edge_churn=2",
        "changed_symbol_count": 3,
        "edge_churn": 2,
        "raw_counts": {"edges_added": 5, "edges_removed": 3},
    }
    assert payload["record"]["code_graph_delta"] == compact

    row = json.loads((tmp_path / "memory" / "outcome" / "records.jsonl").read_text())
    assert row["code_graph_delta"] == compact
    assert "sidecar_path" not in row["code_graph_delta"]
    assert row["digest"] == localio.canonical_json_digest(row, exclude_keys={"digest"})


def test_capture_run_receipt_copies_delta_and_context_eval_into_digest_chain(tmp_path, capsys):
    delta = {
        "status": "ok",
        "summary": "changed_symbols=2 edge_churn=1",
        "changed_symbol_count": 2,
        "edge_churn": 1,
        "raw_counts": {"edges_added": 2, "edges_removed": 1},
        "sidecar_path": "/tmp/not-copied.json",
    }
    context_eval = {
        "counts": {"brief_files": 2, "delta_files": 2, "hits": 1, "missed": 1},
        "hits": ["src/brigade/outcome_cmd.py"],
        "missed": ["tests/test_outcome_cmd.py"],
        "brief_hit_rate": 0.5,
    }
    run_json = _write_run_receipt(
        tmp_path,
        run_id="run-with-delta",
        code_graph_delta=delta,
        context_eval=context_eval,
    )

    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            artifact_kind="skill",
            task_id="t-run",
            run_receipt="run-with-delta",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    compact = {
        "status": "ok",
        "summary": "changed_symbols=2 edge_churn=1",
        "changed_symbol_count": 2,
        "edge_churn": 1,
        "raw_counts": {"edges_added": 2, "edges_removed": 1},
    }
    assert payload["record"]["source"] == "run"
    assert payload["record"]["signal_value"] == 1
    assert payload["record"]["evidence_ref"] == str(run_json)
    assert payload["record"]["code_graph_delta"] == compact
    assert payload["record"]["context_eval"] == context_eval

    row = json.loads((tmp_path / "memory" / "outcome" / "records.jsonl").read_text())
    assert row["code_graph_delta"] == compact
    assert row["context_eval"] == context_eval
    assert "sidecar_path" not in row["code_graph_delta"]
    assert row["digest"] == localio.canonical_json_digest(row, exclude_keys={"digest"})
    records = outcome_cmd.load_records(tmp_path)
    assert records[0].context_eval == context_eval


def test_capture_run_receipt_maps_ok_error_and_dry_run_signals(tmp_path, capsys):
    _write_run_receipt(tmp_path, run_id="ok-run", status="ok")
    _write_run_receipt(tmp_path, run_id="error-run", status="error")
    _write_run_receipt(tmp_path, run_id="failed-run", status="failed")
    _write_run_receipt(tmp_path, run_id="dry-run", status="dry-run", dry_run=True)
    _write_run_receipt(tmp_path, run_id="read-only-run", status="ok", read_only=True)

    expected = {
        "ok-run": 1,
        "error-run": -1,
        "failed-run": -1,
        "dry-run": 0,
        "read-only-run": 0,
    }
    for run_id, signal in expected.items():
        assert (
            outcome_cmd.capture(
                target=tmp_path,
                artifact_id="skill-x",
                run_receipt=run_id,
                json_output=True,
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["record"]["signal_value"] == signal


def test_capture_run_receipt_latest_uses_newest_run_json(tmp_path, capsys):
    _write_run_receipt(tmp_path, run_id="older", status="error", started_at="2026-06-20T00:00:00+00:00")
    latest = _write_run_receipt(tmp_path, run_id="newer", status="ok", started_at="2026-06-20T01:00:00+00:00")

    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_receipt="latest", json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["signal_value"] == 1
    assert payload["record"]["evidence_ref"] == str(latest)


def test_capture_errors_when_run_id_and_run_receipt_are_both_passed(tmp_path, capsys):
    _write_verify_receipt(tmp_path, run_id="verify-run")
    _write_run_receipt(tmp_path, run_id="brigade-run")

    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            run_id="verify-run",
            run_receipt="brigade-run",
        )
        == 1
    )
    assert "pass either --run-id or --run-receipt, not both" in capsys.readouterr().err


def test_cli_outcome_capture_accepts_run_receipt_and_rejects_both_flags(tmp_path, capsys):
    _write_verify_receipt(tmp_path, run_id="verify-run")
    _write_run_receipt(tmp_path, run_id="brigade-run")

    assert (
        cli.main(
            [
                "outcome",
                "capture",
                "skill-x",
                "--target",
                str(tmp_path),
                "--run-receipt",
                "brigade-run",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["source"] == "run"

    assert (
        cli.main(
            [
                "outcome",
                "capture",
                "skill-x",
                "--target",
                str(tmp_path),
                "--run-id",
                "verify-run",
                "--run-receipt",
                "brigade-run",
            ]
        )
        == 1
    )
    assert "pass either --run-id or --run-receipt, not both" in capsys.readouterr().err


def test_capture_omits_code_graph_delta_when_verify_receipt_omits_it(tmp_path, capsys):
    _write_verify_receipt(tmp_path, run_id="legacy-without-delta")

    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            artifact_kind="skill",
            run_id="legacy-without-delta",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    row = json.loads((tmp_path / "memory" / "outcome" / "records.jsonl").read_text())

    assert "code_graph_delta" not in payload["record"]
    assert "code_graph_delta" not in row


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


def test_rebuild_status_check_accepts_mixed_legacy_and_delta_ledger(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord("card-x", "card", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00"),
            outcome.OutcomeRecord(
                "card-x",
                "card",
                "t2",
                "verify",
                1,
                "ref2",
                "2026-06-20T01:00:00+00:00",
                code_graph_delta={
                    "status": "ok",
                    "summary": "changed_symbols=1",
                    "changed_symbol_count": 1,
                },
            ),
        ],
    )
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    capsys.readouterr()

    assert outcome_cmd.rebuild_status(target=tmp_path, check=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reproducible"] is True
    assert payload["drift"] == []


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


def test_rank_human_surfaces_graph_delta_counters_for_delta_subjects(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t1",
                "verify",
                1,
                "ref1",
                "2026-06-20T00:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 2, "edge_churn": 0},
            ),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t2",
                "verify",
                1,
                "ref2",
                "2026-06-20T01:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 0, "edge_churn": 0},
            ),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t3",
                "verify",
                1,
                "ref3",
                "2026-06-20T02:00:00+00:00",
                code_graph_delta={"status": "skipped", "changed_symbol_count": 0, "edge_churn": 0},
            ),
        ],
    )

    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0

    assert "graph: 1 changing / 1 no-op" in capsys.readouterr().out


def test_rank_json_includes_graph_delta_counters_for_mixed_records(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t1",
                "verify",
                1,
                "ref1",
                "2026-06-20T00:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 0, "edge_churn": 1},
            ),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t2",
                "verify",
                1,
                "ref2",
                "2026-06-20T01:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 0, "edge_churn": 0},
            ),
            outcome.OutcomeRecord("skill-y", "skill", "t3", "verify", 1, "ref3", "2026-06-20T02:00:00+00:00"),
        ],
    )

    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ranking = {item["artifact_id"]: item for item in payload["ranking"]}
    assert ranking["skill-x"]["graph_changing"] == 1
    assert ranking["skill-x"]["graph_no_op"] == 1
    assert "graph_changing" not in ranking["skill-y"]
    assert "graph_no_op" not in ranking["skill-y"]


def test_rank_and_reconcile_count_verify_and_run_receipt_graph_deltas_identically(tmp_path, capsys):
    _write_verify_receipt(
        tmp_path,
        run_id="verify-changing",
        code_graph_delta={"status": "ok", "changed_symbol_count": 1, "edge_churn": 0},
    )
    _write_run_receipt(
        tmp_path,
        run_id="run-no-op",
        code_graph_delta={"status": "ok", "changed_symbol_count": 0, "edge_churn": 0},
        started_at="2026-06-20T01:00:00+00:00",
    )

    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            artifact_kind="skill",
            task_id="t-verify",
            run_id="verify-changing",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        outcome_cmd.capture(
            target=tmp_path,
            artifact_id="skill-x",
            artifact_kind="skill",
            task_id="t-run",
            run_receipt="run-no-op",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    ranking = {item["artifact_id"]: item for item in json.loads(capsys.readouterr().out)["ranking"]}
    assert ranking["skill-x"]["helped"] == 2
    assert ranking["skill-x"]["graph_changing"] == 1
    assert ranking["skill-x"]["graph_no_op"] == 1

    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=True) == 0
    decision = {item["artifact_id"]: item for item in json.loads(capsys.readouterr().out)["decisions"]}["skill-x"]
    assert decision["action"] == "install"
    assert decision["graph_changing"] == 1
    assert decision["graph_no_op"] == 1

    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    expected = (
        f"outcome rank: {tmp_path.resolve()}\n"
        f"- skill-x score={outcome.wilson_lower_bound(2, 2):.3f} helped=2 hurt=0 "
        "graph: 1 changing / 1 no-op\n"
    )
    assert capsys.readouterr().out == expected


def test_reconcile_dry_run_json_surfaces_graph_delta_counters(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t1",
                "verify",
                1,
                "ref1",
                "2026-06-20T00:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 1, "edge_churn": 0},
            ),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t2",
                "verify",
                1,
                "ref2",
                "2026-06-20T01:00:00+00:00",
                code_graph_delta={"status": "ok", "changed_symbol_count": 0, "edge_churn": 0},
            ),
        ],
    )

    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    decision = {item["artifact_id"]: item for item in payload["decisions"]}["skill-x"]
    assert decision["action"] == "install"
    assert decision["graph_changing"] == 1
    assert decision["graph_no_op"] == 1
    assert not _status_file(tmp_path).exists()
    assert not _decisions_dir(tmp_path).exists()


def test_rank_human_output_unchanged_without_delta_records(tmp_path, capsys):
    _seed(tmp_path, [outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00")])

    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0

    expected = (
        f"outcome rank: {tmp_path.resolve()}\n- skill-x score={outcome.wilson_lower_bound(1, 1):.3f} helped=1 hurt=0\n"
    )
    assert capsys.readouterr().out == expected


def test_rank_surfaces_brief_hit_rate_and_uses_it_as_secondary_key(tmp_path, capsys):
    # Equal Wilson scores: skill-high mean hit rate should rank above skill-low.
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-high",
                "skill",
                "t1",
                "verify",
                1,
                "ref-h1",
                "2026-06-20T00:00:00+00:00",
                context_eval={"brief_hit_rate": 1.0, "hits": ["a.py"], "missed": []},
            ),
            outcome.OutcomeRecord(
                "skill-high",
                "skill",
                "t2",
                "verify",
                1,
                "ref-h2",
                "2026-06-20T01:00:00+00:00",
                context_eval={"brief_hit_rate": 0.5, "hits": ["a.py"], "missed": ["b.py"]},
            ),
            outcome.OutcomeRecord(
                "skill-low",
                "skill",
                "t3",
                "verify",
                1,
                "ref-l1",
                "2026-06-20T02:00:00+00:00",
                context_eval={"brief_hit_rate": 0.0, "hits": [], "missed": ["c.py"]},
            ),
            outcome.OutcomeRecord(
                "skill-low",
                "skill",
                "t4",
                "verify",
                1,
                "ref-l2",
                "2026-06-20T03:00:00+00:00",
                context_eval={"brief_hit_rate": 0.25, "hits": ["c.py"], "missed": ["d.py", "e.py", "f.py"]},
            ),
        ],
    )

    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ranking = {item["artifact_id"]: item for item in payload["ranking"]}
    ids = [item["artifact_id"] for item in payload["ranking"]]
    assert ids.index("skill-high") < ids.index("skill-low")
    assert ranking["skill-high"]["brief_hit_rate"] == 0.75
    assert ranking["skill-high"]["brief_hit_samples"] == 2
    assert ranking["skill-low"]["brief_hit_rate"] == 0.125

    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "brief_hit: 0.750 (n=2)" in out
    assert "brief_hit: 0.125 (n=2)" in out


def test_reconcile_json_includes_brief_hit_rate_stats(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t1",
                "verify",
                1,
                "ref1",
                "2026-06-20T00:00:00+00:00",
                context_eval={"brief_hit_rate": 1.0},
            ),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t2",
                "verify",
                1,
                "ref2",
                "2026-06-20T01:00:00+00:00",
                context_eval={"brief_hit_rate": 0.5},
            ),
        ],
    )
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=True) == 0
    decision = {item["artifact_id"]: item for item in json.loads(capsys.readouterr().out)["decisions"]}["skill-x"]
    assert decision["action"] == "install"
    assert decision["brief_hit_rate"] == 0.75
    assert decision["brief_hit_samples"] == 2


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


def test_receipts_verify_accepts_code_graph_delta_outcome_chain(tmp_path, capsys):
    _write_verify_receipt(
        tmp_path,
        run_id="with-delta",
        code_graph_delta={
            "status": "ok",
            "summary": "edge_churn=1",
            "edge_churn": 1,
        },
    )
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", run_id="with-delta", json_output=True) == 0
    capsys.readouterr()

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["mismatch"] == 0
    assert payload["summary"]["missing"] == 0
    ledger_items = [item for item in payload["artifacts"] if item["artifact_type"] == "outcome-ledger-record"]
    assert ledger_items
    assert all(item["status"] == "OK" for item in ledger_items)


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


def _write_registry_skill(target, skill_id, text="# skill body v1\n"):
    skill_dir = target / ".brigade" / "skills" / "registry" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text)
    return skill_dir / "SKILL.md"


def _sha256_of(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_capture_stamps_content_fingerprint_of_registry_skill(tmp_path, capsys):
    skill_md = _write_registry_skill(tmp_path, "skill-x")
    _write_verify_receipt(tmp_path)
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["content_fingerprint"] == _sha256_of(skill_md)
    assert outcome_cmd.load_records(tmp_path)[0].content_fingerprint == _sha256_of(skill_md)


def test_capture_fingerprint_falls_back_to_harness_install(tmp_path, capsys):
    skill_md = tmp_path / ".claude" / "skills" / "skill-x" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# harness copy\n")
    _write_verify_receipt(tmp_path)
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["content_fingerprint"] == _sha256_of(skill_md)


def test_fingerprint_prefers_the_installed_copy_over_a_drifted_registry(tmp_path):
    # The verified run exercises the installed skill; when the registry master
    # has drifted ahead, the signal is evidence about the installed text.
    installed = tmp_path / ".claude" / "skills" / "skill-x" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("# installed v1\n")
    _write_registry_skill(tmp_path, "skill-x", "# registry v2, not yet reinstalled\n")

    assert outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill") == _sha256_of(installed)


def test_capture_without_local_artifact_omits_fingerprint(tmp_path, capsys):
    _write_verify_receipt(tmp_path)
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "content_fingerprint" not in payload["record"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "memory" / "outcome" / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert "content_fingerprint" not in rows[0]
    assert outcome_cmd.load_records(tmp_path)[0].content_fingerprint is None


def test_record_stamps_card_content_fingerprint(tmp_path, capsys):
    card = tmp_path / "memory" / "cards" / "card-x.md"
    card.parent.mkdir(parents=True)
    card.write_text("# card body\n")
    assert (
        outcome_cmd.record(
            target=tmp_path,
            artifact_id="card-x",
            source="friction",
            status="cleared",
            artifact_kind="card",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["content_fingerprint"] == _sha256_of(card)


def test_fingerprinted_records_keep_the_digest_chain_verifiable(tmp_path, capsys):
    _write_registry_skill(tmp_path, "skill-x")
    _write_verify_receipt(tmp_path)
    assert outcome_cmd.capture(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    capsys.readouterr()
    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["mismatch"] == 0
    ledger_items = [item for item in payload["artifacts"] if item["artifact_type"] == "outcome-ledger-record"]
    assert ledger_items and all(item["status"] == "OK" for item in ledger_items)


def test_rank_scores_current_fingerprint_cohort_and_shows_lifetime(tmp_path, capsys):
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# original text\n")
    old_fp = _sha256_of(skill_md)
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                f"t{i}",
                "verify",
                1,
                f"ref{i}",
                f"2026-06-20T0{i}:00:00+00:00",
                content_fingerprint=old_fp,
            )
            for i in range(4)
        ],
    )
    # Edit the skill: the accumulated score must not keep vouching for old text.
    skill_md.write_text("# rewritten text\n")
    new_fp = _sha256_of(skill_md)
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t9",
                "verify",
                1,
                "ref9",
                "2026-06-20T09:00:00+00:00",
                content_fingerprint=new_fp,
            )
        ],
    )
    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["ranking"][0]
    assert entry["content_fingerprint"] == new_fp
    assert entry["helped"] == 1  # current revision only
    assert entry["lifetime_helped"] == 5
    assert entry["stale_records"] == 4
    assert entry["legacy_records"] == 0
    assert entry["score"] == outcome.wilson_lower_bound(1, 1)
    assert entry["lifetime_score"] == outcome.wilson_lower_bound(5, 5)


def test_rank_edited_skill_earns_its_rank_back(tmp_path, capsys):
    # skill-b has the bigger lifetime score, but its text changed after every
    # signal; skill-a's smaller score is all for its current text, so it ranks first.
    a_md = _write_registry_skill(tmp_path, "skill-a", "# a text\n")
    b_md = _write_registry_skill(tmp_path, "skill-b", "# b text v1\n")
    a_fp = _sha256_of(a_md)
    b_old_fp = _sha256_of(b_md)
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-a",
                "skill",
                f"ta{i}",
                "verify",
                1,
                f"ra{i}",
                f"2026-06-20T0{i}:00:00+00:00",
                content_fingerprint=a_fp,
            )
            for i in range(2)
        ],
    )
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-b",
                "skill",
                f"tb{i}",
                "verify",
                1,
                f"rb{i}",
                f"2026-06-20T0{i}:00:00+00:00",
                content_fingerprint=b_old_fp,
            )
            for i in range(6)
        ],
    )
    b_md.write_text("# b text v2\n")
    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [item["artifact_id"] for item in payload["ranking"]]
    assert ids.index("skill-a") < ids.index("skill-b")
    out_lines = None
    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    out_lines = capsys.readouterr().out.splitlines()
    b_line = next(line for line in out_lines if "skill-b" in line)
    assert "score=0.000 helped=0 hurt=0" in b_line
    assert "lifetime score=" in b_line and "stale=6" in b_line


def test_rank_grandfathers_legacy_records_and_keeps_rollout_output_identical(tmp_path, capsys):
    _write_registry_skill(tmp_path, "skill-x")
    _seed(tmp_path, _helped("skill-x", 3))  # pre-fingerprint captures
    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["ranking"][0]
    # Legacy records cannot be proven stale, so a never-edited skill keeps its
    # score at rollout instead of collapsing to zero.
    assert entry["helped"] == 3
    assert entry["lifetime_helped"] == 3
    assert entry["legacy_records"] == 3
    assert entry["stale_records"] == 0
    assert entry["score"] == outcome.wilson_lower_bound(3, 3)
    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "skill-x" in line)
    assert line == f"- skill-x score={outcome.wilson_lower_bound(3, 3):.3f} helped=3 hurt=0"


def test_rank_without_local_artifact_keeps_lifetime_score_and_output_shape(tmp_path, capsys):
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["ranking"][0]
    assert entry["content_fingerprint"] is None
    assert entry["helped"] == 2 and entry["lifetime_helped"] == 2
    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "skill-x" in line)
    assert line == "- skill-x score=0.342 helped=2 hurt=0"


def test_explain_splits_current_and_lifetime_and_tags_cohorts(tmp_path, capsys):
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# v1\n")
    old_fp = _sha256_of(skill_md)
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord("skill-x", "skill", "t0", "verify", 1, "ref0", "2026-06-20T00:00:00+00:00"),
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t1",
                "verify",
                1,
                "ref1",
                "2026-06-20T01:00:00+00:00",
                content_fingerprint=old_fp,
            ),
        ],
    )
    skill_md.write_text("# v2\n")
    new_fp = _sha256_of(skill_md)
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                "t2",
                "verify",
                -1,
                "ref2",
                "2026-06-20T02:00:00+00:00",
                content_fingerprint=new_fp,
            )
        ],
    )
    assert outcome_cmd.explain(target=tmp_path, artifact_id="skill-x", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["content_fingerprint"] == new_fp
    assert payload["score"]["helped"] == 1 and payload["score"]["hurt"] == 1
    assert payload["lifetime_score"]["helped"] == 2 and payload["lifetime_score"]["hurt"] == 1
    assert payload["stale_records"] == 1 and payload["legacy_records"] == 1
    assert [t["cohort"] for t in payload["trail"]] == ["legacy", "stale", "current"]
    assert outcome_cmd.explain(target=tmp_path, artifact_id="skill-x", json_output=False) == 0
    out = capsys.readouterr().out
    assert f"fingerprint: {new_fp[:12]}" in out
    assert "(current fingerprint)" in out
    assert "lifetime:" in out and "stale=1 legacy=1" in out
    assert "[legacy]" in out and "[stale]" in out and "[current]" in out


def test_explain_without_local_artifact_keeps_pre_fingerprint_output(tmp_path, capsys):
    _seed(tmp_path, _helped("skill-x", 1))
    assert outcome_cmd.explain(target=tmp_path, artifact_id="skill-x", json_output=False) == 0
    out = capsys.readouterr().out
    assert "fingerprint:" not in out
    assert "lifetime:" not in out
    assert "[legacy]" not in out


def _fp_helped(artifact_id, n, fingerprint, start_hour=0):
    return [
        outcome.OutcomeRecord(
            artifact_id,
            "skill",
            f"t{i}",
            "verify",
            1,
            f"ref-{fingerprint}-{i}",
            f"2026-06-20T{start_hour + i:02d}:00:00+00:00",
            content_fingerprint=fingerprint,
        )
        for i in range(n)
    ]


def test_reconcile_does_not_promote_a_candidate_on_proven_stale_evidence(tmp_path, capsys, monkeypatch):
    # Two helped signals for the old text would cross install_min_helped, but the
    # skill was edited afterward: the old signals are proven stale, so the ratchet
    # must NOT promote text that has no verified evidence of its own.
    _stub_execute(monkeypatch)
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# old text\n")
    old_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, old_fp))
    skill_md.write_text("# rewritten text\n")

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions"] == []  # held, not installed
    assert payload["applied"] == []
    assert not _status_file(tmp_path).exists()


def test_reconcile_still_promotes_a_never_edited_skill_grandfathered(tmp_path, capsys, monkeypatch):
    # Grandfathering safety: a registry skill with only pre-fingerprint (legacy)
    # signals promotes exactly as the pre-fingerprint ratchet did. No proven-stale
    # records, so the decision and its receipt stay byte-identical.
    _stub_execute(monkeypatch)
    _write_registry_skill(tmp_path, "skill-x")
    _seed(tmp_path, _helped("skill-x", 2))  # legacy, no fingerprint

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == ["skill-x"]
    decision = {d["artifact_id"]: d for d in payload["decisions"]}["skill-x"]
    assert decision["action"] == "install"
    # No stale evidence dropped, so no fingerprint audit fields leak into the receipt.
    assert "stale_records" not in decision
    assert "content_fingerprint" not in decision


def test_reconcile_lets_an_edited_skill_re_earn_promotion_on_fresh_signals(tmp_path, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# old text\n")
    old_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, old_fp, start_hour=0))
    skill_md.write_text("# rewritten text\n")
    new_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, new_fp, start_hour=5))

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] == ["skill-x"]
    decision = {d["artifact_id"]: d for d in payload["decisions"]}["skill-x"]
    assert decision["action"] == "install"
    # The decision was scored on the current text; the audit fields record what it dropped.
    assert decision["content_fingerprint"] == new_fp
    assert decision["stale_records"] == 2
    assert decision["lifetime_helped"] == 4


def test_reconcile_human_output_notes_a_fingerprint_narrowed_decision(tmp_path, capsys, monkeypatch):
    _stub_execute(monkeypatch)
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# old text\n")
    old_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, old_fp, start_hour=0))
    skill_md.write_text("# rewritten text\n")
    new_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, new_fp, start_hour=5))

    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=False) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "skill-x" in line)
    assert "scored current text only" in line
    assert f"rev {new_fp[:12]}" in line
    assert "stale=2" in line


def test_reconcile_output_byte_identical_for_unedited_skill(tmp_path, capsys, monkeypatch):
    # The teeth must not disturb the common case: a skill with no proven-stale
    # records produces exactly the pre-fingerprint one-line output.
    _stub_execute(monkeypatch)
    _write_registry_skill(tmp_path, "skill-x")
    _seed(tmp_path, _helped("skill-x", 2))
    assert outcome_cmd.reconcile(target=tmp_path, apply=False, json_output=False) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "skill-x" in line)
    assert line == "- skill-x candidate -> promoted [install] verified helped, no regressions"


def test_fork_projection_uses_the_current_fingerprint_cohort(tmp_path, capsys):
    # Lifetime would cross install_min_helped (2 helped), but all of it is proven
    # stale, so the fork must project a hold, not a promotion.
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# old text\n")
    old_fp = _sha256_of(skill_md)
    _seed(tmp_path, _fp_helped("skill-x", 2, old_fp))
    skill_md.write_text("# rewritten text\n")
    out = tmp_path / "fork.json"

    assert outcome_cmd.fork(target=tmp_path, out=out, json_output=True) == 0
    capsys.readouterr()
    projection = json.loads(out.read_text())
    entry = projection["artifacts"]["skill-x"]
    assert entry["new_status"] != "promoted"
    assert entry["helped"] == 0  # current cohort, not the 2 stale lifetime signals


def _registry_skill_dir(target, skill_id):
    d = target / ".brigade" / "skills" / "registry" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_bundle_fingerprint_reduces_to_skill_md_hash_for_a_lone_file(tmp_path):
    # Backward-compat guarantee: a skill whose only content file is SKILL.md must
    # fingerprint byte-identically to the pre-bundle sha256(SKILL.md), or every
    # existing single-file record would be invalidated on upgrade.
    skill_md = _write_registry_skill(tmp_path, "skill-x", "# body\n")
    assert outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill") == _sha256_of(skill_md)


def test_bundle_fingerprint_ignores_skill_json_and_ds_store(tmp_path):
    # The install-time metadata sidecar and OS cruft are not skill logic, so a
    # skill with SKILL.md plus only those still reduces to the lone-file hash.
    d = _registry_skill_dir(tmp_path, "skill-x")
    skill_md = d / "SKILL.md"
    skill_md.write_text("# body\n")
    (d / "skill.json").write_text('{"id": "skill-x"}')
    (d / ".DS_Store").write_text("junk")
    assert outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill") == _sha256_of(skill_md)


def test_bundle_fingerprint_covers_a_bundled_helper(tmp_path):
    # A real multi-file bundle takes the composite path: its fingerprint differs
    # from sha256(SKILL.md) because it also folds in the helper.
    d = _registry_skill_dir(tmp_path, "skill-x")
    skill_md = d / "SKILL.md"
    skill_md.write_text("# body\n")
    (d / "helper.sh").write_text("echo v1\n")
    fp = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    assert fp is not None
    assert fp != _sha256_of(skill_md)


def test_bundle_fingerprint_changes_when_only_the_helper_changes(tmp_path):
    # The core win: editing a bundled helper while SKILL.md is untouched must move
    # the fingerprint, so signals for the old bundle become proven stale.
    d = _registry_skill_dir(tmp_path, "skill-x")
    (d / "SKILL.md").write_text("# body\n")
    helper = d / "helper.sh"
    helper.write_text("echo v1\n")
    before = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    helper.write_text("echo v2\n")
    after = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    assert before is not None and after is not None
    assert before != after


def test_bundle_fingerprint_changes_when_a_file_is_added(tmp_path):
    d = _registry_skill_dir(tmp_path, "skill-x")
    (d / "SKILL.md").write_text("# body\n")
    before = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    (d / "reference.md").write_text("# extra context\n")
    after = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    assert before != after


def test_editing_a_bundled_helper_makes_prior_records_proven_stale_in_rank(tmp_path, capsys):
    # End to end: a skill's signals were captured against a bundle whose helper has
    # since changed. Rank must drop them from the current score even though SKILL.md
    # never moved.
    d = _registry_skill_dir(tmp_path, "skill-x")
    (d / "SKILL.md").write_text("# body\n")
    helper = d / "helper.sh"
    helper.write_text("echo v1\n")
    old_fp = outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")
    _seed(
        tmp_path,
        [
            outcome.OutcomeRecord(
                "skill-x",
                "skill",
                f"t{i}",
                "verify",
                1,
                f"ref{i}",
                f"2026-06-20T0{i}:00:00+00:00",
                content_fingerprint=old_fp,
            )
            for i in range(3)
        ],
    )
    helper.write_text("echo v2\n")  # only the helper changes

    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    entry = json.loads(capsys.readouterr().out)["ranking"][0]
    assert entry["helped"] == 0  # current bundle has no signals of its own
    assert entry["lifetime_helped"] == 3
    assert entry["stale_records"] == 3
    assert entry["content_fingerprint"] == outcome_cmd.artifact_fingerprint(tmp_path, "skill-x", "skill")


def test_card_fingerprint_stays_single_file(tmp_path):
    card = tmp_path / "memory" / "cards" / "card-x.md"
    card.parent.mkdir(parents=True)
    card.write_text("# card body\n")
    assert outcome_cmd.artifact_fingerprint(tmp_path, "card-x", "card") == _sha256_of(card)
