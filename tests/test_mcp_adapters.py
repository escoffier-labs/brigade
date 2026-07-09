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


def _remote_with_headers(name="docs"):
    return CanonicalServer(
        name=name,
        transport="http",
        url="https://mcp.example.com/v1",
        headers={"Authorization": {"ref": "TOKEN"}},
    )


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


def test_grok_write_preserves_foreign_tables_and_comments():
    existing = '# my grok config\nmodel = "grok-4"\n\n[model_providers.xai]\nname = "xAI"\n'
    adapter = A.ADAPTERS["grok"]
    server_dict = adapter.to_provider(_stdio())
    out = adapter.write_file(existing, {"github": server_dict}, set())
    assert 'model = "grok-4"' in out
    assert "[model_providers.xai]" in out
    assert "[mcp_servers.github]" in out
    from brigade import toml_compat

    parsed = toml_compat.loads(out)
    assert parsed["mcp_servers"]["github"]["command"] == "npx"
    assert parsed["model"] == "grok-4"


def test_codex_read_then_remove():
    adapter = A.ADAPTERS["codex"]
    text = adapter.write_file(None, {"github": adapter.to_provider(_stdio())}, set())
    assert adapter.read_file(text)["github"]["command"] == "npx"
    pruned = adapter.write_file(text, {}, {"github"})
    assert "mcp_servers" not in toml_loads(pruned)


def test_grok_read_then_remove():
    adapter = A.ADAPTERS["grok"]
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


def test_claude_remote_roundtrip_is_idempotent():
    """A remote server projected then re-read yields the same provider dict."""
    adapter = A.ADAPTERS["claude"]
    projected = adapter.to_provider(_remote())
    back, _ = adapter.from_provider("docs", projected)
    reprojected = adapter.to_provider(back)
    assert projected == reprojected


def test_codex_remote_roundtrip_is_idempotent():
    """BUG 1: codex remote tables must render type/transport so a re-sync is a no-op.

    The projected dict (from to_provider) carries ``type``; the table written to disk
    must round-trip back through read_file to the same dict, else mcp_cmd flags the
    server "conflicted" on every sync.
    """
    adapter = A.ADAPTERS["codex"]
    projected = adapter.to_provider(_remote())
    assert projected["type"] == "http"
    text = adapter.write_file(None, {"docs": projected}, set())
    round_tripped = adapter.read_file(text)["docs"]
    assert round_tripped == projected


def test_grok_remote_roundtrip_is_idempotent():
    """Grok remote tables must render type/transport so a re-sync is a no-op."""
    adapter = A.ADAPTERS["grok"]
    projected = adapter.to_provider(_remote())
    assert projected["type"] == "http"
    text = adapter.write_file(None, {"docs": projected}, set())
    round_tripped = adapter.read_file(text)["docs"]
    assert round_tripped == projected


def test_codex_dotted_server_name_is_idempotent_and_valid_toml():
    """BUG 2: a dotted/quoted server name synced twice stays one valid table."""
    adapter = A.ADAPTERS["codex"]
    server = CanonicalServer(
        name="io.github.example",
        transport="stdio",
        command="npx",
        args=("-y", "@mcp/server"),
        timeout=60,
    )
    projected = adapter.to_provider(server)
    first = adapter.write_file(None, {"io.github.example": projected}, set())
    # Re-syncing the same server must not append a duplicate table.
    second = adapter.write_file(first, {"io.github.example": projected}, set())
    assert second.count("[mcp_servers.") == 1
    parsed = toml_loads(second)
    assert parsed["mcp_servers"]["io.github.example"]["command"] == "npx"
    # And it can be removed.
    pruned = adapter.write_file(second, {}, {"io.github.example"})
    assert "mcp_servers" not in toml_loads(pruned)


def test_grok_dotted_server_name_is_idempotent_and_valid_toml():
    """A dotted/quoted Grok server name synced twice stays one valid table."""
    adapter = A.ADAPTERS["grok"]
    server = CanonicalServer(
        name="io.github.example",
        transport="stdio",
        command="npx",
        args=("-y", "@mcp/server"),
        timeout=60,
    )
    projected = adapter.to_provider(server)
    first = adapter.write_file(None, {"io.github.example": projected}, set())
    second = adapter.write_file(first, {"io.github.example": projected}, set())
    assert second.count("[mcp_servers.") == 1
    parsed = toml_loads(second)
    assert parsed["mcp_servers"]["io.github.example"]["command"] == "npx"
    pruned = adapter.write_file(second, {}, {"io.github.example"})
    assert "mcp_servers" not in toml_loads(pruned)


def test_codex_remote_headers_roundtrip():
    """BUG 3: codex remote tables must render and parse Authorization headers."""
    adapter = A.ADAPTERS["codex"]
    projected = adapter.to_provider(_remote_with_headers())
    assert projected["headers"] == {"Authorization": "${TOKEN}"}
    text = adapter.write_file(None, {"docs": projected}, set())
    round_tripped = adapter.read_file(text)["docs"]
    assert round_tripped == projected
    back, _ = adapter.from_provider("docs", round_tripped)
    assert back.headers == {"Authorization": {"ref": "TOKEN"}}


def test_grok_remote_headers_roundtrip():
    """Grok remote tables must render and parse Authorization headers."""
    adapter = A.ADAPTERS["grok"]
    projected = adapter.to_provider(_remote_with_headers())
    assert projected["headers"] == {"Authorization": "${TOKEN}"}
    text = adapter.write_file(None, {"docs": projected}, set())
    round_tripped = adapter.read_file(text)["docs"]
    assert round_tripped == projected
    back, _ = adapter.from_provider("docs", round_tripped)
    assert back.headers == {"Authorization": {"ref": "TOKEN"}}


def test_vscode_remote_headers_roundtrip():
    """BUG 3: vscode remote must emit and parse headers (as ${input:VAR})."""
    adapter = A.ADAPTERS["vscode"]
    d = adapter.to_provider(_remote_with_headers())
    assert d["headers"] == {"Authorization": "${input:TOKEN}"}
    back, _ = adapter.from_provider("docs", d)
    assert back.headers == {"Authorization": {"ref": "TOKEN"}}


def test_opencode_remote_headers_roundtrip():
    """BUG 3: opencode remote must emit and parse headers."""
    adapter = A.ADAPTERS["opencode"]
    d = adapter.to_provider(_remote_with_headers())
    assert d["headers"] == {"Authorization": "${TOKEN}"}
    back, _ = adapter.from_provider("docs", d)
    assert back.headers == {"Authorization": {"ref": "TOKEN"}}


def test_openclaw_remote_headers_roundtrip():
    """BUG 3: openclaw remote must emit and parse headers."""
    adapter = A.ADAPTERS["openclaw"]
    d = adapter.to_provider(_remote_with_headers())
    assert d["headers"] == {"Authorization": "${TOKEN}"}
    back, _ = adapter.from_provider("docs", d)
    assert back.headers == {"Authorization": {"ref": "TOKEN"}}


def test_codex_empty_args_roundtrip_is_idempotent():
    """#181: empty args must not fingerprint-conflict after codex TOML write/read.

    to_provider used to emit args=[] while _codex_render_table omits empty arrays,
    so live fingerprint never matched projected_fingerprint.
    """
    adapter = A.ADAPTERS["codex"]
    server = CanonicalServer(name="dossier", transport="stdio", command="/usr/bin/dossier-mcp")
    projected = adapter.to_provider(server)
    assert "args" not in projected
    text = adapter.write_file(None, {"dossier": projected}, set())
    round_tripped = adapter.read_file(text)["dossier"]
    assert round_tripped == projected
    # user-scope alias shares the same transforms
    user = A.ADAPTERS["codex-user"]
    assert user.to_provider(server) == projected


def test_openclaw_url_only_never_stdio():
    """#182: url-only import must coerce to http/sse, never stdio+url."""
    adapter = A.ADAPTERS["openclaw"]
    # Broken source shapes seen in the wild
    for raw in (
        {"url": "http://127.0.0.1:8000/mcp", "transport": "stdio"},
        {"url": "http://127.0.0.1:8000/mcp"},
        {"command": "https://mcp.example.com/v1"},
    ):
        back, _ = adapter.from_provider("x", raw)
        assert back.is_remote, raw
        assert back.transport in ("http", "sse"), raw
        assert back.url
        assert back.command is None
        projected = adapter.to_provider(back)
        assert "url" in projected
        assert projected.get("transport") in ("http", "sse")


def test_mcpservers_url_only_coerces_http():
    """#182: common mcpServers from_provider coerces url-only + bogus type."""
    adapter = A.ADAPTERS["claude"]
    back, _ = adapter.from_provider("x", {"url": "http://127.0.0.1:8000/mcp", "type": "stdio"})
    assert back.transport == "http"
    assert back.url == "http://127.0.0.1:8000/mcp"


def test_grok_adapters_share_codex_toml_shape():
    """#183: Grok project + user adapters reuse Codex-like mcp_servers TOML."""
    for name, path, user_scope in (
        ("grok", ".grok/config.toml", False),
        ("grok-user", "~/.grok/config.toml", True),
    ):
        adapter = A.ADAPTERS[name]
        assert adapter.path == path
        assert adapter.user_scope is user_scope
        assert adapter.fmt == "toml"
        assert adapter.top_key == "mcp_servers"
    server = CanonicalServer(name="graphtrail", transport="stdio", command="/usr/bin/graphtrail-mcp")
    for name in ("grok", "grok-user", "codex"):
        projected = A.ADAPTERS[name].to_provider(server)
        text = A.ADAPTERS[name].write_file(None, {"graphtrail": projected}, set())
        assert "[mcp_servers.graphtrail]" in text
        assert A.ADAPTERS[name].read_file(text)["graphtrail"] == projected


def test_hermes_stdio_and_remote_roundtrip_preserves_siblings():
    adapter = A.ADAPTERS["hermes"]
    assert adapter.user_scope is True
    assert adapter.path == "~/.hermes/config.yaml"
    existing = "model: gpt-test\nplugins:\n  enabled:\n    - orca-status\n\nmcp_servers: {}\n"
    stdio = adapter.to_provider(_stdio())
    remote = adapter.to_provider(_remote())
    text = adapter.write_file(existing, {"github": stdio, "docs": remote}, set())
    assert "model: gpt-test" in text
    assert "orca-status" in text
    assert "mcp_servers:" in text
    live = adapter.read_file(text)
    assert live["github"]["command"] == "npx"
    assert live["github"]["args"] == ["-y", "@mcp/server-github"]
    assert live["docs"]["url"] == "https://mcp.example.com/v1"
    # second write is idempotent for projected shape
    again = adapter.write_file(text, {"github": stdio, "docs": remote}, set())
    assert adapter.read_file(again) == live


def test_hermes_from_provider_url_only_is_http():
    back, _ = A.ADAPTERS["hermes"].from_provider("x", {"url": "http://127.0.0.1:8000/mcp", "transport": "stdio"})
    assert back.transport == "http"
    assert back.url == "http://127.0.0.1:8000/mcp"


def test_server_dict_roundtrip():
    s = _stdio()
    raw = A.server_to_dict(s)
    rebuilt, warnings = A.server_from_dict("github", raw)
    assert rebuilt == s
    assert warnings == []
