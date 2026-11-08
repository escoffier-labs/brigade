"""Broken-link repair opens review-only replace_block proposals."""

import hashlib
import json

import pytest

from brigade import memory_hub_client as hubc, memory_reflect_links as links, memory_reflect_near as near
from tests.test_memory_reflect_hub import pytestmark as pytestmark, run, setup as setup, writes

SCOPE = "repo:links"
OTHER = "repo:other"
SECRET = "Zanzibarquill"


def card_text(claim, anchor="c-aaaaaa", meta="evidence: []"):
    tail = f" ^{anchor}" if anchor else ""
    return f"---\n{meta}\n---\n{claim}{tail}\n"


def put(hub, card_id, claim, anchor="c-aaaaaa", meta="evidence: []", scope=SCOPE):
    hub._commit("card.put", scope, card_id, card_text(claim, anchor, meta), "live", hubc.event_id())


def retire(hub, card_id, successor, scope=SCOPE):
    meta = f'status: superseded\nsuperseded_by: "{successor}"'
    hub._commit("card.put", scope, card_id, card_text("Retired claim.", "c-bbbbbb", meta), "live", hubc.event_id())


def configure(target, extra="", scopes=(SCOPE,)):
    names = ",".join(f'"{name}"' for name in scopes)
    (target / ".brigade/memory-care.toml").write_text(f'[hub]\nmode="read"\nread_scopes=[{names}]\n{extra}')


def head(card_id, text, revision=1, scope=SCOPE, trust="reviewed", scan="clean", **extra):
    return {
        "scope": scope,
        "card_id": card_id,
        "revision": revision,
        "text": text,
        "trust": trust,
        "scan_state": scan,
        **extra,
    }


def unit(heads, scope=SCOPE):
    stats = {"links": 0, "broken": 0, "repairs": 0}
    submissions, skipped = links.ops([h for h in heads if h["scope"] == scope], index=links.Index(heads), stats=stats)
    return submissions, skipped, stats


def proposed(hub):
    return [call for call in writes(hub) if call["action"] == "propose"]


def successor_pair(source_claim="See [[old]] for more."):
    return [
        head("src", card_text(source_claim)),
        head("old", card_text("Retired claim.", "c-bbbbbb", 'status: superseded\nsuperseded_by: "repo:links/new"')),
        head("new", card_text("Current claim.", "c-cccccc")),
    ]


def test_successor_repoint_opens_pinned_replace_block(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "src", "See [[old]] for more.")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    code, report = run(target, capsys, "run")
    assert code == 0 and report["status"] == "completed"
    [call] = proposed(hub)
    [op] = call["ops"]
    block = "See [[old]] for more. ^c-aaaaaa"
    assert op == {
        "type": "replace_block",
        "scope": SCOPE,
        "card_id": "src",
        "base_revision": hub.cards[(SCOPE, "src")]["revision"],
        "block_id": "c-aaaaaa",
        "expected_block_hash": hashlib.sha256(block.encode()).hexdigest(),
        "text": "See [[new]] for more.",
    }
    assert report["link_repair"] == {"max_proposals": 8, "links": 1, "broken": 1, "repairs": 1}
    assert "text" not in report["ops"][0]


def test_legacy_successor_forms_resolve():
    for value in ('"new"', "new", '"cards/new.md"', '"repo:links/new"'):
        heads = successor_pair()
        heads[1]["text"] = card_text("Retired claim.", "c-bbbbbb", f"status: superseded\nsuperseded_by: {value}")
        [[op]], skipped, _ = unit(heads)
        assert op["text"] == "See [[new]] for more." and skipped == {}


def test_successor_that_is_not_live_falls_through_to_no_target():
    heads = successor_pair()
    heads[2]["state"] = "retired"
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_no_target": 1}


def test_title_or_alias_match_after_normalization():
    new = head("g1", card_text("Gizmo claim.", "c-cccccc", 'title: Gizmo Notes\naliases: ["Widget Log"]'))
    for link in ("gizmo-notes", "GIZMO   notes", "Gizmo-Notes", "widget  log", "widget-LOG"):
        source = head("src", card_text(f"Read [[{link}]] first."))
        [[op]], skipped, _ = unit([source, new])
        assert op["text"] == "Read [[g1]] first." and skipped == {}


def test_card_id_resolves_exactly_and_is_never_broken():
    heads = [
        head("src", card_text("Read [[Gizmo-Notes]] first.")),
        head("gizmo-notes", card_text("Claim.", "c-cccccc")),
    ]
    submissions, skipped, stats = unit(heads)
    assert submissions == [] and skipped == {} and stats["broken"] == 0


def test_ambiguous_and_missing_targets_are_skipped_and_counted():
    twins = [
        head("one", card_text("A.", "c-cccccc", "title: Shared Name")),
        head("two", card_text("B.", "c-dddddd", "title: shared name")),
    ]
    source = head("src", card_text("Read [[shared-name]] and [[nowhere]]."))
    submissions, skipped, stats = unit([source, *twins])
    assert submissions == []
    assert skipped == {"link_ambiguous_target": 1, "link_no_target": 1}
    assert stats == {"links": 2, "broken": 2, "repairs": 0}


def test_two_retired_cards_with_different_successors_are_ambiguous():
    heads = [
        head("src", card_text("Read [[old]].")),
        head("old", card_text("R.", "c-bbbbbb", "status: superseded\nsuperseded_by: n1\naliases: [shared]")),
        head("old2", card_text("R.", "c-bbbbbb", "status: superseded\nsuperseded_by: n2\naliases: [old]")),
        head("n1", card_text("N.", "c-cccccc")),
        head("n2", card_text("M.", "c-dddddd")),
    ]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_ambiguous_target": 1}


def test_cross_scope_targets_are_skipped_and_never_repointed():
    heads = [
        head("src", card_text("Read [[elsewhere]] and [[old]].")),
        head("old", card_text("R.", "c-bbbbbb", f'status: superseded\nsuperseded_by: "{OTHER}/successor"')),
        head("elsewhere", card_text("E.", "c-cccccc"), scope=OTHER),
        head("successor", card_text("S.", "c-dddddd"), scope=OTHER),
    ]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_cross_scope_target": 2}


def test_alias_heading_prefix_and_extension_are_preserved():
    claim = "A [[old#Part One|shown]], [[old|text]], [[old#Head]], [[cards/old.md]], [[ old ]] and [[old#a|b#c]]."
    [[op]], skipped, _ = unit(successor_pair(claim))
    assert op["text"] == (
        "A [[new#Part One|shown]], [[new|text]], [[new#Head]], [[cards/new.md]], [[ new ]] and [[new#a|b#c]]."
    )
    assert skipped == {}


def test_one_op_per_block_even_with_several_links():
    claim = "First [[old]] then [[old]] and [[missing]]."
    [[op]], skipped, stats = unit(successor_pair(claim))
    assert op["text"] == "First [[new]] then [[new]] and [[missing]]."
    assert skipped == {"link_no_target": 1} and stats["repairs"] == 2


def test_code_spans_embeds_escapes_and_heading_only_links_are_untouched():
    claim = "Use `[[old]]` and ``a ` [[old]]`` but not ![[old]] or \\[[old]] or [[#Heading]] or [[|x]]."
    submissions, skipped, stats = unit(successor_pair(claim))
    assert submissions == [] and skipped == {} and stats["links"] == 0
    [[op]], _, _ = unit(successor_pair("Use `[[old]]` here but [[old]] there."))
    assert op["text"] == "Use `[[old]]` here but [[new]] there."


@pytest.mark.parametrize(
    "body",
    [
        "```\n[[old]]\n```\n",
        "~~~\n[[old]]\n~~~\n",
        "> [[old]]\n",
        "| a | b |\n| - | - |\n| [[old]] | x |\n",
        "<div>[[old]]</div>\n",
        "<!-- [[old]] -->\n",
    ],
)
def test_opaque_blocks_are_untouched_and_uncounted(body):
    heads = successor_pair()
    heads[0]["text"] = f"---\nevidence: []\n---\n{body}"
    submissions, skipped, stats = unit(heads)
    assert submissions == [] and skipped == {} and stats["broken"] == 0


def test_list_item_claims_are_repaired_with_their_marker():
    heads = successor_pair()
    heads[0]["text"] = "---\nevidence: []\n---\n- First [[old]]. ^c-aaaaaa\n- Second [[old]]. ^c-eeeeee\n"
    [ops], skipped, _ = unit(heads)
    assert [op["text"] for op in ops] == ["- First [[new]].", "- Second [[new]]."]
    assert [op["block_id"] for op in ops] == ["c-aaaaaa", "c-eeeeee"]


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"scan_state": "flagged"}, "link_non_clean"),
        ({"scan_state": None}, "link_non_clean"),
        ({"trust": "mystery"}, "link_unknown_trust"),
        ({"state": "retired"}, "link_inactive_card"),
    ],
)
def test_ineligible_cards_count_each_broken_link_and_propose_nothing(overrides, reason):
    heads = successor_pair("See [[old]] and [[gone]].")
    heads[0].update(overrides)
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {reason: 2}


def test_unanchored_blocks_are_counted_not_repaired():
    heads = successor_pair()
    heads[0]["text"] = card_text("See [[old]].", anchor=None)
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_missing_pin": 1}


@pytest.mark.parametrize("claim", ["See [[old]] and x^2.", "See [[old]]\ncontinued over two lines."])
def test_blocks_the_hub_would_refuse_are_counted(claim):
    submissions, skipped, _ = unit(successor_pair(claim))
    assert submissions == [] and skipped == {"link_unreplaceable_block": 1}


def test_submissions_hold_whole_cards_and_at_most_seven_ops():
    heads = [head("old", card_text("R.", "c-bbbbbb", 'status: superseded\nsuperseded_by: "new"'))]
    heads.append(head("new", card_text("N.", "c-cccccc")))
    for index in range(9):
        heads.append(head(f"s{index}", card_text(f"See [[old]] number {index}.", f"c-{index:06x}")))
    submissions, _, _ = unit(heads)
    assert [len(ops) for ops in submissions] == [7, 2]
    assert [op["card_id"] for op in submissions[0]] == [f"s{index}" for index in range(7)]
    many = [head("old", heads[0]["text"]), head("new", heads[1]["text"])]
    many.append(
        head("big", "---\nevidence: []\n---\n" + "".join(f"Link [[old]] {i}. ^c-{i:06x}\n\n" for i in range(9)))
    )
    many.append(head("small", card_text("See [[old]].", "c-ffffff")))
    submissions, _, _ = unit(many)
    assert [len(ops) for ops in submissions] == [7, 3]  # A card's ops split only past seven
    assert all(len(ops) <= links.CHUNK_OPS for ops in submissions)


def test_replacement_ops_are_never_tier_zero_or_one_cosmetic():
    # The hub treats replace_block as Tier 1 only when old and new wording are plain
    # ASCII letters and `.,!?` with identical words. Every link repair has `[` in both.
    [[op]], _, _ = unit(successor_pair())
    assert "[" in op["text"] and "[" in successor_pair()[0]["text"]
    assert op["type"] == "replace_block"


def test_dry_run_sends_nothing_and_reports_counts_without_text(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "src", f"{SECRET} sees [[old]] today.")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and report["candidate_count"] == 1
    assert writes(hub) == [] and hub.cards == before
    assert [op["type"] for op in report["ops"]] == ["replace_block"]
    assert SECRET not in json.dumps(report)
    assert report["link_repair"]["repairs"] == 1


def test_real_run_reports_and_state_hold_no_card_text(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "src", f"{SECRET} sees [[old]] and [[gone]].")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    code, report = run(target, capsys, "run")
    assert code == 0 and report["skipped_reasons"] == {"link_no_target": 1}
    assert SECRET not in json.dumps(report)
    assert SECRET in json.dumps(proposed(hub))  # The hub needs the wording to review it.
    state = (target / ".brigade/memory/reflect/schedule.json").read_text()
    assert SECRET not in state and "link_clusters" in state
    assert run(target, capsys, "status")[0] == 0


@pytest.mark.parametrize("value", ["-1", "33", "true", '"8"', "[]", "1.5"])
def test_cap_outside_allowed_range_fails_before_hub_calls(setup, capsys, value):
    target, hub, _ = setup
    configure(target, f"[reflect]\nlink_repair_max_proposals = {value}\n")
    code, report = run(target, capsys, "run")
    assert code == 1 and report["code"] == "hub_reflect_config"
    assert "link_repair_max_proposals" in report["error"]
    assert hub.calls == []


def test_cap_zero_defers_every_link_submission(setup, capsys):
    target, hub, _ = setup
    configure(target, "[reflect]\nlink_repair_max_proposals = 0\n")
    put(hub, "src", "See [[old]].")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    code, report = run(target, capsys, "run")
    assert code == 0 and proposed(hub) == [] and report["skipped_reasons"] == {"link_deferred": 1}


def populate(hub, count):
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    for index in range(count):
        put(hub, f"s{index:02d}", f"See [[old]] number {index}.", f"c-{index:06x}")


def test_cap_and_fair_scheduling_rotate_submissions(setup, capsys):
    target, hub, _ = setup
    configure(target, "[reflect]\nlink_repair_max_proposals = 1\n")
    populate(hub, 16)  # Submissions of 7, 7 and 2 ops.
    seen = []
    for _ in range(3):
        code, report = run(target, capsys, "run")
        assert code == 0 and len(report["proposals"]) == 1
        seen.append(proposed(hub)[-1]["ops"][0]["card_id"])
    assert seen == ["s00", "s07", "s14"]
    code, report = run(target, capsys, "run")
    assert code == 0 and report["proposals"] == [] and report["skipped_reasons"]["already_proposed"] == 3
    assert len(proposed(hub)) == 3


def test_rewriting_a_served_cluster_does_not_jump_the_line(setup, capsys):
    target, hub, _ = setup
    configure(target, "[reflect]\nlink_repair_max_proposals = 1\n")
    populate(hub, 16)
    assert run(target, capsys, "run")[0] == 0
    assert proposed(hub)[-1]["ops"][0]["card_id"] == "s00"
    # A rewrite takes a new revision, so the first submission is new again, but its key
    # keeps its proposed time and the never-proposed submissions go first.
    put(hub, "s00", "See [[old]] rewritten.", "c-000000")
    assert run(target, capsys, "run")[0] == 0
    assert proposed(hub)[-1]["ops"][0]["card_id"] == "s07"
    assert run(target, capsys, "run")[0] == 0
    assert proposed(hub)[-1]["ops"][0]["card_id"] == "s14"
    assert run(target, capsys, "run")[0] == 0
    assert proposed(hub)[-1]["ops"][0]["card_id"] == "s00"


def test_exact_merges_come_first_then_near_then_links(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "dup-a", "Same claim.", "c-111111")
    put(hub, "dup-b", "Same claim.", "c-222222")
    put(hub, "src", "See [[old]].")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    code, report = run(target, capsys, "run", "--limit", "1")
    assert code == 0
    assert [call["ops"][0]["type"] for call in proposed(hub)] == ["merge_exact_duplicates"]
    code, report = run(target, capsys, "run", "--dry-run")
    assert [op["type"] for op in report["ops"]] == ["replace_block"]


def test_scopes_resolve_only_within_themselves(setup, capsys):
    target, hub, _ = setup
    configure(target, scopes=(SCOPE, OTHER))
    put(hub, "src", "See [[elsewhere]].")
    put(hub, "elsewhere", "Elsewhere claim.", "c-cccccc", scope=OTHER)
    put(hub, "src2", "See [[title-one]].", "c-dddddd", scope=OTHER)
    put(hub, "t1", "Title claim.", "c-eeeeee", meta="title: Title One", scope=OTHER)
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0
    assert report["skipped_reasons"] == {"link_cross_scope_target": 1}
    assert [(op["scope"], op["card_id"], op["block_id"]) for op in report["ops"]] == [(OTHER, "src2", "c-dddddd")]


def test_proposal_quota_stops_link_submissions_without_failing(setup, capsys):
    target, hub, _ = setup
    configure(target)
    populate(hub, 3)
    hub.do_propose = lambda body: (429, {"code": "memory_proposal_principal_quota", "error": "full"})
    code, report = run(target, capsys, "run")
    assert code == 0 and report["skipped_reasons"]["proposal_quota"] == 1


def test_config_helper_defaults(tmp_path):
    assert links.settings(tmp_path) == links.DEFAULT_MAX_PROPOSALS == 8
    assert links.MAX_PROPOSALS_LIMIT == near.MAX_PROPOSALS_LIMIT == 32
