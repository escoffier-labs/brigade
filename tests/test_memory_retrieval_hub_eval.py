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
        "log_epoch": "fixture-epoch",
        "corpus_identity": {"fleet": {"live_count": 1, "digest": "fixture-corpus"}},
        "policy_revision": 1,
        "policy_digest": "fixture-policy",
        "index_format": "1",
        "scorer_version": "fixture-v1",
        "ranking": "default",
        "rrf_weights": {"lexical": 1, "dense": 1},
        "embedder_id": "candidate" if mode == "hybrid" else None,
        "retrieval": {"mode": mode, "stale": False, "degraded": False},
    }


def adapter(reply, mode="lexical", cards=(), **options):
    def transport(url, token, body, timeout):
        assert body["scopes"] == ["fleet"]
        assert body["mode"] == mode
        assert body["limit"] == 20
        assert body["eval"] is True
        return 200, reply

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    return HubAdapter(client, mode, HubOptions(**options), cards)


def test_binding_stable_and_rejects_index_or_config_change():
    for key, value in [
        ("indexed_seq", 4),
        ("log_epoch", "other-epoch"),
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


def test_external_scope_identity_rejects_extra_cards_and_binds_changes():
    from brigade.memory_retrieval_eval.corpus import CardDoc
    from brigade.memory_retrieval_eval.hub import corpus_identity

    card = CardDoc("one", "memory/cards/one.md", "one", (), "", "fixture text\n")
    reply = response()
    reply["corpus_identity"]["fleet"] = corpus_identity([card])
    search = adapter(reply, cards=[card])
    assert search("words", 40) == [("one", 1.0)]
    reply["corpus_identity"]["fleet"] = {"live_count": 2, "digest": "extra-card"}
    with pytest.raises(ValueError, match="extra cards"):
        search("words", 40)
    reply = response()
    search = adapter(reply)
    search("words", 40)
    reply["corpus_identity"]["fleet"]["digest"] = "changed-corpus"
    with pytest.raises(ValueError, match="changed"):
        search("words", 40)


def test_switching_timeout_is_explicit_and_healthy_state_does_not_wait():
    from brigade.memory_retrieval_eval.hub import wait_switching

    def transport(url, token, body, timeout):
        return 200, {"retrieval": {"state": "switching"}}

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    with pytest.raises(ValueError, match="coverage timed out while switching"):
        wait_switching(client, timeout=0)


def test_report_identifies_hub_clamp_for_every_adapter(tmp_path):
    report = run_eval(adapters=["bm25", "hub-lexical"], hub_options=HubOptions(binary=tmp_path / "missing"))
    assert report["ranking_depth"] == 20
    assert report["ranking_limit"] == {"hub_clamped": True, "applies_to": ["bm25", "hub-lexical"], "corpus_limit": 20}
    assert run_eval(adapters=["bm25"])["ranking_limit"]["hub_clamped"] is False


def test_sync_spool_preserves_scheduled_and_worker_origin():
    from brigade.memory_hub_sync import _entry, _request

    entry = _entry("fleet", {"card_id": "one", "text": "fixture", "rel": "memory/cards/one.md"}, "fixture-hub")
    assert _request(entry)["origin"] == "interactive"
    for origin in ("scheduled", "worker"):
        entry["origin"] = origin
        assert _request(entry)["origin"] == origin


def test_external_url_mode_refuses_extra_corpus_without_writing(tmp_path, monkeypatch):
    from brigade.memory_retrieval_eval import DEFAULT_FIXTURE_ROOT
    from brigade.memory_retrieval_eval import hub
    from brigade.memory_retrieval_eval.corpus import load_cards
    import hashlib

    cards = load_cards(DEFAULT_FIXTURE_ROOT)
    hashes = {Path(c.path).stem: hashlib.sha256((c.body.rstrip("\n") + "\n").encode()).hexdigest() for c in cards}
    actions = []

    def transport(url, token, body, timeout):
        actions.append(body["action"])
        assert body["action"] in ("card", "search")
        if body["action"] == "card":
            return 200, {"card": {"content_hash": hashes[body["card_id"]]}}
        reply = response()
        reply["corpus_identity"]["fleet"] = {"live_count": len(cards) + 1, "digest": "extra"}
        return 200, reply

    monkeypatch.setattr(hub, "MemoryHubClient", lambda url, token: MemoryHubClient(url, token, transport=transport))
    token_file = tmp_path / "token"
    token_file.write_text("fake-token")
    with pytest.raises(ValueError, match="extra cards"):
        run_eval(
            adapters=["hub-lexical"], hub_options=HubOptions(url="https://hub.example.test", token_file=token_file)
        )
    assert actions.count("card") == len(cards)
    assert actions.count("search") == 1


def test_eval_diagnostics_carry_lineage_budget_overflow():
    reply = response()
    reply.update(lineage_visited=512, lineage_overflow=True, lineage_uncollapsed=2)
    search = adapter(reply)
    search("words", 40)
    assert search.diagnostics == {"max_lineage_visited": 512, "lineage_overflows": 1, "uncollapsed_hits": 2}


def test_record_report_preserves_s17_digest_bytes_and_refuses_tampering():
    import hashlib
    import json

    from brigade.memory_retrieval_eval.hub import record_report

    payload = {"gate": {"name": "R0", "passed": False}, "example": "\N{SNOWMAN}", "metric": 1e-7}
    report = {**payload, "digest": stable_digest(payload)}
    calls = []

    def transport(url, token, body, timeout):
        calls.append(body)
        assert body["action"] == "retrieval_eval.record"
        assert hashlib.sha256(body["report_json"].encode()).hexdigest() == report["digest"]
        assert json.loads(body["report_json"]) == payload
        assert "digest" not in json.loads(body["report_json"])
        return 201, {"record_seq": 1}

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    assert record_report(client, report) == {"record_seq": 1}
    report["metric"] = 1
    with pytest.raises(ValueError, match="digest mismatch"):
        record_report(client, report)
    assert len(calls) == 1


def test_native_s17_report_records_without_reserializing_digest(tmp_path, monkeypatch):
    import json
    import os
    import urllib.request
    from contextlib import nullcontext

    from brigade import fleet_policy
    from brigade.memory_hub_client import event_id
    from brigade.memory_retrieval_eval import DEFAULT_FIXTURE_ROOT, harness
    from brigade.memory_retrieval_eval.corpus import load_cards
    from brigade.memory_retrieval_eval.gates import evaluate_gate
    from brigade.memory_retrieval_eval.hub import hub_client, record_report
    from tests._home import set_home

    binary = os.environ.get("BRIGADE_HUB_BINARY")
    if not binary:
        pytest.skip("native hub binary not selected")
    set_home(monkeypatch, tmp_path / "home")
    options = HubOptions(binary=Path(binary), raw=True)
    with hub_client(options, load_cards(DEFAULT_FIXTURE_ROOT)) as client:
        assert client is not None
        request = urllib.request.Request(
            client.hub_url + "/policy",
            data=json.dumps(
                {
                    "action": "save",
                    "document": {"schema": fleet_policy.POLICY_SCHEMA},
                    "expected_version": 1,
                    "reason": "retrieval eval fixture",
                }
            ).encode(),
            headers={"Authorization": "Bearer " + client._token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
        monkeypatch.setattr(harness, "hub_client", lambda *args: nullcontext(client))
        report = run_eval(adapters=["bm25", "hub-lexical"], hub_options=options, aggregate_only=True)
        report.pop("digest")
        report["gate"] = evaluate_gate(report, "R0")
        report["evaluation_id"] = event_id()
        report["digest"] = stable_digest(report)
        assert report["gate"]["passed"]
        reply = record_report(client, report)
        assert reply["passed"] is True
        assert reply["report_sha256"] == report["digest"]


def test_ranking_hash_survives_aggregate_only_reports():
    full = run_eval(adapters=["bm25"])
    compact = run_eval(adapters=["bm25"], aggregate_only=True)
    expected = stable_digest([row["ranked_candidates"] for row in full["adapters"]["bm25"]["queries"]])
    assert full["adapters"]["bm25"]["ranking_sha256"] == expected
    assert compact["adapters"]["bm25"]["ranking_sha256"] == expected
    assert "queries" not in compact["adapters"]["bm25"]


def test_r0_compact_gate_requires_matching_ranking_hashes():
    from brigade.memory_retrieval_eval.gates import evaluate_gate

    report = {
        "adapters": {
            "bm25": {"available": True, "ranking_sha256": "a" * 64},
            "hub-lexical": {"available": True, "ranking_sha256": "a" * 64, "binding": {"ranking": "raw"}},
        }
    }
    assert evaluate_gate(report, "R0")["passed"]
    report["adapters"]["hub-lexical"]["ranking_sha256"] = "b" * 64
    assert not evaluate_gate(report, "R0")["passed"]
    for block in report["adapters"].values():
        block.pop("ranking_sha256")
    assert not evaluate_gate(report, "R0")["passed"]
