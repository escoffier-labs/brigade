import json

from brigade import cli
from brigade import repos_cmd
from brigade import work_cmd

from tests.test_phase44_cmd import _build_train, _init_repo, _patch_quiet, _seed_operator_report, _seed_release_ready, _seed_workspace
from tests.test_phase45_cmd import _record_required_evidence


def _ready_train(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo-ready"
    _init_repo(repo)
    _seed_workspace(tmp_path, [("ready", "service ready", repo)])
    _seed_operator_report(repo)
    _seed_release_ready(repo)
    _patch_quiet(monkeypatch)
    assert repos_cmd.release_build(target=tmp_path, json_output=True) == 0
    train = json.loads(capsys.readouterr().out)
    assert train["status"] == "ready"
    return train


def test_release_report_checklist_and_hygiene(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    assert cli.main(["repos", "release", "report", train["train_id"], "--target", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    train_dir = tmp_path / ".brigade" / "repos" / "releases" / train["train_id"]
    assert report["report"]["bundle_files"] == ["RELEASE_TRAIN_REPORT.md", "RELEASE_TRAIN_REPORT.json"]
    assert (train_dir / "RELEASE_TRAIN_REPORT.md").is_file()
    assert (train_dir / "RELEASE_TRAIN_REPORT.json").is_file()
    assert "actual-repo-name" not in (train_dir / "RELEASE_TRAIN_REPORT.json").read_text()

    assert repos_cmd.release_checklist(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    checklist = json.loads(capsys.readouterr().out)
    assert checklist["item_count"] == 12
    assert {item["status"] for item in checklist["items"]} == {"missing"}

    assert repos_cmd.release_hygiene(target=tmp_path, json_output=True) == 0
    hygiene = json.loads(capsys.readouterr().out)
    assert hygiene["issue_count"] == 0


def test_release_import_issues_dedupe_and_dismissed_until_changed(tmp_path, monkeypatch, capsys):
    train = _build_train(tmp_path, monkeypatch, capsys)
    assert repos_cmd.release_closeout(target=tmp_path, train_id=train["train_id"], status="reviewed", json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_reconcile(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    capsys.readouterr()

    assert cli.main(["repos", "release", "import-issues", train["train_id"], "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["created"] == dry["issue_count"] == 2
    assert work_cmd._read_imports(tmp_path) == []

    assert repos_cmd.release_import_issues(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["created"] == 2
    assert work_cmd._read_imports(tmp_path)[0]["source"] == "repo-fleet-release"
    assert repos_cmd.release_import_issues(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    deduped = json.loads(capsys.readouterr().out)
    assert deduped["skipped"] == 2

    imports = work_cmd._read_imports(tmp_path)
    imports[0]["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, imports)
    assert repos_cmd.release_import_issues(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    dismissed = json.loads(capsys.readouterr().out)
    assert dismissed["dismissed"] == 1
    assert dismissed["skipped"] == 1


def test_release_ready_gate_blocks_and_passes(tmp_path, monkeypatch, capsys):
    blocked = _build_train(tmp_path, monkeypatch, capsys)
    assert repos_cmd.release_closeout(target=tmp_path, train_id=blocked["train_id"], status="reviewed", json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_actions_build(target=tmp_path, train_id=blocked["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_ready(target=tmp_path, train_id=blocked["train_id"], json_output=True) == 1
    not_ready = json.loads(capsys.readouterr().out)
    assert not_ready["ready"] is False
    assert "train has unresolved actions" in not_ready["blockers"]

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    ready = _ready_train(clean_root, monkeypatch, capsys)
    assert repos_cmd.release_ready(target=clean_root, train_id=ready["train_id"], json_output=True) == 1
    missing = json.loads(capsys.readouterr().out)
    assert "train has missing manual evidence" in missing["blockers"]
    _record_required_evidence(clean_root, ready["train_id"], "ready")
    capsys.readouterr()
    assert repos_cmd.release_reconcile(target=clean_root, train_id=ready["train_id"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.release_ready(target=clean_root, train_id=ready["train_id"], json_output=True) == 0
    ready_payload = json.loads(capsys.readouterr().out)
    assert ready_payload["ready"] is True
