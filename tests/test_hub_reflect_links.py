"""Broken-link repair opens review-only replace_block proposals."""

import hashlib
import json

import pytest

from brigade import memory_hub_client as hubc, memory_reflect_links as links, memory_reflect_near as near
from tests.test_hub_reflect_near_duplicates import group_claim
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
        head("old", card_text("R.", "c-bbbbbb", 'status: superseded\nsuperseded_by: n1\naliases: ["shared"]')),
        head("old2", card_text("R.", "c-bbbbbb", 'status: superseded\nsuperseded_by: n2\naliases: ["old"]')),
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


# --- target admission: the hub never scans or shows the destination of a repair ---------------


def with_new(**overrides):
    heads = successor_pair()
    heads[2].update(overrides)
    return heads


@pytest.mark.parametrize(
    "overrides",
    [{"scan_state": "flagged"}, {"scan_state": None}, {"trust": "imported"}, {"trust": "mystery"}],
    ids=["flagged", "unscanned", "mixed-trust", "unknown-trust"],
)
def test_successor_must_be_clean_and_share_the_source_trust(overrides):
    submissions, skipped, stats = unit(with_new(**overrides))
    assert submissions == [] and skipped == {"link_untrusted_target": 1} and stats["repairs"] == 0


@pytest.mark.parametrize(
    "overrides", [{"scan_state": "flagged"}, {"trust": "agent-unreviewed"}], ids=["flagged", "mixed-trust"]
)
def test_name_matched_target_must_be_clean_and_share_the_source_trust(overrides):
    new = head("g1", card_text("Gizmo claim.", "c-cccccc", "title: Gizmo Notes"), **overrides)
    submissions, skipped, _ = unit([head("src", card_text("Read [[gizmo-notes]].")), new])
    assert submissions == [] and skipped == {"link_untrusted_target": 1}


def test_a_clean_same_trust_target_is_still_repaired():
    new = head("g1", card_text("Gizmo claim.", "c-cccccc", "title: Gizmo Notes"), trust="imported")
    source = head("src", card_text("Read [[gizmo-notes]]."), trust="imported")
    [[op]], skipped, _ = unit([source, new])
    assert op["text"] == "Read [[g1]]." and skipped == {}


@pytest.mark.parametrize(
    "aliases",
    ["foo, bar", "[unterminated", "[1, 2]", '"just-a-string"', "{}", "[foo, bar]"],
)
def test_malformed_aliases_make_a_successor_unusable_and_are_counted(aliases):
    heads = with_new()
    heads[2]["text"] = card_text("Current claim.", "c-cccccc", f"aliases: {aliases}")
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_invalid_metadata": 1}


def test_malformed_aliases_grant_no_names_and_are_never_parsed_loosely():
    new = head("g1", card_text("Gizmo claim.", "c-cccccc", "title: Gizmo Notes\naliases: Widget Log, Gizmo"))
    for link in ("widget log", "gizmo", "gizmo notes"):
        submissions, skipped, _ = unit([head("src", card_text(f"Read [[{link}]].")), new])
        assert submissions == [] and skipped == {"link_no_target": 1}


def test_ambiguous_multiline_aliases_are_malformed():
    heads = with_new()
    heads[2]["text"] = card_text("Current claim.", "c-cccccc", "aliases:\n  - a\n  - b")
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_invalid_metadata": 1}


def test_refused_targets_are_counted_without_card_text(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "src", f"{SECRET} sees [[old]].")
    retire(hub, "old", "repo:links/new")
    put(hub, "new", "Current claim.", "c-cccccc")
    hub.scan_states = {(SCOPE, "new"): "flagged"}
    code, report = run(target, capsys, "run")
    assert code == 0 and proposed(hub) == []
    assert report["skipped_reasons"] == {"link_untrusted_target": 1}
    assert SECRET not in json.dumps(report)


# --- retired names: titles, topics and aliases follow the successor rule ----------------------


def retired_guide(card_id="old", title="Old Guide", extra=""):
    meta = f'status: superseded\nsuperseded_by: "repo:links/new"\ntitle: {title}{extra}'
    return head(card_id, card_text("Retired claim.", "c-bbbbbb", meta))


def test_a_retired_title_follows_the_successor():
    for link in ("Old Guide", "old-guide", "OLD   guide"):
        heads = [head("src", card_text(f"See [[{link}]].")), retired_guide(), head("new", card_text("N.", "c-cccccc"))]
        [[op]], skipped, _ = unit(heads)
        assert op["text"] == "See [[new]]." and skipped == {}


def test_retired_topics_and_aliases_follow_the_successor():
    for extra, link in (("\ntopic: Legacy Topic", "legacy topic"), ('\naliases: ["Older Name"]', "older-name")):
        heads = [
            head("src", card_text(f"See [[{link}]].")),
            retired_guide(extra=extra),
            head("new", card_text("N.", "c-cccccc")),
        ]
        [[op]], skipped, _ = unit(heads)
        assert op["text"] == "See [[new]]." and skipped == {}


def test_a_live_card_with_the_normalized_name_is_a_collision_not_a_redirect():
    live = head("old-guide", card_text("Unrelated.", "c-dddddd"))
    heads = [
        head("src", card_text("See [[Old Guide]].")),
        retired_guide(),
        head("new", card_text("N.", "c-cccccc")),
        live,
    ]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_ambiguous_target": 1}
    live["text"] = card_text("Unrelated.", "c-dddddd", "title: OLD   GUIDE")
    live["card_id"] = "other"
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_ambiguous_target": 1}


def test_two_retired_cards_sharing_a_name_are_ambiguous_even_with_one_successor():
    heads = [
        head("src", card_text("See [[Old Guide]].")),
        retired_guide("old-a"),
        retired_guide("old-b"),
        head("new", card_text("N.", "c-cccccc")),
    ]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_ambiguous_target": 1}


def test_a_retired_name_without_a_live_successor_never_falls_back_to_a_live_name():
    heads = [
        head("src", card_text("See [[Old Guide]].")),
        retired_guide(),
        head("new", card_text("N.", "c-cccccc"), state="retired"),
    ]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_no_target": 1}


def test_a_retired_title_with_a_cross_scope_successor_is_skipped():
    old = retired_guide()
    old["text"] = old["text"].replace("repo:links/new", f"{OTHER}/new")
    heads = [head("src", card_text("See [[Old Guide]].")), old, head("new", card_text("N.", "c-cccccc"), scope=OTHER)]
    submissions, skipped, _ = unit(heads)
    assert submissions == [] and skipped == {"link_cross_scope_target": 1}


# --- lanes alternate, so recurring near-duplicate traffic cannot starve link repairs ------------


def near_pairs(hub, count):
    for group in range(count):
        for member in range(2):
            put(hub, f"g{group}-{member}", group_claim(group, member), f"c-{0x10000 + group * 2 + member:06x}")


def link_types(hub):
    return [call["ops"][0]["type"] for call in proposed(hub)]


def state_of(target):
    return json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())


def test_limit_one_serves_both_lanes_in_turn(setup, capsys):
    target, hub, _ = setup
    configure(target)
    near_pairs(hub, 10)
    populate(hub, 1)
    for _ in range(4):
        assert run(target, capsys, "run", "--limit", "1")[0] == 0
    # Ten near clusters would otherwise fill every run, so the link repair waits for them all.
    assert link_types(hub)[:2] == ["supersede_card", "replace_block"]
    assert set(link_types(hub)[2:]) == {"supersede_card"}
    assert state_of(target)["first_lane"] in {"near_clusters", "link_clusters"}


def test_quota_that_admits_one_proposal_per_run_still_reaches_link_repairs(setup, capsys, monkeypatch):
    target, hub, _ = setup
    configure(target)
    near_pairs(hub, 10)
    populate(hub, 1)
    original = hub.do_propose
    sent = {"run": 0}

    def quota(body):
        if sent["run"] >= 1:
            return 429, {"code": "memory_proposal_principal_quota", "error": "full"}
        sent["run"] += 1
        return original(body)

    monkeypatch.setattr(hub, "do_propose", quota)
    for _ in range(3):
        sent["run"] = 0
        code, report = run(target, capsys, "run")
        assert code == 0 and report["status"] == "completed"
    assert "replace_block" in link_types(hub)
    assert link_types(hub).index("replace_block") <= 1


def test_a_run_that_serves_nothing_keeps_the_lane_order(setup, capsys, monkeypatch):
    target, hub, _ = setup
    configure(target)
    populate(hub, 1)
    monkeypatch.setattr(
        hub, "do_propose", lambda body: (429, {"code": "memory_proposal_principal_quota", "error": "full"})
    )
    assert run(target, capsys, "run")[0] == 0
    assert "first_lane" not in state_of(target)


def test_lane_order_is_validated_in_the_schedule_state(setup, capsys):
    target, hub, _ = setup
    configure(target)
    populate(hub, 1)
    assert run(target, capsys, "run")[0] == 0
    path = target / ".brigade/memory/reflect/schedule.json"
    state = json.loads(path.read_text())
    assert state["first_lane"] == "link_clusters"
    state["first_lane"] = "bogus"
    path.write_text(json.dumps(state))
    from brigade import memory_reflect_hub as reflect

    with pytest.raises(ValueError):
        reflect.schedule_status(target)
