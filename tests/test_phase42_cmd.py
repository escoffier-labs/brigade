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
    (path / "AGENTS.md").write_text("local guidance that must not be copied\n")
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
        "\n".join(
            [
                "[[repo]]",
                'id = "alpha"',
                'label = "service alpha"',
                f'path = "{repo.relative_to(path)}"',
                "enabled = true",
                "expect_brigade = true",
                "",
            ]
        )
    )


def _action(
    *,
    action_id: str = "fleet-act-alpha",
    fingerprint: str = "fp-one",
    status: str = "pending",
    summary: str = "Fix fleet setup",
) -> dict:
    return {
        "fleet_action_id": action_id,
        "repo_id": "alpha",
        "repo_label": "service alpha",
        "source_report_id": "report-one",
        "source_sweep_id": "sweep-one",
        "source_subsystem": "repo-fleet",
        "source_local_id": "repo_missing_brigade_config",
        "status": status,
        "priority": "normal",
        "safe_summary": summary,
        "suggested_command": "brigade repos show alpha",
        "created_at": "2026-05-30T01:00:00+00:00",
        "updated_at": "2026-05-30T01:00:00+00:00",
        "reviewed_at": "2026-05-30T01:00:00+00:00",
        "source_fingerprint": fingerprint,
    }


def _seed_action_queue(path: Path, actions: list[dict]):
    repos_cmd._write_actions(path, actions)


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


def test_fleet_action_dispatch_plan_apply_dry_run_all_reviewed_and_privacy(tmp_path, capsys):
    repo = tmp_path / "private" / "actual-repo-name"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    private_summary = f"Fix setup in {repo}"
    _seed_action_queue(
        tmp_path,
        [
            _action(summary=private_summary),
            _action(action_id="fleet-act-beta", fingerprint="fp-two", summary="Second action"),
        ],
    )

    assert repos_cmd.actions_dispatch_plan(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plans"][0]["dispatchable"] is True
    assert "actual-repo-name" not in json.dumps(plan)

    assert (
        repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", dry_run=True, json_output=True)
        == 0
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert work_cmd._read_imports(repo) == []

    assert (
        cli.main(["repos", "actions", "dispatch", "apply", "fleet-act-alpha", "--target", str(tmp_path), "--json"]) == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["created_count"] == 1
    imports = work_cmd._read_imports(repo)
    assert len(imports) == 1
    metadata = imports[0]["metadata"]
    assert metadata["fleet_action_id"] == "fleet-act-alpha"
    assert metadata["source_report_id"] == "report-one"
    assert metadata["source_sweep_id"] == "sweep-one"
    assert imports[0]["source"] == "repo-fleet"
    assert imports[0]["kind"] == "task"
    assert imports[0]["acceptance"]
    assert "actual-repo-name" not in json.dumps(imports)
    _write_json(
        repo / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {"closeout_id": "closeout-one", "status": "ready", "created_at": "2026-05-30T02:00:00+00:00"},
    )
    assert any(
        check["name"] == "repo_fleet_action_evidence_changed" for check in repos_cmd.actions_health(tmp_path)["checks"]
    )

    assert repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    skipped = json.loads(capsys.readouterr().out)
    assert skipped["skipped_count"] == 1
    assert len(work_cmd._read_imports(repo)) == 1

    assert repos_cmd.actions_dispatch_apply(target=tmp_path, all_reviewed=True, json_output=True) == 0
    all_dispatch = json.loads(capsys.readouterr().out)
    assert all_dispatch["result_count"] == 2
    assert len(work_cmd._read_imports(repo)) == 2


def test_fleet_action_dispatch_dismissed_until_changed_and_supersede(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    action = _action()
    _seed_action_queue(tmp_path, [action])

    assert repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    capsys.readouterr()
    imports = work_cmd._read_imports(repo)
    imports[0]["status"] = "dismissed"
    imports[0]["dismiss_reason"] = "not now"
    work_cmd._write_imports(repo, imports)

    assert repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    skipped = json.loads(capsys.readouterr().out)
    assert skipped["dismissed_count"] == 1
    assert len(work_cmd._read_imports(repo)) == 1
    assert repos_cmd.actions_dispatch_report(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    dismissed_report = json.loads(capsys.readouterr().out)
    assert dismissed_report["actions"][0]["dismissed_import_count"] == 1
    assert any(
        check["name"] == "repo_fleet_dispatch_import_dismissed" for check in dismissed_report["actions"][0]["checks"]
    )

    actions = repos_cmd._read_actions(tmp_path)
    actions[0]["source_fingerprint"] = "fp-two"
    actions[0]["safe_summary"] = "Fix changed setup"
    repos_cmd._write_actions(tmp_path, actions)
    assert repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed["created_count"] == 1
    imports = work_cmd._read_imports(repo)
    statuses = sorted(item["status"] for item in imports)
    assert statuses == ["pending", "superseded"]
    assert (
        cli.main(
            [
                "repos",
                "actions",
                "dispatch",
                "report",
                "fleet-act-alpha",
                "--target",
                str(tmp_path),
                "--record",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["recorded"] is True
    assert report["actions"][0]["superseded_import_count"] == 1
    assert report["actions"][0]["changed_fingerprint_import_count"] == 1
    assert any(item["event"] == "dispatch-applied" for item in report["actions"][0]["history"])
    report_path = tmp_path / ".brigade" / "repos" / "actions" / "dispatch-reports" / report["report_id"]
    assert (report_path / "DISPATCH_REPORT.json").is_file()
    assert (report_path / "DISPATCH_REPORT.md").is_file()


def test_fleet_action_context_plan_build_excludes_private_evidence(tmp_path, capsys):
    repo = tmp_path / "private" / "actual-repo-name"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    _seed_action_queue(tmp_path, [_action(summary=f"Fix setup in {repo}")])

    assert repos_cmd.actions_context_plan(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    rendered = json.dumps(plan)
    assert plan["guidance_presence"]["has_agents"] is True
    assert "actual-repo-name" not in rendered
    assert "local guidance that must not be copied" not in rendered
    assert "raw guidance file contents" in rendered

    assert (
        cli.main(["repos", "actions", "context", "build", "fleet-act-alpha", "--target", str(tmp_path), "--json"]) == 0
    )
    built = json.loads(capsys.readouterr().out)
    pack_path = repo / ".brigade" / "context" / "packs" / built["pack_id"]
    assert (pack_path / "context.json").is_file()
    assert (pack_path / "CONTEXT.md").is_file()
    assert "actual-repo-name" not in (pack_path / "context.json").read_text()
    assert "local guidance that must not be copied" not in (pack_path / "CONTEXT.md").read_text()


def test_fleet_action_reconcile_states_and_completion(tmp_path, capsys):
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    _seed_workspace(tmp_path, repo)
    actions = [
        _action(action_id="fleet-act-alpha", fingerprint="fp-alpha"),
        _action(action_id="fleet-act-dismissed", fingerprint="fp-dismissed"),
        _action(action_id="fleet-act-superseded", fingerprint="fp-superseded"),
        _action(action_id="fleet-act-stale", fingerprint="fp-stale"),
        _action(action_id="fleet-act-broken", fingerprint="fp-broken"),
    ]
    _seed_action_queue(tmp_path, actions)
    assert repos_cmd.actions_dispatch_apply(target=tmp_path, all_reviewed=True, json_output=True) == 0
    capsys.readouterr()

    imports = work_cmd._read_imports(repo)
    for item in imports:
        metadata = item["metadata"]
        if metadata["fleet_action_id"] == "fleet-act-dismissed":
            item["status"] = "dismissed"
            item["dismiss_reason"] = "accepted"
        if metadata["fleet_action_id"] == "fleet-act-superseded":
            item["status"] = "superseded"
        if metadata["fleet_action_id"] == "fleet-act-stale":
            item["created_at"] = "2026-05-20T01:00:00+00:00"
    imports = [item for item in imports if item["metadata"]["fleet_action_id"] != "fleet-act-broken"]
    work_cmd._write_imports(repo, imports)

    assert repos_cmd.actions_reconcile(target=tmp_path, json_output=True) == 0
    reconciled = json.loads(capsys.readouterr().out)
    states = {item["fleet_action_id"]: item["status"] for item in reconciled["results"]}
    assert states["fleet-act-alpha"] == "dispatched"
    assert states["fleet-act-dismissed"] == "dismissed"
    assert states["fleet-act-superseded"] == "superseded"
    assert states["fleet-act-stale"] == "stale"
    assert states["fleet-act-broken"] == "broken-reference"

    alpha_import = next(
        item for item in work_cmd._read_imports(repo) if item["metadata"]["fleet_action_id"] == "fleet-act-alpha"
    )
    assert work_cmd.import_promote(target=repo, import_id=alpha_import["id"]) == 0
    capsys.readouterr()
    promoted = next(
        item for item in work_cmd._read_imports(repo) if item["metadata"]["fleet_action_id"] == "fleet-act-alpha"
    )
    assert repos_cmd.actions_reconcile(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "in-progress"
    assert work_cmd.task_done(target=repo, task_id=promoted["task_id"]) == 0
    capsys.readouterr()
    assert repos_cmd.actions_reconcile(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    done = json.loads(capsys.readouterr().out)["results"][0]
    assert done["status"] == "completed"
    assert repos_cmd._find_action(tmp_path, "fleet-act-alpha")[1]["status"] == "done"


def test_fleet_action_dispatch_health_integrates_with_daily_surfaces(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {"run_id": "verify-one", "status": "completed", "created_at": "2026-05-30T01:00:00+00:00"},
    )
    _write_json(
        tmp_path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {"closeout_id": "closeout-one", "status": "ready", "ready": True, "created_at": "2026-05-30T01:01:00+00:00"},
    )
    repo = tmp_path / "repo-alpha"
    _init_repo(repo)
    (repo / ".claude" / "memory-handoffs").mkdir(parents=True)
    _seed_workspace(tmp_path, repo)
    _seed_action_queue(tmp_path, [_action()])
    _patch_release_health(monkeypatch)
    assert repos_cmd.actions_dispatch_apply(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    capsys.readouterr()
    (repo / ".brigade" / "work" / "imports" / "inbox.jsonl").write_text("")
    assert repos_cmd.actions_reconcile(target=tmp_path, action_id="fleet-act-alpha", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "broken-reference"

    health = repos_cmd.health(tmp_path)
    assert any(check["name"] == "repo_fleet_action_broken-reference" for check in health["actions"]["checks"])
    assert repos_cmd.doctor(target=tmp_path) == 0
    assert "repo_fleet_action_broken-reference" in capsys.readouterr().out
    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center = json.loads(capsys.readouterr().out)
    assert center["repo_fleet"]["actions"]["issue_count"] >= 1
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "repo-fleet" for item in reviews["reviews"])
    assert any(
        "dispatch report" in item["suggested_next_command"]
        for item in reviews["reviews"]
        if item["subsystem"] == "repo-fleet"
    )
    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["repo_fleet"]["actions"]["issue_count"] >= 1
    assert work_cmd.doctor(target=tmp_path) == 1
    assert "repo_fleet_action_broken-reference" in capsys.readouterr().out
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    release = json.loads(capsys.readouterr().out)
    assert any("repo fleet action queue" in warning for warning in release["warnings"])
