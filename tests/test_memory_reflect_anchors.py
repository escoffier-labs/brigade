"""Anchoring stays bounded and makes subsequent link repair addressable."""

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
