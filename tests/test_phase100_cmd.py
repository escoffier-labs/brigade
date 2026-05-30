import json

from brigade import center_cmd, cli


def _write_command_inventory(path):
    (path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (path / "ROADMAP.md").write_text("# Roadmap\n")
    assert cli.main(["roadmap", "commands", "--target", str(path), "--write", "--json"]) == 0


def _write_release_receipt(path, *, ready=True):
    run_dir = path / ".brigade" / "release" / "runs" / "release-ready"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "release-ready",
                "status": "ready" if ready else "blocked",
                "ready": ready,
                "started_at": "2026-05-30T00:00:00+00:00",
                "completed_at": "2026-05-30T00:01:00+00:00",
                "blockers": [] if ready else ["blocked"],
                "warnings": [],
                "checks": [],
            }
        )
    )


def test_center_readiness_clean_closeout_and_manual_checklist(tmp_path, capsys):
    _write_command_inventory(tmp_path)
    capsys.readouterr()
    _write_release_receipt(tmp_path, ready=True)

    assert center_cmd.readiness_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ready"] is True
    assert plan["blocker_count"] == 0
    assert all(item["manual_only"] is True for item in plan["manual_publish_checklist"])
    assert any(item["remote_mutation"] is True for item in plan["manual_publish_checklist"] if "remote_mutation" in item)

    assert cli.main(["center", "readiness", "closeout", "--target", str(tmp_path), "--json"]) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["ready"] is True
    assert closeout["review_status"] == "reviewed"
    assert (tmp_path / ".brigade" / "center" / "readiness" / closeout["readiness_id"] / "MANUAL_PUBLISH_CHECKLIST.md").is_file()

    assert center_cmd.readiness_list(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["readiness_count"] == 1
    assert center_cmd.readiness_show(target=tmp_path, readiness_id="latest", json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["readiness_id"] == closeout["readiness_id"]


def test_center_readiness_blockers_and_imports(tmp_path, capsys):
    _write_command_inventory(tmp_path)
    capsys.readouterr()

    assert center_cmd.readiness_plan(target=tmp_path, json_output=True) == 1
    plan = json.loads(capsys.readouterr().out)
    assert plan["ready"] is False
    assert any(item["name"] == "missing_release_readiness" for item in plan["blockers"])

    assert center_cmd.readiness_import_issues(target=tmp_path, dry_run=True, json_output=True) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["candidate_count"] >= 1
    assert all(record["source"] == "center-readiness" for record in dry_run["records"])

    assert cli.main(["center", "readiness", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["imported"] >= 1


def test_center_readiness_waiver_allows_reviewed_closeout(tmp_path, capsys):
    _write_command_inventory(tmp_path)
    capsys.readouterr()
    assert center_cmd.readiness_plan(target=tmp_path, json_output=True) == 1
    plan = json.loads(capsys.readouterr().out)
    finding_id = next(item["finding_id"] for item in plan["blockers"] if item["name"] == "missing_release_readiness")

    assert (
        cli.main(
            [
                "center",
                "readiness",
                "closeout",
                "--target",
                str(tmp_path),
                "--waive",
                finding_id,
                "--reason",
                "local review accepted",
                "--json",
            ]
        )
        == 0
    )
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["ready"] is True
    assert closeout["waived_count"] >= 1
    assert closeout["findings"][0]["waived"] is True or any(item.get("waived") for item in closeout["findings"])


def test_center_readiness_integrates_with_status_reviews_and_schema(tmp_path, capsys):
    _write_command_inventory(tmp_path)
    capsys.readouterr()
    _write_release_receipt(tmp_path, ready=True)
    assert center_cmd.readiness_closeout(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["operator_readiness"]["issue_count"] == 0

    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert not any(item["subsystem"] == "center-readiness" for item in reviews["reviews"])

    assert center_cmd.schema(target=tmp_path, json_output=True) == 0
    schema = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "center-readiness" for item in schema["schemas"])
