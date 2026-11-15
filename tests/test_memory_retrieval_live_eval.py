"""Private, use-derived judgments and candidate mutation checks for #2376."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade.memory_retrieval_eval import live
from brigade.memory_retrieval_eval.corpus import CardDoc, QuerySpec
from brigade.memory_retrieval_eval.hub import corpus_identity


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    target = tmp_path / "project"
    target.mkdir()
    return target


def history(target, count=20):
    for i in range(count):
        text = f"---\ntitle: Topic {i}\n---\nterm{i} guidance\n"
        live.record_search(
            target,
            f"term{i}",
            [{"scope": "fleet", "card_id": f"card-{i}", "content_hash": live.text_hash(text)}],
            hub="test-hub",
            epoch="test-epoch",
            now=100 + i,
        )
        live.record_use(target, f"card-{i}", text, now=101 + i)


def test_derivation_uses_followup_not_returned_results_and_keeps_text_private(workspace, capsys):
    history(workspace)
    live.record_search(
        workspace,
        "unused private words",
        [{"scope": "fleet", "card_id": "ignored", "content_hash": "a" * 64}],
        hub="test-hub",
        epoch="test-epoch",
        now=130,
    )
    queries = live.derive_queries(workspace, hub="test-hub", epoch="test-epoch", scope="fleet", now=140)
    assert len(queries) == 20
    assert queries[0].gold
    assert all(q.query != "unused private words" for q in queries)
    state = live.state_path(workspace)
    assert not state.is_relative_to(workspace)
    assert state.stat().st_mode & 0o777 == 0o600
    assert state.parent.stat().st_mode & 0o777 == 0o700
    assert "term0" in state.read_text()
    assert not list(workspace.rglob("*"))
    assert "term0" not in str(capsys.readouterr())
    assert live.derived_path(workspace, "test-hub", "test-epoch", "fleet").is_file()


def test_insufficient_duplicate_expired_wrong_epoch_or_scope_queries_fail_closed(workspace):
    history(workspace, 19)
    with pytest.raises(ValueError, match="insufficient judged queries: 19 available, 20 required"):
        live.derive_queries(workspace, hub="test-hub", epoch="test-epoch", scope="fleet", now=140)
    history(workspace, 19)
    for epoch, scope in [("test-epoch", "fleet"), ("other", "fleet"), ("test-epoch", "repo:other")]:
        with pytest.raises(ValueError, match="insufficient judged queries"):
            live.derive_queries(workspace, hub="test-hub", epoch=epoch, scope=scope, now=140)
    history(workspace, 20)
    with pytest.raises(ValueError, match="insufficient judged queries"):
        live.derive_queries(workspace, hub="test-hub", epoch="test-epoch", scope="fleet", now=4_000_000)


def test_open_must_follow_search_with_matching_content_and_unambiguous_scope(workspace):
    text = "useful text\n"
    match = {"scope": "fleet", "card_id": "one", "content_hash": live.text_hash(text)}
    live.record_search(workspace, "private", [match], hub="h", epoch="e", now=100)
    live.record_use(workspace, "one", "different", now=101)
    live.record_use(workspace, "one", text, now=99)
    live.record_use(workspace, "one", text, now=701)
    assert not any(row.get("gold") for row in json.loads(live.state_path(workspace).read_text()))
    live.record_search(workspace, "private", [{**match, "scope": "repo:other"}], hub="h", epoch="e", now=102)
    live.record_use(workspace, "one", text, now=103)
    assert not any(row.get("gold") for row in json.loads(live.state_path(workspace).read_text()))


def test_private_state_refuses_symlinks_and_repository_locations(workspace):
    path = live.state_path(workspace)
    path.parent.mkdir(parents=True)
    victim = workspace / "victim"
    victim.write_text("unchanged")
    path.symlink_to(victim)
    history(workspace)
    assert victim.read_text() == "unchanged"
    with pytest.raises(ValueError, match="private retrieval history unavailable"):
        live.derive_queries(workspace, hub="h", epoch="e", scope="fleet")
    (Path.home() / ".git").mkdir()
    with pytest.raises(ValueError, match="outside repositories"):
        live.state_path(workspace)


def test_live_extra_cards_and_mutation_refuses_regression():
    from brigade.memory_retrieval_eval.adapters import bm25_adapter

    cards = [
        CardDoc(
            f"card-{i}",
            f"memory/cards/card-{i}.md",
            f"Topic {i}",
            (),
            "",
            f"---\ntitle: Topic {i}\n---\nterm{i} guidance\n",
        )
        for i in range(25)
    ]
    queries = [QuerySpec(str(i), f"term{i}", (f"card-{i}",), "observed") for i in range(20)]
    baseline = bm25_adapter(cards)
    proposal = {
        "proposal_id": "candidate",
        "targets": [
            {
                "scope": "fleet",
                "card_id": "card-0",
                "before_hash": live.text_hash(cards[0].body),
                "after_hash": live.text_hash(cards[0].body),
                "after_text": cards[0].body,
                "after_state": "live",
            }
        ],
    }
    binding = {"corpus_identity": corpus_identity(cards), "mode": "lexical", "ranking": "raw"}
    report = live.measure_candidate(cards, queries, proposal, baseline, binding)
    assert report["gate"]["passed"]
    assert report["corpus"]["card_count"] == 25
    assert "term0" not in json.dumps(report)
    mutated = proposal["targets"][0]
    mutated["after_text"] = "unrelated replacement\n"
    mutated["after_hash"] = live.text_hash(mutated["after_text"])
    report = live.measure_candidate(cards, queries, proposal, baseline, binding)
    assert not report["gate"]["passed"]
    assert "candidate retrieval regression" in report["gate"]["failures"]
    assert report["adapters"]["candidate"]["overall"]["recall_at_k"] < 1
    assert cards[0].body.endswith("term0 guidance\n")


def test_live_pipeline_reads_scope_and_records_only_aggregates(workspace, monkeypatch):
    from brigade.memory_hub_client import MemoryHubClient
    from brigade.memory_retrieval_eval.adapters import bm25_adapter
    from tests.test_memory_retrieval_hub_eval import response

    history(workspace)
    cards = [live._card(f"card-{i}", f"---\ntitle: Topic {i}\n---\nterm{i} guidance\n") for i in range(25)]
    search = bm25_adapter(cards)
    candidate = {
        "scope": "fleet",
        "card_id": cards[0].card_id,
        "before_hash": live.text_hash(cards[0].body),
        "after_hash": live.text_hash(cards[0].body),
        "after_text": cards[0].body,
        "after_state": "live",
    }
    recorded = []
    actions = []

    def transport(url, token, body, timeout):
        actions.append(body["action"])
        if body["action"] == "status":
            return 200, {"log_epoch": "test-epoch", "seq": 25, "default_mode": "lexical"}
        if body["action"] == "log":
            return 200, {
                "log_epoch": "test-epoch",
                "next_seq": 25,
                "events": [
                    {"seq": i + 1, "scope": "fleet", "card_id": c.card_id, "revision": 1} for i, c in enumerate(cards)
                ],
            }
        if body["action"] == "card":
            card = next(c for c in cards if c.card_id == body["card_id"])
            return 200, {
                "log_epoch": "test-epoch",
                "card": {
                    "text": card.body,
                    "state": "live",
                    "content_hash": live.text_hash(card.body),
                    "next_offset": None,
                },
            }
        if body["action"] == "proposal_show":
            return 200, {"proposal_id": "candidate", "retrieval_eval_targets": [candidate]}
        if body["action"] == "retrieval_eval.record":
            recorded.append(json.loads(body["report_json"]))
            return 201, {}
        assert body["action"] == "search"
        reply = response()
        reply["ranking"] = "raw"
        reply["corpus_identity"]["fleet"] = corpus_identity(cards)
        reply["matches"] = [{"card_id": i, "score": score} for i, score in search(body["query"], body["limit"])]
        return 200, reply

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    # Bind the synthetic recorded uses to the actual client's opaque hub id.
    path = live.state_path(workspace)
    path.write_text(path.read_text().replace("test-hub", client.identity))
    monkeypatch.setattr(live, "MemoryHubClient", lambda *a: client)
    monkeypatch.setattr(live.time, "time", lambda: 140)
    token_file = workspace.parent / "token"
    token_file.write_text("fake-token")
    options = live.HubOptions(url="https://hub.example.test", token_file=token_file)
    report = live.run_live_eval(workspace, options, "candidate", record=True)
    assert report["gate"]["passed"]
    assert report["live"]["binding"]["corpus_identity"] == corpus_identity(cards)
    assert recorded[0]["live"]["proposal_id"] == "candidate"
    assert "term0" not in json.dumps(recorded)
    assert not set(actions) & {"put", "retire", "auto_apply", "decide"}
    candidate["after_text"] = "different content\n"
    candidate["after_hash"] = live.text_hash(candidate["after_text"])
    assert not live.run_live_eval(workspace, options, "candidate")["gate"]["passed"]


def test_hub_search_log_never_copies_private_queries_into_workspace(workspace):
    from brigade.memory_cmd import _record_search_log, SEARCH_LOG_REL

    _record_search_log(
        workspace,
        query="private query phrase",
        private_query=True,
        matches=[{"scope": "fleet", "card_id": "one", "path": "cards/one.md"}],
    )
    entry = json.loads((workspace / SEARCH_LOG_REL).read_text())
    assert "query" not in entry
    assert "query_hmac" in entry
    assert all("private query phrase" not in p.read_text() for p in workspace.rglob("*") if p.is_file())


def test_cli_insufficient_queries_has_safe_count_reason(workspace, monkeypatch, capsys):
    from brigade.memory_retrieval_eval.__main__ import main

    def unavailable(*args, **kwargs):
        raise live.LiveEvalError("insufficient judged queries: 0 available, 20 required")

    monkeypatch.setattr(live, "run_live_eval", unavailable)
    assert main(["--live-target", str(workspace), "--proposal-id", "candidate"]) == 2
    assert "0 available, 20 required" in capsys.readouterr().err


def test_native_live_scope_snapshot_and_measured_regression(workspace):
    import os
    from brigade.memory_retrieval_eval.hub import hub_client, HubAdapter, HubOptions

    binary = os.environ.get("BRIGADE_HUB_BINARY")
    if not binary:
        pytest.skip("native hub binary not selected")
    cards = [live._card(f"card-{i}", f"---\ntitle: Topic {i}\n---\nterm{i} guidance\n") for i in range(25)]
    options = HubOptions(binary=Path(binary), raw=True)
    with hub_client(options, cards) as client:
        assert client is not None
        loaded, epoch = live.snapshot(client, "fleet")
        assert len(loaded) == 25
        assert epoch
        adapter = HubAdapter(client, "lexical", options, loaded)
        queries = [QuerySpec(str(i), f"term{i}", (f"card-{i}",), "observed") for i in range(20)]
        adapter(queries[0].query, 20)
        target = {
            "scope": "fleet",
            "card_id": "card-0",
            "before_hash": live.text_hash(cards[0].body),
            "after_hash": live.text_hash(cards[0].body),
            "after_text": cards[0].body,
            "after_state": "live",
        }
        proposal = {"proposal_id": "candidate", "targets": [target]}
        assert live.measure_candidate(loaded, queries, proposal, adapter, adapter.binding)["gate"]["passed"]
        target["after_text"] = "unrelated replacement\n"
        target["after_hash"] = live.text_hash(target["after_text"])
        assert not live.measure_candidate(loaded, queries, proposal, adapter, adapter.binding)["gate"]["passed"]
        assert corpus_identity(live.snapshot(client, "fleet")[0]) == corpus_identity(cards)
