"""User-scope adapters (codex-user, claude-user, openclaw), keep-secrets, nested merge."""

from __future__ import annotations

import json

from brigade import mcp_adapters as A
from brigade.mcp_adapters import CanonicalServer


def _stdio():
    return CanonicalServer(
        name="jellyfin",
        transport="stdio",
        command="node",
        args=("server.js",),
        env={"JELLYFIN_API_KEY": {"literal": "realkey32chars"}},
    )


def test_user_scope_adapters_registered():
    for name in ("codex-user", "claude-user", "openclaw", "grok-user"):
        a = A.ADAPTERS[name]
        assert a.user_scope is True
        assert a.path.startswith("~")
    assert {"codex-user", "claude-user", "openclaw", "grok-user"} <= set(A.MCP_TARGETS)


def test_codex_user_is_toml_codex_shape():
    a = A.ADAPTERS["codex-user"]
    assert a.fmt == "toml" and a.path == "~/.codex/config.toml"


def test_openclaw_stdio_has_no_type_field():
    d = A.ADAPTERS["openclaw"].to_provider(_stdio())
    assert "type" not in d
    assert d["command"] == "node"
    assert d["env"]["JELLYFIN_API_KEY"] == "realkey32chars"  # literal preserved on emit


def test_openclaw_remote_uses_url_and_transport():
    remote = CanonicalServer(name="x", transport="http", url="https://x.example/mcp")
    d = A.ADAPTERS["openclaw"].to_provider(remote)
    assert d == {"url": "https://x.example/mcp", "transport": "http"}
    back, _ = A.ADAPTERS["openclaw"].from_provider("x", d)
    assert back.url == "https://x.example/mcp" and back.transport == "http"


def test_openclaw_nested_merge_preserves_siblings():
    adapter = A.ADAPTERS["openclaw"]
    existing = json.dumps(
        {
            "auth": {"token": "keep-me"},
            "mcp": {"servers": {"x": {"url": "https://x/mcp", "transport": "http"}}, "enabled": True},
            "models": {"main": "gpt"},
        }
    )
    out = adapter.write_file(existing, {"jellyfin": adapter.to_provider(_stdio())}, set())
    doc = json.loads(out)
    # foreign top-level keys and the mcp sibling key survive
    assert doc["auth"] == {"token": "keep-me"}
    assert doc["models"] == {"main": "gpt"}
    assert doc["mcp"]["enabled"] is True
    # both the pre-existing remote and the new stdio server are present
    assert "x" in doc["mcp"]["servers"]
    assert doc["mcp"]["servers"]["jellyfin"]["command"] == "node"


def test_openclaw_read_nested():
    adapter = A.ADAPTERS["openclaw"]
    text = json.dumps({"mcp": {"servers": {"jellyfin": {"command": "node", "args": ["server.js"]}}}})
    assert adapter.read_file(text)["jellyfin"]["command"] == "node"


def test_keep_secrets_import_preserves_literal():
    raw = {"command": "node", "env": {"JELLYFIN_API_KEY": "secret32chars", "JELLYFIN_URL": "http://host"}}
    server, demoted = A.ADAPTERS["openclaw"].from_provider("jellyfin", raw, keep_secrets=True)
    assert server.env["JELLYFIN_API_KEY"] == {"literal": "secret32chars"}  # kept, not demoted
    assert demoted == []


def test_default_import_still_demotes_secret():
    raw = {"command": "node", "env": {"JELLYFIN_API_KEY": "secret32chars"}}
    server, demoted = A.ADAPTERS["openclaw"].from_provider("jellyfin", raw)
    assert server.env["JELLYFIN_API_KEY"] == {"ref": "JELLYFIN_API_KEY"}
    assert demoted == ["JELLYFIN_API_KEY"]


def test_claude_user_write_preserves_large_foreign_doc():
    adapter = A.ADAPTERS["claude-user"]
    # mimic ~/.claude.json: lots of unrelated state plus a user mcpServers map
    existing = json.dumps(
        {
            "numStartups": 42,
            "projects": {"/repo": {"history": [1, 2, 3]}},
            "mcpServers": {"wazuh": {"command": "wazuh-mcp"}},
            "oauthAccount": {"emailAddress": "x@y.z"},
        }
    )
    server = CanonicalServer(name="github", transport="stdio", command="npx", args=("-y", "pkg"), timeout=60)
    out = adapter.write_file(existing, {"github": adapter.to_provider(server)}, set())
    doc = json.loads(out)
    assert doc["numStartups"] == 42
    assert doc["projects"] == {"/repo": {"history": [1, 2, 3]}}
    assert doc["oauthAccount"] == {"emailAddress": "x@y.z"}
    assert doc["mcpServers"]["wazuh"] == {"command": "wazuh-mcp"}  # foreign server preserved
    assert doc["mcpServers"]["github"]["command"] == "npx"  # new server added
    # key order preserved (sort_keys=False): numStartups stays first
    assert list(doc) == ["numStartups", "projects", "mcpServers", "oauthAccount"]
