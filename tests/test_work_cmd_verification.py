import json
from pathlib import Path

from brigade import cli
from brigade import localio
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def test_verify_run_marks_parser_rejected_command_as_rejected_not_failed(tmp_path, capsys):
    # A command Brigade's own parser refuses (shell metacharacters here) never runs;
    # it is invalid input, not a verified regression, so the receipt status must be
    # 'rejected' (neutral for outcome capture), never 'failed' (-1).
    _init_git_repo(tmp_path)
    rc = work_cmd.verify_run(target=tmp_path, commands=["echo hi && echo bye"], json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert payload["status"] == "rejected"
    assert payload["commands"][0]["status"] == "rejected"

    from brigade import outcome_cmd

    assert outcome_cmd.capture(target=tmp_path, artifact_id="brigade-work", json_output=True) == 0
    record = json.loads(capsys.readouterr().out)["record"]
    assert record["signal_value"] == 0


def test_verify_run_capture_records_outcome_in_one_step(tmp_path, capsys):
    _init_git_repo(tmp_path)
    from brigade import outcome_cmd

    rc = work_cmd.verify_run(
        target=tmp_path, commands=["python3 -c \"print('ok')\""], capture="skill-x", capture_kind="skill"
    )
    assert rc == 0
    capsys.readouterr()
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1
    assert records[0].artifact_id == "skill-x" and records[0].signal_value == 1


def test_prune_verify_runs_keeps_newest(tmp_path):
    from brigade.work_cmd import helpers, verification

    root = helpers._verify_runs_root(tmp_path)
    root.mkdir(parents=True)
    for name in ("20260101-000001-a", "20260101-000002-b", "20260101-000003-c"):
        (root / name).mkdir()
    removed = verification._prune_verify_runs(tmp_path, keep=2)
    assert removed == 1
    assert sorted(p.name for p in root.iterdir()) == ["20260101-000002-b", "20260101-000003-c"]


def test_outcome_health_flags_dormant_then_half_fed(tmp_path):
    from brigade import outcome_cmd

    dormant = outcome_cmd.health(tmp_path)
    assert dormant["record_count"] == 0 and dormant["verify_run_count"] == 0
    assert dormant["top_issue"]["name"] == "outcome_loop_dormant"

    _init_git_repo(tmp_path)
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""]) == 0
    half_fed = outcome_cmd.health(tmp_path)
    assert half_fed["verify_run_count"] >= 1 and half_fed["record_count"] == 0
    assert half_fed["top_issue"]["name"] == "outcome_loop_half_fed"


def test_work_acceptance_rollup_covers_completion_review_and_closeout(tmp_path, capsys):
    _init_git_repo(tmp_path)
    ledger = {
        "version": 1,
        "tasks": [
            {
                "id": "pending-ready",
                "text": "Pending with acceptance",
                "status": "pending",
                "acceptance": ["Ready acceptance."],
            },
            {
                "id": "pending-missing",
                "text": "Pending missing acceptance",
                "status": "pending",
            },
            {
                "id": "done-ready",
                "text": "Done with completion",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completed_acceptance": ["Done acceptance."],
                "completion": {"session_path": ".brigade/work/session-one"},
            },
            {
                "id": "done-missing-completion",
                "text": "Done missing completion",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completed_acceptance": ["Done acceptance."],
            },
            {
                "id": "done-missing-completed-acceptance",
                "text": "Done missing completed acceptance",
                "status": "done",
                "acceptance": ["Done acceptance."],
                "completion": {"session_path": ".brigade/work/session-two"},
            },
        ],
    }
    work_cmd._write_task_ledger(tmp_path, ledger)
    imports = []
    for finding_id, status, task_id, dismiss_reason in (
        ("pending-finding", "pending", None, None),
        ("dismissed-finding", "dismissed", None, "not actionable"),
        ("completed-finding", "promoted", "done-ready", None),
    ):
        item = work_cmd._make_import(
            f"Review finding {finding_id}",
            kind="task",
            source="code-review",
            metadata={
                "reviewer_id": "codex-review",
                "review_run_id": "run-one",
                "review_finding_id": finding_id,
                "source_item_key": f"code-review:codex-review:{finding_id}",
                "source_fingerprint": f"fp-{finding_id}",
            },
        )
        item["status"] = status
        if task_id:
            item["task_id"] = task_id
        if dismiss_reason:
            item["dismiss_reason"] = dismiss_reason
        imports.append(item)
    work_cmd._write_imports(tmp_path, imports)
    (tmp_path / ".brigade" / "work" / "closeouts" / "blocked-closeout").mkdir(parents=True)
    _write_json(
        tmp_path / ".brigade" / "work" / "closeouts" / "blocked-closeout" / "closeout.json",
        {
            "closeout_id": "blocked-closeout",
            "ready": False,
            "status": "blocked",
            "created_at": "2026-05-29T12:00:00+00:00",
            "acceptance_criteria": ["Closeout acceptance."],
            "blockers": ["review run is not closed out"],
        },
    )

    assert work_cmd.acceptance(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_with_acceptance"] == ["pending-ready"]
    assert payload["pending_missing_acceptance"] == ["pending-missing"]
    assert payload["done_with_completion"] == ["done-ready", "done-missing-completed-acceptance"]
    assert payload["done_missing_completion"] == ["done-missing-completion"]
    assert payload["done_missing_completed_acceptance"] == ["done-missing-completed-acceptance"]
    assert payload["review_findings"]["outcomes"] == {
        "completed": 1,
        "dismissed": 1,
        "pending": 1,
    }
    assert payload["latest_work_closeout"]["closeout_id"] == "blocked-closeout"
    issue_names = {issue["name"] for issue in payload["issues"]}
    assert "acceptance_pending_missing" in issue_names
    assert "acceptance_done_missing_completion" in issue_names
    assert "acceptance_done_missing_completed_acceptance" in issue_names
    assert "acceptance_review_findings_unresolved" in issue_names
    assert "acceptance_work_closeout_blocked" in issue_names

    assert work_cmd.acceptance(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "done_missing_completed_acceptance: 1" in out
    assert "review_findings_unresolved: 1" in out
    assert "work_closeout: blocked-closeout" in out


def test_work_verify_plan_run_list_show(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.verify_plan(target=tmp_path, commands=["python3 -c \"print('ok')\""], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"] == ["python3 -c \"print('ok')\""]
    assert payload["blockers"] == []

    assert (
        work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('ok')\""], timeout=30, json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert receipt["commands"][0]["stdout_summary"] == "ok"
    assert Path(receipt["commands"][0]["stdout_log_path"]).is_file()
    assert Path(receipt["path"], "receipt.json").is_file()
    assert Path(receipt["path"], "summary.md").is_file()

    assert work_cmd.verify_runs(target=tmp_path, json_output=True) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs["runs"][0]["run_id"] == receipt["run_id"]

    assert work_cmd.verify_show(target=tmp_path, run_id="latest") == 0
    out = capsys.readouterr().out
    assert f"work verify run: {receipt['run_id']}" in out
    assert "python3 -c" in out


def test_work_verify_receipt_digests_recompute_from_payload_and_logs(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=["python3 -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    digests = receipt["digests"]
    run_dir = Path(receipt["path"])

    assert digests["algorithm"] == "sha256"
    assert digests["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    assert digests["logs"] == {
        "command-1-stderr.log": localio.file_sha256(run_dir / "command-1-stderr.log"),
        "command-1-stdout.log": localio.file_sha256(run_dir / "command-1-stdout.log"),
    }

    stored = json.loads((run_dir / "receipt.json").read_text())
    assert stored["digests"] == digests


def test_work_closeout_writes_ready_receipt(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = {
        "id": "task-one",
        "text": "Ship feature",
        "source": "manual",
        "type": "feature",
        "priority": "normal",
        "acceptance": ["Tests pass."],
    }
    assert work_cmd.start(target=tmp_path, title="Ship feature", force=False, task_snapshot=task) == 0
    capsys.readouterr()
    assert work_cmd.end(target=tmp_path, note="done", handoff=False) == 0
    capsys.readouterr()
    assert work_cmd.verify_run(target=tmp_path, commands=["python3 -c \"print('verified')\""], timeout=30) == 0
    capsys.readouterr()

    assert work_cmd.closeout(target=tmp_path, session_id="latest", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["status"] == "ready"
    assert payload["acceptance_criteria"] == ["Tests pass."]
    assert payload["verification"]["status"] == "completed"
    assert Path(payload["path"]).is_file()
    assert Path(payload["path"]).with_name("closeout.md").is_file()
    session = json.loads((Path(payload["session_path"]) / "session.json").read_text())
    assert session["closeout"]["closeout_id"] == payload["closeout_id"]


def test_work_closeout_blocks_failed_verification(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = {
        "id": "task-one",
        "text": "Ship feature",
        "source": "manual",
        "type": "feature",
        "priority": "normal",
        "acceptance": ["Tests pass."],
    }
    assert work_cmd.start(target=tmp_path, title="Ship feature", force=False, task_snapshot=task) == 0
    capsys.readouterr()
    assert work_cmd.end(target=tmp_path, note="done", handoff=False) == 0
    capsys.readouterr()
    assert work_cmd.verify_run(target=tmp_path, commands=['python3 -c "raise SystemExit(3)"'], timeout=30) == 3
    capsys.readouterr()

    assert work_cmd.closeout(target=tmp_path, session_id="latest", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["status"] == "blocked"
    assert "latest verification did not complete" in payload["blockers"][0]


def test_work_verify_and_closeout_cli(tmp_path, monkeypatch):
    seen = []

    def fake_verify_plan(**kwargs):
        seen.append(("verify-plan", kwargs))
        return 0

    def fake_verify_run(**kwargs):
        seen.append(("verify-run", kwargs))
        return 0

    def fake_verify_runs(**kwargs):
        seen.append(("verify-runs", kwargs))
        return 0

    def fake_verify_show(**kwargs):
        seen.append(("verify-show", kwargs))
        return 0

    def fake_closeout(**kwargs):
        seen.append(("closeout", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "verify_plan", fake_verify_plan)
    monkeypatch.setattr(work_cmd, "verify_run", fake_verify_run)
    monkeypatch.setattr(work_cmd, "verify_runs", fake_verify_runs)
    monkeypatch.setattr(work_cmd, "verify_show", fake_verify_show)
    monkeypatch.setattr(work_cmd, "closeout", fake_closeout)

    assert (
        cli.main(["work", "verify", "plan", "--target", str(tmp_path), "--command", "python3 -m pytest -q", "--json"])
        == 0
    )
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(tmp_path),
                "--command",
                "python3 -m pytest -q",
                "--timeout",
                "12",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["work", "verify", "runs", "--target", str(tmp_path), "--limit", "3", "--json"]) == 0
    assert cli.main(["work", "verify", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "closeout", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("verify-plan", {"target": tmp_path, "commands": ["python3 -m pytest -q"], "json_output": True}),
        (
            "verify-run",
            {
                "target": tmp_path,
                "commands": ["python3 -m pytest -q"],
                "timeout": 12,
                "json_output": True,
                "capture": None,
                "capture_kind": "skill",
            },
        ),
        ("verify-runs", {"target": tmp_path, "limit": 3, "json_output": True}),
        ("verify-show", {"target": tmp_path, "run_id": "latest", "json_output": True}),
        ("closeout", {"target": tmp_path, "session_id": "latest", "json_output": True}),
    ]
