import json

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


def test_roadmap_audit_json_and_imports(tmp_path, capsys):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text("Run `brigade missing localcommand`.\n")

    assert roadmap_cmd.audit(target=tmp_path, json_output=True, import_issues=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["imported"] >= 1
    assert (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").is_file()
    assert all(item["source"] == "roadmap-audit" for item in _read_imports(tmp_path))


def test_roadmap_patterns_cover_neutral_families_and_decisions(capsys, tmp_path):
    assert roadmap_cmd.patterns(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    families = {item["family"] for item in payload["families"]}
    assert "command-harness patterns" in families
    assert "cross-harness skill/plugin sync patterns" in families
    assert "self-learning" in families

    decisions = {item["decision"] for item in payload["decisions"]}
    assert {"bake-in", "integrate", "catalog-only", "move-candidate", "leave-alone"} <= decisions
    assert any(check["name"] == "pattern_missing_owner" for check in payload["checks"])
    assert any(check["name"] == "pattern_missing_tests" for check in payload["checks"])


def test_roadmap_cli_dispatch(tmp_path, monkeypatch):
    seen = []

    def fake_audit(**kwargs):
        seen.append(("audit", kwargs))
        return 0

    def fake_patterns(**kwargs):
        seen.append(("patterns", kwargs))
        return 0

    monkeypatch.setattr(roadmap_cmd, "audit", fake_audit)
    monkeypatch.setattr(roadmap_cmd, "patterns", fake_patterns)

    assert cli.main(["roadmap", "audit", "--target", str(tmp_path), "--json", "--import-issues"]) == 0
    assert cli.main(["roadmap", "patterns", "--target", str(tmp_path), "--json"]) == 0

    assert seen == [
        ("audit", {"target": tmp_path, "json_output": True, "import_issues": True}),
        ("patterns", {"target": tmp_path, "json_output": True}),
    ]


def _read_imports(path):
    return [
        json.loads(line)
        for line in (path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()
        if line.strip()
    ]
