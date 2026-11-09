"""Verification candidate scoring, the staleness lane, ownership config and the never-read report."""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

import pytest

from brigade import cli
from brigade import memory_fairness as fairness
from brigade import memory_hub_client as hubc
from brigade import memory_hub_store as store
from brigade import memory_proposal_io as io
from brigade import memory_reflect_hub as reflect_hub
from brigade import memory_verify_cmd as verify_cmd
from brigade import memory_verify_config as config
from brigade import memory_verify_select as pick
from brigade import memory_verify_state as vstate
from tests.test_memory_reflect_hub import pytestmark as pytestmark, setup as setup

SCOPE = "repo:verify"
SECRET = "Zanzibarquill"
KEY = "0" * 32
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


def workspace(scope, path, projects=("example",), create=True, **extra):
    """A workspace entry. Roots must exist, so the directory is created unless `create` is false."""
    if create and not os.path.lexists(path):
        os.makedirs(path)
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


def test_commit_refs_default_to_empty_and_parse_when_listed(tmp_path):
    plain, listed = parse(
        workspace("repo:one", tmp_path / "one"),
        workspace(
            "repo:two",
            tmp_path / "two",
            commit_refs=["refs/remotes/origin/main", "refs/remotes/upstream/release/2.x"],
        ),
    ).workspaces
    assert plain.commit_refs == ()
    assert listed.commit_refs == ("refs/remotes/origin/main", "refs/remotes/upstream/release/2.x")


@pytest.mark.parametrize(
    "refs",
    [
        [],
        "refs/remotes/origin/main",
        ["refs/heads/main"],
        ["refs/tags/v1"],
        ["main"],
        ["origin/main"],
        ["refs/remotes/origin"],
        ["refs/remotes/origin/HEAD"],
        ["refs/remotes/origin/../heads/main"],
        ["refs/remotes/origin/a..b"],
        ["refs/remotes/origin/.hidden"],
        ["refs/remotes/origin/main.lock"],
        ["refs/remotes/origin/main."],
        ["refs/remotes/origin//main"],
        ["refs/remotes/-origin/main"],
        ["refs/remotes/origin/ma in"],
        ["refs/remotes/origin/ma~1"],
        ["refs/remotes/origin/main", "refs/remotes/origin/main"],
        [f"refs/remotes/origin/b{index}" for index in range(config.MAX_COMMIT_REFS + 1)],
        [1],
    ],
)
def test_invalid_commit_refs_fail(tmp_path, refs):
    message = refused(workspace("repo:one", tmp_path / "w", commit_refs=refs))
    assert "commit_refs" in message


def test_git_executable_is_optional_and_must_be_an_absolute_normalized_path(tmp_path):
    assert parse(workspace("repo:one", tmp_path / "w")).git_executable is None
    absolute = os.path.join(os.path.abspath(os.sep), "opt", "git", "bin", "git")
    assert parse(workspace("repo:one", tmp_path / "w2"), git_executable=absolute).git_executable == absolute
    for value in ("git", "bin/git", absolute + os.sep + ".." + os.sep + "git", absolute + "\n", 7, ""):
        assert "git_executable" in refused(workspace("repo:one", tmp_path / "w3"), git_executable=value)


# -- commands -----------------------------------------------------------------


def configure(target, workspace_root, extra=""):
    os.makedirs(workspace_root, exist_ok=True)
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet"]\n'
        f'[verify]\n{extra}[[verify.workspace]]\nscope="fleet"\npath="{workspace_root}"\narchive_projects=[]\n'
    )


def verify(target, capsys, *args):
    code = cli.main(["memory", "verify", *args, "--target", str(target), "--json"])
    out = capsys.readouterr().out
    assert SECRET not in out and "Same claim" not in out
    return code, json.loads(out)


class Clock:
    """A fake monotonic clock. Sleeping advances it, so tests never wait."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(verify_cmd, "MONOTONIC", fake.monotonic)
    monkeypatch.setattr(verify_cmd, "SLEEP", fake.sleep)
    return fake


@pytest.fixture
def verify_setup(setup, clock, tmp_path):
    target, hub, home = setup
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


def cache(target, observed_ms):
    """A state file whose fleet scope holds fresh-looking cached counts for card `a`."""
    state = vstate.fresh()
    table = {"since_day": 1, "counts_ms": observed_ms}
    table["cards"] = {key("a", "fleet"): {"seen": NOW, "proposed": 0, "reads": [5, 10, 20]}}
    state["scopes"] = {"fleet": table}
    vstate.save(target, state)


def rate_limited(hub):
    hub.read_stats_reply = (429, {"error": SECRET, "code": hubc.READ_STATS_RATE_LIMITED})


@pytest.mark.parametrize("cached", [False, True])
def test_a_rate_limited_scope_is_skipped_and_cached_counts_change_nothing(verify_setup, capsys, cached):
    target, hub = verify_setup
    rate_limited(hub)
    if cached:
        cache(target, int(time.time() * 1000) - 60_000)
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model", "--list-ids")
    assert code == 0 and report["status"] == "completed"
    assert report["read_stats"] == {"fleet": "unavailable"}
    assert report["selected"] == [] and report["eligible"] == 0
    assert report["skipped"]["read_stats_unavailable"] == 1 and report["skipped"]["read_stats_limited"] == 1
    assert report["never_read"] == {"count": None, "by_scope": {"fleet": None}, "card_ids": []}


def test_only_the_rate_limited_scope_is_skipped(setup, clock, tmp_path, capsys):
    target, hub, _ = setup
    hub._commit("card.put", "repo:second", "c", "---\nevidence: []\n---\nOther claim. ^c-123456\n", "live", "x")
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet", "repo:second"]\n[verify]\n'
        f'[[verify.workspace]]\nscope="fleet"\npath="{tmp_path / "one"}"\narchive_projects=[]\n'
        f'[[verify.workspace]]\nscope="repo:second"\npath="{tmp_path / "two"}"\narchive_projects=[]\n'
    )
    original = hub.do_read_stats

    def refuse_fleet(body):
        if body["scope"] == "fleet":
            return 429, {"error": SECRET, "code": hubc.READ_STATS_RATE_LIMITED}
        return original(body)

    hub.do_read_stats = refuse_fleet
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0
    assert report["read_stats"] == {"fleet": "unavailable", "repo:second": "fresh"}
    assert [(item["scope"], item["card_id"]) for item in report["selected"]] == [("repo:second", "c")]
    assert report["never_read"]["by_scope"] == {"fleet": None, "repo:second": 0}
    assert report["never_read"]["count"] is None


def test_a_programming_error_outside_the_state_boundary_still_raises(verify_setup, monkeypatch):
    target, _ = verify_setup

    def broken(*args, **kwargs):
        raise TypeError("a bug, not hostile input")

    monkeypatch.setattr(pick, "select", broken)
    with pytest.raises(TypeError):
        cli.main(["memory", "verify", "run", "--dry-run", "--no-model", "--target", str(target), "--json"])


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


def test_read_stats_pacing_spans_scopes(setup, clock, tmp_path, capsys):
    """The hub's one-second gap is per principal, so the next scope's first page waits too."""
    target, hub, _ = setup
    scopes = ["fleet", "repo:second", "repo:third"]
    workspaces = "".join(
        f'[[verify.workspace]]\nscope="{scope}"\npath="{tmp_path / str(index)}"\narchive_projects=[]\n'
        for index, scope in enumerate(scopes)
    )
    for index in range(len(scopes)):
        (tmp_path / str(index)).mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        f'[hub]\nmode="read"\nread_scopes={json.dumps(scopes)}\n[verify]\n{workspaces}'
    )
    admitted = []
    original = hub.do_read_stats

    def paced(body):
        # Refuse any request less than one second after the previous admitted one, as the hub does.
        if admitted and clock.now - admitted[-1] < 1.0:
            return 429, {"error": "slow down", "code": hubc.READ_STATS_RATE_LIMITED}
        admitted.append(clock.now)
        # Two pages per scope, so pacing within a scope and across scopes are both exercised.
        status, reply = original({**body, "limit": 1})
        return status, {**reply, "rows": reply["rows"][:1]}

    hub.do_read_stats = paced
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and report["status"] == "completed"
    assert report["read_stats"] == {scope: "fresh" for scope in scopes}
    assert "read_stats_limited" not in report["skipped"]
    assert len(admitted) == 2 * len(scopes)
    assert all(later - earlier >= 1.0 for earlier, later in zip(admitted, admitted[1:], strict=False))


# -- root identity -------------------------------------------------------------


def case_insensitive(monkeypatch):
    """Emulate a case-insensitive filesystem: `Repo` and `repo` name one directory."""
    real = os.stat
    monkeypatch.setattr(config, "_stat", lambda path, *a, **k: real(str(path).replace("/Repo", "/repo"), *a, **k))


def test_a_case_only_alias_is_one_root(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    case_insensitive(monkeypatch)
    upper = workspace("repo:one", tmp_path / "Repo", create=False)
    lower = workspace("repo:two", tmp_path / "repo", create=False)
    assert "two scopes" in refused(upper, lower)
    assert "listed twice" in refused(upper, {**lower, "scope": "repo:one"})


def test_a_nested_root_under_a_differently_spelled_parent_fails(tmp_path, monkeypatch):
    (tmp_path / "repo" / "inner").mkdir(parents=True)
    case_insensitive(monkeypatch)
    outer = workspace("repo:one", tmp_path / "Repo", create=False)
    inner = workspace("repo:two", tmp_path / "repo" / "inner", create=False)
    assert "nested" in refused(outer, inner)
    assert "nested" in refused(inner, outer)


def test_a_nested_root_under_a_symlinked_parent_fails(tmp_path):
    (tmp_path / "w" / "inner").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "w")
    outer = workspace("repo:one", tmp_path / "w")
    inner = workspace("repo:two", tmp_path / "link" / "inner")
    assert "nested" in refused(outer, inner)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_a_root_must_be_an_existing_directory(tmp_path, kind):
    path = tmp_path / "root"
    if kind == "file":
        path.write_text(SECRET)
    assert refused(workspace("repo:one", path, create=False)) == config.MISSING_ROOT


def test_parsed_roots_keep_their_identity_and_revalidate(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "a")
    [bound] = parse(workspace("repo:one", link, create=False)).workspaces
    real = os.stat(tmp_path / "a")
    assert bound.identity == (real.st_dev, real.st_ino)
    assert bound.resolved == os.path.normcase(os.path.realpath(tmp_path / "a"))
    assert config.revalidate(bound) is None
    # Retargeting the link after parsing makes the bound root a different directory.
    link.unlink()
    link.symlink_to(tmp_path / "b")
    assert config.revalidate(bound) == config.ROOT_CHANGED
    link.unlink()
    assert config.revalidate(bound) == config.ROOT_CHANGED
    assert str(tmp_path) not in config.ROOT_CHANGED


def zero_inode(monkeypatch, victim):
    """A filesystem without stable inodes for `victim`: stat reports st_ino 0."""
    real = os.stat

    def fake(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if os.path.realpath(str(path)) != os.path.realpath(str(victim)):
            return result
        fields = list(result[:10])
        fields[1] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(config, "_stat", fake)


@pytest.mark.parametrize("where", ["root", "parent"])
def test_a_root_without_a_stable_identity_is_refused(tmp_path, monkeypatch, where):
    root = tmp_path / "parent" / "root"
    root.mkdir(parents=True)
    zero_inode(monkeypatch, root if where == "root" else tmp_path / "parent")
    assert refused(workspace("repo:one", root, create=False)) == config.IDENTITY_UNAVAILABLE
    assert config.IDENTITY_UNAVAILABLE.startswith("verify_root_identity_unavailable")


def test_revalidate_refuses_a_root_that_lost_its_identity(tmp_path, monkeypatch):
    (tmp_path / "root").mkdir()
    [bound] = parse(workspace("repo:one", tmp_path / "root", create=False)).workspaces
    zero_inode(monkeypatch, tmp_path / "root")
    assert config.revalidate(bound) == config.ROOT_CHANGED


# -- state binding --------------------------------------------------------------


def replica_binding(target):
    meta = verify_replica(target).meta
    return verify_cmd._binding(meta["hub"], meta["principal"], meta["log_epoch"])


def history(target, binding):
    """Card `a` was verified yesterday, which makes it ineligible under a matching binding."""
    state = vstate.fresh()
    if binding is not None:
        state["binding"] = binding
    yesterday = int(time.time() * 1000) // DAY - 1
    entry = {"seen": 1, "proposed": 0, "verified": yesterday, "revision": 1, "verdict": "supported"}
    state["scopes"] = {"fleet": {"cards": {key("a", "fleet"): entry}}}
    vstate.save(target, state)


def test_a_matching_binding_keeps_card_history(verify_setup, capsys):
    target, _ = verify_setup
    assert verify(target, capsys, "run", "--dry-run", "--no-model")[0] == 0
    history(target, replica_binding(target))
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and [item["card_id"] for item in report["selected"]] == ["b"]
    assert report["skipped"]["recently_verified"] == 1
    assert "notes" not in report


@pytest.mark.parametrize("other", ["hub", "principal", "epoch", "unbound"])
def test_history_from_another_hub_principal_or_epoch_is_ignored(verify_setup, capsys, other):
    target, _ = verify_setup
    assert verify(target, capsys, "run", "--dry-run", "--no-model")[0] == 0
    meta = verify_replica(target).meta
    parts = {"hub": meta["hub"], "principal": meta["principal"], "epoch": meta["log_epoch"]}
    if other == "hub":
        parts["hub"] = "http://192.0.2.20:3774"
    elif other == "principal":
        parts["principal"] = "node-2"
    elif other == "epoch":
        parts["epoch"] = hubc.event_id()
    binding = None if other == "unbound" else verify_cmd._binding(parts["hub"], parts["principal"], parts["epoch"])
    history(target, binding)
    before = (target / vstate.STATE).read_bytes()
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0
    # The card verified yesterday under another binding is selectable here.
    assert {item["card_id"] for item in report["selected"]} == {"a", "b"}
    assert "recently_verified" not in report["skipped"]
    assert report["notes"] == [{"code": "verify_binding_changed", "ignored_card_entries": 1}]
    # A dry run still writes nothing.
    assert (target / vstate.STATE).read_bytes() == before


# -- per-scope replica bound ----------------------------------------------------


def put_cards(hub, scope, count, prefix="c"):
    for index in range(count):
        text = f"---\nevidence: []\n---\nClaim {scope} {index}. ^c-{index:06x}\n"
        hub._commit("card.put", scope, f"{prefix}{index}", text, "live", hubc.event_id())


def bind(target, tmp_path, read_scopes, bound):
    entries = ""
    for scope in bound:
        root = tmp_path / scope.replace(":", "-")
        root.mkdir()
        entries += f'[[verify.workspace]]\nscope="{scope}"\npath="{root}"\narchive_projects=[]\n'
    (target / ".brigade/memory-care.toml").write_text(
        f'[hub]\nmode="read"\nread_scopes={json.dumps(read_scopes)}\n[verify]\ncards_per_run=64\n{entries}'
    )


@pytest.fixture
def small_scopes(setup, clock, monkeypatch):
    monkeypatch.setattr(verify_cmd, "MAX_SCOPE_CARDS", 3)
    return setup


def test_bound_scopes_under_the_per_scope_bound_pass_together(small_scopes, tmp_path, capsys):
    target, hub, _ = small_scopes
    put_cards(hub, "repo:one", 3)
    put_cards(hub, "repo:two", 3)
    bind(target, tmp_path, ["repo:one", "repo:two"], ["repo:one", "repo:two"])
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and report["cards_seen"] == 6 and len(report["selected"]) == 6


def test_a_huge_unbound_scope_never_fails_a_small_bound_one(small_scopes, tmp_path, capsys):
    target, hub, _ = small_scopes
    put_cards(hub, "repo:big", 12)
    put_cards(hub, "repo:small", 1)
    # Put the big scope into verify's own replica first, as a run that still bound it would have.
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["repo:big","repo:small"]\n')
    with store.relocated(vstate.REPLICA_ROOT):
        assert cli.main(["memory", "hub", "pull", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    assert {head["scope"] for head in verify_replica(target).heads.values()} == {"repo:big", "repo:small"}
    bind(target, tmp_path, ["repo:big", "repo:small"], ["repo:small"])
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0, report
    assert [(item["scope"], item["card_id"]) for item in report["selected"]] == [("repo:small", "c0")]
    assert report["skipped"]["scope_unbound"] == 1


def test_one_bound_scope_over_the_per_scope_bound_fails(small_scopes, tmp_path, capsys):
    target, hub, _ = small_scopes
    put_cards(hub, "repo:one", 4)
    bind(target, tmp_path, ["repo:one"], ["repo:one"])
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 1 and report == {"status": "failed", "code": "verify_scope_limit", "reason": verify_cmd.SCOPE_LIMIT}


# -- verify keeps its own replica -------------------------------------------------


def verify_replica(target):
    with store.relocated(vstate.REPLICA_ROOT):
        return store.replica(target)


def snapshot(directory):
    """Every file below `directory` with its bytes and modification time."""
    found = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(directory))] = (path.read_bytes(), path.stat().st_mtime_ns)
    return found


def populated_workspace(setup, tmp_path, capsys):
    """A workspace whose shared replica already holds two scopes, then a verify binding for one of them."""
    target, hub, _ = setup
    put_cards(hub, "repo:other", 2, prefix="o")
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:other"]\n')
    assert cli.main(["memory", "hub", "pull", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    shared = target / store.ROOT_REL
    assert {head["scope"] for head in store.replica(target).heads.values()} == {"fleet", "repo:other"}
    (tmp_path / "bound").mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet","repo:other"]\n[verify]\n'
        f'[[verify.workspace]]\nscope="fleet"\npath="{tmp_path / "bound"}"\narchive_projects=[]\n'
    )
    return target, hub, shared


def test_a_dry_run_never_touches_the_shared_replica(setup, clock, tmp_path, capsys):
    target, _, shared = populated_workspace(setup, tmp_path, capsys)
    before = snapshot(shared)
    assert any(name.startswith("replica/") for name in before)
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and report["status"] == "completed"
    assert snapshot(shared) == before
    # The shared replica still serves both scopes. Verify's own holds only the bound one.
    assert {head["scope"] for head in store.replica(target).heads.values()} == {"fleet", "repo:other"}
    assert {head["scope"] for head in verify_replica(target).heads.values()} == {"fleet"}
    assert not (target / vstate.STATE).exists()


def test_a_second_run_reuses_its_own_replica(setup, clock, tmp_path, capsys):
    target, hub, shared = populated_workspace(setup, tmp_path, capsys)
    before = snapshot(shared)
    assert verify(target, capsys, "run", "--dry-run", "--no-model")[0] == 0
    first = verify_replica(target).meta
    calls = len(hub.calls)
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and {item["card_id"] for item in report["selected"]} == {"a", "b"}
    second = hub.calls[calls:]
    # The second pull follows the log from the first run's watermark and fetches no card again.
    assert [call for call in second if call["action"] == "card"] == []
    assert all(call.get("after_seq", 0) == first["seq"] for call in second if call["action"] == "log")
    assert verify_replica(target).meta["heads_sha256"] == first["heads_sha256"]
    assert snapshot(shared) == before


def test_the_store_root_moves_only_below_brigade_memory(tmp_path):
    for bad in ("/abs/path", ".brigade/memory", ".brigade/x/hub", ".brigade/memory/../hub", "elsewhere/hub"):
        with pytest.raises(ValueError):
            with store.relocated(bad):
                pass
    with store.relocated(vstate.REPLICA_ROOT):
        assert store.root(tmp_path) == tmp_path / ".brigade/memory/verify/hub"
    assert store.root(tmp_path) == tmp_path / ".brigade/memory/hub"


def test_a_rate_limited_scope_at_the_pending_ceiling_still_blocks_selection(setup, clock, tmp_path, capsys):
    """Pending proposals count job-wide, even in a scope whose read stats are unavailable this run."""
    target, hub, _ = setup
    put_cards(hub, "repo:second", 2)
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        '[hub]\nmode="read"\nread_scopes=["fleet", "repo:second"]\n[verify]\n'
        f'[[verify.workspace]]\nscope="fleet"\npath="{tmp_path / "one"}"\narchive_projects=[]\n'
        f'[[verify.workspace]]\nscope="repo:second"\npath="{tmp_path / "two"}"\narchive_projects=[]\n'
    )
    # A first run creates verify's replica, which the state binding names.
    assert verify(target, capsys, "run", "--dry-run", "--no-model")[0] == 0
    meta = verify_replica(target).meta
    state = vstate.fresh()
    state["binding"] = verify_cmd._binding(meta["hub"], meta["principal"], meta["log_epoch"])
    pending = {f"{index:032x}": {"seen": 1, "proposed": 1, "pending": hubc.event_id()} for index in range(8)}
    state["scopes"] = {"fleet": {"cards": pending}}
    vstate.save(target, state)
    original = hub.do_read_stats

    def refuse_fleet(body):
        if body["scope"] == "fleet":
            return 429, {"error": SECRET, "code": hubc.READ_STATS_RATE_LIMITED}
        return original(body)

    hub.do_read_stats = refuse_fleet
    code, report = verify(target, capsys, "run", "--dry-run", "--no-model")
    assert code == 0 and report["read_stats"] == {"fleet": "unavailable", "repo:second": "fresh"}
    assert report["selected"] == []
    assert report["skipped"]["pending_ceiling"] == 1
    assert "notes" not in report
