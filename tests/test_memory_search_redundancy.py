"""Read-only repeat retrieval diagnostics over the existing local search log."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import memory_cmd


def test_search_recall_reports_repeats_without_searching_or_changing_log(tmp_path: Path, monkeypatch):
    entries = [
        {"ts": "2026-08-09T12:00:00Z", "query": " Comet  TASK ", "card_ids": ["a", "b"]},
        {"ts": "2026-08-09T12:00:10Z", "query": "comet task", "card_ids": ["b", "a"]},
        {"ts": "2026-08-09T12:00:20Z", "query": "comet task", "card_ids": ["c"]},
        {"ts": "2026-08-09T12:00:30Z", "query": "different", "card_ids": ["c"]},
        {"ts": "2026-08-09T12:02:00Z", "query": "different", "card_ids": ["c"]},
    ]
    path = tmp_path / memory_cmd.SEARCH_LOG_REL
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not perform retrieval")

    monkeypatch.setattr(memory_cmd, "search_cards_payload", forbidden)
    stats = memory_cmd.search_recall_status(tmp_path)
    assert stats["repeat_queries"] == 2
    assert stats["repeat_results"] == 2
    assert stats["repeat_query_results"] == 1
    assert stats["compared_pairs"] == 3
    assert stats["followup_rate"] == 0.2
    assert stats["evidence_class"] == "second_class"
    assert path.read_bytes() == before


def test_repeat_diagnostics_reject_bad_pairs_and_do_not_count_empty_results(tmp_path: Path):
    entries = [
        {"ts": "2026-08-09T12:00:10Z", "query": "same", "card_ids": []},
        {"ts": "2026-08-09T12:00:00Z", "query": "same", "card_ids": []},
        {"ts": "invalid", "query": "same", "card_ids": ["a"]},
        {"ts": "2026-08-09T12:00:20Z", "query": "same", "card_ids": []},
        {"ts": "2026-08-09T12:00:30Z", "query": "same", "card_ids": []},
        {"ts": "2026-08-09T12:00:40Z", "query": "same", "card_ids": "malformed"},
    ]
    path = tmp_path / memory_cmd.SEARCH_LOG_REL
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    stats = memory_cmd.search_recall_status(tmp_path)
    assert stats["compared_pairs"] == 1
    assert stats["repeat_queries"] == 1
    assert stats["repeat_results"] == stats["repeat_query_results"] == 0
    empty = memory_cmd.search_recall_status(tmp_path / "missing")
    assert empty["compared_pairs"] == empty["repeat_queries"] == empty["repeat_results"] == 0
