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


def _refusing(status, code, denied):
    calls = []

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields["scope"])
            if fields["scope"] == denied:
                raise watch.hubc.HubRefusal(status, {"code": code, "error": "refused"})
            return {"results": [{"result": "review_required"}], "next_after": None}

    return Client(), calls


def test_watch_skips_scopes_without_the_reflect_grant():
    client, calls = _refusing(403, watch.GRANT_MISSING, "fleet")
    unwatched = []
    result = watch.run(client, ("fleet", "repo"), "epoch", unwatched=unwatched)
    assert calls == ["fleet", "repo"]
    assert result == [{"result": "review_required"}]
    assert unwatched == ["fleet"]


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "memory_auth_invalid"), (403, "memory_principal_revoked"), (409, watch.GRANT_MISSING)],
)
def test_watch_raises_every_other_refusal(status, code):
    client, calls = _refusing(status, code, "fleet")
    unwatched = []
    with pytest.raises(watch.hubc.HubRefusal):
        watch.run(client, ("fleet", "repo"), "epoch", unwatched=unwatched)
    assert calls == ["fleet"]
    assert unwatched == []
