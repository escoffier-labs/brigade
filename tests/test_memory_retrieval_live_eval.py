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


def test_local_forged_history_is_never_an_authority(workspace):
    from brigade.memory_hub_client import MemoryHubClient

    history = Path.home() / ".brigade/private-retrieval-eval/history.json"
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps([{"query": f"attack {i}", "gold": ["wrong"]} for i in range(20)]))
    calls = []

    def transport(url, token, body, timeout):
        calls.append(body)
        return 409, {"code": "memory_retrieval_history_insufficient"}

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    with pytest.raises(live.LiveEvalError, match="hub judged history"):
        live.derive_queries(client, "fleet")
    assert calls == [{"action": "retrieval_judgments", "scope": "fleet"}]
    assert history.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"session_id": "one"},
        {"session_id": "one", "session_kind": "scheduled"},
        {"session_id": "one", "session_kind": "interactive", "agent_id": "worker"},
    ],
)
def test_missing_or_worker_context_is_not_interactive(monkeypatch, overrides):
    monkeypatch.delenv("BRIGADE_SESSION_ID", raising=False)
    monkeypatch.delenv("BRIGADE_SESSION_KIND", raising=False)
    assert not live.interactive_session(overrides)


@pytest.mark.parametrize("name", ["BRIGADE_RUN_ID", "BRIGADE_SCANNER_RUN_ID"])
def test_job_search_and_use_share_interactive_filter(monkeypatch, name):
    monkeypatch.setenv("BRIGADE_SESSION_ID", "one")
    monkeypatch.setenv("BRIGADE_SESSION_KIND", "interactive")
    monkeypatch.setenv(name, "job")
    assert not live.interactive_session()
    assert not live.interactive_session({"session_id": "one", "session_kind": "interactive"})


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
        if body["action"] == "retrieval_judgments":
            return 200, {
                "queries": [[f"term{i}", [f"card-{i}"]] for i in range(20)],
                "history_seq": 40,
                "query_set_digest": "a" * 64,
            }
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
    monkeypatch.setattr(live, "MemoryHubClient", lambda *a, **kw: client)
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


@pytest.mark.parametrize("budget", ["requests", "bytes", "clock"])
def test_evaluation_aggregate_budgets_fail_closed(monkeypatch, budget):
    from brigade.memory_hub_client import MemoryHubClient

    calls = []
    client = live.BudgetClient(
        MemoryHubClient(
            "https://hub.example.test",
            "fake-token",
            transport=lambda *a: calls.append(a) or (200, {"value": "1234567890"}),
        )
    )
    if budget == "requests":
        monkeypatch.setattr(live, "MAX_REQUESTS", 1)
        client.call("status")
    elif budget == "bytes":
        monkeypatch.setattr(live, "MAX_BYTES", 2)
    else:
        client.deadline = 0
    with pytest.raises(live.LiveEvalError, match="live evaluation budget exceeded"):
        client.call("status")
    assert len(calls) <= 1


@pytest.mark.parametrize("ceiling", ["MAX_EVENTS", "MAX_CARDS"])
def test_snapshot_aggregate_budget(monkeypatch, ceiling):
    from brigade.memory_hub_client import MemoryHubClient

    monkeypatch.setattr(live, ceiling, 1)

    def transport(url, token, body, timeout):
        if body["action"] == "status":
            return 200, {"log_epoch": "e", "seq": 2}
        assert body["action"] == "log"
        return 200, {
            "log_epoch": "e",
            "next_seq": 2,
            "events": [{"seq": i + 1, "scope": "fleet", "card_id": str(i), "revision": 1} for i in range(2)],
        }

    client = MemoryHubClient("https://hub.example.test", "fake-token", transport=transport)
    with pytest.raises(live.LiveEvalError, match="live evaluation budget exceeded"):
        live.snapshot(client, "fleet")


def test_missing_gold_ids_are_dropped_before_query_floor():
    from brigade.memory_retrieval_eval.adapters import bm25_adapter

    cards = [live._card(f"c-{i}", f"term{i}") for i in range(20)]
    queries = [QuerySpec(str(i), f"term{i}", (f"c-{i}", "retired"), "observed") for i in range(20)]
    queries.append(QuerySpec("old", "old query", ("retired",), "observed"))
    proposal = {
        "proposal_id": "p",
        "targets": [
            {
                "scope": "fleet",
                "card_id": "c-0",
                "before_hash": live.text_hash(cards[0].body),
                "after_hash": live.text_hash(cards[0].body),
                "after_text": cards[0].body,
                "after_state": "live",
            }
        ],
    }
    report = live.measure_candidate(cards, queries, proposal, bm25_adapter(cards), {})
    assert report["corpus"]["query_count"] == 20
    assert report["gate"]["passed"]
    # Every surviving label may veto, including a single query in one session.
    assert live.measure_candidate(cards, queries[1:2], proposal, bm25_adapter(cards), {})["gate"]["passed"]
    with pytest.raises(live.LiveEvalError, match="0 available"):
        live.measure_candidate(cards, [], proposal, bm25_adapter(cards), {})


def test_private_search_read_ledger_counts_all_twenty(workspace):
    from brigade.memory_cmd import _record_search_log
    from brigade.memory_card_reads import load

    matches = [{"card_id": f"c-{i}", "path": f"cards/c-{i}.md"} for i in range(20)]
    _record_search_log(workspace, query="private", matches=matches, private_query=True)
    counts = load(workspace).last_read
    assert all(f"c-{i}" in counts for i in range(20))


def test_wire_budget_counts_raw_bytes_before_json_decode(monkeypatch):
    from brigade import fleet_client
    from brigade.memory_hub_client import MemoryHubClient, urllib_transport
    from functools import partial

    requested = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            requested.append(size)
            return b" " * size

    monkeypatch.setattr(fleet_client, "_hub_open", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "MAX_BYTES", 10)
    client = live.BudgetClient(
        MemoryHubClient(
            "https://hub.example.test", "fake-token", transport=partial(urllib_transport, max_response_bytes=10)
        )
    )
    with pytest.raises(live.LiveEvalError, match="live evaluation budget exceeded"):
        client.call("status")
    assert requested == [11]


def test_record_use_sends_only_identity_and_session_to_hub(workspace, monkeypatch):
    from brigade import memory_hub_client as hubc

    requests = []
    client = hubc.MemoryHubClient(
        "https://hub.example.test",
        "fake-token",
        transport=lambda url, token, body, timeout: requests.append(body) or (201, {}),
    )
    monkeypatch.setattr(live, "MemoryHubClient", lambda *a, **kw: client)
    monkeypatch.setattr(
        hubc,
        "load_settings",
        lambda *a: hubc.HubSettings(mode="read", hub_url="https://hub.example.test", token="fake-token"),
    )
    context = {"session_id": "one", "session_kind": "interactive"}
    live.record_use(workspace, "published", "private card text", payload=context)
    assert len(requests) == 1
    assert requests[0]["session_id"] == "one"
    assert requests[0]["action"] == "retrieval_use"
    assert requests[0]["content_hash"] == live.text_hash("private card text")
    assert "private card text" not in json.dumps(requests)
    monkeypatch.setenv("BRIGADE_RUN_ID", "job")
    live.record_use(workspace, "published", "private card text", payload=context)
    assert len(requests) == 1


def test_single_query_vetoes_a_gold_drop_despite_equal_aggregate_metrics():
    from brigade.memory_retrieval_eval.adapters import bm25_adapter

    cards = [live._card("a", "needle\n"), live._card("b", "needle needle needle\n")]
    baseline = bm25_adapter(cards)
    assert [i for i, _ in baseline("needle", 20)] == ["b", "a"]
    after_text = "needle\n"
    proposal = {
        "proposal_id": "candidate",
        "targets": [
            {
                "scope": "fleet",
                "card_id": "b",
                "before_hash": live.text_hash(cards[1].body),
                "after_hash": live.text_hash(after_text),
                "after_text": after_text,
                "after_state": "live",
            }
        ],
    }
    query = QuerySpec("one", "needle", ("a", "b"), "observed")
    report = live.measure_candidate(cards, [query], proposal, baseline, {})
    assert report["adapters"]["before"]["overall"] == report["adapters"]["candidate"]["overall"]
    assert not report["gate"]["passed"]
    assert report["gate"]["authorizes"] is False
    assert report["live"]["veto_count"] == 1
