import json
from pathlib import Path

import pytest

from brigade import cli
from brigade import roadmap_cmd


def test_read_text_reads_utf8_docs_when_locale_codec_is_cp1252(tmp_path, monkeypatch):
    """Regression for #752: Windows locale default (cp1252) must not decode repo docs.

    Forces the Path.read_text default codec to cp1252 (as on Windows) so this
    fails on Linux CI too if encoding=\"utf-8\" is dropped from _read_text.
    """
    path = tmp_path / "README.md"
    # U+2510 BOX DRAWINGS LIGHT DOWN AND LEFT — UTF-8 e2 94 90; byte 0x90 is
    # undefined in cp1252 and raises UnicodeDecodeError under that codec.
    box = "\u2510"
    path.write_bytes(f"Run `brigade work brief` near {box}\n".encode("utf-8"))

    real_read_text = Path.read_text

    def read_text_locale_default(self, *args, encoding=None, errors=None, **kwargs):
        if encoding is None:
            encoding = "cp1252"
        return real_read_text(self, *args, encoding=encoding, errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_locale_default)

    text = roadmap_cmd._read_text(path)
    assert box in text
    assert "brigade work brief" in text

    (tmp_path / "ROADMAP.md").write_bytes(b"# Roadmap\n")
    documented = roadmap_cmd._documented_brigade_commands(tmp_path)
    assert "brigade work brief" in documented


def test_read_text_returns_empty_on_undecodable_utf8(tmp_path):
    path = tmp_path / "broken.md"
    path.write_bytes(b"ok\xff\xfe not utf-8")
    assert roadmap_cmd._read_text(path) == ""


def test_target_owns_brigade_cli_ignores_malformed_pyproject(tmp_path):
    tmp_path.joinpath("pyproject.toml").write_bytes(b'name = "brigade-cli"\xff')
    assert roadmap_cmd._target_owns_brigade_cli(tmp_path) is False


def _write_versioned_roadmap(tmp_path: Path, *, headline: str, project_version: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "brigade-cli"\nversion = "{project_version}"\n')
    (tmp_path / "ROADMAP.md").write_text(f"# Roadmap\n\n## Where things stand\n\n{headline} is current.\n")


def test_roadmap_audit_version_current_when_headline_matches_minor(tmp_path):
    _write_versioned_roadmap(tmp_path, headline="**v0.27.x on main**", project_version="0.27.0")
    payload = roadmap_cmd.audit_payload(tmp_path)

    check = next(item for item in payload["checks"] if item["name"] == "roadmap_version_current")
    assert check["status"] == "ok"
    assert not any(item["name"] == "roadmap_version_current" for item in payload["issues"])
    assert not any(item["name"] == "roadmap_version_headline_unparseable" for item in payload["issues"])


def test_roadmap_audit_version_issue_when_headline_minor_behind(tmp_path):
    _write_versioned_roadmap(tmp_path, headline="**v0.26.x on main**", project_version="0.27.0")
    payload = roadmap_cmd.audit_payload(tmp_path)

    issue = next(item for item in payload["issues"] if item["name"] == "roadmap_version_current")
    assert issue["status"] == "fail"
    assert "0.26" in issue["detail"]
    assert "0.27" in issue["detail"]
    assert not any(item["name"] == "roadmap_version_headline_unparseable" for item in payload["issues"])


def test_roadmap_audit_version_fail_closed_when_headline_unparseable(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "brigade-cli"\nversion = "0.27.0"\n')
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n\n## Current Phase\n- Keep going.\n")
    payload = roadmap_cmd.audit_payload(tmp_path)

    issue = next(item for item in payload["issues"] if item["name"] == "roadmap_version_headline_unparseable")
    assert issue["status"] == "fail"
    assert not any(item["name"] == "roadmap_version_current" for item in payload["issues"])


def test_roadmap_audit_version_ok_when_patch_differs(tmp_path):
    _write_versioned_roadmap(tmp_path, headline="**v0.27.x on main**", project_version="0.27.5")
    payload = roadmap_cmd.audit_payload(tmp_path)

    check = next(item for item in payload["checks"] if item["name"] == "roadmap_version_current")
    assert check["status"] == "ok"
    assert not any(item["name"] == "roadmap_version_current" for item in payload["issues"])


def test_roadmap_audit_version_reports_ahead_without_claiming_a_match(tmp_path):
    _write_versioned_roadmap(tmp_path, headline="**v0.28.x on main**", project_version="0.27.0")
    payload = roadmap_cmd.audit_payload(tmp_path)

    check = next(item for item in payload["checks"] if item["name"] == "roadmap_version_current")
    assert check["status"] == "ok"
    assert "matches" not in check["detail"]
    assert "ahead of" in check["detail"]
    assert not any(item["name"] == "roadmap_version_current" for item in payload["issues"])


def test_roadmap_audit_skips_version_check_without_a_parseable_pyproject_version(tmp_path):
    """A ROADMAP.md-bearing repo that declares no project.version gets no version check.

    Regression for #1005: a Node repo with a ROADMAP.md must not carry a
    permanent roadmap_version_headline_unparseable issue in `brigade work brief`.
    """
    (tmp_path / "package.json").write_text('{"name": "node-thing", "version": "2.1.0"}\n')
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n\n## Now\n- Ship the thing.\n\n## Next\n- Ship more.\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    names = {item["name"] for item in payload["checks"]}
    assert "roadmap_version_current" not in names
    assert "roadmap_version_headline_unparseable" not in names
    assert not any(item["name"] in roadmap_cmd.ROADMAP_VERSION_CHECK_NAMES for item in payload["issues"])
    assert cli.main(["roadmap", "audit", "--target", str(tmp_path), "--check"]) == 0


def test_roadmap_audit_skips_version_check_when_pyproject_has_no_project_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["hatchling"]\n')
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n\n## Now\n- Ship the thing.\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    names = {item["name"] for item in payload["checks"]}
    assert not names & roadmap_cmd.ROADMAP_VERSION_CHECK_NAMES


def test_roadmap_audit_check_exits_nonzero_when_headline_lags(tmp_path):
    _write_versioned_roadmap(tmp_path, headline="**v0.26.x on main**", project_version="0.27.0")
    assert cli.main(["roadmap", "audit", "--target", str(tmp_path), "--check"]) == 1


def test_roadmap_audit_classifies_stale_sections_and_command_mismatch(tmp_path):
    (tmp_path / "ROADMAP.md").write_text(
        "# Roadmap\n\n"
        "## Current Phase\n"
        "- Build command registry. Status: implemented\n"
        "- Wire daily loop. Status: started\n"
        "- Add closeout map. Status: started\n"
        "- Keep future work visible. Status: planned\n"
    )
    (tmp_path / "README.md").write_text("Run `brigade roadmap audit` and `brigade imaginary command`.\n")
    payload = roadmap_cmd.audit_payload(tmp_path)

    assert payload["roadmap"]["status_counts"]["implemented"] == 1
    assert payload["roadmap"]["status_counts"]["started"] == 2
    assert payload["roadmap"]["status_counts"]["planned"] == 1
    assert any(check["name"] == "roadmap_stale_phase_section" for check in payload["issues"])
    assert "brigade imaginary command" in payload["missing_cli_commands"]
    assert "brigade roadmap audit" not in payload["missing_cli_commands"]


def test_commands_from_text_requires_brigade_as_first_token():
    text = (
        "A concurrent `brigade work import` is real.\n"
        "Prose between backticks: `) keeps the user-level brigade directory and outside the workspace`.\n"
    )
    commands = roadmap_cmd._commands_from_text(text)
    assert "brigade work import" in commands
    assert "brigade directory and outside the workspace" not in commands


def test_apply_command_alias_rewrites_run_cloud_and_fragments():
    assert roadmap_cmd._apply_command_alias("brigade run-cloud status") == "brigade run cloud status"
    assert roadmap_cmd._apply_command_alias("brigade openclaw-fragments") == "brigade harness fragments"
    assert roadmap_cmd._apply_command_alias("brigade hermes-fragments") == "brigade harness fragments"
    assert roadmap_cmd._apply_command_alias("brigade run cloud status") == "brigade run cloud status"


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


def test_roadmap_audit_ignores_rejected_brigade_components_alternative(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text(
        "## Command Alternatives (Scouting)\n\n"
        "### Alternative A — Top-level `brigade setup` (recommended)\n\n"
        "Add `brigade setup` as a first-class top-level command.\n\n"
        "### Alternative C — `brigade components` command group\n\n"
        "Expose `brigade components install`, `status`, and `rollback` backed by the same engine.\n\n"
        "Tradeoffs: adds CLI surface area before the current phase needs it.\n\n"
        "**Recommendation:** Alternative A.\n"
    )

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert "brigade setup" in payload["documented_commands"]
    assert not any(command.startswith("brigade components") for command in payload["documented_commands"])
    assert not any(command.startswith("brigade components") for command in payload["missing_cli_commands"])


def test_roadmap_audit_rejected_section_does_not_poison_later_sibling_headings(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text(
        "## Rejected design ideas\n\n"
        "### Alternative B — discarded `brigade junk` path\n\n"
        "This section names a rejected command.\n\n"
        "## Documented commands\n\n"
        "Run `brigade doctor` for health checks.\n"
    )

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert "brigade doctor" in payload["documented_commands"]
    assert "brigade junk" not in payload["documented_commands"]


def test_roadmap_audit_accepts_alternative_configuration_heading(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text(
        "## Alternative configuration\n\nUse `brigade doctor` when wiring optional settings.\n"
    )

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert "brigade doctor" in payload["documented_commands"]
    assert "brigade doctor" not in payload["missing_cli_commands"]


def test_roadmap_audit_extracts_inline_backtick_commands_inside_fenced_blocks(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    (tmp_path / "README.md").write_text("```bash\nprefix `brigade doctor` suffix\n```\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert "brigade doctor" in payload["documented_commands"]
    assert "brigade doctor" not in payload["missing_cli_commands"]


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
        (
            "audit",
            {"target": tmp_path, "json_output": True, "import_issues": True, "check_version": False},
        ),
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


def test_command_inventory_marks_extras_gated_commands(tmp_path):
    payload = roadmap_cmd.command_contract_payload(tmp_path)
    inventory = payload["expected_inventory"]
    # gated top-level groups carry the marker on group and command lines
    assert "- `brigade release` (extras):" in inventory
    assert "- `brigade release candidate audit` (extras)" in inventory
    # the gated work subcommand is marked even though work itself is core
    assert "- `brigade work phases list` (extras)" in inventory
    # core commands stay unmarked
    assert "- `brigade mcp sync`\n" in inventory
    assert "- `brigade work brief`\n" in inventory
    # the marker is explained once in the header
    assert "brigade extras on" in inventory


def test_command_inventory_includes_memory_care_init_with_runbooks_flag(tmp_path, capsys):
    payload = roadmap_cmd.command_contract_payload(tmp_path)
    assert "brigade memory care init" in payload["cli_commands"]
    assert "- `brigade memory care init`" in payload["expected_inventory"]

    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "care", "init", "--help"])
    assert exc.value.code == 0
    assert "--with-runbooks" in capsys.readouterr().out
