import json
from datetime import timedelta

from brigade import center_cmd, cli, release_cmd


def test_release_smoke_plan_record_list_show_and_receipt_json(tmp_path, capsys):
    assert cli.main(["release", "smoke", "plan", "--target", str(tmp_path), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["matrix_count"] == len(release_cmd.INSTALL_SMOKE_MATRIX)
    assert any(item["matrix_id"] == "repo-codex" for item in plan["matrix"])

    assert (
        cli.main(
            [
                "release",
                "smoke",
                "record",
                "--target",
                str(tmp_path),
                "--depth",
                "repo",
                "--harnesses",
                "codex",
                "--status",
                "passed",
                "--command-label",
                "brigade init --depth repo --harnesses codex",
                "--summary",
                "smoke passed",
                "--json",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)["record"]
    assert recorded["matrix_id"] == "repo-codex"
    assert recorded["harnesses"] == ["codex"]

    assert cli.main(["release", "smoke", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["receipt_count"] == 1

    assert cli.main(["release", "smoke", "show", recorded["receipt_id"], "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["receipt"]["receipt_id"] == recorded["receipt_id"]

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "depth": "workspace",
                "harnesses": ["claude", "codex", "openclaw"],
                "status": "passed",
                "command_label": "brigade init --depth workspace --harnesses claude,codex,openclaw",
                "safe_summary": "workspace smoke passed",
            }
        )
    )
    assert (
        cli.main(
            ["release", "smoke", "record", "--target", str(tmp_path), "--receipt-json", str(receipt_path), "--json"]
        )
        == 0
    )
    parsed = json.loads(capsys.readouterr().out)["record"]
    assert parsed["matrix_id"] == "workspace-claude-codex-openclaw"


def test_release_smoke_health_stale_candidate_and_center_activity(tmp_path, capsys):
    stale_time = (release_cmd._now() - timedelta(days=10)).isoformat()
    assert (
        release_cmd.install_smoke_record(
            target=tmp_path,
            depth="repo",
            harnesses="codex",
            status="passed",
            summary="old smoke",
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)["record"]
    receipts_path = tmp_path / ".brigade" / "release" / "install-smoke" / "receipts.jsonl"
    receipt["created_at"] = stale_time
    receipt["completed_at"] = stale_time
    receipts_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")

    assert cli.main(["release", "smoke", "doctor", "--target", str(tmp_path), "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in doctor["issues"]}
    assert "install_smoke_stale" in names
    assert "install_smoke_missing" in names

    assert cli.main(["release", "candidate", "plan", "--target", str(tmp_path), "--base-ref", "", "--json"]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["install_smoke"]["issue_count"] >= 1

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    activity = json.loads(capsys.readouterr().out)
    assert any(
        item["subsystem"] == "install-smoke" and item["local_id"] == receipt["receipt_id"]
        for item in activity["activity"]
    )
