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


def test_run_eval_first_gold_rank_below_k(monkeypatch, tmp_path: Path):
    """Gold below K must still appear in first_gold_rank while top-K metrics stay capped."""
    import brigade.memory_retrieval_eval.harness as harness_mod

    fixture = tmp_path / "fixture"
    _copy_minimal_fixture(fixture)

    gold_id = "restic-backup-schedule"
    decoys = [f"decoy-{i}" for i in range(5)]
    dst_cards = fixture / "memory" / "cards"
    for decoy_id in decoys:
        (dst_cards / f"{decoy_id}.md").write_text("decoy", encoding="utf-8")
    full_ranking = decoys + [gold_id]

    real_build = harness_mod.build_adapters

    def _patched_build(target, cards, *, wanted):
        meta = real_build(target, cards, wanted=wanted)

        def search(_query: str, limit: int) -> list[tuple[str, float]]:
            pairs = [(card_id, float(100 - i)) for i, card_id in enumerate(full_ranking)]
            return pairs[:limit]

        if "current" in meta:
            meta["current"]["search"] = search
        return meta

    monkeypatch.setattr(harness_mod, "build_adapters", _patched_build)

    report = run_eval(fixture_root=fixture, adapters=["current"], k=5)
    row = report["adapters"]["current"]["queries"][0]
    assert row["hit_at_k"] == 0.0
    assert row["first_gold_rank"] == 6
    assert row["ranked"] == decoys


def test_run_eval_current_and_grep_never_touch_semantic_adapter(monkeypatch):
    import brigade.memory_retrieval_eval.adapters as adapters_mod

    def _forbidden_semantic(_cards):
        raise AssertionError("semantic_adapter must not run for current/grep-only eval")

    monkeypatch.setattr(adapters_mod, "semantic_adapter", _forbidden_semantic)
    report = run_eval(adapters=["current", "grep"])
    assert "semantic" not in report["adapters"]


def test_semantic_adapter_handles_broken_sentence_transformers_import(monkeypatch):
    import builtins

    import brigade.memory_retrieval_eval.adapters as adapters_mod

    real_import = builtins.__import__

    def _broken_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sentence_transformers":
            raise OSError("simulated broken native extension")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _broken_import)
    cards = load_cards(DEFAULT_FIXTURE_ROOT)
    search_fn, reason = adapters_mod.semantic_adapter(cards)
    assert search_fn is None
    assert reason is not None
    assert "OSError" in reason

    report = run_eval(adapters=["semantic"])
    assert report["adapters"]["semantic"]["skipped"] is True
    assert report["adapters"]["semantic"]["reason"]


def test_malformed_query_missing_id_raises_value_error(tmp_path: Path):
    fixture = tmp_path / "fixture"
    _copy_minimal_fixture(fixture)
    queries_path = fixture / "queries.json"
    queries_path.write_text(
        json.dumps({"k": 5, "queries": [{"query": "restic", "gold": ["restic-backup-schedule"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="id"):
        load_queries(queries_path)


def test_malformed_query_missing_query_raises_value_error(tmp_path: Path):
    fixture = tmp_path / "fixture"
    _copy_minimal_fixture(fixture)
    queries_path = fixture / "queries.json"
    queries_path.write_text(
        json.dumps({"k": 5, "queries": [{"id": "q1", "gold": ["restic-backup-schedule"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="query"):
        load_queries(queries_path)


def test_cli_malformed_query_exits_2(tmp_path: Path, capsys):
    fixture = tmp_path / "fixture"
    _copy_minimal_fixture(fixture)
    queries_path = fixture / "queries.json"
    queries_path.write_text(
        json.dumps({"k": 5, "queries": [{"id": "q1", "gold": ["restic-backup-schedule"]}]}),
        encoding="utf-8",
    )
    rc = eval_main.main(["--fixture-root", str(fixture), "--adapters", "grep", "--json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "query" in err.lower()


def test_default_report_fixture_root_is_stable_and_not_absolute():
    report = run_eval(adapters=["current", "grep"])
    fixture_root = report["fixture_root"]
    assert not Path(fixture_root).is_absolute()
    assert fixture_root == "evals/memory-retrieval"


def test_custom_fixture_root_label_is_deterministic(tmp_path: Path):
    fixture = tmp_path / "custom-eval"
    _copy_minimal_fixture(fixture)
    first = run_eval(fixture_root=fixture, adapters=["grep"])
    second = run_eval(fixture_root=fixture, adapters=["grep"])
    label = first["fixture_root"]
    assert label == second["fixture_root"]
    assert not Path(label).is_absolute()
    assert label != "evals/memory-retrieval"


def _copy_minimal_fixture(fixture: Path) -> None:
    """Copy one card and a valid query from the default fixture for isolated tests."""
    import shutil

    src_cards = DEFAULT_FIXTURE_ROOT / "memory" / "cards"
    dst_cards = fixture / "memory" / "cards"
    dst_cards.mkdir(parents=True)
    shutil.copy2(src_cards / "restic-backup-schedule.md", dst_cards / "restic-backup-schedule.md")
    (fixture / "queries.json").write_text(
        json.dumps(
            {
                "k": 5,
                "queries": [
                    {
                        "id": "q1",
                        "query": "restic backup schedule",
                        "gold": ["restic-backup-schedule"],
                        "category": "exact",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
