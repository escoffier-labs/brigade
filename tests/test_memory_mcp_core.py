"""Memory MCP tool contract across the repo and user stores."""

from __future__ import annotations

import http.client
import io
import json
from urllib.parse import urlsplit
import threading
from pathlib import Path

import pytest

from brigade import mcp_server, memory_mcp_core
from tests._home import set_home


def _card(root: Path, name: str, title: str, body: str) -> None:
    cards = root / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / name).write_text(f"---\ntitle: {title}\n---\n{body}\n")


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    user = home / ".brigade"
    repo = tmp_path / "repo"
    repo.mkdir()
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("BRIGADE_HOME", str(user))
    return repo, user


WIRED_CONFIG = '{"version": 1, "depth": "repo", "harnesses": ["claude"], "owner": "claude"}'


def _tools(repo: Path) -> memory_mcp_core.MemoryTools:
    return memory_mcp_core.MemoryTools(target=repo, inbox="claude")


def test_tool_specs_name_six_tools_with_annotations():
    specs = {spec["name"]: spec for spec in memory_mcp_core.tool_specs()}
    assert sorted(specs) == sorted(
        ["list_cards", "get_card", "search_cards", "memory_status", "work_brief", "write_handoff"]
    )
    assert specs["get_card"]["annotations"]["readOnlyHint"] is True
    assert specs["write_handoff"]["annotations"]["readOnlyHint"] is False
    assert specs["write_handoff"]["annotations"]["destructiveHint"] is False


def test_list_cards_labels_origin_and_honors_scope(stores):
    repo, user = stores
    _card(repo, "a.md", "Repo card", "repo body")
    _card(user, "b.md", "User card", "user body")
    tools = _tools(repo)
    items, failed = tools.call("list_cards", {})
    assert failed is False
    assert {(i["origin"], i["ref"]) for i in items} == {
        ("repo", "repo:memory/cards/a.md"),
        ("user", "user:memory/cards/b.md"),
    }
    repo_only, _ = tools.call("list_cards", {"scope": "repo"})
    assert [i["origin"] for i in repo_only] == ["repo"]
    assert tools.call("list_cards", {"scope": "nope"}) == ("scope must be one of: repo, user, all", True)


def test_get_card_resolves_repo_first_then_user(stores):
    repo, user = stores
    _card(repo, "same.md", "Repo", "from repo")
    _card(user, "same.md", "User", "from user")
    _card(user, "only.md", "Only", "user only")
    tools = _tools(repo)
    assert "from repo" in tools.call("get_card", {"path": "memory/cards/same.md"})[0]
    assert "from repo" in tools.call("get_card", {"path": "card://memory/cards/same.md"})[0]
    assert "from user" in tools.call("get_card", {"path": "user:memory/cards/same.md"})[0]
    assert "user only" in tools.call("get_card", {"path": "memory/cards/only.md"})[0]
    assert tools.call("get_card", {"path": "repo:memory/cards/only.md"}) == (
        "card not found: repo:memory/cards/only.md",
        True,
    )


def test_get_card_refuses_traversal_and_symlink_escape(stores, tmp_path: Path):
    repo, _ = stores
    _card(repo, "real.md", "Real", "ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    (repo / "memory" / "cards" / "evil.md").symlink_to(secret)
    tools = _tools(repo)
    assert tools.call("get_card", {"path": "memory/cards/evil.md"})[1] is True
    assert tools.call("get_card", {"path": "../secret.txt"})[1] is True


def test_search_ranks_repo_first_on_ties(stores):
    repo, user = stores
    _card(repo, "r.md", "Vlan notes", "trunk")
    _card(user, "u.md", "Vlan notes", "trunk")
    matches, failed = _tools(repo).call("search_cards", {"query": "vlan"})
    assert failed is False
    assert [m["origin"] for m in matches] == ["repo", "user"]
    assert matches[0]["ref"] == "repo:memory/cards/r.md"


def test_resources_use_card_uris_per_origin(stores):
    repo, user = stores
    _card(repo, "a.md", "A", "a")
    _card(user, "b.md", "B", "b")
    tools = _tools(repo)
    uris = {r["uri"] for r in tools.resources()}
    assert uris == {"card://memory/cards/a.md", "user-card://memory/cards/b.md"}
    for uri in uris:
        assert urlsplit(uri).port is None  # every advertised URI parses as a valid URI
    text, mime = tools.read_resource("user-card://memory/cards/b.md")
    assert text is not None and mime == "text/markdown"
    assert tools.call("get_card", {"path": "user-card://memory/cards/b.md"})[1] is False


def test_user_card_uris_are_percent_encoded(stores):
    repo, user = stores
    _card(user, "release notes #2.md", "Notes", "body")
    tools = _tools(repo)
    uris = [r["uri"] for r in tools.resources() if r["uri"].startswith("user-card://")]
    assert uris == ["user-card://memory/cards/release%20notes%20%232.md"]
    assert urlsplit(uris[0]).fragment == ""
    text, _ = tools.read_resource(uris[0])
    assert text is not None and "body" in text


def test_memory_status_reports_stores_and_config_error(stores):
    repo, user = stores
    _card(repo, "a.md", "A", "a")
    _card(user, "b.md", "B", "b")
    status, failed = _tools(repo).call("memory_status", {})
    assert failed is False
    assert status["repo"]["root"] == str(repo.resolve())
    assert status["repo"]["wired"] is False
    assert status["repo"]["card_count"] == 1
    assert status["user"]["card_count"] == 1
    assert status["claude_hooks_installed"] is False
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    degraded, _ = _tools(repo).call("memory_status", {})
    assert degraded["repo"]["config_error"]
    assert degraded["repo"]["card_count"] == 0


def test_unknown_tool_is_a_tool_error(stores):
    repo, _ = stores
    assert _tools(repo).call("nope", {}) == ("unknown tool: nope", True)
    assert json.dumps(_tools(repo).call("list_cards", {})[0]) == "[]"


def test_write_handoff_lands_in_repo_inbox(stores):
    repo, _ = stores
    result, failed = _tools(repo).call(
        "write_handoff", {"title": "Plugin memory", "summary": "Short summary.", "content": "Body.", "facts": ["f1"]}
    )
    assert failed is False
    assert result["written"] is True and result["origin"] == "repo"
    assert result["path"].startswith(".claude/memory-handoffs/")
    assert (repo / result["path"]).is_file()


def test_write_handoff_without_repo_uses_user_store(stores, monkeypatch, tmp_path: Path):
    _, user = stores
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    tools = memory_mcp_core.MemoryTools(cwd=tmp_path, inbox="claude")
    result, failed = tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})
    assert failed is False and result["origin"] == "user"
    assert result["path"].startswith("memory/handoffs/")
    assert (user / result["path"]).is_file()
    assert (user / "memory").stat().st_mode & 0o777 == 0o700


def test_write_handoff_rejects_bad_input_and_removes_invalid_drafts(stores):
    repo, _ = stores
    tools = _tools(repo)
    assert tools.call("write_handoff", {"summary": "S.", "content": "Body."}) == ("title is required", True)
    assert tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body.", "facts": "x"}) == (
        "facts must be a list of strings",
        True,
    )
    result, failed = tools.call(
        "write_handoff",
        {"title": "Card", "summary": "S.", "content": "no frontmatter", "action": "create-card", "target_card": "c.md"},
    )
    assert failed is True and result["written"] is False
    assert any("frontmatter" in error for error in result["errors"])
    inbox = repo / ".claude" / "memory-handoffs"
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_work_brief_outside_wired_repo_points_at_user_store(stores, monkeypatch, tmp_path: Path):
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    result, failed = memory_mcp_core.MemoryTools(cwd=tmp_path).call("work_brief", {})
    assert failed is False and result["wired"] is False
    assert result["user_store"].endswith(".brigade/memory")


def test_work_brief_in_wired_repo_uses_cheap_helpers_only(stores, monkeypatch):
    repo, _ = stores
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "config.json").write_text(WIRED_CONFIG)
    from brigade.work_cmd.session import briefing

    def _full_brief(*args, **kwargs):
        raise AssertionError("work_brief must not build the full brief")

    monkeypatch.setattr(briefing, "_brief_payload", _full_brief)
    result, failed = _tools(repo).call("work_brief", {})
    assert failed is False
    assert result["wired"] is True and result["root"] == str(repo.resolve())
    assert set(result) == {"wired", "root", "git", "active_session", "latest_session", "next"}
    assert result["active_session"] is None and result["latest_session"] is None
    assert isinstance(result["next"], str) and result["next"]


def test_unexpected_tool_exception_is_a_tool_error(stores, monkeypatch):
    repo, _ = stores
    tools = _tools(repo)

    def _boom(arguments):
        raise KeyError("missing")

    monkeypatch.setattr(tools, "_tool_table", lambda: {"list_cards": _boom})
    assert tools.call("list_cards", {}) == ("list_cards failed: KeyError: 'missing'", True)


def test_write_handoff_uses_repo_inbox_when_repo_config_is_invalid(stores):
    repo, _ = stores
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "config.json").write_text(WIRED_CONFIG)
    (repo / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    tools = _tools(repo)
    assert tools.resolution().repo_config_error
    result, failed = tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})
    assert failed is False and result["origin"] == "repo"
    assert result["path"].startswith(".claude/memory-handoffs/")
    assert (repo / result["path"]).is_file()


def test_memory_status_reports_the_real_handoff_destination(stores, monkeypatch, tmp_path: Path):
    repo, user = stores
    status, _ = _tools(repo).call("memory_status", {})
    assert status["handoff_inbox"] == "claude"
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    outside, _ = memory_mcp_core.MemoryTools(cwd=tmp_path, inbox="claude").call("memory_status", {})
    assert outside["handoff_inbox"] == str(user.resolve() / "memory" / "handoffs")


def test_unwired_hint_names_the_user_store_only_without_a_repo_store(stores, monkeypatch, tmp_path: Path):
    repo, _ = stores
    explicit, _ = _tools(repo).call("work_brief", {})
    assert explicit["wired"] is False
    assert "user store" not in explicit["hint"]
    from brigade import memory_scope

    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    outside, _ = memory_mcp_core.MemoryTools(cwd=tmp_path).call("work_brief", {})
    assert "use the user store" in outside["hint"]


def test_write_handoff_errors_name_tool_arguments(stores):
    repo, _ = stores
    tools = _tools(repo)
    message, failed = tools.call(
        "write_handoff", {"title": "T", "summary": "S.", "content": "Body.", "action": "create-card"}
    )
    assert failed is True
    assert message == "target_card is required for card handoffs"
    message, failed = tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body.", "action": "x"})
    assert failed is True and message.startswith("action must be one of:")


def test_search_uses_each_store_config(stores):
    repo, user = stores
    _card(user, "u.md", "Uniquekey card", "body")
    (user / ".brigade").mkdir(parents=True)
    (user / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    tools = _tools(repo)
    matches, failed = tools.call("search_cards", {"query": "uniquekey"})
    assert failed is False
    assert [m["ref"] for m in matches] == ["user:memory/cards/u.md"]
    assert tools.call("get_card", {"path": "user:memory/cards/u.md"})[1] is False


def test_symlinked_card_root_yields_no_cards(stores, tmp_path: Path):
    repo, _ = stores
    outside = tmp_path / "outside"
    _card(outside, "leak.md", "Leaked secret", "vlan secret body")
    (repo / "memory").mkdir()
    (repo / "memory" / "cards").symlink_to(outside / "memory" / "cards")
    tools = _tools(repo)
    assert tools.call("list_cards", {"scope": "repo"}) == ([], False)
    assert tools.call("search_cards", {"query": "vlan"}) == ([], False)
    assert tools.resources() == []
    assert tools.call("get_card", {"path": "repo:memory/cards/leak.md"})[1] is True
    status, _ = tools.call("memory_status", {})
    assert status["repo"]["card_count"] == 0


def test_write_handoff_refuses_symlinked_inbox(stores, tmp_path: Path):
    repo, _ = stores
    outside = tmp_path / "outside-inbox"
    outside.mkdir()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "memory-handoffs").symlink_to(outside)
    message, failed = _tools(repo).call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})
    assert failed is True
    assert "outside the memory store" in message
    assert list(outside.iterdir()) == []


def test_write_handoff_user_store_modes_are_private(stores, monkeypatch, tmp_path: Path):
    _, user = stores
    from brigade import memory_scope

    user.mkdir(mode=0o755)
    user.chmod(0o755)
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    tools = memory_mcp_core.MemoryTools(cwd=tmp_path, inbox="claude")
    result, failed = tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})
    assert failed is False
    assert (user / "memory" / "handoffs").stat().st_mode & 0o777 == 0o700
    assert (user / result["path"]).stat().st_mode & 0o777 == 0o600
    assert user.stat().st_mode & 0o777 == 0o755


def test_write_handoff_tightens_an_existing_user_inbox(stores, monkeypatch, tmp_path: Path):
    _, user = stores
    from brigade import memory_scope

    inbox = user / "memory" / "handoffs"
    inbox.mkdir(parents=True)
    inbox.chmod(0o755)
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    tools = memory_mcp_core.MemoryTools(cwd=tmp_path, inbox="claude")
    assert tools.call("write_handoff", {"title": "T", "summary": "S.", "content": "Body."})[1] is False
    assert inbox.stat().st_mode & 0o777 == 0o700


def test_write_handoff_bounds_content_and_lists(stores):
    repo, _ = stores
    tools = _tools(repo)
    base = {"title": "T", "summary": "S."}
    message, failed = tools.call("write_handoff", {**base, "content": "x" * (256 * 1024 + 1)})
    assert failed is True and "content" in message
    for key in ("facts", "evidence"):
        message, failed = tools.call("write_handoff", {**base, "content": "Body.", key: ["f"] * 51})
        assert failed is True and key in message
    inbox = repo / ".claude" / "memory-handoffs"
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_stdio_and_http_answer_identically(stores, monkeypatch: pytest.MonkeyPatch):
    repo, _ = stores
    _card(repo, "a.md", "Vlan", "trunk")
    callbacks = _tools(repo).callbacks()
    assert callbacks["server_name"] == "brigade-memory"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_cards", "arguments": {"query": "vlan"}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "card://memory/cards/a.md"}},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert mcp_server.serve_stdio(**callbacks) == 0
    stdio_replies = [json.loads(line) for line in out.getvalue().splitlines()]
    token = "t" * 40
    srv = mcp_server.make_http_server(**callbacks, host="127.0.0.1", port=0, token=token)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    http_replies = []
    try:
        for request in requests:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            try:
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
                conn.request("POST", "/mcp", body=json.dumps(request).encode(), headers=headers)
                http_replies.append(json.loads(conn.getresponse().read()))
            finally:
                conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(10)
    assert len(stdio_replies) == len(requests)
    assert http_replies == stdio_replies
