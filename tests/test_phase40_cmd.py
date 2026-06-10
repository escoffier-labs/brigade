import json
import subprocess
from pathlib import Path

from brigade import center_cmd
from brigade import cli
from brigade import handoff_cmd
from brigade import release_cmd
from brigade import repos_cmd
from brigade import security_cmd
from brigade import work_cmd


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "AGENTS.md").write_text("local guidance\n")
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("changelog\n")
    (path / "ROADMAP.md").write_text("roadmap\n")
    (path / "tests").mkdir()
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_workspace(path: Path, repo: Path):
    config = path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""
[[repo]]
id = "alpha"
label = "service alpha"
path = "{repo.relative_to(path)}"
enabled = true
expect_brigade = true
"""
    )


def _seed_repo_state(repo: Path, capsys):
    inbox = repo / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps(
            {"id": "import-one", "text": "Fix local issue", "kind": "task", "source": "scanner", "status": "pending"},
            sort_keys=True,
        )
        + "\n"
    )
    assert center_cmd.report_build(target=repo, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        center_cmd.report_closeout(target=repo, report_id=report["report_id"], status="reviewed", json_output=True) == 0
    )
    capsys.readouterr()
    assert center_cmd.actions_build(target=repo, report_id=report["report_id"], json_output=True) == 0
    capsys.readouterr()
    _write_json(
        repo / ".brigade" / "release" / "runs" / "release-one" / "release.json",
        {
            "run_id": "release-one",
            "status": "ready",
            "ready": True,
            "created_at": "2026-05-30T01:00:00+00:00",
            "path": str(repo / ".brigade" / "release" / "runs" / "release-one"),
        },
    )
    _write_json(
        repo / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "status": "ready",
            "ready": True,
            "created_at": "2026-05-30T01:00:00+00:00",
            "path": str(repo / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json"),
        },
    )
    (repo / "README.md").write_text("changed\n")


def _seed_release_prereqs(path: Path):
    _write_json(
        path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-30T01:00:00+00:00",
            "completed_at": "2026-05-30T01:00:10+00:00",
            "path": str(path / ".brigade" / "work" / "verify-runs" / "verify-one"),
        },
    )
    _write_json(
        path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "status": "ready",
            "ready": True,
            "created_at": "2026-05-30T01:01:00+00:00",
            "path": str(path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json"),
        },
    )


def _patch_release_health(monkeypatch):
    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {
            "config_path": str(target / ".brigade" / "security.toml"),
            "valid": True,
            "issue_count": 0,
            "top_issue": None,
            "top_finding": None,
            "evidence": {"ready": True, "finding_count": 0},
            "checks": [],
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target, **kwargs: {
            "counts": {"pending": 0},
            "issue_count": 0,
            "top_issue": None,
            "latest_ingest_run": None,
            "drafts": [],
            "checks": [],
        },
    )
    monkeypatch.setattr(
        release_cmd,
        "_run_content_guard_check",
        lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"},
    )
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)


def test_repos_report_plan_build_list_show_archive_and_safe_labels(tmp_path, capsys):
    repo = tmp_path / "private" / "actual-repo-name"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    _seed_repo_state(repo, capsys)

    assert repos_cmd.report_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["repos"][0]["repo_id"] == "alpha"
    assert plan["repos"][0]["repo_label"] == "service alpha"
    assert "actual-repo-name" not in json.dumps(plan)
    assert plan["repos"][0]["dirty_tracked_count"] == 1
    assert plan["repos"][0]["action_queue"]["open_count"] >= 1

    assert repos_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert (Path(report["path"]) / "FLEET_REPORT.md").is_file()
    assert (Path(report["path"]) / "FLEET_EVIDENCE.json").is_file()
    assert repos_cmd.report_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["report_count"] == 1
    assert repos_cmd.report_show(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["report"]["report_id"] == report["report_id"]
    assert cli.main(["repos", "report", "show", report["report_id"], "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["report"]["report_id"] == report["report_id"]
    assert repos_cmd.report_archive(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"


def test_repos_actions_review_gate_dedupe_transitions_and_archive(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    _seed_repo_state(repo, capsys)
    assert repos_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert repos_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 2
    capsys.readouterr()
    assert (
        repos_cmd.actions_build(target=tmp_path, report_id=report["report_id"], allow_unreviewed=True, json_output=True)
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["created_count"] >= 1
    action_id = first["created_actions"][0]["fleet_action_id"]
    assert (
        repos_cmd.actions_build(target=tmp_path, report_id=report["report_id"], allow_unreviewed=True, json_output=True)
        == 0
    )
    assert json.loads(capsys.readouterr().out)["created_count"] == 0

    assert repos_cmd.actions_start(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "active"
    assert repos_cmd.actions_done(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "done"
    assert repos_cmd.actions_archive_completed(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["archived_count"] == 1
    assert (
        repos_cmd.actions_build(target=tmp_path, report_id=report["report_id"], allow_unreviewed=True, json_output=True)
        == 0
    )
    assert json.loads(capsys.readouterr().out)["created_count"] == 0

    remaining = repos_cmd._read_actions(tmp_path)
    if remaining:
        defer_id = remaining[0]["fleet_action_id"]
        assert repos_cmd.actions_defer(target=tmp_path, action_id=defer_id, reason="later", json_output=True) == 0
        assert json.loads(capsys.readouterr().out)["action"]["defer_reason"] == "later"
        assert cli.main(["repos", "actions", "show", defer_id, "--target", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["action"]["fleet_action_id"] == defer_id


def test_repos_fleet_integrates_with_center_work_and_release(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_release_prereqs(tmp_path)
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    _seed_repo_state(repo, capsys)
    _patch_release_health(monkeypatch)
    assert repos_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        repos_cmd.report_closeout(target=tmp_path, report_id=report["report_id"], status="reviewed", json_output=True)
        == 0
    )
    capsys.readouterr()
    assert repos_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    capsys.readouterr()

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["repo_fleet"]["actions"]["open_count"] >= 1
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "repo-fleet" for item in reviews["reviews"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["repo_fleet"]["actions"]["open_count"] >= 1
    assert work_cmd.doctor(target=tmp_path) == 1
    assert "repo_fleet_actions_open" in capsys.readouterr().out
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["repo_fleet"]["actions"]["open_count"] >= 1
    assert any("repo fleet action queue" in warning for warning in release["warnings"])
