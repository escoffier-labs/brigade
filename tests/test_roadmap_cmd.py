import json
from pathlib import Path

from brigade import cli
from brigade import roadmap_cmd


def test_roadmap_audit_classifies_stale_sections_and_command_mismatch(tmp_path):
    (tmp_path / "ROADMAP.md").write_text(
        "# Roadmap\n\n"
        "## Current Phase\n"
        "- Build command registry. Status: implemented\n"
        "- Wire daily loop. Status: started\n"
        "- Add closeout map. Status: started\n"
        "- Keep future work visible. Status: planned\n"
    )
    (tmp_path / "README.md").write_text(
        "Run `brigade roadmap audit` and `brigade imaginary command`.\n"
    )
    payload = roadmap_cmd.audit_payload(tmp_path)

    assert payload["roadmap"]["status_counts"]["implemented"] == 1
    assert payload["roadmap"]["status_counts"]["started"] == 2
    assert payload["roadmap"]["status_counts"]["planned"] == 1
    assert any(check["name"] == "roadmap_stale_phase_section" for check in payload["issues"])
    assert "brigade imaginary command" in payload["missing_cli_commands"]
    assert "brigade roadmap audit" not in payload["missing_cli_commands"]


def test_roadmap_audit_normalizes_parameterized_and_parent_commands(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text(
        "Use `brigade tools show simplify`, `brigade center`, and `brigade run review this repo`.\n"
        "A prose sentence says brigade makes no network calls by default.\n"
        "A refactor note mentions the brigade.io helper module.\n"
        "```bash\n"
        "brigade chat surfaces show surface-one --json\n"
        "```\n"
    )

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert "brigade tools show simplify" in payload["documented_commands"]
    assert "brigade tools show" in payload["normalized_documented_commands"]
    assert "brigade center" in payload["normalized_documented_commands"]
    assert "brigade run" in payload["normalized_documented_commands"]
    assert "brigade chat surfaces show" in payload["normalized_documented_commands"]
    assert "brigade makes no network calls" not in payload["documented_commands"]
    assert "brigade io" not in payload["documented_commands"]
    assert "brigade tools show simplify" not in payload["missing_cli_commands"]
    assert "brigade chat surfaces show surface-one" not in payload["missing_cli_commands"]


def test_roadmap_audit_json_and_imports(tmp_path, capsys):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text("Run `brigade missing localcommand`.\n")

    assert roadmap_cmd.audit(target=tmp_path, json_output=True, import_issues=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["imported"] >= 1
    assert (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").is_file()
    assert all(item["source"] == "roadmap-audit" for item in _read_imports(tmp_path))


def test_roadmap_audit_includes_deferred_ownership_records(tmp_path, capsys):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "roadmap-archive.md").write_text("# Roadmap Archive\n")
    payload = roadmap_cmd.audit_payload(tmp_path)

    deferred = {item["id"]: item for item in payload["deferred_items"]}
    assert payload["active_queue_item_count"] == 0
    assert payload["deferred_item_count"] == 0
    assert "cross-producer-provenance-audit" not in deferred
    archived = {item["id"]: item for item in payload["archived_items"]}
    item = archived["cross-producer-provenance-audit"]
    assert item["owner"] == "work"
    assert item["subsystem"] == "work-inbox"
    assert item["archive_reason"]
    assert item["closed_phase"] == 64
    assert archived["stale-issue-repair-imports"]["closed_phase"] == 80
    assert all(check["status"] == "ok" for check in payload["checks"] if check["name"].startswith("roadmap_deferred_"))

    assert roadmap_cmd.audit(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "deferred_items:" in out


def test_roadmap_archive_reports_closed_items(tmp_path, capsys):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "roadmap-archive.md").write_text("# Roadmap Archive\n")

    assert roadmap_cmd.archive(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    archived = {item["id"]: item for item in payload["archived_items"]}
    assert archived["repo-shareable-workflow-rule-templates"]["status"] == "implemented"
    assert archived["context-harness-destination-writes"]["status"] == "carried-forward"
    assert archived["repo-shareable-workflow-rule-templates"]["archive_reason"]
    assert payload["archived_item_count"] >= 13
    assert payload["issue_count"] == 0


def test_roadmap_patterns_cover_neutral_families_and_decisions(capsys, tmp_path):
    assert roadmap_cmd.patterns(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    families = {item["family"] for item in payload["families"]}
    assert "command-harness patterns" in families
    assert "cross-harness skill/plugin sync patterns" in families
    assert "self-learning" in families

    decisions = {item["decision"] for item in payload["decisions"]}
    assert {"bake-in", "integrate", "catalog-only", "move-candidate", "leave-alone"} <= decisions
    assert payload["issue_count"] == 0
    assert all(check["status"] == "ok" for check in payload["checks"])


def test_roadmap_commands_reports_top_level_documentation(capsys, tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text(
        "Use `brigade roadmap audit`, `brigade roadmap commands`, `brigade tools show simplify`, and `brigade work brief`.\n"
    )

    assert roadmap_cmd.commands(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    groups = {item["command"]: item for item in payload["groups"]}
    assert groups["brigade roadmap"]["documented"] is True
    assert groups["brigade tools"]["documented"] is True
    assert groups["brigade work"]["documented"] is True
    assert groups["brigade release"]["documented"] is False
    assert "brigade release" in payload["checks"][0]["commands"]


def test_phase_61_100_plan_lists_forty_public_safe_phases():
    plan = (Path(__file__).parents[1] / "docs" / "phase-61-100-plan.md").read_text()

    assert plan.count("### Phase ") == 40
    for phase in range(61, 101):
        assert f"### Phase {phase}:" in plan
    assert "private repo names" in plan


def test_roadmap_cli_dispatch(tmp_path, monkeypatch):
    seen = []

    def fake_audit(**kwargs):
        seen.append(("audit", kwargs))
        return 0

    def fake_patterns(**kwargs):
        seen.append(("patterns", kwargs))
        return 0

    def fake_archive(**kwargs):
        seen.append(("archive", kwargs))
        return 0

    def fake_commands(**kwargs):
        seen.append(("commands", kwargs))
        return 0

    monkeypatch.setattr(roadmap_cmd, "audit", fake_audit)
    monkeypatch.setattr(roadmap_cmd, "patterns", fake_patterns)
    monkeypatch.setattr(roadmap_cmd, "archive", fake_archive)
    monkeypatch.setattr(roadmap_cmd, "commands", fake_commands)

    assert cli.main(["roadmap", "audit", "--target", str(tmp_path), "--json", "--import-issues"]) == 0
    assert cli.main(["roadmap", "patterns", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["roadmap", "archive", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--json"]) == 0

    assert seen == [
        ("audit", {"target": tmp_path, "json_output": True, "import_issues": True}),
        ("patterns", {"target": tmp_path, "json_output": True}),
        ("archive", {"target": tmp_path, "json_output": True}),
        ("commands", {"target": tmp_path, "json_output": True, "write_inventory": False, "check_inventory": False}),
    ]


def _read_imports(path):
    return [
        json.loads(line)
        for line in (path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()
        if line.strip()
    ]
