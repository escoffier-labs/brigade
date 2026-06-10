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
    (path / "AGENTS.md").write_text("private guidance that must not be copied\n")
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("changelog\n")
    (path / "ROADMAP.md").write_text("roadmap\n")
    (path / "tests").mkdir()
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_workspace(path: Path, repos: list[tuple[str, str, Path]]):
    lines: list[str] = []
    for repo_id, label, repo in repos:
        lines.extend(
            [
                "[[repo]]",
                f'id = "{repo_id}"',
                f'label = "{label}"',
                f'path = "{repo.relative_to(path)}"',
                "enabled = true",
                "expect_brigade = true",
                "",
            ]
        )
    config = path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines))


def _seed_operator_report(repo: Path, report_id: str = "operator-one"):
    _write_json(
        repo / ".brigade" / "center" / "reports" / report_id / "CENTER_EVIDENCE.json",
        {
            "report_id": report_id,
            "status": "ready",
            "created_at": "2026-05-30T01:00:00+00:00",
            "report_fingerprint": f"fp-{report_id}",
        },
    )


def _seed_release_readiness(repo: Path, run_id: str = "release-one", status: str = "ready"):
    _write_json(
        repo / ".brigade" / "release" / "runs" / run_id / "receipt.json",
        {
            "run_id": run_id,
            "status": status,
            "ready": status == "ready",
            "started_at": "2026-05-30T02:00:00+00:00",
            "source_fingerprint": f"fp-{run_id}",
        },
    )


def _seed_release_candidate(repo: Path, candidate_id: str = "candidate-one", status: str = "ready"):
    _write_json(
        repo / ".brigade" / "release" / "candidates" / candidate_id / "EVIDENCE.json",
        {
            "candidate_id": candidate_id,
            "status": status,
            "ready": status in {"ready", "reviewed"},
            "created_at": "2026-05-30T02:10:00+00:00",
            "source_fingerprint": f"fp-{candidate_id}",
        },
    )


def _seed_work_evidence(repo: Path):
    _write_json(
        repo / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "status": "ready",
            "created_at": "2026-05-30T01:30:00+00:00",
            "source_fingerprint": "fp-closeout",
        },
    )
    _write_json(
        repo / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-30T01:40:00+00:00",
            "source_fingerprint": "fp-verify",
        },
    )


def _action(
    action_id: str,
    repo_id: str,
    label: str,
    *,
    status: str = "pending",
    resolution: str | None = None,
    dispatched: bool = False,
) -> dict:
    payload = {
        "fleet_action_id": action_id,
        "repo_id": repo_id,
        "repo_label": label,
        "source_report_id": "fleet-report-one",
        "source_sweep_id": "fleet-sweep-one",
        "source_subsystem": "repo-fleet",
        "source_local_id": action_id,
        "status": status,
        "priority": "normal",
        "safe_summary": f"Resolve {repo_id} release blocker",
        "suggested_command": f"brigade repos show {repo_id}",
        "created_at": "2026-05-30T01:00:00+00:00",
        "updated_at": "2026-05-30T01:00:00+00:00",
        "source_fingerprint": f"fp-{action_id}",
    }
    if resolution:
        payload["resolution_status"] = resolution
    if dispatched:
        payload["dispatch"] = {
            "target_import_id": f"import-{action_id}",
            "source_fingerprint": payload["source_fingerprint"],
        }
    return payload


def _patch_quiet_health(monkeypatch):
    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {
            "config_path": str(target / ".brigade" / "security.toml"),
            "valid": True,
            "issue_count": 0,
            "top_issue": None,
            "top_finding": None,
            "checks": [],
            "evidence": {"ready": True},
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


def test_repos_release_plan_build_list_show_closeout_archive_and_privacy(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "private" / "actual-repo-name"
    _init_repo(repo)
    _seed_workspace(tmp_path, [("alpha", "service alpha", repo)])
    _seed_operator_report(repo)
    _seed_work_evidence(repo)
    _seed_release_readiness(repo)
    _seed_release_candidate(repo)
    _patch_quiet_health(monkeypatch)

    assert repos_cmd.release_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["repos"][0]["classification"] == "ready"
    assert "actual-repo-name" not in json.dumps(plan)
    assert "private guidance that must not be copied" not in json.dumps(plan)
    assert plan["release_train_root_label"] == ".brigade/repos/releases"

    assert cli.main(["repos", "release", "build", "--target", str(tmp_path), "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["status"] == "ready"
    assert "path" not in built
    train_path = tmp_path / ".brigade" / "repos" / "releases" / built["train_id"]
    assert (train_path / "FLEET_RELEASE_TRAIN.md").is_file()
    assert (train_path / "FLEET_RELEASE_EVIDENCE.json").is_file()
    publish_plan = (train_path / "MANUAL_PUBLISH_PLAN.md").read_text()
    assert "Manual-only remote steps" in publish_plan
    assert "brigade release doctor" in publish_plan
    assert "brigade release candidate compare latest" in publish_plan
    assert "actual-repo-name" not in (train_path / "FLEET_RELEASE_EVIDENCE.json").read_text()

    assert repos_cmd.release_list(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["train_count"] == 1
    assert listed["trains"][0]["path_label"] == built["train_id"]
    assert repos_cmd.release_show(target=tmp_path, train_id=built["train_id"], json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["train"]["train_id"] == built["train_id"]

    assert (
        repos_cmd.release_closeout(
            target=tmp_path, train_id=built["train_id"], status="reviewed", reason="ready", json_output=True
        )
        == 0
    )
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "reviewed"
    assert (train_path / "CLOSEOUT.json").is_file()
    assert repos_cmd.release_archive(target=tmp_path, train_id=built["train_id"], json_output=True) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["archive_path_label"] == built["train_id"]
    assert (tmp_path / ".brigade" / "repos" / "releases" / "archive" / built["train_id"]).is_dir()


def test_repos_release_classifies_required_repo_states(tmp_path, monkeypatch, capsys):
    specs = [
        ("ready", "service ready"),
        ("blocked", "service blocked"),
        ("needs_review", "service needs review"),
        ("needs_dispatch", "service needs dispatch"),
        ("in_progress", "service in progress"),
        ("stale", "service stale"),
        ("no_candidate", "service no candidate"),
        ("deferred", "service deferred"),
    ]
    repos = []
    for repo_id, label in specs:
        repo = tmp_path / f"repo-{repo_id}"
        _init_repo(repo)
        _seed_operator_report(repo)
        _seed_work_evidence(repo)
        _seed_release_readiness(repo)
        _seed_release_candidate(repo)
        repos.append((repo_id, label, repo))
    _seed_workspace(tmp_path, repos)
    _patch_quiet_health(monkeypatch)

    (tmp_path / "repo-blocked" / "README.md").write_text("dirty\n")
    _seed_release_candidate(tmp_path / "repo-needs_review", status="draft")
    repos_cmd._write_actions(
        tmp_path,
        [
            _action("act-needs-dispatch", "needs_dispatch", "service needs dispatch"),
            _action("act-in-progress", "in_progress", "service in progress", resolution="dispatched", dispatched=True),
            _action("act-deferred", "deferred", "service deferred", status="deferred"),
        ],
    )
    (tmp_path / "repo-stale" / ".brigade" / "center" / "reports" / "operator-one" / "CENTER_EVIDENCE.json").unlink()
    candidate_dir = tmp_path / "repo-no_candidate" / ".brigade" / "release" / "candidates" / "candidate-one"
    for child in candidate_dir.iterdir():
        child.unlink()
    candidate_dir.rmdir()

    assert repos_cmd.release_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    classes = {repo["repo_id"]: repo["classification"] for repo in plan["repos"]}
    assert classes == {
        "ready": "ready",
        "blocked": "blocked",
        "needs_review": "needs-review",
        "needs_dispatch": "needs-dispatch",
        "in_progress": "in-progress",
        "stale": "stale-evidence",
        "no_candidate": "no-release-candidate",
        "deferred": "deferred",
    }


def test_repos_release_compare_detects_stale_train_evidence(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    (repo / ".claude" / "memory-handoffs").mkdir(parents=True)
    _seed_workspace(tmp_path, [("alpha", "service alpha", repo)])
    _seed_operator_report(repo)
    _seed_work_evidence(repo)
    _seed_release_readiness(repo, "release-one")
    _seed_release_candidate(repo, "candidate-one")
    repos_cmd._write_actions(
        tmp_path, [_action("act-alpha", "alpha", "service alpha", resolution="dispatched", dispatched=True)]
    )
    _patch_quiet_health(monkeypatch)

    assert repos_cmd.release_build(target=tmp_path, json_output=True) == 0
    train = json.loads(capsys.readouterr().out)
    (repo / "README.md").write_text("changed head\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "docs: update readme"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    _seed_release_readiness(repo, "release-two")
    _write_json(
        repo / ".brigade" / "release" / "runs" / "release-two" / "receipt.json",
        {
            "run_id": "release-two",
            "status": "ready",
            "ready": True,
            "started_at": "2026-05-30T03:00:00+00:00",
            "source_fingerprint": "fp-release-two",
        },
    )
    _seed_release_candidate(repo, "candidate-two")
    _write_json(
        repo / ".brigade" / "release" / "candidates" / "candidate-two" / "EVIDENCE.json",
        {
            "candidate_id": "candidate-two",
            "status": "ready",
            "ready": True,
            "created_at": "2026-05-30T03:10:00+00:00",
            "source_fingerprint": "fp-candidate-two",
        },
    )
    actions = repos_cmd._read_actions(tmp_path)
    actions[0]["resolution_status"] = "completed"
    actions[0]["status"] = "done"
    repos_cmd._write_actions(tmp_path, actions)

    assert repos_cmd.release_compare(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    compare = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in compare["issues"]}
    assert {
        "train_repo_head_changed",
        "newer_release_readiness",
        "newer_release_candidate",
        "train_fleet_actions_changed",
        "train_unresolved_state_changed",
    } <= names

    train_file = tmp_path / ".brigade" / "repos" / "releases" / train["train_id"] / "FLEET_RELEASE_EVIDENCE.json"
    stored = json.loads(train_file.read_text())
    stored["repos"][0]["evidence"]["latest_release_candidate"]["id"] = "missing-candidate"
    train_file.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
    for child in (repo / ".brigade" / "release" / "candidates").iterdir():
        for item in child.iterdir():
            item.unlink()
        child.rmdir()
    assert repos_cmd.release_compare(target=tmp_path, train_id=train["train_id"], json_output=True) == 0
    missing = json.loads(capsys.readouterr().out)
    assert any(issue["name"] == "train_missing_receipt" for issue in missing["issues"])


def test_repos_release_train_health_integrates_with_daily_surfaces(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_work_evidence(tmp_path)
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    (repo / ".claude" / "memory-handoffs").mkdir(parents=True)
    _seed_workspace(tmp_path, [("alpha", "service alpha", repo)])
    _seed_operator_report(repo)
    _seed_work_evidence(repo)
    _seed_release_readiness(repo, status="blocked")
    _seed_release_candidate(repo)
    _patch_quiet_health(monkeypatch)

    assert repos_cmd.release_build(target=tmp_path, json_output=True) == 0
    train = json.loads(capsys.readouterr().out)
    assert train["status"] == "blocked"
    health = repos_cmd.health(tmp_path)
    assert any(check["name"] == "repo_fleet_release_train_blocked" for check in health["release_train"]["checks"])
    assert repos_cmd.doctor(target=tmp_path) == 0
    assert "repo_fleet_release_train_blocked" in capsys.readouterr().out

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center = json.loads(capsys.readouterr().out)
    assert center["repo_fleet"]["release_train"]["latest"]["train_id"] == train["train_id"]
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(
        item["subsystem"] == "repo-fleet" and item["local_id"] == "repo_fleet_release_train_blocked"
        for item in reviews["reviews"]
    )

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["repo_fleet"]["release_train"]["latest"]["train_id"] == train["train_id"]
    assert work_cmd.doctor(target=tmp_path) == 1
    assert "repo_fleet_release_train_blocked" in capsys.readouterr().out
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 1
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["repo_fleet"]["release_train"]["latest"]["train_id"] == train["train_id"]
    assert any("repo fleet release train" in warning for warning in release["warnings"])
