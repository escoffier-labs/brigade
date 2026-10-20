"""Memory MCP tool contract across the repo and user stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import memory_mcp_core


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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIGADE_HOME", str(user))
    return repo, user


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
    assert uris == {"card://memory/cards/a.md", "card://user:memory/cards/b.md"}
    text, mime = tools.read_resource("card://user:memory/cards/b.md")
    assert text is not None and mime == "text/markdown"


def test_memory_status_reports_stores_and_config_error(stores):
    repo, user = stores
    _card(user, "b.md", "B", "b")
    status, failed = _tools(repo).call("memory_status", {})
    assert failed is False
    assert status["repo"]["root"] == str(repo.resolve())
    assert status["repo"]["wired"] is False
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
