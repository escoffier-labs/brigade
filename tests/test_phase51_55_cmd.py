import json

from brigade import cli
from brigade import repos_cmd

from tests.test_phase44_cmd import _build_train


def test_release_waivers_record_list_show_revoke_and_ready_gate(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 1
    not_ready = json.loads(capsys.readouterr().out)
    assert "train has blocked repos" in not_ready["blockers"]
    assert "train has unresolved actions" in not_ready["blockers"]
    assert "train has missing manual evidence" in not_ready["blockers"]

    for scope in ("blocked-repo", "unresolved-action", "missing-evidence"):
        assert (
            cli.main(
                [
                    "repos",
                    "release",
                    "waivers",
                    "record",
                    train["train_id"],
                    "--scope",
                    scope,
                    "--reason",
                    f"{scope} reviewed",
                    "--target",
                    str(tmp_path),
                    "--json",
                ]
            )
            == 0
        )
        waiver = json.loads(capsys.readouterr().out)["waiver"]
    assert waiver["status"] == "active"
    assert waiver["scope"] == "missing-evidence"

    assert (
        cli.main(["repos", "release", "waivers", "list", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    waivers = json.loads(capsys.readouterr().out)
    assert waivers["waiver_count"] == 3
    assert {item["scope"] for item in waivers["waivers"]} == {"blocked-repo", "unresolved-action", "missing-evidence"}

    assert (
        cli.main(["repos", "release", "waivers", "show", waiver["waiver_id"], "--target", str(tmp_path), "--json"]) == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["waiver"]["waiver_id"] == waiver["waiver_id"]

    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["ready"] is True
    assert ready["blockers"] == []
    assert {item["scope"] for item in ready["waived"]} == {"blocked-repo", "unresolved-action", "missing-evidence"}

    assert (
        cli.main(
            [
                "repos",
                "release",
                "waivers",
                "revoke",
                waiver["waiver_id"],
                "--reason",
                "missing evidence must be refreshed",
                "--target",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["waiver"]["status"] == "revoked"
    assert repos_cmd.release_ready(target=tmp_path, train_id=train["train_id"], json_output=True) == 1
    not_ready_again = json.loads(capsys.readouterr().out)
    assert "train has missing manual evidence" in not_ready_again["blockers"]


def test_release_manifest_and_activity_include_safe_receipt_events(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(
            target=tmp_path, train_id=train["train_id"], status="reviewed", reason="reviewed train", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert (
        repos_cmd.release_evidence_record(
            target=tmp_path,
            train_id=train["train_id"],
            repo_id="blocked",
            step="verification",
            status="blocked",
            summary="verification blocked",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        repos_cmd.release_waiver_record(
            target=tmp_path,
            train_id=train["train_id"],
            scope="blocked-evidence",
            reason="accepted local risk",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_report(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    assert cli.main(["repos", "release", "manifest", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    manifest_payload = json.loads(capsys.readouterr().out)
    manifest = manifest_payload["manifest"]
    assert manifest["train_id"] == train["train_id"]
    assert {item["path_label"] for item in manifest["files"]} >= {
        "FLEET_RELEASE_EVIDENCE.json",
        "RELEASE_TRAIN_MANIFEST.json",
    }
    assert all(not str(item.get("path_label")).startswith(str(tmp_path)) for item in manifest["files"])
    train_dir = tmp_path / ".brigade" / "repos" / "releases" / train["train_id"]
    assert (train_dir / "RELEASE_TRAIN_MANIFEST.json").is_file()

    assert cli.main(["repos", "release", "activity", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    activity = json.loads(capsys.readouterr().out)
    event_types = {event["event_type"] for event in activity["events"]}
    assert {"train", "closeout", "action", "evidence", "waiver", "report", "manifest"} <= event_types
    assert all("actual-repo-name" not in json.dumps(event) for event in activity["events"])


def test_release_audit_reports_missing_bundle_and_evidence_then_improves(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert (
        repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    assert cli.main(["repos", "release", "audit", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    audit = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in audit["issues"]}
    assert "release_train_bundle_file_missing" in names
    assert "release_train_open_actions" in names
    assert "release_train_missing_evidence" in names
    assert "release_train_blocked_repos" in names

    assert repos_cmd.release_report(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_matrix(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_manifest(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_audit(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    after = json.loads(capsys.readouterr().out)
    after_names = {issue["name"] for issue in after["issues"]}
    assert "release_train_bundle_file_missing" not in after_names
    assert "release_train_missing_evidence" in after_names
