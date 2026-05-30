import json
import subprocess
import sys
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


def _seed_workspace(path: Path, repo_a: Path, repo_b: Path | None = None, *, disabled_b: bool = True):
    lines = [
        "[[repo]]",
        'id = "alpha"',
        'label = "service alpha"',
        f'path = "{repo_a.relative_to(path)}"',
        "enabled = true",
        "expect_brigade = true",
        "",
    ]
    if repo_b is not None:
        lines.extend(
            [
                "[[repo]]",
                'id = "beta"',
                'label = "service beta"',
                f'path = "{repo_b.relative_to(path)}"',
                f"enabled = {'false' if disabled_b else 'true'}",
                "expect_brigade = true",
                "",
            ]
        )
    config = path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines))


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
        lambda target, **kwargs: {"counts": {"pending": 0}, "issue_count": 0, "top_issue": None, "latest_ingest_run": None, "drafts": [], "checks": []},
    )
    monkeypatch.setattr(release_cmd, "_run_content_guard_check", lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"})
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)


def _seed_release_prereqs(path: Path):
    _write_json(
        path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {"run_id": "verify-one", "status": "completed", "started_at": "2026-05-30T01:00:00+00:00", "completed_at": "2026-05-30T01:00:10+00:00"},
    )
    _write_json(
        path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {"closeout_id": "closeout-one", "status": "ready", "ready": True, "created_at": "2026-05-30T01:01:00+00:00"},
    )


def test_repos_sweep_plan_filters_disabled_stale_and_cli(tmp_path, capsys):
    repo_a = tmp_path / "private" / "repo-alpha"
    repo_b = tmp_path / "private" / "repo-beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    _seed_workspace(tmp_path, repo_a, repo_b)

    assert repos_cmd.sweep_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [repo["repo_id"] for repo in plan["repos"]] == ["alpha"]
    assert plan["repos"][0]["commands"][0]["label"] == "center-report-build"
    assert "repo-alpha" not in json.dumps(plan)
    assert repos_cmd.sweep_plan(target=tmp_path) == 0
    assert "repo fleet sweep plan" in capsys.readouterr().out

    assert repos_cmd.sweep_plan(target=tmp_path, repo_ids=["beta"], include_disabled=True, json_output=True) == 0
    beta_plan = json.loads(capsys.readouterr().out)
    assert [repo["repo_id"] for repo in beta_plan["repos"]] == ["beta"]
    assert cli.main(["repos", "sweep", "plan", "--target", str(tmp_path), "--repo", "alpha", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repo_count"] == 1

    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.sweep_plan(target=tmp_path, stale_only=True, json_output=True) == 0
    stale_plan = json.loads(capsys.readouterr().out)
    assert stale_plan["repo_count"] == 0
    assert repos_cmd.sweep_plan(target=tmp_path, stale_only=True, force=True, json_output=True) == 0
    forced = json.loads(capsys.readouterr().out)
    assert forced["repo_count"] == 1


def test_repos_sweep_run_writes_receipts_logs_show_closeout_and_privacy(tmp_path, capsys):
    repo = tmp_path / "private" / "actual-repo-name"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)

    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    sweep = json.loads(capsys.readouterr().out)
    assert sweep["status"] == "completed"
    assert sweep["repos"][0]["repo_id"] == "alpha"
    assert {command["label"] for command in sweep["repos"][0]["commands"]} == {"center-report-build", "release-plan", "work-brief"}
    assert all("stdout_log_label" in command for command in sweep["repos"][0]["commands"])
    assert "actual-repo-name" not in json.dumps(sweep)
    assert sweep["path_label"] == sweep["sweep_id"]
    sweep_path = tmp_path / ".brigade" / "repos" / "sweeps" / sweep["sweep_id"]
    assert (sweep_path / "sweep.json").is_file()
    assert (sweep_path / "logs" / "alpha" / "center-report-build" / "stdout.log").is_file()
    assert (repo / ".brigade" / "center" / "reports").is_dir()

    assert repos_cmd.sweep_runs(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["sweep_count"] == 1
    assert repos_cmd.sweep_show(target=tmp_path, sweep_id=sweep["sweep_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["sweep"]["sweep_id"] == sweep["sweep_id"]
    assert repos_cmd.sweep_show(target=tmp_path, sweep_id=sweep["sweep_id"]) == 0
    assert "path_label" in capsys.readouterr().out
    assert repos_cmd.sweep_closeout(target=tmp_path, sweep_id=sweep["sweep_id"], status="deferred", reason="review later", json_output=True) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "deferred"
    assert closeout["source_fingerprint"]


def test_repos_sweep_failed_repo_does_not_block_other_repo(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    missing = tmp_path / "missing-repo"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo, missing, disabled_b=False)

    assert repos_cmd.sweep_run(target=tmp_path, all_repos=True, json_output=True) == 1
    sweep = json.loads(capsys.readouterr().out)
    statuses = {repo["repo_id"]: repo["status"] for repo in sweep["repos"]}
    assert statuses["alpha"] == "completed"
    assert statuses["beta"] == "failed"
    assert sweep["failed_count"] == 1


def test_repos_sweep_records_nonzero_and_timeout_commands(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    monkeypatch.setattr(
        repos_cmd,
        "_sweep_commands",
        lambda: [
            repos_cmd.SweepCommand("nonzero", [sys.executable, "-c", "import sys; sys.exit(3)"]),
            repos_cmd.SweepCommand("timeout", [sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01),
        ],
    )

    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 1
    sweep = json.loads(capsys.readouterr().out)
    commands = {command["label"]: command for command in sweep["repos"][0]["commands"]}
    assert commands["nonzero"]["exit_code"] == 3
    assert commands["nonzero"]["status"] == "failed"
    assert commands["timeout"]["timed_out"] is True
    assert commands["timeout"]["status"] == "timeout"
    assert all("repo-alpha" not in json.dumps(command) for command in commands.values())


def test_repos_sweep_runs_configured_read_only_health_commands(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    config = tmp_path / ".brigade" / "repos.toml"
    config.write_text(
        config.read_text()
        + """
[[repo.health_command]]
label = "custom-health"
command = "python3 -c 'print(42)'"
timeout = 120
"""
    )

    assert repos_cmd.sweep_plan(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert "custom-health" in plan["command_labels"]
    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    sweep = json.loads(capsys.readouterr().out)
    commands = {command["label"]: command for command in sweep["repos"][0]["commands"]}
    assert commands["custom-health"]["status"] == "completed"
    assert commands["custom-health"]["stdout_summary"] == "42"


def test_repos_health_command_registry_labels_timeouts_receipts_and_report(tmp_path, capsys):
    repo = tmp_path / "private" / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    config = tmp_path / ".brigade" / "repos.toml"
    config.write_text(
        config.read_text()
        + """
[[repo.health_command]]
label = "custom-health"
argv = ["python3", "-c", "print(42)"]
timeout = 7
"""
    )

    assert repos_cmd.health_commands(target=tmp_path, json_output=True) == 1
    missing = json.loads(capsys.readouterr().out)
    assert missing["health_command_count"] == 1
    assert missing["repos"][0]["health_commands"][0]["label"] == "custom-health"
    assert missing["repos"][0]["health_commands"][0]["timeout"] == 7
    assert missing["repos"][0]["health_commands"][0]["receipt_status"] == "missing"
    assert missing["issues"][0]["name"] == "repo_health_command_receipt_missing"
    assert "repo-alpha" not in json.dumps(missing)

    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    sweep = json.loads(capsys.readouterr().out)
    sweep_path = tmp_path / ".brigade" / "repos" / "sweeps" / sweep["sweep_id"] / "sweep.json"
    sweep_payload = json.loads(sweep_path.read_text())
    for command in sweep_payload["repos"][0]["commands"]:
        if command["label"] == "custom-health":
            command["completed_at"] = "2000-01-01T00:00:00+00:00"
    _write_json(sweep_path, sweep_payload)

    assert repos_cmd.health_commands(target=tmp_path, json_output=True) == 1
    stale = json.loads(capsys.readouterr().out)
    command = stale["repos"][0]["health_commands"][0]
    assert command["receipt_status"] == "completed"
    assert command["latest_receipt"]["sweep_id"] == sweep["sweep_id"]
    assert command["stale"] is True
    assert any(issue["name"] == "repo_health_command_receipt_stale" for issue in stale["issues"])

    assert repos_cmd.report_plan(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["health_commands"]["health_command_count"] == 1
    assert report["health_commands"]["issue_count"] >= 1
    assert any(warning["name"] == "repo_health_command_receipt_stale" for warning in report["warnings"])


def test_repos_health_command_registry_rejects_high_risk_commands(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    config = tmp_path / ".brigade" / "repos.toml"
    config.write_text(
        config.read_text()
        + """
[[repo.health_command]]
label = "bad-health"
command = "sh -c echo"
timeout = 5
"""
    )

    assert repos_cmd.health_commands(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["health_command_count"] == 0
    assert any("high-risk" in issue["detail"] for issue in payload["issues"])
    assert cli.main(["repos", "health-commands", "--target", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["issues"]


def test_repos_sweep_integrates_with_report_center_work_and_release(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_release_prereqs(tmp_path)
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    (repo / ".claude" / "memory-handoffs").mkdir(parents=True)
    _seed_workspace(tmp_path, repo)
    _patch_release_health(monkeypatch)

    assert repos_cmd.sweep_run(target=tmp_path, repo_ids=["alpha"], json_output=True) == 0
    sweep = json.loads(capsys.readouterr().out)
    assert repos_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["latest_sweep"]["sweep_id"] == sweep["sweep_id"]
    health = repos_cmd.health(tmp_path)
    assert health["sweep"]["issue_count"] >= 1
    assert repos_cmd.doctor(target=tmp_path) == 0
    assert "repo_fleet_sweep_unclosed" in capsys.readouterr().out

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["repo_fleet"]["sweep"]["latest"]["sweep_id"] == sweep["sweep_id"]
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "repo-fleet" for item in reviews["reviews"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["repo_fleet"]["sweep"]["latest"]["sweep_id"] == sweep["sweep_id"]
    assert work_cmd.doctor(target=tmp_path) == 1
    assert "repo_fleet_sweep_unclosed" in capsys.readouterr().out
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["repo_fleet"]["sweep"]["latest"]["sweep_id"] == sweep["sweep_id"]
    assert any("repo fleet sweep" in warning for warning in release["warnings"])
