from __future__ import annotations

import json

from brigade import cli, skills_cmd


def _write_skill(root, name="security-review"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Security Review\n\nReview code with the configured tools.\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": "Security Review",
                "required_tools": ["git"],
                "required_mcp_servers": ["github"],
                "supported_harnesses": ["codex", "claude", "opencode", "gemini", "openclaw", "hermes", "mcp"],
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
    assert install["receipt"]["targets"] == ["codex", "claude", "opencode", "gemini", "openclaw", "hermes", "mcp"]
    assert (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".openclaw" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".hermes" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".brigade" / "skills" / "mcp-resources" / "security-review" / "SKILL.md").is_file()


def test_skills_cli_install_uses_target_for_harness(tmp_path):
    source = _write_skill(tmp_path / "source")
    assert cli.main(["skills", "import", str(source), "--target", str(tmp_path), "--json"]) == 0

    assert cli.main(["skills", "install", "security-review", "--workspace", str(tmp_path), "--target", "all", "--json"]) == 0

    assert (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").is_file()


def test_skills_serve_mcp_reports_planned_contract(capsys, tmp_path):
    assert skills_cmd.serve_mcp(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert "search_skills" in payload["tools"]


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

    assert cli.main(["skills", "adapters", "list", "--include-planned", "--json"]) == 0
    adapters = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in adapters["adapters"]}
    assert {"codex", "cursor", "antigravity", "pi"} <= ids

    assert cli.main(["skills", "adapters", "show", "cursor", "--json"]) == 0
    cursor = json.loads(capsys.readouterr().out)
    assert cursor["status"] == "planned"
