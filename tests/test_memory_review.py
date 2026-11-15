"""Promotion ranking uses observed opens without changing cards or state."""

from datetime import date, timedelta
import json
import time

import pytest

from brigade import memory_card_reads, memory_fairness, memory_hooks, memory_hub_store, memory_review
from brigade import memory_verify_state
from brigade.claude_hooks import hub_recall
from brigade.cli import main
from brigade.memory_verify_cmd import _binding


def card(target, name, *, trust="agent-unreviewed", scope=None):
    path = target / "memory/cards" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    scope_line = f"scope: {scope}\n" if scope else ""
    path.write_text(f"---\nid: {name}\ntrust: {trust}\n{scope_line}---\nA saved fact.\n")
    return path


def opened(target, session, name, days):
    def change(ledger):
        ledger["opened"] = [name]
        ledger["interactive_open_days"] = {name: days}

    hub_recall.update(hub_recall.ledger_path(target, session), change)


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    monkeypatch.delenv("BRIGADE_RUN_ID", raising=False)
    monkeypatch.delenv("BRIGADE_SCANNER_RUN_ID", raising=False)


def test_heavy_use_ranks_first_and_every_file_is_unchanged(tmp_path):
    card(tmp_path, "a-unused")
    card(tmp_path, "z-used")
    card(tmp_path, "reviewed", trust="reviewed")
    today = date.today()
    days = [today.isoformat(), (today - timedelta(days=1)).isoformat()]
    opened(tmp_path, "one", "z-used", days)
    opened(tmp_path, "two", "z-used", days)
    memory_card_reads.record(tmp_path, ["z-used"])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = memory_review.review_payload(tmp_path)
    assert [row["card_id"] for row in report["items"]] == ["z-used", "a-unused"]
    use = report["items"][0]["use"]
    assert use["interactive_sessions"] == 2
    assert use["distinct_days"] == 2
    assert use["score"] == 221
    assert use["last_recorded_read"] == today.isoformat()
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "context", [{"session_kind": "worker"}, {"session_kind": "cron"}, {"agent_type": "subagent"}, {"agent_id": "child"}]
)
def test_automated_opens_do_not_outrank_interactive(tmp_path, monkeypatch, context):
    automated = card(tmp_path, "a-worker")
    interactive = card(tmp_path, "z-human")
    monkeypatch.setattr(memory_hooks, "resolve_memory_recall_target", lambda _: (tmp_path, "active"))
    for i in range(20):
        assert hub_recall.observe_read(
            tmp_path, {"session_id": f"worker-{i}", "tool_input": {"file_path": str(automated)}, **context}
        )
    assert hub_recall.observe_read(tmp_path, {"session_id": "human", "tool_input": {"file_path": str(interactive)}})
    rows = memory_review.review_payload(tmp_path)["items"]
    assert [row["card_id"] for row in rows] == ["z-human", "a-worker"]
    assert rows[1]["use"]["score"] == 0


def test_dispatch_environment_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_RUN_ID", "run-example")
    ledger = hub_recall._empty()
    memory_review.note_open(ledger, "card", {})
    assert ledger["interactive_open_days"] == {}


def test_ties_unused_and_legacy_records_keep_path_order(tmp_path):
    for name in ("b", "a", "c"):
        card(tmp_path, name)
    memory_card_reads.record(tmp_path, ["c"])
    for name in ("a", "b"):
        opened(tmp_path, name, name, [date.today().isoformat()])
    report = memory_review.review_payload(tmp_path)
    assert [row["card_id"] for row in report["items"]] == ["a", "b", "c"]
    assert report["items"][-1]["use"]["score"] == 0


def replica(target):
    snapshot = memory_hub_store.replica(target)
    snapshot.meta = {
        "hub": "hub-example",
        "principal": "example",
        "log_epoch": "epoch-example",
        "read_scopes": ["fleet"],
    }
    snapshot.heads = {
        snapshot.key("fleet", "hub-card"): {"scope": "fleet", "card_id": "hub-card", "trust": "agent-unreviewed"}
    }
    snapshot.save()
    return snapshot


def test_cached_hub_counts_are_scoped_bound_and_capped(tmp_path):
    snapshot = replica(tmp_path)
    key = memory_fairness.key_digest("fleet", ["hub-card"])
    state = memory_verify_state.fresh()
    state["binding"] = _binding("hub-example", "example", "epoch-example")
    state["scopes"] = {
        "fleet": {
            "counts_ms": int(time.time() * 1000),
            "cards": {key: {"seen": 1, "proposed": 0, "reads": [100, 100, 100000]}},
        }
    }
    memory_verify_state.save(tmp_path, state)
    card(tmp_path, "local")
    opened(tmp_path, "human", "local", [date.today().isoformat()])
    rows = memory_review.review_payload(tmp_path)["items"]
    assert [row["card_id"] for row in rows] == ["local", "hub-card"]
    assert rows[1]["use"]["hub_total_reads"] == 100000
    assert rows[1]["use"]["score"] == 9
    snapshot.meta["log_epoch"] = "another-epoch"
    snapshot.save()
    assert memory_review.review_payload(tmp_path)["items"][1]["use"]["hub_total_reads"] is None


def test_scope_collision_gets_no_unscoped_session_credit(tmp_path):
    replica(tmp_path)
    card(tmp_path, "hub-card")
    opened(tmp_path, "human", "hub-card", [date.today().isoformat()])
    assert all(row["use"]["interactive_sessions"] == 0 for row in memory_review.review_payload(tmp_path)["items"])


def test_malformed_legacy_and_future_hook_data_are_ignored(tmp_path):
    card(tmp_path, "a")
    path = hub_recall.ledger_path(tmp_path, "one")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "opened": ["a"],
                "interactive_open_days": {"a": [None, "bad", (date.today() + timedelta(days=1)).isoformat()]},
            }
        )
    )
    assert memory_review.review_payload(tmp_path)["items"][0]["use"]["score"] == 0
    path.write_text("invalid json")
    assert memory_review.review_payload(tmp_path)["items"][0]["use"]["score"] == 0


def test_cli_json_and_text(tmp_path, capsys):
    card(tmp_path, "a")
    assert main(["memory", "review", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["use"]["interactive_sessions"] == 0
    assert main(["memory", "review", "--target", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "score=0 sessions=0 days=0 hub_reads=None" in text


def test_matching_local_scope_and_cached_head_are_one_item(tmp_path):
    replica(tmp_path)
    card(tmp_path, "hub-card", scope="fleet")
    opened(tmp_path, "human", "hub-card", [date.today().isoformat()])
    rows = memory_review.review_payload(tmp_path)["items"]
    assert len(rows) == 1
    assert rows[0]["path"] == "memory/cards/hub-card.md"
    assert rows[0]["use"]["interactive_sessions"] == 1


def test_invalid_local_scope_does_not_break_listing(tmp_path):
    card(tmp_path, "local", scope="[bad, scope]")
    assert memory_review.review_payload(tmp_path)["items"][0]["scope"] is None
