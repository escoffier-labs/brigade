import json

from brigade import cli, release_cmd, work_cmd


def test_release_ci_doctor_detects_workflow_and_summary_deprecations(tmp_path, capsys):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "\n".join(
            [
                "name: ci",
                "jobs:",
                "  test:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - uses: actions/upload-artifact@v3",
            ]
        )
        + "\n"
    )
    summary = tmp_path / ".brigade" / "ci" / "github-actions-summary.txt"
    summary.parent.mkdir(parents=True)
    summary.write_text(
        "Warning: Node.js 16 actions are deprecated for actions/cache@v3 token=abcd1234abcd1234 /home/private/repo\n"
    )

    assert cli.main(["release", "ci", "doctor", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 2
    titles = {item["title"] for item in payload["findings"]}
    assert "GitHub Actions platform deprecation warning" in titles
    assert "GitHub Actions action may use a deprecated Node runtime" in titles
    rendered = json.dumps(payload)
    assert "abcd1234" not in rendered
    assert "/home/private" not in rendered
    assert "[REDACTED]" in rendered or "[redacted" in rendered


def test_release_ci_imports_dedupe_and_release_evidence(tmp_path, capsys):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "publish.yml").write_text(
        "\n".join(
            [
                "name: publish",
                "jobs:",
                "  package:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - uses: actions/setup-node@v3",
            ]
        )
        + "\n"
    )

    assert cli.main(["release", "ci", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["created"] == 1
    records = work_cmd._read_imports(tmp_path)
    assert records[0]["source"] == "ci-platform-deprecation"
    assert records[0]["metadata"]["action"] == "actions/setup-node@v3"

    assert release_cmd.ci_import_issues(target=tmp_path, json_output=True) == 0
    deduped = json.loads(capsys.readouterr().out)
    assert deduped["skipped"] == 1

    assert cli.main(["release", "plan", "--target", str(tmp_path), "--base-ref", "", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["evidence"]["ci_platform"]["issue_count"] == 1
    assert any("ci platform deprecation" in item for item in plan["warnings"])

    assert cli.main(["release", "candidate", "plan", "--target", str(tmp_path), "--base-ref", "", "--json"]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["ci_platform"]["issue_count"] == 1
