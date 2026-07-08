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
