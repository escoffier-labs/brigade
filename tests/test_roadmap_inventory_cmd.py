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
    (tmp_path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")

    payload = roadmap_cmd.audit_payload(tmp_path)

    assert any(check["name"] == "roadmap_command_inventory_current" for check in payload["checks"])
    assert any(issue["name"] == "roadmap_command_inventory_current" for issue in payload["issues"])


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
