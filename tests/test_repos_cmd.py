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

    monkeypatch.setattr(repos_cmd, "first_run_plan", fake_first_run_plan)
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

    monkeypatch.setattr(repos_cmd, "init", record("init"))
    monkeypatch.setattr(repos_cmd, "list_repos", record("list"))
    monkeypatch.setattr(repos_cmd, "show", record("show"))
    monkeypatch.setattr(repos_cmd, "scan", record("scan"))
    monkeypatch.setattr(repos_cmd, "doctor", record("doctor"))
    monkeypatch.setattr(repos_cmd, "import_issues", record("import-issues"))
    monkeypatch.setattr(repos_cmd, "health_commands", record("health-commands"))
    monkeypatch.setattr(repos_cmd, "discover_plan", record("discover-plan"))

    assert cli.main(["repos", "init", "--target", str(tmp_path), "--force", "--no-gitignore", "--json"]) == 0
    assert cli.main(["repos", "list", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "show", "current", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "scan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert cli.main(["repos", "health-commands", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["repos", "discover", "plan", "--target", str(tmp_path), "--json"]) == 0

    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False, "json_output": True}),
        ("list", {"target": tmp_path, "json_output": True}),
        ("show", {"target": tmp_path, "repo_id": "current", "json_output": True}),
        ("scan", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "dry_run": True, "json_output": True}),
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
    monkeypatch.setattr(repos_cmd, "_repo_summary", lambda entry: {"id": entry.repo_id})
    result = repos_cmd._repo_summaries(entries)
    assert [row["id"] for row in result] == [entry.repo_id for entry in entries if entry.enabled]


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
