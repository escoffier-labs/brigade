"""Watch transport forwards only current failed card receipts."""

from __future__ import annotations

import pytest

from brigade import memory_compensation_watch as watch
from brigade.memory_proposal_io import sha256
from tests.test_memory_negative_outcomes import failed_card as failed_card
from tests.test_memory_outcome_evidence import capture


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
        assert signals[0]["card_id"] == "cache"
        assert signals[0]["evidence_id"].endswith("/receipt.json")
