import json

from brigade import cli, roadmap_cmd


def test_roadmap_commands_writes_and_checks_inventory(tmp_path, capsys):
    (tmp_path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    inventory = tmp_path / "docs" / "command-inventory.md"

    assert payload["inventory_current"] is True
    assert inventory.is_file()
    text = inventory.read_text()
    assert "# Brigade Command Inventory" in text
    assert "`brigade roadmap audit`" in text
    assert "private-root" not in text
    assert "private-repo" not in text.casefold()

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--check", "--json"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["inventory_current"] is True


def test_roadmap_commands_check_reports_stale_inventory(tmp_path, capsys):
    (tmp_path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    inventory = tmp_path / "docs" / "command-inventory.md"
    inventory.parent.mkdir()
    inventory.write_text("# stale\n")

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--check", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["inventory_current"] is False
    assert any(check["name"] == "roadmap_command_inventory_current" for check in payload["checks"])


def test_roadmap_audit_includes_command_inventory_drift(tmp_path):
    # The command-inventory drift check only applies to the repo that owns the
    # brigade CLI, so mark this target as brigade-cli.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "brigade-cli"\n')
    (tmp_path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert any(check["name"] == "roadmap_command_inventory_current" for check in payload["checks"])
    assert any(issue["name"] == "roadmap_command_inventory_current" for issue in payload["issues"])


def test_roadmap_audit_skips_command_catalog_checks_for_consumer_repo(tmp_path):
    # A repo that merely consumes brigade (no brigade-cli pyproject) must not be
    # audited against brigade's own command surface.
    (tmp_path / "README.md").write_text("Use `brigade work verify run`.\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    names = {check["name"] for check in payload["checks"]}
    assert "roadmap_cli_command_missing_docs" not in names
    assert "roadmap_command_inventory_current" not in names
    # The universal "documented a command that does not exist" check still runs.
    assert "roadmap_documented_command_missing_cli" in names
    assert payload["missing_documented_commands"] == []


def test_roadmap_commands_cli_dispatch(tmp_path, monkeypatch):
    seen = []

    def fake_commands(**kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(roadmap_cmd, "commands", fake_commands)

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--json", "--write", "--check"]) == 0

    assert seen == [
        {
            "target": tmp_path,
            "json_output": True,
            "write_inventory": True,
            "check_inventory": True,
        }
    ]
