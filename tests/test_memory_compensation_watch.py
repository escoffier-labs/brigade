"""Watch transport forwards only current failed card receipts."""

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
def test_negative_signals_require_current_receipt_card_and_scope(tmp_path, failed_card, drift):
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
    signals = watch.negative_signals(tmp_path, [head], head["scope"])
    if drift:
        assert signals == []
    else:
        assert len(signals) == 1
        assert signals[0].endswith("/receipt.json")


def test_negative_history_is_batched_without_losing_any_ids(tmp_path, monkeypatch):
    ids = [f"verifier/workspace/failed-{n}" for n in range(130)]
    monkeypatch.setattr(watch, "negative_signals", lambda *args: ids)
    calls = []

    class Client:
        def call(self, action, **fields):
            if action == "status":
                return {"capabilities": [watch.CAPABILITY]}
            calls.append(fields)
            return {"results": [], "next_after": None}

    assert watch.run(tmp_path, Client(), ("repo",), [], "epoch") == []
    assert [len(c["negative_outcomes"]) for c in calls] == [64, 64, 2]
    assert [i for c in calls for i in c["negative_outcomes"]] == ids


def test_retained_intact_negative_history_does_not_raise(tmp_path, failed_card, monkeypatch):
    path, receipt, _ = failed_card
    assert capture(tmp_path, receipt) == 0
    rows, warnings, unsupported = watch.evidence.read_evidence(tmp_path)
    assert not warnings and not unsupported
    monkeypatch.setattr(watch.evidence, "read_evidence", lambda target: (rows * 65, [], False))
    head = {"scope": "repo", "card_id": "cache", "content_hash": sha256(path.read_bytes())}
    assert watch.negative_signals(tmp_path, [head], "repo") == [rows[0]["evidence_id"]]
