"""Scheduled watches leave receipt resolution to the hub's attested evidence store."""

from __future__ import annotations

import os

import pytest

from brigade import memory_compensation_watch as watch
from brigade.memory_proposal_io import sha256
from tests.test_memory_negative_outcomes import failed_card as failed_card
from tests.test_memory_outcome_evidence import capture


pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


@pytest.mark.parametrize("drift", [None, "card", "scope", "receipt"])
def test_local_negative_receipts_are_never_forwarded(tmp_path, failed_card, drift):
    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    text = path.read_text()
    head = {"scope": "repo", "card_id": "cache", "content_hash": sha256(text.encode())}
    if drift == "card":
        path.write_text(text + "A later edit.\n")
    elif drift == "scope":
        head["scope"] = "fleet"
    elif drift == "receipt":
        stored = tmp_path / ".brigade/work/verify-runs" / receipt["run_id"] / "receipt.json"
        stored.write_text("{}\n")
    calls = []

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields)
            return {"results": [], "next_after": None}

    assert watch.run(Client(), ("repo",), "epoch") == []
    assert len(calls) == 1
    assert "negative_outcomes" not in calls[0]


def test_watch_paginates_each_scope_without_receipt_batches(tmp_path):
    calls = []
    cursor = "01MCER5BZ8B2Y78ED29HAFZZ10"

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields)
            return {"results": [{"result": "review_required"}], "next_after": cursor if "after" not in fields else None}

    result = watch.run(Client(), ("repo", "fleet"), "epoch")
    assert len(result) == 4
    assert [c["scope"] for c in calls] == ["repo", "repo", "fleet", "fleet"]
    assert [c.get("after") for c in calls] == [None, cursor, None, cursor]
    assert all("negative_outcomes" not in c for c in calls)


def test_watch_ignores_local_evidence_history(tmp_path):
    evidence = tmp_path / ".brigade/memory/evidence.jsonl"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("invalid local evidence\n" * 130)
    calls = []

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields)
            return {"results": [], "next_after": None}

    assert watch.run(Client(), ("repo",), "epoch") == []
    assert len(calls) == 1


def test_watch_treats_a_repeated_cursor_as_the_end_of_the_poll():
    cursor = "01MCER5BZ8B2Y78ED29HAFZZ10"
    calls = []

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields)
            return {"results": [{"result": "applied"}], "next_after": cursor}

    assert watch.run(Client(), ("repo",), "epoch") == [{"result": "applied"}, {"result": "applied"}]
    assert [call.get("after") for call in calls] == [None, cursor]


def test_watch_rejects_a_cursor_that_returns_to_an_earlier_page():
    first = "01MCER5BZ8B2Y78ED29HAFZZ10"
    second = "01MCER5BZ8B2Y78ED29HAFZZ11"
    following = {None: first, first: second, second: first}

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            return {"results": [], "next_after": following[fields.get("after")]}

    with pytest.raises(ValueError, match="invalid compensation watch cursor"):
        watch.run(Client(), ("repo",), "epoch")


def test_watch_budget_is_success_and_next_run_continues():
    from brigade import memory_hub_client as hubc

    class Client:
        def __init__(self):
            self.page = 0

        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            self.page += 1
            return {
                "results": [{"result": "applied"}] if self.page == 33 else [],
                "next_after": hubc.event_id() if self.page < 34 else None,
            }

    client = Client()
    assert watch.run(client, ("repo",), "epoch") == []
    assert client.page == 32
    assert watch.run(client, ("repo",), "epoch") == [{"result": "applied"}]
