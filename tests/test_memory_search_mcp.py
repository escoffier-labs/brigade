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


def test_followup_rate_disjoint_within_window_counts_as_miss():
    entries = [
        {"ts": "2026-08-09T12:00:00Z", "query": "oauth refresh", "card_ids": ["alpha", "beta"]},
        {"ts": "2026-08-09T12:00:20Z", "query": "token rotation", "card_ids": ["gamma"]},
    ]
    stats = memory_cmd.followup_rate_from_entries(entries, window_s=45)
    assert stats["searches"] == 2
    assert stats["followups"] == 1
    assert stats["followup_rate"] == 0.5


def test_followup_rate_overlapping_followup_is_satisfied():
    entries = [
        {"ts": "2026-08-09T12:00:00Z", "query": "backup restic", "card_ids": ["alpha", "beta"]},
        {"ts": "2026-08-09T12:00:15Z", "query": "restic schedule", "card_ids": ["beta", "gamma"]},
    ]
    stats = memory_cmd.followup_rate_from_entries(entries, window_s=45)
    assert stats["searches"] == 2
    assert stats["followups"] == 0
    assert stats["followup_rate"] == 0.0


def test_followup_rate_outside_window_starts_fresh_event():
    entries = [
        {"ts": "2026-08-09T12:00:00Z", "query": "first", "card_ids": ["alpha"]},
        {"ts": "2026-08-09T12:02:00Z", "query": "later", "card_ids": ["beta"]},
    ]
    stats = memory_cmd.followup_rate_from_entries(entries, window_s=45)
    assert stats["searches"] == 2
    assert stats["followups"] == 0
    assert stats["followup_rate"] == 0.0


def test_memory_search_writes_local_search_log(tmp_path: Path, capsys):
    _card(tmp_path, "alpha.md", "Alpha UniqueTitle", "alpha body unique")
    _card(tmp_path, "beta.md", "Beta Other", "beta body distinct")
    assert memory_cmd.search(target=tmp_path, query="Alpha UniqueTitle", json_output=True) == 0
    capsys.readouterr()
    log_path = tmp_path / ".brigade" / "memory" / "search-log.jsonl"
    assert log_path.is_file()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["query"] == "alpha uniquetitle"
    assert lines[0]["card_ids"] == ["alpha"]
    assert "body" not in lines[0]
    # payload helper stays pure so the retrieval eval harness does not write logs
    memory_cmd.search_cards_payload(tmp_path, "Beta Other")
    assert len(log_path.read_text().splitlines()) == 1


def test_search_log_keeps_legacy_alias_for_explicit_card_id(tmp_path: Path, capsys):
    card_id = "card-123e4567-e89b-42d3-a456-426614174000"
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "renamed.md").write_text(f"---\nid: {card_id}\ntitle: Renamed\n---\nidentity token\n")

    assert memory_cmd.search(target=tmp_path, query="identity", json_output=True) == 0
    capsys.readouterr()
    entry = json.loads((tmp_path / ".brigade" / "memory" / "search-log.jsonl").read_text())
    assert entry["card_ids"] == [card_id]
    assert entry["card_aliases"] == ["memory/cards/renamed.md", "renamed"]
    legacy = {"ts": "2026-08-09T12:00:00Z", "query": "before rename", "card_ids": ["renamed"]}
    assert memory_cmd.followup_rate_from_entries([legacy, entry])["followup_rate"] == 0.0


def test_search_log_drops_oldest_beyond_max_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(memory_cmd, "SEARCH_LOG_MAX_ENTRIES", 2)
    _card(tmp_path, "alpha.md", "Alpha", "alpha unique token")
    assert memory_cmd.search(target=tmp_path, query="alpha unique", json_output=True) == 0
    assert memory_cmd.search(target=tmp_path, query="alpha token", json_output=True) == 0
    assert memory_cmd.search(target=tmp_path, query="unique token", json_output=True) == 0
    lines = [
        json.loads(line)
        for line in (tmp_path / ".brigade" / "memory" / "search-log.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert lines[0]["query"] == "alpha token"
    assert lines[1]["query"] == "unique token"


def test_memory_search_skips_dashboard_list_query_in_search_log(tmp_path: Path, capsys):
    _card(tmp_path, "alpha.md", "Alpha", "content: present")
    assert memory_cmd.search(target=tmp_path, query=":", json_output=True) == 0
    capsys.readouterr()
    assert not (tmp_path / ".brigade" / "memory" / "search-log.jsonl").exists()


def test_memory_care_status_reports_search_recall(tmp_path: Path, capsys):
    log_path = tmp_path / ".brigade" / "memory" / "search-log.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-08-09T12:00:00Z",
                        "query": "oauth",
                        "card_ids": ["alpha"],
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "ts": "2026-08-09T12:00:10Z",
                        "query": "token rotation",
                        "card_ids": ["beta"],
                    },
                    sort_keys=True,
                ),
            ]
        )
        + "\n"
    )
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    recall = payload["search_recall"]
    assert recall["searches"] == 2
    assert recall["followup_rate"] == 0.5
    assert recall["window_s"] == memory_cmd.SEARCH_FOLLOWUP_WINDOW_S
    assert recall["top_k"] == memory_cmd.SEARCH_LOG_TOP_K
    assert recall["evidence_class"] == "second_class"
    assert memory_cmd.status(target=tmp_path, json_output=False) == 0
    human = capsys.readouterr().out
    assert "search_recall: searches=2 followup_rate=0.5" in human


def test_card_mcp_resources_and_read_are_scoped(tmp_path: Path):
    _card(tmp_path, "a.md", "Card A", "hello world")
    config = load_config(tmp_path) or MemoryCareConfig()
    resources = _mcp_card_resources(tmp_path, config)
    assert any(r["uri"] == "card://memory/cards/a.md" for r in resources)
    text, mime = _mcp_read_card(tmp_path, config, "card://memory/cards/a.md")
    assert text is not None and "hello world" in text and mime == "text/markdown"
    # path traversal outside the card roots is refused
    assert _mcp_read_card(tmp_path, config, "card://../secret.md") == (None, None)


def test_card_mcp_read_refuses_symlink_escape(tmp_path: Path):
    target = tmp_path / "ws"
    _card(target, "real.md", "Real", "ok content")
    secret = tmp_path / "secret.txt"  # outside the workspace
    secret.write_text("TOPSECRET")
    (target / "memory" / "cards" / "evil.md").symlink_to(secret)
    config = load_config(target) or MemoryCareConfig()
    # a .md symlink pointing outside the card root is not a read primitive
    assert _mcp_read_card(target, config, "card://memory/cards/evil.md") == (None, None)
    # the real card still reads fine
    text, _ = _mcp_read_card(target, config, "card://memory/cards/real.md")
    assert text is not None and "ok content" in text


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
