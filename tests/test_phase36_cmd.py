import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from brigade import center_cmd
from brigade import cli
from brigade import context_cmd
from brigade import handoff_cmd
from brigade import learn_cmd
from brigade import memory_cmd
from brigade import projects_cmd
from brigade import release_cmd
from brigade import security_cmd
from brigade import tools_cmd
from brigade import work_cmd


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _init_git(path: Path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Local operator updates.\n")
    (path / "ROADMAP.md").write_text("# Roadmap\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_task(path: Path):
    _write_json(
        path / ".brigade" / "work" / "tasks.json",
        {
            "version": 1,
            "tasks": [
                {
                    "id": "task-one",
                    "text": "Implement local operator center",
                    "status": "pending",
                    "acceptance": ["Center status reports pending reviews."],
                    "created_at": "2026-05-29T12:00:00+00:00",
                }
            ],
        },
    )


def _seed_import(path: Path):
    record = {
        "id": "import-one",
        "text": "Review local finding",
        "kind": "task",
        "source": "security-scan",
        "status": "pending",
        "priority": "high",
        "metadata": {"source_fingerprint": "fp-one", "source_item_key": "security:one"},
        "created_at": "2026-05-29T12:01:00+00:00",
    }
    imports = path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports.parent.mkdir(parents=True, exist_ok=True)
    imports.write_text(json.dumps(record, sort_keys=True) + "\n")


def _seed_release_evidence(path: Path):
    _write_json(
        path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-29T12:02:00+00:00",
            "completed_at": "2026-05-29T12:02:10+00:00",
            "path": str(path / ".brigade" / "work" / "verify-runs" / "verify-one"),
        },
    )
    _write_json(
        path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "ready": True,
            "status": "ready",
            "created_at": "2026-05-29T12:03:00+00:00",
            "path": str(path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json"),
        },
    )


def test_context_pack_build_list_show_archive_excludes_private_evidence(tmp_path, capsys):
    _seed_task(tmp_path)
    (tmp_path / "README.md").write_text("local readme\n")
    assert context_cmd.plan(target=tmp_path, kind="task", task_id="task-one", json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["task"]["acceptance"] == ["Center status reports pending reviews."]
    assert "raw chat exports" in plan["excluded_private_evidence"]

    assert context_cmd.build(target=tmp_path, kind="task", task_id="task-one", json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    assert Path(built["path"], "context.json").is_file()
    assert Path(built["path"], "CONTEXT.md").is_file()

    assert context_cmd.list_packs(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["pack_count"] == 1
    assert context_cmd.show(target=tmp_path, pack_id=built["pack_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["pack"]["pack_id"] == built["pack_id"]
    assert context_cmd.archive(target=tmp_path, pack_id=built["pack_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"


def test_context_sync_plan_receipts_freshness_conflicts_and_release_center_integration(tmp_path, monkeypatch, capsys):
    _seed_task(tmp_path)
    (tmp_path / "README.md").write_text("local readme\n")
    assert context_cmd.build(target=tmp_path, kind="task", task_id="task-one", json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    config = tmp_path / ".brigade" / "context" / "sync-targets.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        config,
        {
            "targets": [
                {"id": "codex", "harness": "codex", "path": ".codex/context.md", "enabled": True},
                {"id": "claude", "harness": "claude", "path": ".claude/context.md", "enabled": True},
            ]
        },
    )
    conflict = tmp_path / ".claude" / "context.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("user context\n")
    (tmp_path / "README.md").unlink()

    assert context_cmd.sync_plan(target=tmp_path, pack_id=built["pack_id"], json_output=True) == 1
    plan = json.loads(capsys.readouterr().out)
    statuses = {item["target_id"]: item["status"] for item in plan["destinations"]}
    assert statuses == {"codex": "missing", "claude": "conflict"}
    assert plan["blocker_count"] == 1
    assert plan["write_default"] is False
    assert any(issue["name"] == "context_sync_missing_source_reference" for issue in plan["issues"])

    _write_json(config, {"targets": [{"id": "codex", "harness": "codex", "path": ".codex/context.md", "enabled": True}]})
    assert context_cmd.sync_record(target=tmp_path, pack_id="latest", json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert Path(receipt["path"], "sync-plan.json").is_file()
    assert receipt["destination_count"] == 1
    assert receipt["destinations"][0]["status"] == "missing"

    health = context_cmd.health(tmp_path)
    assert health["sync"]["destination_count"] == 1
    assert any(issue["name"] == "context_sync_missing_source_reference" for issue in health["issues"])

    monkeypatch.setattr(release_cmd, "_run_content_guard_check", lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "exit_code": 0, "detail": "clean"})
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)
    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) in {0, 1}
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["context"]["sync"]["destination_count"] == 1

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    activity = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "context-sync" for item in activity["activity"])


def test_context_pack_freshness_doctor_imports_and_daily_release_surfaces(tmp_path, monkeypatch, capsys):
    _seed_task(tmp_path)
    _seed_import(tmp_path)
    (tmp_path / "README.md").write_text("local readme\n")
    monkeypatch.setattr(context_cmd, "_now", lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc))
    assert context_cmd.build(target=tmp_path, kind="task", task_id="task-one", tool_id="missing-tool", json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    context_path = Path(built["path"], "context.json")
    payload = json.loads(context_path.read_text())
    payload["created_at"] = "2026-05-20T12:00:00+00:00"
    _write_json(context_path, payload)
    tasks_path = tmp_path / ".brigade" / "work" / "tasks.json"
    tasks = json.loads(tasks_path.read_text())
    tasks["tasks"][0]["acceptance"] = ["Center status reports pending reviews.", "Context pack is refreshed."]
    _write_json(tasks_path, tasks)
    (tmp_path / "README.md").unlink()

    assert context_cmd.doctor(target=tmp_path, json_output=True) == 0
    health = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in health["issues"] if issue.get("issue_type")}
    assert {"pack_stale", "missing_source_reference", "task_acceptance_stale", "tool_reference_stale"} <= issue_types

    assert context_cmd.import_issues(target=tmp_path, json_output=True) == 0
    imports = json.loads(capsys.readouterr().out)
    assert imports["created"] == 4
    assert {item["source"] for item in imports["imports"]} == {"context-pack"}

    assert work_cmd.brief(target=tmp_path) == 0
    brief = capsys.readouterr().out
    assert "context_top_issue:" in brief

    assert work_cmd.doctor(target=tmp_path) in {0, 1}
    doctor = capsys.readouterr().out
    assert "context_pack_stale" in doctor

    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "context" for item in reviews["reviews"])

    monkeypatch.setattr(release_cmd, "_run_content_guard_check", lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "exit_code": 0, "detail": "clean"})
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)
    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) in {0, 1}
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["context"]["top_issue"]["issue_type"] == "pack_stale"


def test_projects_audit_imports_and_learning_candidates(tmp_path, capsys):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "projects.toml").write_text(
        """
[[project]]
id = "project-alpha"
label = "Project Alpha"
category = "public side project"
decision = "move-candidate"
reason = "Needs reviewed migration planning."
docs_ready = true
license_ready = false
security_ready = false
release_ready = false

[[project]]
id = "workflow-kit"
category = "workflow helper"
decision = "bake-in"
docs_ready = true
license_ready = true
security_ready = true
release_ready = true
"""
    )
    assert projects_cmd.audit(target=tmp_path, json_output=True) == 0
    audit = json.loads(capsys.readouterr().out)
    assert {item["decision"] for item in audit["projects"]} == {"move-candidate", "bake-in"}
    assert audit["issue_count"] == 1
    assert projects_cmd.import_issues(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 1

    _seed_import(tmp_path)
    assert learn_cmd.plan(target=tmp_path, json_output=True) == 0
    learning = json.loads(capsys.readouterr().out)
    assert learning["candidate_count"] >= 1
    assert learn_cmd.import_issues(target=tmp_path, dry_run=True, json_output=True) == 0
    assert "created" in json.loads(capsys.readouterr().out)


def test_tool_pack_and_sync_plan(tmp_path, capsys):
    assert tools_cmd.init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()
    assert tools_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)
    assert Path(pack["path"], "tool-pack.json").is_file()
    assert tools_cmd.pack_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["pack_count"] == 1
    assert tools_cmd.pack_show(target=tmp_path, pack_id=pack["pack_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["pack"]["pack_id"] == pack["pack_id"]
    assert tools_cmd.sync_plan(target=tmp_path, json_output=True) in {0, 1}
    sync = json.loads(capsys.readouterr().out)
    assert sync["delete_supported"] is False
    assert tools_cmd.sync_apply(target=tmp_path, json_output=True) == 0
    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_payload["dry_run"] is True
    assert tools_cmd.pack_archive(target=tmp_path, pack_id=pack["pack_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"


def test_closeout_commands_and_acceptance_summary(tmp_path, capsys):
    _seed_task(tmp_path)
    assert work_cmd.acceptance(target=tmp_path, json_output=True) == 0
    acceptance = json.loads(capsys.readouterr().out)
    assert acceptance["coverage"]["pending_with_acceptance"] == 1

    assert work_cmd.backup_init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()
    assert work_cmd.backup_closeout(target=tmp_path, reason="reviewed", json_output=True) == 0
    backup = json.loads(capsys.readouterr().out)
    assert backup["status"] == "reviewed"

    queue = {
        "version": 1,
        "cards": [
            {
                "card_id": "card-one",
                "card_file": "memory/cards/card-one.md",
                "issue_type": "stale",
                "source_fingerprint": "memory-fp-one",
            }
        ],
    }
    _write_json(tmp_path / "memory" / "cards" / "decay" / "refresh-queue.json", queue)
    assert memory_cmd.closeout(target=tmp_path, reason="reviewed", json_output=True) == 0
    memory = json.loads(capsys.readouterr().out)
    assert memory["source_fingerprints"] == ["memory-fp-one"]

    handoff_dir = tmp_path / ".claude" / "memory-handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "valid.md").write_text(
        """# Memory Handoff

## Type
learning

## Title
Reviewed local note

## Summary
Reviewed local note.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
Reviewed local note.
"""
    )
    assert handoff_cmd.closeout(target=tmp_path, json_output=True) == 0
    handoff = json.loads(capsys.readouterr().out)
    assert handoff["draft_count"] == 1


def test_security_closeout_and_release_candidate_compare_closeout(tmp_path, monkeypatch, capsys):
    _init_git(tmp_path)
    _seed_release_evidence(tmp_path)
    security_dir = tmp_path / ".brigade" / "security" / "latest"
    _write_json(
        security_dir / "security-report.json",
        {
            "generated_at": "2026-05-29T12:04:00+00:00",
            "policy": "personal",
            "finding_count": 1,
            "findings": [
                {
                    "id": "security-one",
                    "fingerprint": "abcdef1234567890",
                    "severity": "medium",
                    "category": "permissions",
                    "path": "AGENTS.md",
                    "line": 1,
                    "title": "Reviewed local risk",
                    "suggestion": "Review local policy.",
                }
            ],
        },
    )
    (security_dir / "security-report.md").write_text("# Security\n")
    assert security_cmd.closeout(target=tmp_path, accept_risk=True, json_output=True) == 0
    security = json.loads(capsys.readouterr().out)
    assert security["status"] == "accepted-risk"

    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {"valid": True, "issue_count": 0, "top_issue": None, "top_finding": None, "evidence": {"ready": True, "finding_count": 0}},
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target: {"counts": {"pending": 0}, "issue_count": 0, "top_issue": None, "latest_ingest_run": None},
    )
    monkeypatch.setattr(
        work_cmd,
        "_scanner_sweep_health",
        lambda target: {"latest": None, "review": {"issue_count": 0}, "due_count": 0},
    )
    monkeypatch.setattr(
        work_cmd,
        "_review_health",
        lambda target: {"latest_run": None, "latest_unclosed_run": None, "unresolved_finding_count": 0},
    )
    monkeypatch.setattr(release_cmd, "_run_content_guard_check", lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"})
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)
    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert release_cmd.candidate_compare(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 0
    compare = json.loads(capsys.readouterr().out)
    assert compare["status"] == "current"
    assert release_cmd.candidate_closeout(target=tmp_path, candidate_id=candidate["candidate_id"], status="reviewed", json_output=True) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert Path(closeout["path"]).is_file()


def test_center_views_and_cli_dispatch(tmp_path, capsys):
    _seed_task(tmp_path)
    _seed_import(tmp_path)
    assert context_cmd.build(target=tmp_path, kind="repo", json_output=True) == 0
    capsys.readouterr()
    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["pending_task_count"] == 1
    assert status["review_queue_count"] >= 1
    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    assert "activity" in json.loads(capsys.readouterr().out)
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["review_count"] >= 1
    assert center_cmd.templates(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["template_count"] >= 1

    assert cli.main(["context", "list", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pack_count"] == 1
    assert cli.main(["context", "sync", "plan", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["write_default"] is False
    assert cli.main(["projects", "audit", "--target", str(tmp_path), "--json"]) == 1
    assert cli.main(["learn", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["center", "reviews", "--target", str(tmp_path), "--json"]) == 0
