"""Near-duplicate hub reflection opens review-only supersede proposals."""

import hashlib
import json

import pytest

from brigade import cli, memory_hub_client as hubc, memory_reflect_near as near
from tests.test_memory_reflect_hub import pytestmark as pytestmark, run, setup as setup, writes

SCOPE = "repo:near"
SECRET = "Zanzibarquill"
CAP = near.POSTING_CAP


def words(count, tail="end"):
    """A claim of `count` words whose last word is `tail`."""
    return " ".join([SECRET, *(f"w{i}" for i in range(1, count - 1)), tail])


def card_text(claim, anchor, meta="evidence: []"):
    return f"---\n{meta}\n---\n{claim}. ^{anchor}\n"


def put(hub, card_id, claim, anchor, meta="evidence: []", scope=SCOPE):
    hub._commit("card.put", scope, card_id, card_text(claim, anchor, meta), "live", hubc.event_id())


def configure(target, extra=""):
    (target / ".brigade/memory-care.toml").write_text(f'[hub]\nmode="read"\nread_scopes=["{SCOPE}"]\n{extra}')


def head(card_id, text, revision, trust="reviewed", scan="clean"):
    return {"scope": SCOPE, "card_id": card_id, "revision": revision, "text": text, "trust": trust, "scan_state": scan}


def anchor(index):
    return f"c-{index:06x}"


def proposed(hub):
    return [call for call in writes(hub) if call["action"] == "propose"]


def test_cluster_at_threshold_opens_pinned_supersede(setup, capsys):
    # 23 words give 19 shingles. One changed word leaves 18 of 20: Jaccard 0.9.
    target, hub, _ = setup
    configure(target)
    put(hub, "older", words(23), "c-aaaaaa")
    put(hub, "newer", words(23, "other"), "c-bbbbbb")
    code, report = run(target, capsys, "run")
    assert code == 0 and report["status"] == "completed"
    [call] = proposed(hub)
    [op] = call["ops"]
    older, newer = hub.cards[(SCOPE, "older")], hub.cards[(SCOPE, "newer")]
    block = f"{words(23, 'other')}. ^c-bbbbbb"
    assert op == {
        "type": "supersede_card",
        "scope": SCOPE,
        "card_id": "newer",
        "base_revision": newer["revision"],
        "block_id": "c-bbbbbb",
        "expected_block_hash": hashlib.sha256(block.encode()).hexdigest(),
        "other_card": {"scope": SCOPE, "card_id": "older", "base_revision": older["revision"]},
    }
    assert report["near_duplicate"]["clusters"] == 1
    assert report["near_duplicate"]["threshold"] == 0.9


def test_just_under_threshold_needs_a_lower_configured_threshold(setup, capsys):
    # 22 words give 18 shingles. One changed word leaves 17 of 19: Jaccard 0.89.
    target, hub, _ = setup
    configure(target)
    put(hub, "older", words(22), "c-aaaaaa")
    put(hub, "newer", words(22, "other"), "c-bbbbbb")
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and report["ops"] == [] and report["near_duplicate"]["clusters"] == 0
    configure(target, "[reflect]\nnear_duplicate_threshold = 0.85\n")
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and [op["type"] for op in report["ops"]] == ["supersede_card"]


@pytest.mark.parametrize("value", ["0.49", "1.01", "true", '"0.9"', "[]"])
def test_threshold_outside_allowed_range_fails_before_hub_calls(setup, capsys, value):
    target, hub, _ = setup
    configure(target, f"[reflect]\nnear_duplicate_threshold = {value}\n")
    code, report = run(target, capsys, "run")
    assert code == 1 and report["code"] == "hub_reflect_config"
    assert "near_duplicate_threshold" in report["error"]
    assert hub.calls == []


def test_keeper_is_least_recent_revision_then_lowest_card_id():
    texts = {name: card_text(words(40, name), anchor(i)) for i, name in enumerate(("a", "b", "c"))}
    submissions, skipped = near.ops(
        [head("a", texts["a"], 7), head("b", texts["b"], 9), head("c", texts["c"], 8)],
        limit=0.9,
        budget=near.Budget(),
    )
    assert skipped == {}
    [ops] = submissions
    assert {op["other_card"]["card_id"] for op in ops} == {"a"}
    assert [op["card_id"] for op in ops] == ["b", "c"]
    tied, _ = near.ops(
        [head("c", texts["c"], 5), head("a", texts["a"], 5), head("b", texts["b"], 5)],
        limit=0.9,
        budget=near.Budget(),
    )
    assert [(op["card_id"], op["other_card"]["card_id"]) for op in tied[0]] == [("b", "a"), ("c", "a")]
    assert (
        tied
        == near.ops(
            [head("b", texts["b"], 5), head("c", texts["c"], 5), head("a", texts["a"], 5)],
            limit=0.9,
            budget=near.Budget(),
        )[0]
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"trust": "imported"}, "near_mixed_trust"),
        ({"trust": "mystery"}, "near_mixed_trust"),
        ({"scan": "flagged"}, "near_non_clean"),
        ({"scan": "private"}, "near_non_clean"),
    ],
)
def test_ineligible_clusters_are_skipped_and_counted(changes, reason):
    heads = [head(name, card_text(words(40, name), anchor(i)), i + 1) for i, name in enumerate("abc")]
    heads[0] = head("a", heads[0]["text"], 1, **changes)
    submissions, skipped = near.ops(heads, limit=0.9, budget=near.Budget())
    assert submissions == [] and skipped == {reason: 2}


def test_unknown_trust_shared_by_the_whole_cluster_is_skipped():
    heads = [head(name, card_text(words(40, name), anchor(i)), i + 1, trust="mystery") for i, name in enumerate("ab")]
    assert near.ops(heads, limit=0.9, budget=near.Budget()) == ([], {"near_unknown_trust": 1})


def test_unanchored_keeper_skips_cluster_and_unanchored_member_skips_its_op():
    keeper = head("k", f"---\nevidence: []\n---\n{words(40, 'k')}.\n", 0)
    members = [head(name, card_text(words(40, name), anchor(i)), i + 1) for i, name in enumerate("ab")]
    assert near.ops([keeper, *members], limit=0.9, budget=near.Budget()) == ([], {"near_missing_pin": 2})
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 0)
    unanchored = head("u", f"---\nevidence: []\n---\n{words(40, 'u')}.\n", 3)
    [ops], skipped = near.ops([keeper, *members, unanchored], limit=0.9, budget=near.Budget())
    assert [op["card_id"] for op in ops] == ["a", "b"] and skipped == {"near_missing_pin": 1}


def test_ambiguous_member_metadata_skips_only_that_op():
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 0)
    good = head("a", card_text(words(40, "a"), anchor(1)), 1)
    bad = head("b", card_text(words(40, "b"), anchor(2), meta="status: |\n  active"), 2)
    [ops], skipped = near.ops([keeper, good, bad], limit=0.9, budget=near.Budget())
    assert [op["card_id"] for op in ops] == ["a"] and skipped == {"near_invalid_metadata": 1}


def test_exact_and_superseded_cards_are_left_out():
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 0)
    twins = [head(name, card_text(words(40, "twin"), anchor(i)), i + 1) for i, name in enumerate("ab")]
    retired = head("s", card_text(words(40, "s"), anchor(5), meta="status: superseded"), 5)
    pointer = head("p", card_text(words(40, "p"), anchor(6), meta="superseded_by: repo:near/k"), 6)
    assert near.ops([keeper, *twins, retired, pointer], limit=0.9, budget=near.Budget()) == ([], {})


def test_large_cluster_splits_within_eight_cards_per_submission(setup, capsys):
    target, hub, _ = setup
    configure(target)
    for i in range(10):
        put(hub, f"card-{i:02d}", words(60, f"t{i}"), anchor(i + 1))
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 2
    calls = proposed(hub)
    assert [len(call["ops"]) for call in calls] == [7, 2]
    for call in calls:
        cards = {op["card_id"] for op in call["ops"]} | {op["other_card"]["card_id"] for op in call["ops"]}
        assert len(cards) <= near.MAX_CARDS and len(call["ops"]) <= near.MAX_OPS
        assert {op["other_card"]["card_id"] for op in call["ops"]} == {"card-00"}
    assert run(target, capsys, "run")[1]["skipped_reasons"] == {"already_proposed": 2}


def group_claim(group, member):
    # Five group words keep groups apart (Jaccard 0.78). Members share 40 of 42 shingles.
    lead = " ".join(f"group{group}{letter}" for letter in "abcde")
    return f"{lead} {words(41, f'm{member}')}"


def near_groups(hub, count):
    for group in range(count):
        for member in range(2):
            put(hub, f"g{group}-{member}", group_claim(group, member), anchor(group * 2 + member + 1))


def test_full_quota_stops_without_failure_or_pause(setup, capsys, monkeypatch):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 3)
    original = hub.do_propose
    monkeypatch.setattr(
        hub,
        "do_propose",
        lambda body: (
            original(body)
            if len(proposed(hub)) == 1
            else (429, {"code": "memory_proposal_principal_quota", "error": "quota"})
        ),
    )
    for _ in range(4):
        code, report = run(target, capsys, "run", "--hub", "--scheduled")
        assert code == 0 and report["status"] == "completed" and report["refused_ops"] == []
        assert report["schedule"] == {"consecutive_failures": 0, "paused": False}
    assert report["skipped_reasons"] == {"already_proposed": 1, "proposal_quota": 2}
    assert len(report["proposals"]) == 0


def test_duplicate_pending_records_the_existing_proposal(setup, capsys, monkeypatch):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 1)
    existing = hubc.event_id()
    monkeypatch.setattr(
        hub,
        "do_propose",
        lambda body: (409, {"code": "memory_proposal_duplicate_pending", "proposal_id": existing, "error": "dup"}),
    )
    code, report = run(target, capsys, "run")
    assert code == 0 and report["skipped_reasons"] == {"duplicate_pending": 1}
    assert report["refused_ops"] == [] and report["schedule"]["consecutive_failures"] == 0
    state = json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())
    assert [entry["proposal_id"] for entry in state["submissions"].values()] == [existing]
    calls = len(proposed(hub))
    assert run(target, capsys, "run")[1]["skipped_reasons"] == {"already_proposed": 1}
    assert len(proposed(hub)) == calls


def test_dry_run_reports_near_ops_without_proposing(setup, capsys):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 2)
    before = dict(hub.cards)
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0 and report["candidate_count"] == 2
    assert [op["type"] for op in report["ops"]] == ["supersede_card", "supersede_card"]
    assert writes(hub) == [] and hub.cards == before


def pair_checks(count, cap, monkeypatch):
    """Cards sharing a 30-word template and a unique 3-word tail (Jaccard 0.81)."""
    monkeypatch.setattr(near, "POSTING_CAP", cap)
    monkeypatch.setattr(near, "MAX_PAIRS", 10**9)
    template = " ".join(f"common{i}" for i in range(30))
    heads = [
        head(f"card-{i:04d}", card_text(f"{template} u{i}a u{i}b u{i}c", anchor(i + 1)), i + 1) for i in range(count)
    ]
    budget = near.Budget()
    assert near.ops(heads, limit=0.9, budget=budget) == ([], {})
    return budget.stats


def test_common_shingle_cap_keeps_pair_work_near_linear(monkeypatch):
    uncapped = [pair_checks(n, 10**9, monkeypatch)["pair_checks"] for n in (200, 400)]
    capped = [pair_checks(n, CAP, monkeypatch) for n in (200, 400)]
    assert uncapped[1] >= 3.5 * uncapped[0]  # Template text alone makes pairs quadratic.
    assert capped[1]["common_shingles"] > 0
    assert capped[1]["pair_checks"] <= 2.5 * max(capped[0]["pair_checks"], 1)
    assert capped[1]["pair_checks"] <= 400 * CAP


def test_candidate_pair_budget_is_counted(monkeypatch):
    monkeypatch.setattr(near, "MAX_PAIRS", 1)
    heads = []
    for group in range(3):
        for member in range(2):
            name = f"g{group}-{member}"
            heads.append(head(name, card_text(group_claim(group, member), anchor(len(heads) + 1)), 1))
    budget = near.Budget()
    submissions, skipped = near.ops(heads, limit=0.9, budget=budget)
    assert len(submissions) <= 1 and skipped == {"near_pair_limit": 1}
    assert budget.stats["pair_checks"] <= 1 and budget.stats["candidate_pairs"] <= 1
    monkeypatch.setattr(near, "MAX_PAIRS", 1000)
    assert len(near.ops(heads, limit=0.9, budget=near.Budget())[0]) == 3


def test_reports_and_state_never_carry_card_text(setup, capsys, monkeypatch):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 3)
    hub.trust_classes = {(SCOPE, "g2-1"): "imported"}
    outputs = [json.dumps(run(target, capsys, "run", "--dry-run")[1])]
    assert cli.main(["memory", "reflect", "run", "--target", str(target)]) == 0
    outputs.append(capsys.readouterr().out)
    outputs.append((target / ".brigade/memory/reflect/schedule.json").read_text())
    assert len(proposed(hub)) == 2 and "skipped: near_mixed_trust=1" in outputs[1]
    for output in outputs:
        assert SECRET not in output and "w12 w13" not in output


@pytest.mark.parametrize("code", ["memory_proposal_principal_quota", "memory_proposal_scope_quota"])
def test_client_keeps_coded_quota_refusals_and_other_429s_unavailable(code):
    def answer(body):
        return lambda url, token, request, timeout: (429, body)

    client = hubc.MemoryHubClient("https://hub.example.test", "token", transport=answer({"code": code}))
    with pytest.raises(hubc.HubRefusal) as refusal:
        client.call("propose", ops=[])
    assert refusal.value.status == 429 and refusal.value.code == code
    # Any other 429, including a code of the wrong type, stays on the unavailable fallback path.
    for body in ({}, {"code": "rate_limited"}, {"code": ["rate_limited"]}, {"code": {"a": 1}}, {"code": 7}):
        client = hubc.MemoryHubClient("https://hub.example.test", "token", transport=answer(body))
        with pytest.raises(hubc.HubUnavailable):
            client.call("propose", ops=[])


def crafted(count=64, shared=400):
    """Near-identical cards sharing hundreds of shingles, each with its own last word."""
    body = " ".join(f"shared{i}" for i in range(shared))
    return [head(f"card-{i:03d}", card_text(f"{body} tail{i}", anchor(i + 1)), i + 1) for i in range(count)]


def test_pair_budget_charges_every_generated_combination(monkeypatch):
    heads = crafted()
    budget = near.Budget()
    submissions, skipped = near.ops(heads, limit=0.9, budget=budget)
    # Dozens of shared prefix shingles each expand to 64 * 63 / 2 combinations.
    assert budget.stats["pair_checks"] > 20 * 2016 and budget.stats["pair_checks"] <= near.MAX_PAIRS
    assert budget.stats["candidate_pairs"] <= 2016  # Dedupe verifies each distinct pair once.
    assert skipped == {} and sum(len(ops) for ops in submissions) == 63
    monkeypatch.setattr(near, "MAX_PAIRS", 10_000)
    budget = near.Budget()
    submissions, skipped = near.ops(heads, limit=0.9, budget=budget)
    assert budget.stats["pair_checks"] <= 10_000 and skipped == {"near_pair_limit": 1}
    assert budget.stopped
    # A stopped pass does no further work in later scopes.
    assert near.ops(heads, limit=0.9, budget=budget) == ([], {})
    assert budget.stats["pair_checks"] <= 10_000


def test_index_entry_cap_stops_the_pass(monkeypatch):
    monkeypatch.setattr(near, "MAX_INDEX_ENTRIES", 100)
    budget = near.Budget()
    assert near.ops(crafted(), limit=0.9, budget=budget) == ([], {"near_index_limit": 1})
    assert budget.stats["index_entries"] <= 100 and budget.stats["pair_checks"] == 0


def test_verification_cap_stops_the_pass(monkeypatch):
    monkeypatch.setattr(near, "MAX_VERIFY_SHINGLES", 2000)
    budget = near.Budget()
    _, skipped = near.ops(crafted(), limit=0.9, budget=budget)
    assert skipped == {"near_pair_limit": 1} and budget.stats["candidate_pairs"] <= 3


def test_rewritten_or_new_card_never_becomes_keeper(setup, capsys):
    target, hub, _ = setup
    configure(target)
    put(hub, "established", words(40, "one"), "c-aaaaaa")
    put(hub, "a-rewritten", words(40, "two"), "c-bbbbbb")
    put(hub, "a-rewritten", words(40, "three"), "c-bbbbbb")  # a later write never promotes a card
    put(hub, "a-fresh", words(40, "four"), "c-cccccc")
    code, report = run(target, capsys, "run", "--dry-run")
    assert code == 0
    assert {op["other_card"]["card_id"] for op in report["ops"]} == {"established"}
    assert {op["card_id"] for op in report["ops"]} == {"a-rewritten", "a-fresh"}


def test_per_run_near_proposal_cap_defers_the_rest(setup, capsys):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 10)
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 8
    assert report["skipped_reasons"] == {"near_deferred": 2}
    assert report["near_duplicate"]["max_proposals"] == 8
    code, report = run(target, capsys, "run")
    assert len(report["proposals"]) == 2 and report["skipped_reasons"] == {"already_proposed": 8}
    configure(target, "[reflect]\nnear_duplicate_max_proposals = 0\n")
    for group in (10, 11):
        for member in range(2):
            put(hub, f"g{group}-{member}", group_claim(group, member), anchor(group * 2 + member + 1))
    assert run(target, capsys, "run", "--dry-run")[1]["skipped_reasons"]["near_deferred"] == 2


@pytest.mark.parametrize("value", ["-1", "33", "1.5", "true"])
def test_near_proposal_cap_outside_range_fails_before_hub_calls(setup, capsys, value):
    target, hub, _ = setup
    configure(target, f"[reflect]\nnear_duplicate_max_proposals = {value}\n")
    code, report = run(target, capsys, "run")
    assert code == 1 and report["code"] == "hub_reflect_config"
    assert "near_duplicate_max_proposals" in report["error"] and hub.calls == []


def test_exact_merges_come_before_near_duplicates(setup, capsys):
    target, hub, _ = setup
    (target / ".brigade/memory-care.toml").write_text(f'[hub]\nmode="read"\nread_scopes=["{SCOPE}","fleet"]\n')
    near_groups(hub, 1)
    code, report = run(target, capsys, "run", "--limit", "1")
    assert code == 0 and [op["type"] for op in report["ops"]] == ["merge_exact_duplicates"]


def proposed_groups(calls):
    return {op["other_card"]["card_id"].split("-")[0] for call in calls for op in call["ops"]}


def clusters_state(target, scope=SCOPE):
    return json.loads((target / ".brigade/memory/reflect/schedule.json").read_text())["near_clusters"].get(scope, {})


def test_rewritten_clusters_cannot_starve_unserved_ones(setup, capsys):
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 10)
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 8
    served = proposed_groups(proposed(hub))
    waiting = {f"g{group}" for group in range(10)} - served
    assert len(waiting) == 2
    # Rewriting members changes every digest of the served clusters, but not their card ids.
    for group in sorted(served):
        number = int(group[1:])
        put(hub, f"{group}-1", group_claim(number, "rewrite"), anchor(number * 2 + 2))
    before = len(proposed(hub))
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 8
    assert waiting <= proposed_groups(proposed(hub)[before:])
    assert report["skipped_reasons"] == {"near_deferred": 2}


def test_cluster_keys_survive_rewrites_and_drop_when_clusters_go(setup, capsys, monkeypatch):
    from brigade import memory_reflect_hub as reflect

    clock = iter(range(1_000, 10_000, 1_000))
    monkeypatch.setattr(reflect, "_now_ms", lambda: next(clock))
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 2)
    assert run(target, capsys, "run")[0] == 0
    first = clusters_state(target)
    assert len(first) == 2 and all(entry == [1_000, 1_000] for entry in first.values())
    put(hub, "g0-1", group_claim(0, "rewrite"), anchor(2))
    assert run(target, capsys, "run")[0] == 0
    second = clusters_state(target)
    assert {key: entry[0] for key, entry in second.items()} == {key: 1_000 for key in first}
    put(hub, "g1-1", " ".join(f"unrelated{i}" for i in range(50)), anchor(4))
    assert run(target, capsys, "run")[0] == 0
    third = clusters_state(target)
    assert len(third) == 1 and set(third) < set(first)
    assert SECRET not in json.dumps(third)


def test_every_observed_cluster_key_gets_a_durable_entry(setup, capsys, monkeypatch):
    from brigade import memory_reflect_hub as reflect

    # Lower the scope card bound. The cluster cap follows it, and 20 cards fill the scope.
    monkeypatch.setattr(reflect, "MAX_SCOPE_CARDS", 20)
    assert reflect._cluster_cap() == 10
    target, hub, _ = setup
    configure(target)
    near_groups(hub, 10)
    code, report = run(target, capsys, "run")
    assert code == 0 and report["near_duplicate"]["clusters"] == 10
    assert len(report["proposals"]) == 8 and report["skipped_reasons"] == {"near_deferred": 2}
    entries = clusters_state(target)
    assert len(entries) == 10
    assert sorted(entry[1] > 0 for entry in entries.values()) == [False] * 2 + [True] * 8


def test_forced_overflow_still_serves_every_cluster(setup, capsys, monkeypatch):
    from brigade import memory_reflect_hub as reflect

    clock = iter(range(1_000, 100_000, 1_000))
    monkeypatch.setattr(reflect, "_now_ms", lambda: next(clock))
    monkeypatch.setattr(reflect, "_cluster_cap", lambda: 3)
    target, hub, _ = setup
    configure(target, "[reflect]\nnear_duplicate_max_proposals = 1\n")
    near_groups(hub, 4)
    served = set()
    for round_number in range(4):
        before = len(proposed(hub))
        assert run(target, capsys, "run")[0] == 0
        served |= proposed_groups(proposed(hub)[before:])
        assert len(clusters_state(target)) <= 3
        # The writer rewrites a member of every cluster, so no proposal is ever remembered.
        for group in range(4):
            put(hub, f"g{group}-1", group_claim(group, f"rewrite{round_number}"), anchor(group * 2 + 2))
    assert served == {"g0", "g1", "g2", "g3"}


def test_worst_case_cluster_schedule_stays_small():
    from brigade import memory_proposal_io as io, memory_reflect_hub as reflect

    entries = {f"{i:032x}": [9_999_999_999_999, 9_999_999_999_999] for i in range(reflect._cluster_cap())}
    size = len(io.json_bytes({"binding": "a" * 64, "near_clusters": {SCOPE: entries}}))
    assert len(entries) == 25_000 and size < 2 * 1024 * 1024


@pytest.mark.parametrize(
    "clusters",
    [
        [],
        {SCOPE: []},
        {SCOPE: {"x": [1, 0]}},
        {SCOPE: {"a" * 64: [1, 0]}},
        {SCOPE: {"a" * 32: [1]}},
        {SCOPE: {"a" * 32: [-1, 0]}},
        {SCOPE: {"a" * 32: [True, 0]}},
        {SCOPE: {"a" * 32: {"first_seen_ms": 1}}},
        {"": {"a" * 32: [1, 0]}},
    ],
)
def test_invalid_cluster_state_is_refused(setup, capsys, clusters):
    from brigade import memory_reflect_hub as reflect

    target, hub, _ = setup
    reflect._save(target, {"consecutive_failures": 0, "paused": False, "binding": "b" * 64, "near_clusters": clusters})
    assert run(target, capsys, "status")[1]["schedule"]["paused"]
    assert run(target, capsys, "run")[0] == 2 and hub.calls == []
