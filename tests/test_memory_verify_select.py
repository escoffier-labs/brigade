"""Verification candidate scoring, the staleness lane, ownership config and the never-read report."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

import pytest

from brigade import cli
from brigade import memory_fairness as fairness
from brigade import memory_hub_client as hubc
from brigade import memory_proposal_io as io
from brigade import memory_reflect_hub as reflect_hub
from brigade import memory_verify_cmd as verify_cmd
from brigade import memory_verify_config as config
from brigade import memory_verify_select as pick
from brigade import memory_verify_state as vstate
from tests.test_memory_reflect_hub import pytestmark as pytestmark, setup as setup

SCOPE = "repo:verify"
SECRET = "Zanzibarquill"
DAY = pick.DAY_MS
NOW = 20_400 * DAY + 3_600_000
TODAY = NOW // DAY


def text(anchor="c-abcdef", meta="evidence: []", claim=f"{SECRET} listens on port 7. "):
    return f"---\n{meta}\n---\n{claim}^{anchor}\n"


def head(card_id, *, scope=SCOPE, revision=1, trust="reviewed", scan="clean", body=None, updated=None, state="live"):
    item = {
        "scope": scope,
        "card_id": card_id,
        "revision": revision,
        "text": body if body is not None else text(),
        "trust": trust,
        "scan_state": scan,
        "state": state,
    }
    if updated is not None:
        item["updated_at"] = updated
    return item


def key(card_id, scope=SCOPE):
    return fairness.key_digest(scope, [card_id])


def entry(*, verified=None, seen=NOW, proposed=0, **extra):
    value = {"seen": seen, "proposed": proposed, **extra}
    if verified is not None:
        value["verified"] = verified
    return value


def choose(heads, entries=None, reads=None, slots=12, minimum=7, evidence=None):
    return pick.select(
        heads,
        entries=entries or {},
        reads=reads or {},
        slots=slots,
        now_ms=NOW,
        min_interval_days=minimum,
        evidence=evidence,
    )


# -- score -------------------------------------------------------------------


def test_score_values_for_fixed_inputs():
    # A never-verified, never-read card with no known revision age: only staleness, capped at 180 days.
    assert pick.score(pick.Reads(), revision_age_days=None, evidence_newer=False, days_since_verified=None) == 6.0
    assert pick.score(pick.Reads(3, 9, 12), revision_age_days=10, evidence_newer=True, days_since_verified=45) == (
        pytest.approx(math.log2(1 + 12 + 9) + 4 + 1.5)
    )
    assert pick.score(pick.Reads(0, 1, 1), revision_age_days=30, evidence_newer=False, days_since_verified=0) == 1 + 1
    assert pick.access(pick.Reads(10**6, 10**7, 10**7)) == math.log2(1 + 4 * 7000 + 30000)
    assert pick.access(pick.Reads(10**6, 10**7)) == pick.access(pick.Reads(7000, 30000))
    assert pick.staleness(500) == pick.staleness(180) == pick.staleness(None) == 6.0
    assert pick.staleness(-3) == 0.0
    assert [pick.volatility(days, False) for days in (0, 14, 15, 60, 61, None, -1)] == [2, 2, 1, 1, 0, 0, 0]
    assert pick.volatility(61, True) == 2


def test_selected_scores_use_reads_revision_age_and_verification_age():
    updated = datetime.fromtimestamp(TODAY * 86400, timezone.utc).isoformat()  # Revision age 0.
    heads = [head("fresh", updated=updated), head("quiet")]
    reads = {key("fresh"): pick.Reads(3, 9, 12)}
    entries = {key("quiet"): entry(verified=TODAY - 45)}
    result = choose(heads, entries, reads)
    scores = {card.card_id: card.score for card, _ in result.selected}
    assert scores == {"fresh": pytest.approx(math.log2(22) + 2 + 6), "quiet": 1.5}
    assert pick.updated_day({"updated_at": updated}) == TODAY
    assert pick.updated_day({"updated_at": "2025-11-08T00:00:00Z"}) == 20400
    assert pick.updated_day({"updated_at": "not a time"}) is None


# -- the staleness lane ------------------------------------------------------


def hot_and_cold():
    hot = [head(f"hot-{i:02d}") for i in range(20)]
    cold = [head(f"cold-{days}") for days in (100, 150, 120)]
    entries = {key(card["card_id"]): entry(verified=TODAY - 10) for card in hot}
    entries.update({key(f"cold-{days}"): entry(verified=TODAY - days) for days in (100, 150, 120)})
    reads = {key(card["card_id"]): pick.Reads(10**6, 10**7, 10**7) for card in hot}
    return hot + cold, entries, reads


@pytest.mark.parametrize("slots, lane", [(8, 2), (5, 2), (4, 1), (1, 1), (12, 3)])
def test_staleness_lane_takes_a_quarter_rounded_up_despite_far_higher_reads(slots, lane):
    heads, entries, reads = hot_and_cold()
    result = choose(heads, entries, reads, slots=slots)
    assert len(result.selected) == slots
    stale = [card.card_id for card, name in result.selected if name == pick.STALENESS_LANE]
    # Oldest verification first: 150 days, then 120, then 100.
    assert stale == ["cold-150", "cold-120", "cold-100"][:lane]
    assert all(card.card_id.startswith("hot-") for card, name in result.selected if name == pick.SCORE_LANE)
    assert result.eligible == 23
    assert result.skipped == {"verify_deferred_slots": 23 - slots}


def test_never_verified_cards_lead_the_staleness_lane():
    heads, entries, reads = hot_and_cold()
    heads.append(head("brand-new"))
    result = choose(heads, entries, reads, slots=8)
    assert [card.card_id for card, name in result.selected if name == pick.STALENESS_LANE] == ["brand-new", "cold-150"]


# -- intervals ---------------------------------------------------------------


def test_minimum_interval():
    heads = [head("six"), head("seven")]
    entries = {key("six"): entry(verified=TODAY - 6), key("seven"): entry(verified=TODAY - 7)}
    result = choose(heads, entries)
    assert [card.card_id for card, _ in result.selected] == ["seven"]
    assert result.skipped == {"recently_verified": 1}
    assert choose(heads, entries, minimum=5).eligible == 2


def test_thirty_day_interval_for_unchanged_supported_cards():
    digest = "e" * 64
    found = pick.Evidence(digest=digest, newest_day=TODAY - 40)

    def eligible(days, *, revision=3, evidence=found, verdict="supported", stored=digest):
        card = head("card", revision=3)
        state = {key("card"): entry(verified=TODAY - days, revision=revision, evidence=stored, verdict=verdict)}
        return choose([card], state, evidence={key("card"): evidence} if evidence else None).eligible == 1

    assert not eligible(29) and eligible(30)
    # A changed revision, changed evidence, another verdict or unknown evidence falls back to the minimum.
    assert eligible(7, revision=2)
    assert eligible(7, stored="f" * 64)
    assert eligible(7, verdict="contradicted")
    assert eligible(7, evidence=None)
    assert not eligible(6, revision=2)


# -- eligibility -------------------------------------------------------------


def test_eligibility_filters_count_reasons_without_text():
    heads = [
        head("ok"),
        head("retired", state="retired"),
        head("superseded", body=text(meta="status: superseded")),
        head("flagged-scan", scan="flagged"),
        head("flagged-trust", trust="quarantined"),
        head("huge", body=text(claim="word " * 4000)),
        head("unanchored", body="---\nevidence: []\n---\nNo anchor here.\n"),
        head("pending"),
    ]
    entries = {key("pending"): entry(pending=hubc.event_id())}
    result = choose(heads, entries)
    assert [card.card_id for card, _ in result.selected] == ["ok"]
    assert result.skipped == {
        "retired": 1,
        "superseded": 1,
        "non_clean": 1,
        "flagged_trust": 1,
        "card_too_large": 1,
        "no_anchor": 1,
        "pending_proposal": 1,
    }


def test_pending_ceiling_verifies_nothing():
    heads = [head(f"c{i}") for i in range(10)]
    entries = {key(f"c{i}"): entry(pending=hubc.event_id()) for i in range(pick.PENDING_CEILING)}
    result = choose(heads, entries)
    assert result.selected == [] and result.skipped == {"pending_ceiling": 1}


# -- ties follow the shared fairness helper ---------------------------------


def test_ties_follow_the_shared_fairness_order():
    heads = [head(name) for name in ("a", "b", "c", "d")]
    entries = {
        key("a"): entry(seen=5, proposed=9),
        key("b"): entry(seen=7, proposed=1),
        key("c"): entry(seen=3, proposed=1),
        key("d"): entry(seen=3, proposed=1),
    }
    order = [card.card_id for card, _ in choose(heads, entries, slots=4).selected]
    expected = sorted(
        ("a", "b", "c", "d"),
        key=lambda n: fairness.priority(key(n), [entries[key(n)]["seen"], entries[key(n)]["proposed"]]),
    )
    assert order == expected
    # Least recently proposed first, then oldest first seen, then the key digest.
    assert expected[-1] == "a" and expected[2] == "b"
    assert expected[:2] == sorted(("c", "d"), key=key)


def test_reflect_scheduling_uses_the_shared_helper_unchanged():
    ops = [
        {"scope": SCOPE, "card_id": "z", "other_card": {"scope": SCOPE, "card_id": "k"}},
        {"scope": SCOPE, "card_id": "m", "other_card": {"scope": SCOPE, "card_id": "k"}},
    ]
    # The historical key: sha256 of [scope, sorted card ids], first 32 hex digits.
    assert reflect_hub._cluster_key(ops) == io.sha256(io.json_bytes([SCOPE, ["k", "m", "z"]]))[:32]
    assert reflect_hub.fairness is fairness
    assert fairness.priority("k", [5, 9]) == (9, 5, "k")


# -- never read ---------------------------------------------------------------


def test_never_read_stays_null_until_counting_covers_the_window():
    old = NOW - 100 * DAY
    heads = [head("old-unread"), head("old-read"), head("new-unread")]
    entries = {key("old-unread"): entry(seen=old), key("old-read"): entry(seen=old), key("new-unread"): entry()}
    reads = {key("old-read"): pick.Reads(0, 0, 4)}

    def report(since):
        return pick.never_read(heads, entries=entries, reads=reads, since_day=since, now_ms=NOW, never_read_days=90)

    assert report(None) is None
    assert report(TODAY - 89) is None
    assert report(TODAY - 90) == ["old-unread"]
    assert report(TODAY - 400) == ["old-unread"]


# -- [verify] ownership configuration ----------------------------------------


def workspace(scope, path, projects=("example",), **extra):
    item = {"scope": scope, "path": str(path), **extra}
    if projects is not None:
        item["archive_projects"] = list(projects)
    return item


def parse(*items, **table):
    return config.parse({"verify": {**table, "workspace": list(items)}})


def refused(*items, **table):
    with pytest.raises(config.VerifyConfigError) as exc_info:
        parse(*items, **table)
    for item in items:
        assert item["path"] not in str(exc_info.value)
    return str(exc_info.value)


def test_valid_ownership_parses(tmp_path):
    parsed = parse(
        workspace("repo:one", tmp_path / "one"),
        workspace("repo:one", tmp_path / "one-more", projects=()),
        workspace("repo:two", tmp_path / "two"),
        enabled=True,
        propose_seat="verify-propose",
        confirm_seat="verify-confirm",
    )
    assert parsed.scopes() == ("repo:one", "repo:two")
    assert parsed.workspaces[1].archive_projects == ()
    assert config.parse({}) == config.VerifyConfig()


def test_a_root_bound_to_two_scopes_fails(tmp_path):
    assert "two scopes" in refused(workspace("repo:one", tmp_path / "w"), workspace("repo:two", tmp_path / "w"))


def test_a_symlinked_alias_of_a_root_bound_to_two_scopes_fails(tmp_path):
    (tmp_path / "w").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "w")
    assert "two scopes" in refused(workspace("repo:one", tmp_path / "w"), workspace("repo:two", tmp_path / "alias"))


@pytest.mark.parametrize("same_scope", [True, False])
def test_a_nested_root_fails(tmp_path, same_scope):
    outer = workspace("repo:one", tmp_path / "w")
    inner = workspace("repo:one" if same_scope else "repo:two", tmp_path / "w" / "inner")
    assert "nested" in refused(outer, inner)
    assert "nested" in refused(inner, outer)


def test_a_sibling_with_a_shared_prefix_is_not_nested(tmp_path):
    parse(workspace("repo:one", tmp_path / "w"), workspace("repo:two", tmp_path / "w2"))


def test_a_missing_project_list_fails(tmp_path):
    assert "archive_projects" in refused(workspace("repo:one", tmp_path / "w", projects=None))


@pytest.mark.parametrize(
    "item, table",
    [
        ({"scope": "repo:one", "path": "relative/path", "archive_projects": []}, {}),
        ({"scope": "repo:one", "path": "/a/../b", "archive_projects": []}, {}),
        ({"scope": "nope", "path": "/srv/w", "archive_projects": []}, {}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": ["bad name"]}, {}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": ["x", "x"]}, {}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": [], "extra": 1}, {}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": []}, {"cards_per_run": 65}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": []}, {"propose_seat": "s", "confirm_seat": "s"}),
        ({"scope": "repo:one", "path": "/srv/w", "archive_projects": []}, {"unknown": True}),
    ],
)
def test_invalid_workspace_settings_fail(item, table):
    refused(item, **table)


@pytest.mark.parametrize("name", ["hub_url", "node_token_file", "token"])
def test_the_workspace_file_never_names_a_hub_url_or_credential_path(tmp_path, name):
    value = "https://192.0.2.10/memory" if name == "hub_url" else "/etc/brigade/memory.token"
    assert refused(workspace("repo:one", tmp_path / "w"), **{name: value}) == config.HUB_KEY_REFUSAL
    assert refused(workspace("repo:one", tmp_path / "w", **{name: value})) == config.HUB_KEY_REFUSAL


# -- commands -----------------------------------------------------------------


def configure(target, workspace_root, extra=""):
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet"]\n'
        f'[verify]\n{extra}[[verify.workspace]]\nscope="fleet"\npath="{workspace_root}"\narchive_projects=[]\n'
    )


def verify(target, capsys, *args):
    code = cli.main(["memory", "verify", *args, "--target", str(target), "--json"])
    out = capsys.readouterr().out
    assert SECRET not in out and "Same claim" not in out
    return code, json.loads(out)


@pytest.fixture
def verify_setup(setup, monkeypatch, tmp_path):
    target, hub, home = setup
    monkeypatch.setattr(verify_cmd, "PAGE_PAUSE_SECONDS", 0)
    configure(target, tmp_path / "bound")
    return target, hub


def test_dry_run_prints_ids_scores_and_skip_counts_only(verify_setup, capsys):
    target, hub = verify_setup
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model", "--list-ids")
    assert code == 0 and report["status"] == "completed"
    assert {item["card_id"] for item in report["selected"]} == {"a", "b"}
    # Both cards are never verified, so the staleness lane of three slots takes them.
    assert {item["lane"] for item in report["selected"]} == {"staleness"}
    for item in report["selected"]:
        assert set(item) == {"scope", "card_id", "score", "lane"} and item["scope"] == "fleet"
    assert report["read_stats"] == {"fleet": "fresh"}
    # Counting began on day 20300, so the window is covered, but nothing was first seen 90 days ago.
    assert report["never_read"] == {"count": 0, "by_scope": {"fleet": 0}, "card_ids": []}
    assert {call["action"] for call in hub.calls} <= {"status", "log", "card", "read_stats"}
    assert not (target / vstate.STATE).exists()


def test_text_output_has_no_card_text(verify_setup, capsys):
    target, _ = verify_setup
    assert cli.main(["memory", "verify", "run", "--dry-run", "--no-model", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert "Same claim" not in out and "selected fleet a " in out


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--no-model"]])
def test_live_runs_are_refused_before_any_hub_call(verify_setup, capsys, flags):
    target, hub = verify_setup
    code, report = verify(target, capsys, "run", *flags)
    assert code == 2 and report["code"] == "verify_live_unavailable"
    assert hub.calls == []


def test_rate_limited_counts_fall_back_to_the_state_file(verify_setup, capsys):
    target, hub = verify_setup
    hub.read_stats_reply = (429, {"error": SECRET, "code": hubc.READ_STATS_RATE_LIMITED})
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and report["read_stats"] == {"fleet": "cached"}
    assert report["skipped"]["read_stats_limited"] == 1
    assert report["never_read"]["count"] is None


def test_a_refused_grant_fails_with_a_fixed_reason_and_no_state(verify_setup, capsys):
    target, hub = verify_setup
    hub.read_stats_reply = (403, {"error": SECRET, "code": "memory_grant_missing"})
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 1 and report == {
        "status": "failed",
        "code": "memory_grant_missing",
        "reason": "hub refused the verify request",
    }
    assert not (target / vstate.STATE).exists()


def test_bad_config_and_bad_state_fail_closed(verify_setup, capsys, tmp_path):
    target, hub = verify_setup
    configure(target, tmp_path / "bound", extra=f'hub_url = "https://192.0.2.10/{SECRET}"\n')
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 1 and report["code"] == "verify_config" and hub.calls == []
    configure(target, tmp_path / "bound")
    path = target / vstate.STATE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("{" + SECRET)
    os.chmod(path, 0o600)
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 1 and report == {"status": "failed", "code": "verify_state", "reason": vstate.STATE_ERROR}
    assert hub.calls == []


def test_status_reports_configuration_counts(verify_setup, capsys):
    target, hub = verify_setup
    code, report = verify(target, capsys, "status")
    assert code == 0
    assert report == {
        "status": "ok",
        "configured": True,
        "enabled": False,
        "scopes": 1,
        "workspaces": 1,
        "cards_per_run": config.DEFAULT_CARDS_PER_RUN,
        "state": {"consecutive_failures": 0, "paused": False, "never_read": {}, "tracked_cards": 0},
    }
    assert hub.calls == []
