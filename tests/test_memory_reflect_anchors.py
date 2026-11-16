"""Anchoring stays bounded and makes subsequent link repair addressable."""

import os

import pytest

from brigade import memory_reflect_anchors as anchors
from tests.test_hub_reflect_links import card_text, head, successor_pair, unit
from tests.test_memory_reflect_hub import setup as setup, run, writes


def test_non_clean_non_live_and_unverifiable_cards_are_skipped():
    heads = [head("a", "Claim.", state="live"), head("b", "Other.", scan="blocked", state="live")]
    heads += [head("c", "Retired.", state="retired"), head("d", "Unknown state.")]
    [[op]] = anchors.ops(heads)
    assert op["card_id"] == "a"
    assert anchors.coverage(heads)["clean_live_cards"] == 1
    assert anchors.coverage(heads)["fraction"] == 0
    heads[0]["text"] = "Claim. ^c-abcdef\n"
    assert anchors.coverage(heads)["fraction"] == 1


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_budget_and_status(setup, capsys):
    target, hub, _ = setup
    for i in range(30):
        hub._commit("card.put", "fleet", f"unanchored-{i:02}", card_text(f"Unique claim {i}.", None), "live", str(i))
    code, report = run(target, capsys, "run", "--limit", "100")
    assert code == 0
    assert (
        len([call for call in writes(hub) if call["action"] == "propose" and call["ops"][0]["type"] == "anchor_blocks"])
        == 25
    )
    assert report["skipped_reasons"]["anchor_deferred"] == 5
    code, second = run(target, capsys, "run", "--limit", "100")
    assert code == 0
    assert len([op for op in second["ops"] if op["type"] == "anchor_blocks"]) == 5
    code, status = run(target, capsys, "status")
    assert code == 0
    assert status["anchoring"]["clean_live_cards"] >= 30
    assert status["anchoring"]["source"] == "replica"


def test_anchoring_allows_later_broken_link_repair():
    heads = successor_pair()
    heads[0]["text"] = card_text("See [[old]] for more.", None)
    _, skipped, _ = unit(heads)
    assert skipped["link_missing_pin"] == 1
    # The hub adds only this suffix, as covered by its byte-preservation test.
    heads[0]["text"] = heads[0]["text"].replace("more.\n", "more. ^c-abcdef\n")
    [[op]], skipped, _ = unit(heads)
    assert "link_missing_pin" not in skipped
    assert op["text"] == "See [[new]] for more."


def test_hub_classifier_shared_table():
    import json
    from pathlib import Path

    cases = json.loads((Path(__file__).parent / "fixtures/anchor_spans.json").read_text())
    for case in cases:
        assert [block.text for block in anchors.blocks(case["text"])] == case["blocks"], case["name"]
        candidate = head(case["name"], case["text"], state="live")
        missing = any(not anchors.suffix(block) for block in case["blocks"])
        assert bool(anchors.ops([candidate])) == missing, case["name"]
        assert anchors.coverage([candidate])["fraction"] == int(not missing), case["name"]


def test_limit_counts_deferred_anchors(setup, capsys):
    target, hub, _ = setup
    for i in range(4):
        hub._commit("card.put", "fleet", f"unique-{i}", card_text(f"Unique {i}.", None), "live", str(i))
    code, report = run(target, capsys, "run", "--dry-run", "--limit", "1")
    assert code == 0
    # The exact duplicate proposal occupies the sole new-work slot.
    assert report["skipped_reasons"].get("anchor_deferred", 0) == 4


def test_status_never_creates_store(setup):
    from brigade import memory_hub_store as store

    target, _, _ = setup
    assert not store.root(target).exists()
    assert anchors.status(target) is None
    assert not store.root(target).exists()


def test_status_does_not_take_write_lock(setup, capsys, monkeypatch):
    from brigade import memory_hub_store as store

    target, _, _ = setup
    assert run(target, capsys, "run", "--dry-run")[0] == 0

    def forbidden(*args, **kwargs):
        pytest.fail("status must not acquire the write lock")

    monkeypatch.setattr(store, "locked", forbidden)
    assert anchors.status(target)["clean_live_cards"] == 2


@pytest.mark.parametrize("reason, expected", [("op has no delta", 0), ("invalid ops", 1)])
def test_anchor_no_delta_is_successful_skip(setup, capsys, monkeypatch, reason, expected):
    target, hub, _ = setup
    hub.cards.clear()
    hub._commit("card.put", "fleet", "unique", card_text("Unique.", None), "live", "unique")
    monkeypatch.setattr(hub, "do_propose", lambda body: (422, {"code": "memory_proposal_invalid", "error": reason}))
    code, report = run(target, capsys, "run")
    assert code == expected
    assert report["status"] == ("completed" if expected == 0 else "failed")


def test_partly_anchored_card_stays_covered_after_revision_change():
    candidate = head("mixed", "Use `pytest`.\n\n1. One.\n2. Two. ^c-abcdef   \n", state="live")
    for revision in (1, 2, 3):
        candidate["revision"] = revision
        assert anchors.ops([candidate]) == []
        assert anchors.coverage([candidate])["fraction"] == 1


@pytest.mark.parametrize("limit, budget, deferred", [(1, 25, 3), (100, 2, 2)])
def test_held_anchors_count_deferred_for_limit_and_budget(setup, capsys, monkeypatch, limit, budget, deferred):
    from brigade import memory_reflect_policy as policy

    target, hub, _ = setup
    hub.cards.clear()
    for i in range(4):
        hub._commit("card.put", "fleet", f"unique-{i}", card_text(f"Unique {i}.", None), "live", str(i))
    assert run(target, capsys, "run")[0] == 0
    policy.set_enabled(target, True)
    monkeypatch.setattr(policy, "attempt", lambda *args, **kwargs: {"state": "pending"})
    monkeypatch.setattr(anchors, "MAX_PROPOSALS", budget)
    code, report = run(target, capsys, "run", "--apply", "--limit", str(limit))
    assert code == 0
    assert report["skipped_reasons"]["anchor_deferred"] == deferred
    assert sum(op["type"] == "anchor_blocks" for op in report["ops"]) == 4 - deferred
