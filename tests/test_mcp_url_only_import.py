"""Regression for #182: a url-only source entry must import as a remote transport,
never as stdio+url. Covers the adapter transform, the `mcp doctor` false-positive it
caused, and the import->sync round-trip that would otherwise emit an invalid server."""

from __future__ import annotations

import json

from brigade import mcp_adapters as A
from brigade import mcp_cmd


def _invalid_stdio_url(server: A.CanonicalServer) -> bool:
    """A server is malformed if it carries a URL but is still typed stdio."""
    return server.transport not in ("http", "sse") and bool(server.url)


# --------------------------------------------------------------------------- #
# (a) a url-only entry imports as a remote transport, never stdio
# --------------------------------------------------------------------------- #


def test_openclaw_url_only_remote_imports_as_remote_not_stdio():
    # A pure-URL OpenClaw remote: only a url, no command.
    server, _ = A.ADAPTERS["openclaw"].from_provider("remote", {"url": "http://127.0.0.1:8000/mcp"})
    assert server.transport == "http"
    assert server.url == "http://127.0.0.1:8000/mcp"
    assert server.command is None
    assert not _invalid_stdio_url(server)


def test_openclaw_url_only_ignores_stray_stdio_transport_field():
    # OpenClaw may stamp transport="stdio" on an entry that only has a url; the
    # importer must not trust it and emit an invalid stdio+url server.
    raw = {"url": "http://127.0.0.1:8000/mcp", "transport": "stdio"}
    server, _ = A.ADAPTERS["openclaw"].from_provider("remote", raw)
    assert server.transport == "http"
    assert server.url == "http://127.0.0.1:8000/mcp"
    assert server.command is None
    assert not _invalid_stdio_url(server)


def test_openclaw_url_in_command_field_imports_as_remote():
    # Some sources place the endpoint in `command`; a bare URL there is remote.
    server, _ = A.ADAPTERS["openclaw"].from_provider("remote", {"command": "http://127.0.0.1:8000/mcp"})
    assert server.transport == "http"
    assert server.url == "http://127.0.0.1:8000/mcp"
    assert server.command is None
    assert not _invalid_stdio_url(server)


def test_openclaw_url_only_honors_sse_signal():
    raw = {"url": "http://127.0.0.1:8000/mcp", "transport": "sse"}
    server, _ = A.ADAPTERS["openclaw"].from_provider("remote", raw)
    assert server.transport == "sse"
    assert server.url == "http://127.0.0.1:8000/mcp"


def test_mcpservers_url_in_command_field_imports_as_remote():
    # Same defect on the shared mcpServers importer (Claude/Cursor/Antigravity/Codex).
    server, _ = A.ADAPTERS["claude"].from_provider("remote", {"command": "http://127.0.0.1:8000/mcp"})
    assert server.transport == "http"
    assert server.url == "http://127.0.0.1:8000/mcp"
    assert server.command is None
    assert not _invalid_stdio_url(server)


def test_real_stdio_server_with_command_is_preserved():
    # Guard: a legitimate stdio server (real command, args) stays stdio.
    raw = {"command": "npx", "args": ["-y", "@mcp/server-github"]}
    server, _ = A.ADAPTERS["openclaw"].from_provider("github", raw)
    assert server.transport == "stdio"
    assert server.command == "npx"
    assert server.args == ("-y", "@mcp/server-github")
    assert server.url is None


# --------------------------------------------------------------------------- #
# (b) doctor no longer reports stdio-without-command for a pure-URL remote
# --------------------------------------------------------------------------- #


def _seed_openclaw_remote(home, url_only_raw):
    cfg = home / ".openclaw" / "openclaw.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"mcp": {"servers": {"remote": url_only_raw}}}))
    return cfg


def test_doctor_no_false_stdio_without_command_for_pure_url_remote(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    _seed_openclaw_remote(home, {"url": "http://127.0.0.1:8000/mcp", "transport": "stdio"})

    assert mcp_cmd.import_servers(target=repo, harness="openclaw", user_scope=True, merge=True, json_output=True) == 0

    servers, _, _ = mcp_cmd.load_canonical(repo)
    assert servers["remote"].transport == "http"

    rc = mcp_cmd.doctor(target=repo, json_output=True)
    assert rc == 0
    catalog, _, _ = mcp_cmd.load_canonical(repo)
    for server in catalog.values():
        messages = [m for _, m in A.validate_server(server)]
        assert not any("stdio transport requires a command" in m for m in messages)


# --------------------------------------------------------------------------- #
# (c) import -> sync never emits an invalid stdio+url server
# --------------------------------------------------------------------------- #


def test_import_then_sync_never_emits_stdio_plus_url(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    mcp_cmd.init(target=repo, json_output=True)
    _seed_openclaw_remote(home, {"url": "http://127.0.0.1:8000/mcp", "transport": "stdio"})

    assert mcp_cmd.import_servers(target=repo, harness="openclaw", user_scope=True, merge=True, json_output=True) == 0
    assert mcp_cmd.sync(target=repo, harness="claude", write=True, json_output=True) == 0

    written = json.loads((repo / ".mcp.json").read_text())["mcpServers"]["remote"]
    # A valid remote server carries a url and never a stdio command/type.
    assert written.get("url") == "http://127.0.0.1:8000/mcp"
    assert "command" not in written
    assert written.get("type") in ("http", "sse")
