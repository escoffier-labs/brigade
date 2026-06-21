"""Per-adapter transform round-trips and format-specific shape assertions."""

from __future__ import annotations

import json

from brigade import mcp_adapters as A
from brigade.mcp_adapters import CanonicalServer


def _stdio(name="github"):
    return CanonicalServer(
        name=name,
        transport="stdio",
        command="npx",
        args=("-y", "@mcp/server-github"),
        env={"GITHUB_TOKEN": {"ref": "GITHUB_TOKEN"}},
        timeout=60,
    )


def _remote(name="docs"):
    return CanonicalServer(name=name, transport="http", url="https://mcp.example.com/v1")


def test_claude_cursor_share_mcpservers_shape():
    for harness in ("claude", "cursor"):
        adapter = A.ADAPTERS[harness]
        d = adapter.to_provider(_stdio())
        assert d["command"] == "npx"
        assert d["args"] == ["-y", "@mcp/server-github"]
        assert d["env"] == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}  # ref, never the literal
        back, _ = adapter.from_provider("github", d)
        assert back.command == "npx"
        assert back.env == {"GITHUB_TOKEN": {"ref": "GITHUB_TOKEN"}}


def test_claude_remote_uses_url_and_type():
    d = A.ADAPTERS["claude"].to_provider(_remote())
    assert d["url"] == "https://mcp.example.com/v1"
    assert d["type"] == "http"


def test_antigravity_uses_serverurl_not_url():
    d = A.ADAPTERS["antigravity"].to_provider(_remote())
    assert d.get("serverUrl") == "https://mcp.example.com/v1"
    assert "url" not in d
    assert A.ADAPTERS["antigravity"].user_scope is True


def test_vscode_uses_servers_key_and_input_refs():
    adapter = A.ADAPTERS["vscode"]
    assert adapter.top_key == "servers"
    d = adapter.to_provider(_stdio())
    assert d["type"] == "stdio"
    assert d["env"] == {"GITHUB_TOKEN": "${input:GITHUB_TOKEN}"}
    text = adapter.write_file(None, {"github": d}, set())
    doc = json.loads(text)
    assert "github" in doc["servers"]
    ids = {i["id"] for i in doc["inputs"]}
    assert "GITHUB_TOKEN" in ids
    assert all(i["password"] for i in doc["inputs"])


def test_opencode_command_array_and_environment_key():
    adapter = A.ADAPTERS["opencode"]
    d = adapter.to_provider(_stdio())
    assert d["type"] == "local"
    assert d["command"] == ["npx", "-y", "@mcp/server-github"]
    assert d["environment"] == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
    back, _ = adapter.from_provider("github", d)
    assert back.command == "npx"
    assert back.args == ("-y", "@mcp/server-github")


def test_codex_write_preserves_foreign_tables_and_comments():
    existing = '# my codex config\nmodel = "gpt-5.5"\n\n[model_providers.openai]\nname = "OpenAI"\n'
    adapter = A.ADAPTERS["codex"]
    server_dict = adapter.to_provider(_stdio())
    out = adapter.write_file(existing, {"github": server_dict}, set())
    assert 'model = "gpt-5.5"' in out
    assert "[model_providers.openai]" in out
    assert "[mcp_servers.github]" in out
    # round-trips through the reader and is byte-valid TOML
    from brigade import toml_compat

    parsed = toml_compat.loads(out)
    assert parsed["mcp_servers"]["github"]["command"] == "npx"
    assert parsed["model"] == "gpt-5.5"


def test_codex_read_then_remove():
    adapter = A.ADAPTERS["codex"]
    text = adapter.write_file(None, {"github": adapter.to_provider(_stdio())}, set())
    assert adapter.read_file(text)["github"]["command"] == "npx"
    pruned = adapter.write_file(text, {}, {"github"})
    assert "mcp_servers" not in toml_loads(pruned)


def toml_loads(text):
    from brigade import toml_compat

    return toml_compat.loads(text)


def test_json_merge_preserves_foreign_server():
    adapter = A.ADAPTERS["claude"]
    seed = json.dumps({"mcpServers": {"local": {"command": "mylocal"}}})
    out = adapter.write_file(seed, {"github": adapter.to_provider(_stdio())}, set())
    doc = json.loads(out)
    assert doc["mcpServers"]["local"] == {"command": "mylocal"}  # untouched
    assert "github" in doc["mcpServers"]


def test_validate_flags_missing_command_high_risk_and_timeout():
    missing = CanonicalServer(name="x", transport="stdio", command=None, timeout=5)
    assert any(sev == "error" and "command" in msg for sev, msg in A.validate_server(missing))
    risky = CanonicalServer(name="y", transport="stdio", command="bash -c whatever", timeout=5)
    assert any(sev == "error" and "high risk" in msg for sev, msg in A.validate_server(risky))
    no_timeout = CanonicalServer(name="z", transport="stdio", command="npx", timeout=None)
    assert any(sev == "warn" and "timeout" in msg for sev, msg in A.validate_server(no_timeout))


def test_validate_flags_inlined_secret():
    s = CanonicalServer(name="s", transport="stdio", command="npx", env={"API_KEY": {"literal": "sk-123"}}, timeout=5)
    assert any("inlined secret" in msg for _, msg in A.validate_server(s))


def test_import_demotes_literal_secret_to_ref():
    raw = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_realsecret", "HOME_DIR": "/tmp"}}
    server, demoted = A.ADAPTERS["claude"].from_provider("gh", raw)
    assert server.env["GITHUB_TOKEN"] == {"ref": "GITHUB_TOKEN"}  # value dropped
    assert server.env["HOME_DIR"] == {"literal": "/tmp"}  # non-secret literal kept
    assert demoted == ["GITHUB_TOKEN"]


def test_server_dict_roundtrip():
    s = _stdio()
    raw = A.server_to_dict(s)
    rebuilt, warnings = A.server_from_dict("github", raw)
    assert rebuilt == s
    assert warnings == []
