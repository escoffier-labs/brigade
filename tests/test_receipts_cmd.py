import json

from brigade import cli, localio, outcome, outcome_cmd, receipts_cmd, runbook_cmd, work_cmd

from tests.work_cmd_test_helpers import _init_git_repo


def _write_digestless_work_receipt(target):
    run_dir = target / ".brigade" / "work" / "verify-runs" / "legacy"
    run_dir.mkdir(parents=True)
    receipt = {
        "run_id": "legacy",
        "target": str(target),
        "status": "completed",
        "commands": [],
    }
    localio.write_json(run_dir / "receipt.json", receipt)


def _write_runbook(path):
    path.write_text(
        json.dumps(
            {
                "id": "smoke",
                "description": "tiny runbook",
                "allowed_commands": ["printf"],
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )
    return path


def _append_outcome(target, evidence_ref):
    outcome_cmd.append_records(
        target,
        [
            outcome.OutcomeRecord(
                "taste",
                "skill",
                evidence_ref,
                "verify",
                1,
                evidence_ref,
                f"2026-07-08T00:00:0{evidence_ref[-1]}+00:00",
            )
        ],
    )


def test_receipts_verify_fresh_target_passes(tmp_path, capsys):
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
    capsys.readouterr()
    assert (
        runbook_cmd.run(
            target=tmp_path,
            runbook=_write_runbook(tmp_path / "runbook.json"),
            approved=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    _append_outcome(tmp_path, "ref1")

    assert cli.main(["receipts", "verify", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 0
    assert payload["summary"]["missing"] == 0
    assert payload["summary"]["ok"] >= 2
    assert {item["status"] for item in payload["artifacts"]} == {"OK"}
    assert "runbook-receipt" in {item["artifact_type"] for item in payload["artifacts"]}


def test_receipts_verify_reports_mismatch_when_receipt_field_is_edited(tmp_path, capsys):
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
    path = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"] / "receipt.json"
    tampered = json.loads(path.read_text())
    tampered["status"] = "failed"
    localio.write_json(path, tampered)

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-receipt"
    assert problem["check"] == "receipt_sha256"


def test_receipts_verify_reports_missing_when_referenced_log_is_deleted(tmp_path, capsys):
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
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"]
    (run_dir / "command-1-stdout.log").unlink()

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["missing"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISSING"][0]
    assert problem["artifact_type"] == "work-verify-log"
    assert problem["artifact_id"].endswith("command-1-stdout.log")


def test_receipts_verify_reports_mismatch_when_middle_ledger_record_is_edited(tmp_path, capsys):
    _append_outcome(tmp_path, "ref1")
    _append_outcome(tmp_path, "ref2")
    _append_outcome(tmp_path, "ref3")
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["signal_value"] = -1
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "outcome-ledger-record"
    assert problem["artifact_id"].endswith("records.jsonl:2")
    assert problem["check"] == "digest"


def test_receipts_verify_reports_mismatch_when_middle_ledger_record_is_deleted(tmp_path, capsys):
    _append_outcome(tmp_path, "ref1")
    _append_outcome(tmp_path, "ref2")
    _append_outcome(tmp_path, "ref3")
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    rows = path.read_text().splitlines()
    path.write_text(rows[0] + "\n" + rows[2] + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "outcome-ledger-record"
    assert problem["artifact_id"].endswith("records.jsonl:2")
    assert problem["check"] == "prev_digest"


def test_receipts_verify_reports_legacy_and_exits_zero(tmp_path, capsys):
    _write_digestless_work_receipt(tmp_path)
    legacy = {
        "artifact_id": "taste",
        "artifact_kind": "skill",
        "task_id": "legacy",
        "source": "verify",
        "signal_value": 1,
        "evidence_ref": "legacy",
        "ts": "2026-07-08T00:00:00+00:00",
    }
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out

    assert "legacy=2" in out
    assert "LEGACY" in out


def test_doctor_and_outcome_doctor_include_receipt_summary(tmp_path, capsys):
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
    path = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"] / "receipt.json"
    tampered = json.loads(path.read_text())
    tampered["status"] = "failed"
    localio.write_json(path, tampered)

    assert cli.main(["doctor", "--target", str(tmp_path)]) == 1
    doctor_out = capsys.readouterr().out
    assert "receipts: verify" in doctor_out
    assert "mismatch=1" in doctor_out

    assert cli.main(["outcome", "doctor", "--target", str(tmp_path)]) == 0
    outcome_out = capsys.readouterr().out
    assert "outcome doctor:" in outcome_out
    assert "mismatch=1" in outcome_out


def test_receipts_verify_reports_mismatch_when_referenced_log_is_edited(tmp_path, capsys):
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
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / receipt["run_id"]
    log_path = run_dir / "command-1-stdout.log"
    log_path.write_text(log_path.read_text() + "tampered evidence\n")

    assert receipts_cmd.verify(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["mismatch"] == 1
    problem = [item for item in payload["artifacts"] if item["status"] == "MISMATCH"][0]
    assert problem["artifact_type"] == "work-verify-log"
    assert problem["artifact_id"].endswith("command-1-stdout.log")
    assert problem["check"] == "log_sha256"
