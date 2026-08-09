"""Tests for the local memory-retrieval eval harness (#722)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade.memory_retrieval_eval import DEFAULT_FIXTURE_ROOT, run_eval
from brigade.memory_retrieval_eval import __main__ as eval_main
from brigade.memory_retrieval_eval.adapters import grep_adapter, tokenize
from brigade.memory_retrieval_eval.corpus import load_cards, load_queries, validate_gold
from brigade.memory_retrieval_eval.metrics import (
    ceiling_for_queries,
    first_gold_rank,
    hit_at_k,
    precision_at_k,
    recall_at_k,
)


def test_fixture_corpus_size_and_gold_resolve():
    cards = load_cards(DEFAULT_FIXTURE_ROOT)
    k, queries = load_queries(DEFAULT_FIXTURE_ROOT / "queries.json")
    assert 30 <= len(cards) <= 50
    assert len(queries) >= 20
    assert k == 5
    assert validate_gold(cards, queries) == []
    paraphrase = sum(1 for q in queries if q.category == "paraphrase")
    exact = sum(1 for q in queries if q.category == "exact")
    assert paraphrase > exact  # weighted toward hard cases


def test_paraphrase_slice_is_harder_than_exact_for_baselines():
    report = run_eval(adapters=["current", "grep"])
    for name in ("current", "grep"):
        exact_hit = report["adapters"][name]["by_category"]["exact"]["hit_rate"]
        para_hit = report["adapters"][name]["by_category"]["paraphrase"]["hit_rate"]
        assert exact_hit == pytest.approx(1.0)
        assert para_hit < exact_hit


def test_metrics_primitives():
    ranked = ["a", "b", "c", "d", "e"]
    gold = ["c", "z"]
    assert precision_at_k(ranked, gold, 5) == pytest.approx(0.2)
    assert recall_at_k(ranked, gold, 5) == pytest.approx(0.5)
    assert hit_at_k(ranked, gold, 5) == 1.0
    assert first_gold_rank(ranked, gold) == 3
    assert first_gold_rank(["x", "y"], gold) is None


def test_grep_adapter_is_token_substring_floor():
    cards = load_cards(DEFAULT_FIXTURE_ROOT)
    search = grep_adapter(cards)
    ranked = search("restic snapshot prune", limit=5)
    assert ranked
    assert ranked[0][0] == "restic-backup-schedule"
    assert tokenize("Auth-Token expires!") == ["auth", "token", "expires"]


def test_run_eval_offline_current_and_grep_reproducible():
    first = run_eval(adapters=["current", "grep"])
    second = run_eval(adapters=["current", "grep"])
    assert first["kind"] == "memory-retrieval-eval"
    assert first["issue"] == 722
    assert first["corpus"]["card_count"] == 40
    assert "semantic" not in first["adapters"]
    assert first["adapters"]["current"]["skipped"] is False
    assert first["adapters"]["grep"]["skipped"] is False
    assert "ceiling" in first
    assert first["ceiling"]["overall"]["hit_rate"] == pytest.approx(1.0)
    # Drop per-query ranked lists are deterministic; full payload must match.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_run_eval_includes_skipped_semantic_without_dependency(monkeypatch):
    import brigade.memory_retrieval_eval.adapters as adapters_mod

    def _no_semantic(cards):
        return None, "sentence_transformers not installed"

    monkeypatch.setattr(adapters_mod, "semantic_adapter", _no_semantic)
    report = run_eval(adapters=["current", "grep", "semantic"])
    assert report["adapters"]["semantic"]["skipped"] is True
    assert "sentence_transformers" in report["adapters"]["semantic"]["reason"]


def test_cli_table_and_json(capsys):
    rc = eval_main.main(["--adapters", "current,grep"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "memory retrieval eval" in out
    assert "current" in out
    assert "grep" in out
    assert "ceiling (oracle)" in out

    rc = eval_main.main(["--adapters", "current,grep", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["k"] == 5
    assert payload["adapters"]["current"]["overall"]["query_count"] >= 20


def test_readme_states_limits():
    readme = (DEFAULT_FIXTURE_ROOT / "README.md").read_text(encoding="utf-8")
    assert "40" in readme
    assert "cannot" in readme.lower()
    assert "grep" in readme.lower()
    assert "paraphrase" in readme.lower()


def test_ceiling_helpers_match_oracle():
    _k, queries = load_queries(DEFAULT_FIXTURE_ROOT / "queries.json")
    ceiling = ceiling_for_queries(queries, 5)
    assert ceiling["overall"]["precision_at_k"] > 0
    assert ceiling["overall"]["recall_at_k"] == pytest.approx(1.0)
    assert ceiling["overall"]["hit_rate"] == pytest.approx(1.0)


def test_missing_fixture_root_errors(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        run_eval(fixture_root=tmp_path)
