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
    block = f"{words(23)}. ^c-aaaaaa"
    assert op == {
        "type": "supersede_card",
        "scope": SCOPE,
        "card_id": "older",
        "base_revision": older["revision"],
        "block_id": "c-aaaaaa",
        "expected_block_hash": hashlib.sha256(block.encode()).hexdigest(),
        "other_card": {"scope": SCOPE, "card_id": "newer", "base_revision": newer["revision"]},
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


def test_keeper_is_most_recent_revision_then_lowest_card_id():
    texts = {name: card_text(words(40, name), anchor(i)) for i, name in enumerate(("a", "b", "c"))}
    submissions, skipped = near.ops(
        [head("a", texts["a"], 7), head("b", texts["b"], 9), head("c", texts["c"], 8)],
        limit=0.9,
        budget=near.Budget(),
    )
    assert skipped == {}
    [ops] = submissions
    assert {op["other_card"]["card_id"] for op in ops} == {"b"}
    assert [op["card_id"] for op in ops] == ["a", "c"]
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
    keeper = head("k", f"---\nevidence: []\n---\n{words(40, 'k')}.\n", 9)
    members = [head(name, card_text(words(40, name), anchor(i)), i + 1) for i, name in enumerate("ab")]
    assert near.ops([keeper, *members], limit=0.9, budget=near.Budget()) == ([], {"near_missing_pin": 2})
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 9)
    unanchored = head("u", f"---\nevidence: []\n---\n{words(40, 'u')}.\n", 3)
    [ops], skipped = near.ops([keeper, *members, unanchored], limit=0.9, budget=near.Budget())
    assert [op["card_id"] for op in ops] == ["a", "b"] and skipped == {"near_missing_pin": 1}


def test_ambiguous_member_metadata_skips_only_that_op():
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 9)
    good = head("a", card_text(words(40, "a"), anchor(1)), 1)
    bad = head("b", card_text(words(40, "b"), anchor(2), meta="status: |\n  active"), 2)
    [ops], skipped = near.ops([keeper, good, bad], limit=0.9, budget=near.Budget())
    assert [op["card_id"] for op in ops] == ["a"] and skipped == {"near_invalid_metadata": 1}


def test_exact_and_superseded_cards_are_left_out():
    keeper = head("k", card_text(words(40, "k"), "c-00000f"), 9)
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
        assert {op["other_card"]["card_id"] for op in call["ops"]} == {"card-09"}
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
    assert budget.stats["candidate_pairs"] == 1
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
    for body in ({}, {"code": "rate_limited"}):
        client = hubc.MemoryHubClient("https://hub.example.test", "token", transport=answer(body))
        with pytest.raises(hubc.HubUnavailable):
            client.call("propose", ops=[])
