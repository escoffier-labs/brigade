import json
from datetime import datetime, timezone

from brigade import cli
from brigade import friction_cmd


def test_friction_scan_writes_artifacts_and_imports_candidates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    learnings = tmp_path / ".learnings"
    learnings.mkdir()
    (learnings / "ERRORS.md").write_text(
        "## ERR\n\nTool failed with HTTP 403 Authentication error, had to use browser fallback.\n"
    )

    code = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--days",
            "30",
            "--import-candidates",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "friction scan:" in out
    assert "candidates: 1" in out
    payload = json.loads((tmp_path / ".brigade" / "friction" / "latest.json").read_text())
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["friction_type"] == "auth"
    assert (tmp_path / ".brigade" / "friction" / "latest.md").is_file()
    imports = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()
    assert len(imports) == 1
    imported = json.loads(imports[0])
    assert imported["source"] == "friction-scan"
    assert imported["kind"] == "finding"
    metadata = imported["metadata"]
    assert metadata["friction_type"] == "auth"
    assert metadata["source_item_key"] == metadata["friction_id"]
    assert metadata["source_fingerprint"]
    assert "source_key" not in metadata
    assert "fingerprint" not in metadata


def test_friction_scan_import_dedupes_by_source_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "issue.md").write_text("Deploy was blocked because the token expired.\n")

    code = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--days",
            "30",
            "--import-candidates",
        ]
    )

    assert code == 0
    capsys.readouterr()
    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert len(imports) == 1
    imports[0]["text"] = "Operator edited the summary text after import."
    imports_path.write_text(json.dumps(imports[0], sort_keys=True) + "\n")

    code = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--days",
            "30",
            "--import-candidates",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "imports_added: 0" in out
    assert "imports_skipped: 1" in out
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert len(imports) == 1


def test_friction_scan_import_skips_when_evidence_line_drifts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    notes = tmp_path / "notes"
    notes.mkdir()
    issue = notes / "issue.md"
    issue.write_text("Deploy was blocked because the token expired.\n")

    code = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--days",
            "30",
            "--import-candidates",
        ]
    )

    assert code == 0
    capsys.readouterr()
    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    assert len(imports_path.read_text().splitlines()) == 1

    issue.write_text(
        "Weekly maintenance summary.\n"
        "Nothing unusual in the morning window.\n"
        "Deploy was blocked because the token expired.\n"
    )

    code = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--days",
            "30",
            "--import-candidates",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "imports_added: 0" in out
    assert "imports_skipped: 1" in out
    assert len(imports_path.read_text().splitlines()) == 1


def test_friction_scan_json_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "note.md").write_text("The workflow was blocked because the docs were missing.\n")

    code = cli.main(["friction", "scan", "--target", str(tmp_path), "--dry-run", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["friction_type"] == "blocked"
    assert payload["output"]["dry_run"] is True
    assert not (tmp_path / ".brigade" / "friction" / "latest.json").exists()


def test_friction_scan_ignores_claude_hook_boilerplate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    handoffs = tmp_path / ".claude" / "memory-handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "session.jsonl").write_text(
        '{"attachment":{"type":"hook_success","content":"blocked failed missing token"},"message":{"content":"blocked"}}\n'
        '{"message":{"role":"assistant","content":[{"type":"text","text":"Real tool failed with timeout."}]}}\n'
    )

    code = cli.main(["friction", "scan", "--target", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["evidence"]["snippet"] == "Real tool failed with timeout."


def test_friction_add_creates_manual_import(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )

    code = cli.main(
        [
            "friction",
            "add",
            "--target",
            str(tmp_path),
            "--type",
            "latency",
            "--severity",
            "low",
            "--workflow",
            "screenshots",
            "Cloche screenshot task took too long for a simple capture",
        ]
    )

    assert code == 0
    assert "friction:" in capsys.readouterr().out
    imports = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()
    assert len(imports) == 1
    imported = json.loads(imports[0])
    assert imported["source"] == "friction-manual"
    metadata = imported["metadata"]
    assert metadata["workflow"] == "screenshots"
    assert metadata["source_item_key"] == metadata["friction_id"]
    assert metadata["source_fingerprint"]
    assert "source_key" not in metadata
    assert "fingerprint" not in metadata


def test_friction_scan_ignores_passing_verify_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "20260610-run"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [{"command": "./scripts/verify", "exit_code": 0, "status": "completed"}],
                "evidence": {"handoff_drafts": {"counts": {"failed": 0}}},
                "timeouts": {"timeout": 900},
            },
            indent=2,
        )
    )

    code = cli.main(["friction", "scan", "--target", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 0


def test_friction_scan_reports_failing_verify_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "20260610-run"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "commands": [
                    {
                        "command": "pytest -q",
                        "exit_code": 1,
                        "status": "completed",
                        "stderr_summary": "2 failed, 310 passed",
                    },
                    {
                        "command": "slow-check",
                        "exit_code": None,
                        "status": "timeout",
                        "stderr_summary": "",
                    },
                ],
            },
            indent=2,
        )
    )

    code = cli.main(["friction", "scan", "--target", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 2
    by_type = {c["friction_type"]: c for c in payload["candidates"]}
    failure = by_type["tool_failure"]
    assert failure["severity"] == "high"
    assert "pytest -q" in failure["evidence"]["snippet"]
    assert "2 failed" in failure["evidence"]["snippet"]
    timeout = by_type["network_timeout"]
    assert timeout["severity"] == "medium"
    assert "slow-check" in timeout["evidence"]["snippet"]


def test_friction_scan_ignores_numeric_json_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    learnings = tmp_path / ".learnings"
    learnings.mkdir()
    (learnings / "stats.json").write_text(
        '{\n  "failed": 0,\n  "timeout": 900,\n  "note": "everything blocked because auth expired"\n}\n'
    )

    code = cli.main(["friction", "scan", "--target", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1


def test_friction_v2_preserves_each_typed_source_family_before_truncation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        friction_cmd,
        "_now",
        lambda: datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    verify = tmp_path / ".brigade" / "work" / "verify-runs" / "v1"
    verify.mkdir(parents=True)
    (verify / "receipt.json").write_text(
        json.dumps({"status": "failed", "commands": [{"command": "check", "exit_code": 1, "status": "failed"}]})
    )
    run = tmp_path / ".brigade" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "worker-results.json").write_text(
        json.dumps({"results": [{"worker": "composer", "ok": False, "detail": "empty output"}]})
    )
    cell = tmp_path / ".brigade" / "evals" / "e1" / "cells" / "c1"
    cell.mkdir(parents=True)
    (cell / "cell.json").write_text(
        json.dumps({"cell_id": "c1", "case_id": "case", "seat": "composer", "state": "rejected"})
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"status": "failed", "operation": "archive", "error": "locked"}) + "\n")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "issue.md").write_text("Manual workflow was blocked because docs were missing.\n")

    rc = cli.main(
        [
            "friction",
            "scan",
            "--target",
            str(tmp_path),
            "--miseledger",
            str(ledger),
            "--max-candidates",
            "5",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["source_family"] for item in payload["candidates"]} == {
        "verification",
        "run",
        "evaluation",
        "miseledger",
        "regex",
    }
    assert payload["structured_hits"] == 4
    assert payload["regex_hits"] == 1
    assert all(value == 1 for value in payload["quota_use"].values())


def test_structured_recurrence_dedupes_across_receipt_paths(tmp_path, capsys):
    for run_id in ("r1", "r2"):
        run = tmp_path / ".brigade" / "runs" / run_id
        run.mkdir(parents=True)
        (run / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "composer", "ok": False, "detail": "same failure"}]})
        )

    assert cli.main(["friction", "scan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["duplicates"] == 1
