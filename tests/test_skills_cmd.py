from __future__ import annotations

import io
import json
import sys

from brigade import cli, skills_cmd


def _write_skill(root, name="security-review"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Security Review\n\nReview code with the configured tools.\n")
    (skill_dir / "CHANGELOG.md").write_text("# 0.1.0\n\n- Initial reviewed workflow.\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": "Security Review",
                "version": "0.1.0",
                "required_tools": ["git"],
                "required_mcp_servers": ["github"],
                "supported_harnesses": ["codex", "claude", "opencode", "antigravity", "openclaw", "hermes", "mcp"],
                "trust_level": "workspace",
                "tests": ["brigade skills lint security-review"],
            }
        )
    )
    return skill_dir


def test_skills_import_lint_search_and_install_all(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["skill_id"] == "security-review"
    assert imported["lint"]["valid"] is True

    assert skills_cmd.search(target=tmp_path, query="security github", json_output=True) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["count"] == 1

    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="all", json_output=True) == 0
    install = json.loads(capsys.readouterr().out)
    assert install["receipt"]["targets"] == ["codex", "claude", "opencode", "antigravity", "openclaw", "hermes", "mcp"]
    codex_skill = tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md"
    assert codex_skill.is_file()
    codex_text = codex_skill.read_text()
    assert codex_text.startswith("---\n")
    assert 'name: "security-review"' in codex_text
    assert "# Security Review" in codex_text
    assert (tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".antigravity" / "skills" / "security-review" / "SKILL.md").is_file()
    assert not (tmp_path / ".agents" / "skills" / "security-review" / "SKILL.md").exists()
    assert (tmp_path / ".openclaw" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".hermes" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".brigade" / "skills" / "mcp-resources" / "security-review" / "SKILL.md").is_file()


def test_skills_cli_install_uses_target_for_harness(tmp_path):
    source = _write_skill(tmp_path / "source")
    assert cli.main(["skills", "import", str(source), "--target", str(tmp_path), "--json"]) == 0

    assert cli.main(["skills", "install", "security-review", "--workspace", str(tmp_path), "--target", "all", "--json"]) == 0

    assert (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").is_file()


def test_skills_serve_mcp_reports_resource_contract(capsys, tmp_path):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.serve_mcp(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["read_only"] is True
    assert "search_skills" in payload["tools"]
    assert "install_skill" in payload["blocked_tools"]
    assert payload["resource_count"] == 1
    assert payload["registered_resources"][0]["skill"] == "skill://registry/security-review/SKILL.md"
    assert payload["registered_resources"][0]["history"] == "skill://registry/security-review/history.json"


def test_skills_inbox_add_diff_accept_and_reject(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="review me", json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    proposal_id = proposal["proposal_id"]
    assert proposal["status"] == "pending"

    assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["proposal_count"] == 1

    assert skills_cmd.inbox_diff(target=tmp_path, proposal_id=proposal_id, json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert any("Security Review" in line for line in diff["diff"])

    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal_id, json_output=True) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"
    assert (tmp_path / ".brigade" / "skills" / "registry" / "security-review" / "SKILL.md").is_file()

    other = _write_skill(tmp_path / "source2", name="docs-review")
    assert skills_cmd.inbox_add(target=tmp_path, source=other, json_output=True) == 0
    other_proposal = json.loads(capsys.readouterr().out)
    assert skills_cmd.inbox_reject(target=tmp_path, proposal_id=other_proposal["proposal_id"], reason="duplicate", json_output=True) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "duplicate"


def test_skills_cli_inbox_and_adapters(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert cli.main(["skills", "inbox", "add", str(source), "--target", str(tmp_path), "--json"]) == 0
    proposal = json.loads(capsys.readouterr().out)

    assert cli.main(["skills", "inbox", "show", proposal["proposal_id"], "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["proposal_id"] == proposal["proposal_id"]

    assert cli.main(["skills", "adapters", "init", "--target", str(tmp_path), "--json"]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["adapter_count"] == 2

    assert cli.main(["skills", "adapters", "list", "--target", str(tmp_path), "--include-planned", "--json"]) == 0
    adapters = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in adapters["adapters"]}
    assert {"codex", "cursor", "antigravity", "pi"} <= ids
    antigravity = next(item for item in adapters["adapters"] if item["id"] == "antigravity")
    assert antigravity["status"] == "built-in"

    assert cli.main(["skills", "adapters", "show", "cursor", "--target", str(tmp_path), "--json"]) == 0
    cursor = json.loads(capsys.readouterr().out)
    assert cursor["status"] == "planned"


def test_skills_compatibility_reports_installed_and_planned_adapters(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.compatibility(target=tmp_path, skill="security-review", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {row["id"]: row for row in payload["adapters"]}
    assert by_id["codex"]["installed"] is True
    assert by_id["codex"]["render_valid"] is True
    assert by_id["codex"]["render_errors"] == []
    assert by_id["codex"]["installed_version"] == "0.1.0"
    assert by_id["codex"]["install_history_count"] == 1
    assert payload["trust_score"]["score"] > 0
    assert payload["changelog"]["present"] is True
    assert "adapter planned" in by_id["cursor"]["blockers"]


def test_skills_lint_can_validate_harness_rendering(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.lint(target=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["harness"] == "codex"
    assert payload["render_errors"] == []
    assert payload["valid"] is True

    assert skills_cmd.lint(target=tmp_path, skill="security-review", harness="cursor", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "harness adapter is planned: cursor" in payload["errors"]


def test_skills_doctor_and_import_issues_surface_registry_health(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="rough-skill")
    (source / "CHANGELOG.md").unlink()
    (source / "skill.json").write_text(
        json.dumps(
            {
                "id": "rough-skill",
                "title": "Rough Skill",
                "version": "0.1.0",
                "trust_level": "unreviewed",
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in doctor["issues"]}
    assert {"unreviewed_trust", "tests_missing", "changelog_missing"} <= issue_types

    assert skills_cmd.import_issues(target=tmp_path, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["source"] == "skill-registry"
    assert imported["imported_count"] >= 3
    assert all(item["source"] == "skill-registry" for item in imported["imports"])


def test_skills_codex_install_preserves_frontmatter_and_adds_missing_keys(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    (source / "SKILL.md").write_text("---\nmode: careful\n---\n# Security Review\n\nReview code.\n")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()

    text = (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "mode: careful\n" in text
    assert 'name: "security-review"\n' in text
    assert 'description: "Security Review"\n' in text


def test_skills_rollback_restores_previous_install(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md"
    installed.write_text("# Security Review\n\nchanged locally\n")

    updated = _write_skill(tmp_path / "source2")
    (updated / "SKILL.md").write_text("# Security Review\n\nnew version\n")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", force=True, json_output=True) == 0
    capsys.readouterr()
    assert "new version" in installed.read_text()

    assert skills_cmd.rollback(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "claude"
    assert "changed locally" in installed.read_text()


def test_skills_rollback_restores_transformed_codex_install(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md"
    installed.write_text("---\nname: \"security-review\"\ndescription: \"local\"\n---\n# Local\n")

    updated = _write_skill(tmp_path / "source2")
    (updated / "SKILL.md").write_text("# Security Review\n\nnew version\n")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", force=True, json_output=True) == 0
    capsys.readouterr()
    assert "new version" in installed.read_text()

    assert skills_cmd.rollback(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "codex"
    assert 'description: "local"' in installed.read_text()


def test_skills_history_and_diff_report_install_drift(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.history(target=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["count"] == 1
    assert history["history"][0]["version"] == "0.1.0"
    assert history["history"][0]["render_fingerprint"]

    installed = tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md"
    installed.write_text("# Security Review\n\nchanged locally\n")
    assert skills_cmd.diff(target=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    diff_payload = json.loads(capsys.readouterr().out)
    assert diff_payload["changed"] is True
    assert any("changed locally" in line for line in diff_payload["diff"])


def test_skills_pack_build_show_import_and_archive(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)
    pack_path = pack["path"]
    assert pack["skill_count"] == 1
    assert (tmp_path / ".brigade" / "skills" / "packs" / pack["pack_id"] / "skills" / "security-review" / "SKILL.md").is_file()

    assert skills_cmd.pack_show(target=tmp_path, pack_id="latest", json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["pack"]["pack_id"] == pack["pack_id"]

    other = tmp_path / "other"
    other.mkdir()
    assert skills_cmd.pack_import(target=other, pack=tmp_path / ".brigade" / "skills" / "packs" / pack["pack_id"], json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["imported_count"] == 1
    assert (other / ".brigade" / "skills" / "registry" / "security-review" / "SKILL.md").is_file()

    assert cli.main(["skills", "pack", "list", "--target", str(tmp_path), "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["pack_count"] == 1

    assert skills_cmd.pack_archive(target=tmp_path, pack_id=pack["pack_id"], json_output=True) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["status"] == "archived"
    assert archived["archive_path"].endswith(pack["pack_id"])
    assert pack_path not in archived["archive_path"]


def test_skills_mcp_stdio_serves_read_only_resources(tmp_path, capsys, monkeypatch):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    requests = "\n".join(
        json.dumps(item)
        for item in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "skill://registry/security-review/SKILL.md"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_skill_metadata", "arguments": {"skill_id": "security-review"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "install_skill", "arguments": {"skill_id": "security-review"}}},
        )
    ) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))

    assert cli.main(["skills", "serve-mcp", "--target", str(tmp_path), "--stdio"]) == 0
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_id = {item["id"]: item for item in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "brigade-skills-readonly"
    assert any(item["uri"] == "skill://registry/security-review/SKILL.md" for item in by_id[2]["result"]["resources"])
    assert "# Security Review" in by_id[3]["result"]["contents"][0]["text"]
    tool_names = {item["name"] for item in by_id[4]["result"]["tools"]}
    assert "get_skill_metadata" in tool_names
    assert "install_skill" not in tool_names
    assert '"id": "security-review"' in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["result"]["isError"] is True
