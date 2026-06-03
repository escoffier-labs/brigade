from __future__ import annotations

import json

from brigade import cli, operator_cmd


def test_operator_plan_lists_safe_local_configs(tmp_path, capsys):
    assert operator_cmd.plan(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["steps"]}
    assert {"daily", "handoff-sources", "work-scanners", "security", "tools"} <= ids
    assert "Does not start services." in payload["boundaries"]
    assert payload["profile"] == "local-operator"


def test_operator_internal_dogfood_plan_includes_dogfood(tmp_path, capsys):
    assert operator_cmd.plan(target=tmp_path, profile="internal-dogfood", json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["steps"]}
    assert "dogfood" in ids
    assert payload["profile"] == "internal-dogfood"
    assert any("security evidence" in boundary for boundary in payload["boundaries"])


def test_operator_guide_json_and_cli_dispatch(capsys):
    assert operator_cmd.guide(profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "internal-dogfood"
    assert "brigade operator status --profile internal-dogfood --target ." in payload["startup_commands"]
    assert "brigade daily plan --target ." in payload["startup_commands"]
    assert payload["tool_sync_command"] == "brigade operator sync-tools --target ."
    assert any("does not publish" in item.lower() for item in payload["boundaries"])

    assert cli.main(["operator", "guide", "--profile", "local-operator"]) == 0
    out = capsys.readouterr().out
    assert "operator guide" in out
    assert "profile: local-operator" in out


def test_operator_init_dry_run_does_not_write(tmp_path, capsys):
    assert operator_cmd.init(target=tmp_path, dry_run=True, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert not (tmp_path / ".brigade" / "daily.toml").exists()


def test_operator_internal_dogfood_init_and_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "brigade.scrub.hook_status",
        lambda target: {
            "available": True,
            "scanner_dir": "/tools/content-guard",
            "policy": "public-repo",
            "pre_push_hook_enabled": True,
        },
    )

    assert operator_cmd.init(target=tmp_path, profile="internal-dogfood", waive_public_release=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "internal-dogfood"
    assert (tmp_path / ".brigade" / "dogfood.toml").is_file()
    assert (tmp_path / ".brigade" / "security" / "latest" / "security-report.json").is_file()
    assert any(row["id"] == "security-scan" for row in payload["post_actions"])
    assert any(row["id"] == "public-release-readiness-waiver" for row in payload["post_actions"])

    assert operator_cmd.status(target=tmp_path, profile="internal-dogfood", json_output=True) in {0, 1}
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] == "internal-dogfood"
    assert status["dogfood"]["ready"] is True
    assert status["brigade"]["version"]
    assert status["repo"]["missing_config_count"] == 0
    assert status["security"]["issue_count"] == 0
    assert status["content_guard"]["available"] is True
    assert status["machine"]["content_guard_installed"] is True


def test_operator_status_content_guard_missing_unconfigured_is_nonblocking(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "dogfood.toml").write_text("[dogfood]\n")
    monkeypatch.setattr("brigade.daily_cmd.health", lambda target: {"issue_count": 0, "top_issue": None, "latest_plan": None, "latest_run": None})
    monkeypatch.setattr("brigade.security_cmd.health", lambda target: {"issue_count": 0, "top_issue": None, "evidence": None})
    monkeypatch.setattr("brigade.center_cmd._readiness_payload", lambda target: {"status": "ready", "blocker_count": 0, "warning_count": 0, "waived_count": 0, "blockers": []})
    monkeypatch.setattr("brigade.notifications_cmd.health", lambda target: {"installed": False, "configured": False, "config_path": None})
    monkeypatch.setattr(
        "brigade.scrub.hook_status",
        lambda target: {
            "available": False,
            "scanner_dir": "/missing/content-guard",
            "policy": "public-repo",
            "pre_push_hook_enabled": False,
            "pre_push_hook_exists": False,
            "configured_pre_push_hook_exists": False,
            "git_pre_push_hook_exists": False,
            "hooks_path": None,
            "checks": [
                {"status": "warn", "name": "content_guard_missing", "detail": "content-guard not found"},
                {"status": "warn", "name": "content_guard_hook_not_enabled", "detail": "no executable pre-push hook found"},
            ],
            "suggested_commands": ["clone content-guard"],
        },
    )

    assert operator_cmd.status(target=tmp_path, profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 0
    assert payload["content_guard"]["available"] is False
    assert payload["content_guard"]["checks"][0]["name"] == "content_guard_missing"


def test_operator_status_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "status", fake_status)
    assert cli.main(["operator", "status", "--target", str(tmp_path), "--profile", "internal-dogfood", "--json"]) == 0
    assert seen == {"target": tmp_path, "profile": "internal-dogfood", "json_output": True}


def test_operator_doctor_json_and_cli_dispatch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        operator_cmd,
        "status_payload",
        lambda target, profile="internal-dogfood": {
            "target": str(tmp_path),
            "profile": profile,
            "issue_count": 0,
            "checks": [],
            "dogfood": {"ready": True},
            "repo": {"missing_config_count": 0, "not_gitignored_count": 0},
            "security": {"issue_count": 0},
            "daily": {"issue_count": 0},
            "content_guard": {"available": True, "pre_push_hook_enabled": True, "policy": "public-repo"},
        },
    )
    monkeypatch.setattr(
        "brigade.daily_cmd.status_payload",
        lambda target: {
            "daily_health": {"issue_count": 0},
            "selected_action": {"action_type": "build-operator-report"},
            "next_recommended_command": "brigade center report build",
        },
    )
    monkeypatch.setattr(
        "brigade.tools_cmd.health",
        lambda target: {"issue_count": 0, "tool_count": 2, "top_issue": None},
    )

    assert operator_cmd.doctor(target=tmp_path, profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["blocking_issue_count"] == 0
    assert payload["next_command"] == "brigade center report build"
    assert payload["content_guard"]["available"] is True
    assert any("tools/" in item for item in payload["tracked_vs_generated"])

    assert cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "internal-dogfood"]) == 0
    out = capsys.readouterr().out
    assert "operator doctor:" in out
    assert "ready: yes" in out


def test_operator_doctor_blocks_on_tool_projection_health(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        operator_cmd,
        "status_payload",
        lambda target, profile="internal-dogfood": {
            "target": str(tmp_path),
            "profile": profile,
            "issue_count": 0,
            "checks": [],
            "dogfood": {"ready": True},
            "repo": {"missing_config_count": 0, "not_gitignored_count": 0},
            "security": {"issue_count": 0},
            "daily": {"issue_count": 0},
        },
    )
    monkeypatch.setattr("brigade.daily_cmd.status_payload", lambda target: {"daily_health": {"issue_count": 0}, "next_recommended_command": "brigade daily plan"})
    monkeypatch.setattr(
        "brigade.tools_cmd.health",
        lambda target: {
            "issue_count": 1,
            "tool_count": 2,
            "top_issue": {"detail": "claude: projection will be created"},
        },
    )

    assert operator_cmd.doctor(target=tmp_path, profile="internal-dogfood", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["blocking_issue_count"] == 1
    assert payload["next_command"] == "brigade operator sync-tools --target ."
    assert payload["blockers"][0]["name"] == "tool_projection_health"


def test_operator_verify_harness_hermes_after_handoff_init_and_draft(tmp_path, capsys):
    assert cli.main(["handoff", "sources", "init", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "handoff",
            "draft",
            "--target",
            str(tmp_path),
            "--inbox",
            "hermes",
            "--title",
            "Hermes verification",
            "--summary",
            "Hermes has a local handoff draft.",
            "--content",
            "### Hermes verification\n\nUse the Hermes inbox for local handoffs.",
            "--json",
        ]
    ) == 0
    capsys.readouterr()

    assert operator_cmd.verify_harness(target=tmp_path, harness="hermes", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["handoff_inbox"]["relative_path"] == ".hermes/memory-handoffs"
    assert payload["handoff_inbox"]["watched"] is True
    assert any(row["name"] == "handoff_lint" and row["status"] == "ok" for row in payload["checks"])


def test_operator_verify_harness_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_verify_harness(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "verify_harness", fake_verify_harness)
    assert cli.main(["operator", "verify-harness", "--target", str(tmp_path), "--harness", "hermes", "--json"]) == 0
    assert seen == {"target": tmp_path, "harness": "hermes", "json_output": True}


def test_operator_sync_tools_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_sync_tools(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "sync_tools", fake_sync_tools)
    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--dry-run", "--force", "--json"]) == 0
    assert seen == {"target": tmp_path, "dry_run": True, "force": True, "json_output": True}


def test_operator_sync_tools_projects_tracked_sources(tmp_path, capsys):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "simplify.md").write_text("# Simplify\n\nSummarize clearly.\n")
    (tools_dir / "superpowers.md").write_text("# Superpowers\n\nShare capabilities across harnesses.\n")

    assert cli.main(["operator", "init", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["apply"]["applied_count"] == 5
    assert not (tmp_path / ".claude" / "commands" / "simplify.md").exists()

    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["apply"]["applied_count"] == 5
    assert payload["tool_health"]["issue_count"] == 0
    assert (tmp_path / ".claude" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".claude" / "commands" / "superpowers.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "superpowers" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "superpowers" / "superpowers.md").is_file()


def test_internal_dogfood_fresh_repo_onboarding_loop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / "README.md").write_text("# Test repo\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "simplify.md").write_text("# Simplify\n\nSummarize clearly.\n")
    (tools_dir / "superpowers.md").write_text("# Superpowers\n\nShare capabilities across harnesses.\n")

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--write", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["operator", "init", "--profile", "internal-dogfood", "--target", str(tmp_path), "--waive-public-release", "--json"]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["profile"] == "internal-dogfood"

    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--json"]) == 0
    sync_payload = json.loads(capsys.readouterr().out)
    assert sync_payload["status"] == "ok"
    assert sync_payload["apply"]["applied_count"] == 5

    assert cli.main(["operator", "status", "--profile", "internal-dogfood", "--target", str(tmp_path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["issue_count"] == 0
    assert status["dogfood"]["ready"] is True
    assert status["repo"]["missing_config_count"] == 0
    assert status["security"]["issue_count"] == 0

    assert cli.main(["daily", "status", "--target", str(tmp_path), "--json"]) == 0
    daily_status = json.loads(capsys.readouterr().out)
    assert daily_status["daily_health"]["issue_count"] == 0
