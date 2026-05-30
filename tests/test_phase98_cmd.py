import json

from brigade import cli, release_cmd, security_cmd


def test_security_template_audit_covers_templates_docs_and_allowlisted_examples(tmp_path, capsys):
    workspace = tmp_path / "src" / "brigade" / "templates" / "workspace"
    harness = tmp_path / "src" / "brigade" / "templates" / "codex" / "skills" / "demo"
    docs = tmp_path / "docs"
    workspace.mkdir(parents=True)
    harness.mkdir(parents=True)
    docs.mkdir()
    (workspace / "AGENTS.md").write_text("Use <repo-path> and https://example.com/callback.\n")
    (harness / "SKILL.md").write_text("Set TOKEN=<token> and endpoint http://local" + "host:11434.\n")
    (docs / "template.md").write_text("Reference {{PROJECT_NAME}} and $SAFE_ENV_LABEL only.\n")

    assert cli.main(["security", "template-audit", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding_count"] == 0
    assert {
        "src/brigade/templates/workspace/AGENTS.md",
        "src/brigade/templates/codex/skills/demo/SKILL.md",
        "docs/template.md",
    } <= set(payload["scanned_files"])


def test_security_template_audit_flags_private_values_and_integrates_with_release(tmp_path, capsys):
    workspace = tmp_path / "src" / "brigade" / "templates" / "workspace"
    harness = tmp_path / "src" / "brigade" / "templates" / "hermes"
    docs = tmp_path / "docs"
    workspace.mkdir(parents=True)
    harness.mkdir(parents=True)
    docs.mkdir()
    (workspace / "AGENTS.md").write_text("Private path /home/private/operator should not ship.\n")
    (harness / "config.md").write_text("api_key=abcd1234abcd1234\n")
    (docs / "bad.md").write_text("Callback https://service.internal/hook should not ship.\n")

    assert security_cmd.template_audit(target=tmp_path, json_output=True) == 0
    audit = json.loads(capsys.readouterr().out)
    categories = {finding["category"] for finding in audit["findings"]}
    assert {"private-path", "secret", "private-url"} <= categories
    rendered = json.dumps(audit)
    assert "abcd1234" not in rendered
    assert "[REDACTED]" in rendered

    assert cli.main(["security", "doctor", "--target", str(tmp_path), "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["template_privacy"]["finding_count"] == 3
    assert any(check["name"] == "security_template_privacy" for check in doctor["checks"])

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["security"]["template_privacy"]["finding_count"] == 3
