"""S17 binding, failure handling and gate semantics, without remote embedders."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from brigade.memory_hub_client import MemoryHubClient
from brigade.memory_retrieval_eval import run_eval
from brigade.memory_retrieval_eval.__main__ import main
from brigade.memory_retrieval_eval.hub import HubAdapter, HubOptions, stable_digest
from brigade.memory_retrieval_eval.metrics import aggregate_scores, score_query


def response(mode="lexical"):
    return {
        "matches": [{"card_id": "one", "score": 1}],
        "indexed_seq": 3,
        "policy_revision": 1,
        "policy_digest": "fixture-policy",
        "index_format": "1",
        "scorer_version": "fixture-v1",
        "ranking": "default",
        "rrf_weights": {"lexical": 1, "dense": 1},
        "embedder_id": "candidate" if mode == "hybrid" else None,
        "retrieval": {"mode": mode, "stale": False, "degraded": False},
    }


def adapter(reply, mode="lexical", **options):
    def transport(url, token, body, timeout):
        assert body["scopes"] == ["fleet"]
        assert body["mode"] == mode
        assert body["limit"] == 20
        return 200, reply

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    return HubAdapter(client, mode, HubOptions(**options))


def test_binding_stable_and_rejects_index_or_config_change():
    for key, value in [
        ("indexed_seq", 4),
        ("scorer_version", "new"),
        ("index_format", "2"),
        ("ranking", "raw"),
        ("policy_revision", 2),
        ("policy_digest", "other-policy"),
        ("rrf_weights", {"lexical": 2, "dense": 1}),
    ]:
        reply = response()
        search = adapter(reply)
        assert search("words", 40) == [("one", 1.0)]
        assert search("words", 40) == [("one", 1.0)]
        reply[key] = value
        with pytest.raises(ValueError, match="changed"):
            search("words", 40)


def test_no_fallback_stale_or_wrong_candidate_can_pass():
    for mode, extra in [("lexical-fallback", {}), ("hybrid", {"degraded": True}), ("hybrid", {"stale": True})]:
        reply = response(mode)
        reply["retrieval"].update(extra)
        with pytest.raises(ValueError, match="healthy"):
            adapter(reply, "hybrid")("words", 40)
    with pytest.raises(ValueError, match="candidate"):
        adapter(response("hybrid"), "hybrid", candidate_embedder="different")("words", 40)


def test_skipped_adapter_cannot_pass_gate(tmp_path: Path, capsys):
    assert main(["--gate", "R0", "--hub-binary", str(tmp_path / "missing"), "--json"]) == 1
    assert '"passed": false' in capsys.readouterr().out


def test_digest_binds_fixture_and_results():
    first = run_eval(adapters=["bm25"])
    second = run_eval(adapters=["bm25"])
    assert first["digest"] == second["digest"]
    content = copy.deepcopy(first)
    digest = content.pop("digest")
    assert stable_digest(content) == digest
    content["adapters"]["bm25"]["overall"]["mrr"] = 0
    assert stable_digest(content) != digest


def test_mrr_counts_misses_and_deep_hits():
    rows = [score_query(["a", "b"], ["b"], 1), score_query([], ["a"], 1)]
    assert aggregate_scores(rows)["mrr"] == 0.25


def test_native_public_fixture_parity_and_default_ranking():
    import os
    from brigade.memory_retrieval_eval.gates import evaluate_gate

    binary = os.environ.get("BRIGADE_HUB_BINARY")
    if not binary:
        pytest.skip("native hub binary not selected")
    raw = run_eval(adapters=["bm25", "hub-lexical"], hub_options=HubOptions(binary=Path(binary), raw=True))
    assert evaluate_gate(raw, "R0")["passed"]
    after = run_eval(adapters=["hub-lexical"], hub_options=HubOptions(binary=Path(binary)))
    for metric in ("hit_rate", "mrr"):
        assert after["adapters"]["hub-lexical"]["overall"][metric] >= raw["adapters"]["hub-lexical"]["overall"][metric]


def test_public_r1_requires_each_category_and_a_strict_hard_category_gain():
    from brigade.memory_retrieval_eval.gates import evaluate_gate

    report = run_eval(adapters=["bm25"])
    baseline = report["adapters"]["bm25"]
    hybrid = copy.deepcopy(baseline)
    hybrid["binding"] = {"ranking": "default"}
    report["adapters"]["hub-hybrid"] = hybrid
    assert not evaluate_gate(report, "R1")["passed"]
    hybrid["by_category"]["paraphrase"]["mrr"] += 0.01
    assert evaluate_gate(report, "R1")["passed"]
    hybrid["by_category"]["exact"]["mrr"] = 0
    assert not evaluate_gate(report, "R1")["passed"]
