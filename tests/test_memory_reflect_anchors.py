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


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_limit_counts_deferred_anchors(setup, capsys):
    target, hub, _ = setup
    for i in range(4):
        hub._commit("card.put", "fleet", f"unique-{i}", card_text(f"Unique {i}.", None), "live", str(i))
    code, report = run(target, capsys, "run", "--dry-run", "--limit", "1")
    assert code == 0
    # The exact duplicate proposal occupies the sole new-work slot.
    assert report["skipped_reasons"].get("anchor_deferred", 0) == 4


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_status_never_creates_store(setup):
    from brigade import memory_hub_store as store

    target, _, _ = setup
    assert not store.root(target).exists()
    assert anchors.status(target) is None
    assert not store.root(target).exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_status_does_not_take_write_lock(setup, capsys, monkeypatch):
    from brigade import memory_hub_store as store

    target, _, _ = setup
    assert run(target, capsys, "run", "--dry-run")[0] == 0

    def forbidden(*args, **kwargs):
        pytest.fail("status must not acquire the write lock")

    monkeypatch.setattr(store, "locked", forbidden)
    assert anchors.status(target)["clean_live_cards"] == 2


@pytest.mark.parametrize("hub_code, expected", [("memory_anchor_no_delta", 0), ("memory_proposal_invalid", 1)])
@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_anchor_no_delta_is_successful_skip(setup, capsys, monkeypatch, hub_code, expected):
    target, hub, _ = setup
    hub.cards.clear()
    hub._commit("card.put", "fleet", "unique", card_text("Unique.", None), "live", "unique")
    monkeypatch.setattr(
        hub,
        "do_propose",
        lambda body: (
            422,
            {
                "code": hub_code,
                "error": "proposal refused because at least one op is invalid",
                "reason": "proposal refused because at least one op is invalid",
                "ops": [{"index": 0, "valid": False, "reason": "op has no delta"}],
            },
        ),
    )
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
@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
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


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_human_anchor_holds_do_not_starve_automated_heads(setup, capsys, monkeypatch):
    from brigade import memory_reflect_policy as policy

    target, hub, _ = setup
    hub.cards.clear()
    for i in range(anchors.MAX_PROPOSALS + 1):
        hub._commit("card.put", "fleet", f"human-{i:02}", card_text(f"Human claim {i}.", None), "live", str(i))
    for i in range(2):
        hub._commit("card.put", "fleet", f"robot-{i}", card_text(f"Robot claim {i}.", None), "live", str(i))
    policy.set_enabled(target, True)
    attempted = []

    def attempt(target, client, proposal_id, ops, epoch, **kwargs):
        if ops[0]["type"] != "anchor_blocks":
            return {"state": "pending"}
        ident = ops[0]["card_id"]
        attempted.append(ident)
        if ident.startswith("human"):
            return {
                "state": "pending",
                "reason": "auto_apply_held",
                "checks": [{"gate": "candidate", "state": "held", "code": "tier_requires_review"}],
            }
        text = hub.cards[("fleet", ident)]["text"].replace(".\n", ". ^c-abcdef\n")
        hub._commit("proposal.apply", "fleet", ident, text, "live", ident)
        return {"state": "applied", "checks": []}

    monkeypatch.setattr(policy, "attempt", attempt)
    for _ in range(3):
        assert run(target, capsys, "run", "--apply", "--limit", "100")[0] == 0
    assert attempted.count("robot-0") == attempted.count("robot-1") == 1
    assert all(attempted.count(f"human-{i:02}") == 1 for i in range(anchors.MAX_PROPOSALS + 1))
    hub._commit("card.put", "fleet", "human-00", card_text("Changed human claim.", None), "live", "changed")
    assert run(target, capsys, "run", "--apply", "--limit", "100")[0] == 0
    assert attempted.count("human-00") == 2


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)
def test_no_delta_does_not_hide_a_real_refusal(setup, capsys, monkeypatch):
    target, hub, _ = setup
    hub.cards.clear()
    for ident in ("already", "refused"):
        hub._commit("card.put", "fleet", ident, card_text(f"Claim {ident}.", None), "live", ident)

    def refuse(body):
        code = "memory_anchor_no_delta" if body["ops"][0]["card_id"] == "already" else "memory_proposal_invalid"
        return 422, {
            "code": code,
            "error": "proposal refused because at least one op is invalid",
            "ops": [
                {
                    "index": 0,
                    "valid": False,
                    "reason": "op has no delta" if code == "memory_anchor_no_delta" else "card missing",
                }
            ],
        }

    monkeypatch.setattr(hub, "do_propose", refuse)
    code, report = run(target, capsys, "run")
    assert code == 1
    assert report["error"] == "all_ops_refused"


@pytest.mark.parametrize("whitespace", ["", "  ", "\t"])
def test_trailing_whitespace_anchors_support_link_and_near_reflection(whitespace):
    import hashlib
    from brigade import memory_proposal_v2_ops as delta, memory_reflect_near as near
    from tests.test_hub_reflect_near_duplicates import head as near_head, card_text as near_text, words

    heads = successor_pair()
    heads[0]["text"] = heads[0]["text"].replace("^c-aaaaaa\n", f"^c-aaaaaa{whitespace}\n")
    [[repair]], skipped, _ = unit(heads)
    assert "link_missing_pin" not in skipped
    assert repair["block_id"] == "c-aaaaaa"
    block = delta.blocks(heads[0]["text"])[0]
    assert block.text.endswith(whitespace)
    assert repair["expected_block_hash"] == hashlib.sha256(block.text.encode()).hexdigest()
    assert delta.claim_body(heads[0]["text"]).endswith(f"See [[old]] for more.{whitespace}\n")
    pair = [
        near_head("a", near_text(words(23), "c-aaaaaa").replace("^c-aaaaaa\n", f"^c-aaaaaa{whitespace}\n"), 1),
        near_head("b", near_text(words(23, "other"), "c-bbbbbb").replace("^c-bbbbbb\n", f"^c-bbbbbb{whitespace}\n"), 2),
    ]
    [[operation]], skipped = near.ops(pair, limit=0.9, budget=near.Budget())
    assert "near_missing_pin" not in skipped
    assert operation["block_id"] == "c-bbbbbb"
    assert operation["expected_block_hash"] == delta.blocks(pair[1]["text"])[0].sha256
