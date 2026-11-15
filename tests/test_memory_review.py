"""Promotion ranking uses observed opens without changing cards or state."""

from datetime import date, timedelta
import json
import os
import time

import pytest

from brigade import memory_card_reads, memory_fairness, memory_hooks, memory_hub_store, memory_review
from brigade import memory_verify_state
from brigade.claude_hooks import hub_recall
from brigade.cli import main
from brigade.memory_verify_cmd import _binding

# memory_verify_state.save replaces the file through directory descriptors, which Windows lacks.
needs_dir_fd = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


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


@needs_dir_fd
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


def test_redirected_hook_opens_are_reviewed_at_memory_root(tmp_path):
    from brigade import config
    from brigade.selection import Selection

    target = tmp_path / "repo"
    memory = tmp_path / "shared-memory"
    config.write_config(
        target,
        config.Config(
            version=1,
            selection=Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]),
            memory_recall_target=str(memory),
        ),
    )
    path = card(memory, "opened")
    assert memory_hooks.resolve_memory_recall_target(target) == (memory, "active")
    assert hub_recall.observe_read(target, {"session_id": "human", "tool_input": {"file_path": str(path)}})
    row = memory_review.review_payload(memory)["items"][0]
    assert row["use"]["interactive_sessions"] == 1
    assert row["use"]["distinct_days"] == 1
    assert row["use"]["last_recorded_read"] == date.today().isoformat()
    # Hub capture still consumes the wired target's opened ledger.
    assert hub_recall.load(hub_recall.ledger_path(target, "human"))["opened"] == ["opened"]


@pytest.mark.parametrize("operation", ["replica", "readable_heads"])
@pytest.mark.parametrize("error", [OSError, ValueError, memory_hub_store.StoreError])
def test_replica_errors_preserve_local_queue(tmp_path, monkeypatch, operation, error):
    replica(tmp_path)
    card(tmp_path, "local", scope="fleet")
    opened(tmp_path, "human", "local", [date.today().isoformat()])

    def fail(*args, **kwargs):
        raise error("unreadable replica")

    if operation == "replica":
        monkeypatch.setattr(memory_hub_store, operation, fail)
    else:
        monkeypatch.setattr(memory_hub_store.Replica, operation, fail)
    rows = memory_review.review_payload(tmp_path)["items"]
    assert [row["card_id"] for row in rows] == ["local"]
    assert rows[0]["use"]["interactive_sessions"] == 1
    assert rows[0]["use"]["hub_total_reads"] is None


def test_corrupt_replica_preserves_local_queue(tmp_path):
    snapshot = replica(tmp_path)
    (snapshot.directory / "meta.json").write_text('{"heads_sha256": "invalid"}')
    card(tmp_path, "local")
    rows = memory_review.review_payload(tmp_path)["items"]
    assert [row["card_id"] for row in rows] == ["local"]
    assert rows[0]["use"]["hub_total_reads"] is None


@needs_dir_fd
def test_stale_hub_counts_are_unavailable(tmp_path):
    from brigade.memory_verify_cmd import SNAPSHOT_MAX_AGE_MS

    replica(tmp_path)
    key = memory_fairness.key_digest("fleet", ["hub-card"])
    state = memory_verify_state.fresh()
    state["binding"] = _binding("hub-example", "example", "epoch-example")
    state["scopes"] = {
        "fleet": {
            "counts_ms": int(time.time() * 1000) - SNAPSHOT_MAX_AGE_MS - 1,
            "cards": {key: {"seen": 1, "proposed": 0, "reads": [1, 1, 20]}},
        }
    }
    memory_verify_state.save(tmp_path, state)
    use = memory_review.review_payload(tmp_path)["items"][0]["use"]
    assert use["hub_total_reads"] is None
    assert use["score"] == 0


def test_corrupt_verify_state_drops_hub_counts(tmp_path):
    replica(tmp_path)
    path = tmp_path / memory_verify_state.STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid json")
    with pytest.raises(memory_verify_state.VerifyStateError):
        memory_verify_state.load(tmp_path)
    rows = memory_review.review_payload(tmp_path)["items"]
    assert [row["card_id"] for row in rows] == ["hub-card"]
    assert rows[0]["use"]["hub_total_reads"] is None


def test_retired_hub_heads_are_excluded(tmp_path):
    snapshot = replica(tmp_path)
    snapshot.heads[snapshot.key("fleet", "retired")] = {
        "scope": "fleet",
        "card_id": "retired",
        "trust": "agent-unreviewed",
        "state": "retired",
    }
    snapshot.save()
    assert [row["card_id"] for row in memory_review.review_payload(tmp_path)["items"]] == ["hub-card"]


def test_duplicate_local_ids_get_no_session_credit(tmp_path):
    first = card(tmp_path, "shared")
    duplicate = first.with_name("duplicate.md")
    duplicate.write_bytes(first.read_bytes())
    opened(tmp_path, "human", "shared", [date.today().isoformat()])
    rows = memory_review.review_payload(tmp_path)["items"]
    assert len(rows) == 2
    assert all(row["card_id"] == "shared" for row in rows)
    assert all(row["use"]["interactive_sessions"] == 0 for row in rows)
    assert all(row["use"]["distinct_days"] == 0 for row in rows)
    assert all(row["use"]["score"] == 0 for row in rows)


def test_interactive_open_key_cap_survives_load_update(tmp_path):
    path = hub_recall.ledger_path(tmp_path, "human")

    def fill(ledger):
        for i in range(201):
            memory_review.note_open(ledger, f"card-{i:03d}", {})

    hub_recall.update(path, fill)
    ledger = hub_recall.load(path)
    assert len(ledger["interactive_open_days"]) == 200
    assert "card-200" not in ledger["interactive_open_days"]
    ledger = hub_recall.update(path, lambda value: memory_review.note_open(value, "card-000", {}))
    assert hub_recall.load(path) == ledger
    assert ledger["interactive_open_days"]["card-000"] == [date.today().isoformat()]


def test_interactive_open_days_truncate_and_survive_load_update(tmp_path):
    path = hub_recall.ledger_path(tmp_path, "human")
    path.parent.mkdir(parents=True)
    days = [(date.today() - timedelta(days=i)).isoformat() for i in reversed(range(400))]
    path.write_text(json.dumps({"v": 1, "opened": ["card"], "interactive_open_days": {"card": days}}))
    assert hub_recall.load(path)["interactive_open_days"]["card"] == days[-366:]
    ledger = hub_recall.update(path, lambda value: memory_review.note_open(value, "card", {}))
    assert ledger["interactive_open_days"]["card"] == days[-366:]
    assert hub_recall.load(path) == ledger
