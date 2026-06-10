import json
from datetime import timedelta

from brigade import cli
from brigade import repos_cmd
from brigade import work_cmd

from tests.test_phase44_cmd import _build_train


def _iso_after(days: int) -> str:
    return (repos_cmd._now() + timedelta(days=days)).isoformat()


def test_expired_waivers_do_not_satisfy_ready_until_renewed(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    future = _iso_after(1)
    past = _iso_after(-1)
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="blocked-repo",
            reason="reviewed",
            expires_at=future,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="unresolved-action",
            reason="reviewed",
            expires_at=future,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="missing-evidence",
            reason="expired review",
            expires_at=past,
            json_output=True,
        )
        == 0
    )
    expired = json.loads(capsys.readouterr().out)["waiver"]

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 1
    not_ready = json.loads(capsys.readouterr().out)
    assert "train has missing manual evidence" in not_ready["blockers"]
    assert {item["scope"] for item in not_ready["waived"]} == {"blocked-repo", "unresolved-action"}
    assert any(issue["name"] == "release_waiver_expired" for issue in not_ready["waiver_issues"])

    assert (
        cli.main(
            [
                "repos",
                "release",
                "waivers",
                "renew",
                expired["waiver_id"],
                "--reason",
                "reviewed again",
                "--expires-at",
                future,
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    renewed = json.loads(capsys.readouterr().out)
    assert renewed["waiver"]["status"] == "active"
    assert renewed["waiver"]["expires_at"] == future

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["ready"] is True
    assert {item["scope"] for item in ready["waived"]} == {"blocked-repo", "unresolved-action", "missing-evidence"}


def test_waiver_doctor_and_import_issues_dedupe(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="blocked-repo",
            reason="temporary review",
            json_output=True,
        )
        == 0
    )
    waiver = json.loads(capsys.readouterr().out)["waiver"]
    waiver_path = tmp_path / ".brigade" / "repos" / "releases" / "waivers.jsonl"
    waiver["created_at"] = (repos_cmd._now() - timedelta(days=10)).isoformat()
    waiver["updated_at"] = waiver["created_at"]
    waiver_path.write_text(json.dumps(waiver, sort_keys=True) + "\n")

    assert (
        cli.main(["repos", "release", "waivers", "doctor", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    doctor = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in doctor["issues"]}
    assert {"release_waiver_missing_expiry", "release_waiver_stale_review", "release_waiver_missing_owner"} <= names

    assert (
        cli.main(
            ["repos", "release", "waivers", "import-issues", train["train_id"], "--target", str(tmp_path), "--json"]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["created"] == 3
    assert {item["source"] for item in work_cmd._read_imports(tmp_path)} == {"repo-fleet-release-waiver"}

    assert repos_cmd.release_waiver_import_issues(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    deduped = json.loads(capsys.readouterr().out)
    assert deduped["skipped"] == 3


def test_release_audit_and_ready_include_waiver_health(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="blocked-repo",
            reason="temporary review",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert cli.main(["repos", "release", "audit", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["waiver_issue_count"] >= 1
    assert "release_waiver_missing_expiry" in {issue["name"] for issue in audit["issues"]}

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 1
    ready = json.loads(capsys.readouterr().out)
    assert ready["waiver_issue_count"] >= 1
    assert "release_waiver_missing_expiry" in {issue["name"] for issue in ready["waiver_issues"]}
    assert ready["waiver_policy"]["requires_owner_label"] is True


def test_release_waiver_policy_templates_owner_reason_and_scope_checks(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    future = _iso_after(2)

    assert cli.main(["repos", "release", "waivers", "templates", "--json"]) == 0
    templates = json.loads(capsys.readouterr().out)
    assert templates["template_count"] == len(repos_cmd.RELEASE_WAIVER_SCOPES)
    assert all(item["requires_owner_label"] for item in templates["templates"])
    assert all("--owner-label <label>" in item["suggested_command"] for item in templates["templates"])

    assert (
        cli.main(
            [
                "repos",
                "release",
                "waivers",
                "record",
                train["train_id"],
                "--scope",
                "blocked-repo",
                "--reason",
                "ok",
                "--expires-at",
                future,
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    waiver = json.loads(capsys.readouterr().out)["waiver"]
    assert waiver["owner_label"] == ""

    assert (
        cli.main(["repos", "release", "waivers", "doctor", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    doctor = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in doctor["issues"]}
    assert {"release_waiver_reason_too_short", "release_waiver_reason_generic", "release_waiver_missing_owner"} <= names

    assert (
        cli.main(
            [
                "repos",
                "release",
                "waivers",
                "renew",
                waiver["waiver_id"],
                "--reason",
                "reviewed blocker with current train context",
                "--expires-at",
                future,
                "--owner-label",
                "release-review",
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    renewed = json.loads(capsys.readouterr().out)["waiver"]
    assert renewed["owner_label"] == "release-review"

    assert (
        cli.main(["repos", "release", "waivers", "doctor", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    doctor_after = json.loads(capsys.readouterr().out)
    names_after = {issue["name"] for issue in doctor_after["issues"]}
    assert "release_waiver_reason_too_short" not in names_after
    assert "release_waiver_missing_owner" not in names_after

    waiver_path = tmp_path / ".brigade" / "repos" / "releases" / "waivers.jsonl"
    invalid = dict(renewed)
    invalid["waiver_id"] = "train-waiver-invalid-scope"
    invalid["scope"] = "unknown-scope"
    invalid["repo_id"] = "repo-not-in-train"
    waiver_path.write_text(waiver_path.read_text() + json.dumps(invalid, sort_keys=True) + "\n")

    assert (
        cli.main(["repos", "release", "waivers", "doctor", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    doctor_invalid = json.loads(capsys.readouterr().out)
    invalid_names = {issue["name"] for issue in doctor_invalid["issues"]}
    assert {"release_waiver_invalid_scope", "release_waiver_repo_missing"} <= invalid_names

    assert (
        repos_cmd.release_waiver_import_issues(
            target=tmp_path, train_id=train["train_id"], dry_run=True, json_output=True
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["issue_count"] >= 2

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 1
    ready = json.loads(capsys.readouterr().out)
    waived = {item["scope"]: item for item in ready["waived"]}
    assert waived["blocked-repo"]["owner_label"] == "release-review"
    assert "release_waiver_invalid_scope" in {issue["name"] for issue in ready["waiver_issues"]}
