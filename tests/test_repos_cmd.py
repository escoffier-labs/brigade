import json
import subprocess

from brigade import cli
from brigade import repos_cmd


def _init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)


def test_repos_init_list_show_scan_doctor_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("local guidance\n")
    (tmp_path / "README.md").write_text("readme\n")
    (tmp_path / "CHANGELOG.md").write_text("changes\n")
    (tmp_path / "ROADMAP.md").write_text("roadmap\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    assert repos_cmd.init(target=tmp_path, json_output=True) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["repo_count"] == 1

    assert repos_cmd.list_repos(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["repos"][0]["has_agents"] is True

    assert repos_cmd.show(target=tmp_path, repo_id="current", json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["repo"]["guidance_source"] == "AGENTS.md"

    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["repos"][0]["test_hints"]

    assert repos_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctored = json.loads(capsys.readouterr().out)
    assert doctored["issue_count"] == 0

    daily_use = repos_cmd.daily_use_health(tmp_path)
    assert daily_use["manual_only"] is True
    assert daily_use["privacy"]["safe_labels_only"] is True
    assert daily_use["issue_count"] >= 1
    assert any(check["phase"] in {145, 147, 148} for check in daily_use["checks"])


def test_repos_first_run_plan_guides_empty_fleet(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert repos_cmd.first_run_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_only"] is True
    assert payload["would_write"] is False
    assert payload["would_run_commands"] is False
    assert payload["ready"] is False
    assert payload["next_step"]["id"] == "config"
    commands = [step["command"] for step in payload["steps"]]
    assert "brigade repos init --target ." in commands
    assert "brigade repos sweep run --target ." in commands
    assert "brigade repos report build --target ." in commands
    assert "brigade repos release build --target ." in commands
    assert payload["privacy"]["safe_labels_only"] is True


def test_repos_first_run_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_first_run_plan(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(repos_cmd.fleet_health, "first_run_plan", fake_first_run_plan)
    assert cli.main(["repos", "first-run", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "json_output": True}


def test_repos_claude_fallback_detection_does_not_copy_contents(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("private setup detail should stay local\n")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    assert repos_cmd.init(target=tmp_path) == 0
    capsys.readouterr()

    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["repos"][0]["guidance_source"] == "CLAUDE.md"
    rendered = json.dumps(payload)
    assert "private setup detail" not in rendered
    assert any(check["name"] == "repo_claude_fallback" for check in payload["issues"])


def test_repos_import_issues_dedupe_and_dismissed_until_changed(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / ".brigade").mkdir()
    assert repos_cmd.init(target=tmp_path) == 0
    capsys.readouterr()

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] >= 1

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] == 0
    assert second["skipped"] >= 1

    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    imports[0]["status"] = "dismissed"
    imports_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in imports))

    assert repos_cmd.import_issues(target=tmp_path, json_output=True) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["created"] == 0
    assert third["dismissed"] >= 1


def test_repos_discover_plan_uses_configured_roots_and_redacts_paths(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    included = workspace / "services" / "private-alpha"
    excluded = workspace / "scratch" / "private-beta"
    _init_git_repo(included)
    _init_git_repo(excluded)
    config = tmp_path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[discovery_root]]
id = "workspace"
label = "workspace root"
path = "workspace"
include = ["services/*"]
exclude = ["scratch/*"]
max_depth = 3
enabled = true
"""
    )
    before = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    assert repos_cmd.discover_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    after = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    assert after == before
    assert payload["dry_run"] is True
    assert payload["would_clone"] is False
    assert payload["would_write"] is False
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["path_label"] == "workspace:candidate-1"
    assert payload["candidates"][0]["label_suggestion"] == "workspace root candidate 1"
    assert "private-alpha" not in rendered
    assert "private-beta" not in rendered
    assert str(tmp_path) not in rendered
    assert any(item["reason"] == "excluded" for item in payload["skipped"])
    assert cli.main(["repos", "discover", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["candidate_count"] == 1


def test_repos_cli_dispatch(tmp_path, monkeypatch):
    seen = []

    def record(name):
        def _fake(**kwargs):
            seen.append((name, kwargs))
            return 0

        return _fake

    monkeypatch.setattr(repos_cmd.fleet, "init", record("init"))
    monkeypatch.setattr(repos_cmd.fleet, "list_repos", record("list"))
    monkeypatch.setattr(repos_cmd.fleet, "show", record("show"))
    monkeypatch.setattr(repos_cmd.fleet, "scan", record("scan"))
    monkeypatch.setattr(repos_cmd.fleet, "doctor", record("doctor"))
    monkeypatch.setattr(repos_cmd.sweeps, "import_issues", record("import-issues"))
    monkeypatch.setattr(repos_cmd.fleet, "rearm", record("rearm"))
    monkeypatch.setattr(repos_cmd.fleet_health, "health_commands", record("health-commands"))
    monkeypatch.setattr(repos_cmd.fleet, "discover_plan", record("discover-plan"))

    assert cli.main(["repos", "init", "--target", str(tmp_path), "--force", "--no-gitignore", "--json"]) == 0
    assert cli.main(["repos", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "show", "current", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "scan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert cli.main(["repos", "rearm", "--target", str(tmp_path), "--apply", "--json"]) == 0
    assert cli.main(["repos", "health-commands", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "discover", "plan", "--target", str(tmp_path), "--json"]) == 0

    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False, "json_output": True}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "repo_id": "current", "json_output": True}),
        ("scan", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True, "deep": False}),
        ("import-issues", {"target": tmp_path, "dry_run": True, "json_output": True}),
        ("rearm", {"target": tmp_path, "apply": True, "json_output": True}),
        ("health-commands", {"target": tmp_path, "json_output": True}),
        ("discover-plan", {"target": tmp_path, "json_output": True}),
    ]


def test_repos_doctor_warns_on_stale_handoff_backlog(tmp_path, capsys):
    import os
    import time

    _init_git_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("local guidance\n")
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    stale = inbox / "stuck.md"
    stale.write_text("# Memory Handoff\n")
    old = time.time() - (repos_cmd.BACKLOG_STALE_DAYS + 1) * 24 * 60 * 60
    os.utime(stale, (old, old))

    assert repos_cmd.init(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    scanned = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in scanned["checks"]}
    assert "repo_handoff_backlog" in names
    backlog = next(c for c in scanned["checks"] if c["name"] == "repo_handoff_backlog")
    assert "un-ingested" in backlog["detail"]


def test_repos_scan_no_backlog_warn_for_fresh_handoff(tmp_path, capsys):
    _init_git_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("local guidance\n")
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "fresh.md").write_text("# Memory Handoff\n")  # just written, not stale

    assert repos_cmd.init(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert repos_cmd.scan(target=tmp_path, json_output=True) == 0
    scanned = json.loads(capsys.readouterr().out)
    names = {c["name"] for c in scanned["checks"]}
    assert "repo_handoff_backlog" not in names


def test_repos_ingest_routes_fleet_handoffs_into_owner(tmp_path, capsys):
    """brigade repos ingest sweeps each fleet repo's handoffs into the owner."""
    owner = tmp_path / "workspace"
    repo = tmp_path / "writer-repo"
    _init_git_repo(owner)
    _init_git_repo(repo)
    (owner / "AGENTS.md").write_text("owner guidance\n")
    (repo / "AGENTS.md").write_text("repo guidance\n")
    # owner registers itself + the writer repo in its fleet config
    assert repos_cmd.init(target=owner, json_output=True) == 0
    capsys.readouterr()
    cfg = repos_cmd.config_path(owner)
    cfg.write_text(cfg.read_text() + f'\n[[repo]]\nid = "writer-repo"\nlabel = "writer"\npath = "{repo}"\n')
    # the writer repo has a card handoff pending
    inbox = repo / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "note.md").write_text(
        "# Memory Handoff\n\n## Recommended memory action\ncreate-card\n\n"
        "## Target card\nfleet-note.md\n\n## Suggested card content\n"
        "---\ntopic: fleet\n---\n\n# Fleet note\nbody\n"
    )

    # dry run writes nothing
    assert repos_cmd.ingest_fleet(target=owner, apply=False, json_output=True) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert not (owner / "memory" / "cards" / "fleet-note.md").exists()

    # apply routes the card into the OWNER and archives the source handoff in the REPO
    assert repos_cmd.ingest_fleet(target=owner, apply=True, json_output=True) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["totals"]["promoted"] == 1
    assert (owner / "memory" / "cards" / "fleet-note.md").is_file()
    assert (inbox / "processed" / "note.md").is_file()
    assert not (inbox / "note.md").exists()


def _write_fleet_config(owner, repos):
    config = owner / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "".join(f'[[repo]]\nid = "{repo_id}"\nlabel = "{label}"\npath = "{path}"\n' for repo_id, label, path in repos)
    )


def _write_brigade_config(repo, harnesses=("codex",), owner="codex"):
    brigade = repo / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": list(harnesses),
                "owner": owner,
                "includes": [],
            },
            indent=2,
        )
        + "\n"
    )


def _capture_json_call(capsys, func, **kwargs):
    rc = func(**kwargs, json_output=True)
    return rc, json.loads(capsys.readouterr().out)


def test_repos_rearm_plan_reports_armed_and_dormant_without_writes(tmp_path, capsys):
    owner = tmp_path / "workspace"
    armed = tmp_path / "armed-repo"
    dormant = tmp_path / "dormant-repo"
    _init_git_repo(owner)
    _init_git_repo(armed)
    _init_git_repo(dormant)
    _write_fleet_config(owner, [("armed", "armed repo", armed), ("dormant", "dormant repo", dormant)])
    _write_brigade_config(armed)
    _write_brigade_config(dormant)
    (armed / ".brigade" / "dogfood.toml").write_text("[dogfood]\n")
    (armed / ".brigade" / "mcp.json").write_text('{"mcpServers":{}}\n')
    (dormant / ".brigade" / "mcp.json").write_text('{"mcpServers":{}}\n')
    before = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    rc, payload = _capture_json_call(capsys, repos_cmd.rearm, target=owner)
    after = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    assert rc == 0
    assert after == before
    assert payload["dry_run"] is True
    assert payload["totals"]["armed"] == 1
    assert payload["totals"]["dormant"] == 1
    assert payload["repos"] == [
        {
            "repo_id": "armed",
            "label": "armed repo",
            "status": "armed",
            "action": "none",
            "reason": None,
            "has_config": True,
            "has_dogfood": True,
            "has_mcp": True,
            "harnesses": ["codex"],
        },
        {
            "repo_id": "dormant",
            "label": "dormant repo",
            "status": "dormant",
            "action": "plan quickstart",
            "reason": "missing .brigade/dogfood.toml",
            "has_config": True,
            "has_dogfood": False,
            "has_mcp": True,
            "harnesses": ["codex"],
        },
    ]
    assert not (dormant / ".brigade" / "dogfood.toml").exists()


def test_repos_rearm_apply_arms_dormant_repo_without_clobbering_existing_files(tmp_path, monkeypatch, capsys):
    owner = tmp_path / "workspace"
    dormant = tmp_path / "dormant-repo"
    _init_git_repo(owner)
    _init_git_repo(dormant)
    _write_fleet_config(owner, [("dormant", "dormant repo", dormant)])
    _write_brigade_config(dormant, harnesses=("codex", "cursor"), owner="cursor")
    agents = dormant / "AGENTS.md"
    agents.write_text("existing guidance\n")
    mcp = dormant / ".brigade" / "mcp.json"
    mcp.write_text('{"mcpServers":{"existing":{"command":"keep"}}}\n')
    calls = []

    def fake_quickstart(**kwargs):
        calls.append(kwargs)
        (kwargs["target"] / ".brigade" / "dogfood.toml").write_text("[dogfood]\n")
        return 0

    monkeypatch.setattr("brigade.operator_cmd.quickstart", fake_quickstart)

    rc, payload = _capture_json_call(capsys, repos_cmd.rearm, target=owner, apply=True)

    assert rc == 0
    assert payload["dry_run"] is False
    assert payload["totals"]["armed"] == 0
    assert payload["totals"]["dormant"] == 1
    assert payload["totals"]["applied"] == 1
    assert (dormant / ".brigade" / "dogfood.toml").is_file()
    assert agents.read_text() == "existing guidance\n"
    assert mcp.read_text() == '{"mcpServers":{"existing":{"command":"keep"}}}\n'
    assert calls == [
        {
            "target": dormant,
            "depth": "repo",
            "harnesses": "codex,cursor",
            "owner": "cursor",
            "dry_run": False,
            "force": False,
            "full": False,
            "json_output": True,
        }
    ]


def test_repos_rearm_skips_unwired_repo_with_reason(tmp_path, capsys):
    owner = tmp_path / "workspace"
    unwired = tmp_path / "unwired-repo"
    _init_git_repo(owner)
    _init_git_repo(unwired)
    (unwired / ".brigade").mkdir()
    _write_fleet_config(owner, [("unwired", "unwired repo", unwired)])

    rc, payload = _capture_json_call(capsys, repos_cmd.rearm, target=owner)

    assert rc == 0
    assert payload["totals"]["unwired"] == 1
    assert payload["repos"][0]["status"] == "unwired"
    assert payload["repos"][0]["action"] == "skip"
    assert payload["repos"][0]["reason"] == "unwired; run quickstart with explicit harnesses"
    assert not (unwired / ".brigade" / "dogfood.toml").exists()


def test_repo_summary_counts_opencode_inbox(tmp_path):
    from brigade import repos_cmd

    (tmp_path / ".opencode" / "memory-handoffs").mkdir(parents=True)
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)
    summary = repos_cmd._repo_summary(entry)
    assert ".opencode/memory-handoffs" in summary["handoff_inboxes"]


def test_repo_summaries_preserve_config_order_and_skip_disabled(tmp_path, monkeypatch):
    # The fleet sweep runs summaries on a thread pool; output must stay in config
    # order and exclude disabled repos regardless of which thread finishes first.
    entries = [
        repos_cmd.RepoEntry(repo_id=f"r{i}", label=f"R{i}", path=tmp_path, enabled=(i % 4 != 0)) for i in range(1, 9)
    ]
    monkeypatch.setattr(repos_cmd.fleet, "_repo_summary", lambda entry: {"id": entry.repo_id})
    result = repos_cmd._repo_summaries(entries)
    assert [row["id"] for row in result] == [entry.repo_id for entry in entries if entry.enabled]


def test_repos_doctor_deep_aggregates_checkup(tmp_path, monkeypatch, capsys):
    # issue #78: `repos doctor --deep` runs the operator checkup in each enabled
    # repo and rolls the per-repo verdicts up to a fleet verdict.
    from brigade import operator_cmd

    (tmp_path / "r1").mkdir()
    (tmp_path / "r2").mkdir()
    config = tmp_path / ".brigade" / "repos.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[[repo]]\nid = "r1"\npath = "r1"\n\n'
        '[[repo]]\nid = "r2"\npath = "r2"\n\n'
        '[[repo]]\nid = "r3"\npath = "r3"\nenabled = false\n'
    )

    def fake_checkup(target, **kwargs):
        ready = target.name == "r1"
        return {"ready": ready, "blocking_surface_count": 0 if ready else 2, "surfaces": []}

    monkeypatch.setattr(operator_cmd, "checkup_payload", fake_checkup)
    rc = repos_cmd.doctor(target=tmp_path, json_output=True, deep=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_count"] == 2  # r3 is disabled
    assert {r["id"] for r in payload["repos"]} == {"r1", "r2"}
    assert payload["blocking_repo_count"] == 1
    assert payload["ready"] is False
    assert rc == 1


def test_repo_summary_counts_antigravity_inbox(tmp_path):
    from brigade import repos_cmd

    (tmp_path / ".antigravity" / "memory-handoffs").mkdir(parents=True)
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)
    summary = repos_cmd._repo_summary(entry)
    assert ".antigravity/memory-handoffs" in summary["handoff_inboxes"]


def test_repo_summary_counts_pi_inbox(tmp_path):
    from brigade import repos_cmd

    (tmp_path / ".pi" / "memory-handoffs").mkdir(parents=True)
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)
    summary = repos_cmd._repo_summary(entry)
    assert ".pi/memory-handoffs" in summary["handoff_inboxes"]


def test_repo_summary_counts_cursor_inbox(tmp_path):
    from brigade import repos_cmd

    (tmp_path / ".cursor" / "memory-handoffs").mkdir(parents=True)
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)
    summary = repos_cmd._repo_summary(entry)
    assert ".cursor/memory-handoffs" in summary["handoff_inboxes"]


def test_repo_summary_reports_selected_grok_inbox_missing(tmp_path):
    _write_brigade_config(tmp_path, harnesses=("grok",), owner="grok")
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)

    summary = repos_cmd._repo_summary(entry)
    checks = repos_cmd.fleet._repo_checks(summary)

    assert summary["handoff_inboxes_expected"] == [".grok/memory-handoffs"]
    assert summary["handoff_inboxes_missing"] == [".grok/memory-handoffs"]
    assert summary["handoff_inboxes_unwatched"] == []
    assert [check["name"] for check in checks].count("repo_handoff_inbox_missing") == 1
    assert not any(check["name"] == "repo_missing_handoff_inbox" for check in checks)


def test_repo_summary_reports_selected_grok_inbox_unwatched(tmp_path):
    _write_brigade_config(tmp_path, harnesses=("grok",), owner="grok")
    (tmp_path / ".grok" / "memory-handoffs").mkdir(parents=True)
    (tmp_path / ".brigade" / "handoff-sources.json").write_text(
        json.dumps({"sources": [{"root": ".", "inboxes": [".codex/memory-handoffs"]}]}) + "\n"
    )
    entry = repos_cmd.RepoEntry(repo_id="r1", label="R1", path=tmp_path)

    summary = repos_cmd._repo_summary(entry)
    checks = repos_cmd.fleet._repo_checks(summary)

    assert summary["handoff_inboxes_expected"] == [".grok/memory-handoffs"]
    assert summary["handoff_inboxes_missing"] == []
    assert summary["handoff_inboxes_unwatched"] == [".grok/memory-handoffs"]
    assert [check["name"] for check in checks].count("repo_handoff_inbox_unwatched") == 1
