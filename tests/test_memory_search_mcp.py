"""Tests for `brigade memory search` (#90) and `brigade memory serve-mcp` (#88)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from brigade import memory_cmd
from brigade.memory_cmd import MemoryCareConfig, _mcp_card_resources, _mcp_read_card, load_config


def _card(target: Path, name: str, title: str, body: str, tags: str | None = None) -> None:
    cards = target / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    front = f"---\ntitle: {title}\n"
    if tags is not None:
        front += f"tags: {tags}\n"
    front += "---\n"
    (cards / name).write_text(front + body + "\n")


def test_memory_search_ranks_title_over_body(tmp_path: Path):
    _card(tmp_path, "alpha.md", "Backup Restic", "uses restic to back up", tags="['backup']")
    _card(tmp_path, "beta.md", "Other", "mentions restic once in the body")
    payload = memory_cmd.search_cards_payload(tmp_path, "restic")
    assert payload["match_count"] == 2
    assert payload["matches"][0]["path"].endswith("alpha.md")  # title match outranks body


def test_memory_search_cli_json(tmp_path: Path, capsys):
    _card(tmp_path, "a.md", "Networking", "vlan trunking notes")
    rc = memory_cmd.search(target=tmp_path, query="vlan", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["match_count"] == 1


def test_card_mcp_resources_and_read_are_scoped(tmp_path: Path):
    _card(tmp_path, "a.md", "Card A", "hello world")
    config = load_config(tmp_path) or MemoryCareConfig()
    resources = _mcp_card_resources(tmp_path, config)
    assert any(r["uri"] == "card://memory/cards/a.md" for r in resources)
    text, mime = _mcp_read_card(tmp_path, config, "card://memory/cards/a.md")
    assert text is not None and "hello world" in text and mime == "text/markdown"
    # path traversal outside the card roots is refused
    assert _mcp_read_card(tmp_path, config, "card://../secret.md") == (None, None)


def test_card_mcp_stdio_roundtrip(tmp_path: Path, monkeypatch, capsys):
    _card(tmp_path, "a.md", "Card A", "hello mcp")
    requests = (
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_card", "arguments": {"path": "memory/cards/a.md"}},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(requests))
    rc = memory_cmd.serve_mcp(target=tmp_path, stdio=True)
    assert rc == 0
    responses = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "brigade-memory-readonly"
    assert any(r["uri"] == "card://memory/cards/a.md" for r in responses[1]["result"]["resources"])
    assert "hello mcp" in responses[2]["result"]["content"][0]["text"]
