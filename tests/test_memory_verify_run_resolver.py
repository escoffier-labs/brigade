"""`memory verify run` and slice 9a's resolver: one `Run` per non-dry run, closed once, never in a dry run.

The fixtures are `test_memory_verify_run`'s. Slice 9a's resolver is replaced
by a recorder at `memory_verify_resolver.commit_resolver`, and its `Run` by a
subclass that counts `close()`, so no test here runs git. The unsupported
platform test keeps 9a's real `commit_resolver`, which on that platform
returns a resolver that never starts a process.
"""

from __future__ import annotations

import time

import pytest

from brigade import cli
from brigade import memory_hub_client as hubc
from brigade import memory_verify_contract as vc
from brigade import memory_verify_resolver as vr
from brigade import memory_verify_run as run_mod
from brigade import memory_verify_state as vstate
from tests.test_memory_reflect_hub import pytestmark as pytestmark
from tests.test_memory_verify_contract import BLOCK, CARD_ID, CLAIM, card_text
from tests.test_memory_verify_run import SCOPE, SECOND_ID, verify, writes
from tests.test_memory_verify_run import verify_hub as verify_hub

REAL_COMMIT_RESOLVER = vr.commit_resolver


class CountingRun(vr.Run):
    """A resolver `Run` that counts `close()` calls."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        super().close()


class Commits:
    """Stands in for slice 9a: records each workspace scope and `Run` it is asked for, resolves nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, vr.Run | None]] = []
        self.runs: list[CountingRun] = []

    def commit_resolver(self, workspace, run=None):
        self.calls.append((workspace.scope, run))
        return lambda abbreviation: None

    def make_run(self) -> CountingRun:
        run = CountingRun()
        self.runs.append(run)
        return run


@pytest.fixture
def commits(verify_hub, monkeypatch):
    recorder = Commits()
    monkeypatch.setattr(vr, "commit_resolver", recorder.commit_resolver)
    monkeypatch.setattr(vr, "Run", recorder.make_run)
    return recorder


def add_commit_card(hub, card_id=SECOND_ID):
    hub._commit("card.put", SCOPE, card_id, card_text((BLOCK, CLAIM)), "live", hubc.event_id())


def test_every_card_of_one_run_shares_one_resolver_run(verify_hub, capsys, commits):
    target, hub, _, _ = verify_hub
    add_commit_card(hub)
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    checked = {card["card_id"] for card in report["cards"]}
    assert {CARD_ID, SECOND_ID} <= checked
    # One resolver per checked card, all on the bound scope and all on the run's single Run.
    assert len(commits.calls) == len(report["cards"]) >= 2
    assert {scope for scope, _ in commits.calls} == {SCOPE}
    assert len(commits.runs) == 1
    assert all(run is commits.runs[0] for _, run in commits.calls)
    assert commits.runs[0].closed == 1
    # The next run gets a fresh Run, so no fetch proof outlives its run.
    later = int(time.time() * 1000) + 8 * run_mod.DAY_MS
    run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False, now_ms=later)
    assert len(commits.runs) == 2 and commits.runs[1] is not commits.runs[0]
    assert [run.closed for run in commits.runs] == [1, 1]


def test_the_run_is_closed_once_when_a_card_raises_mid_run(verify_hub, commits, monkeypatch):
    target, _, _, _ = verify_hub

    def broken(heads, settings, archive, *, now_ms=None, **_):
        raise RuntimeError("assembly broke")
        yield  # pragma: no cover - makes this a generator like the real assembly

    monkeypatch.setattr(run_mod, "ASSEMBLE", broken)
    with pytest.raises(RuntimeError, match="assembly broke"):
        run_mod.run_payload(target=target, dry_run=False, list_ids=False, scheduled=False)
    assert [run.closed for run in commits.runs] == [1]


def test_the_run_is_closed_once_when_a_run_fails(verify_hub, capsys, commits):
    target, _, fake, _ = verify_hub
    fake.status = "seat_auth"
    status, report = verify(target, capsys)
    assert status == 1 and report["status"] == "failed"
    assert [run.closed for run in commits.runs] == [1]


def test_the_run_is_closed_once_on_the_paused_early_return(verify_hub, capsys, commits):
    target, hub, _, _ = verify_hub
    state = vstate.fresh()
    state.update(consecutive_failures=3, paused=True)
    vstate.save(target, state)
    status, report = verify(target, capsys, "--scheduled")
    assert status == 0 and report["status"] == "paused"
    assert commits.calls == []
    assert [run.closed for run in commits.runs] == [1]


def test_a_dry_run_opens_no_run_and_never_asks_the_resolver(verify_hub, capsys, commits):
    target, hub, fake, _ = verify_hub
    status, report = verify(target, capsys, "--dry-run")
    assert status == 0 and report["status"] == "completed" and report["dry_run"] is True
    assert commits.calls == [] and commits.runs == []
    # The expansion abstains, so only the proposing seat was asked and nothing would be proposed.
    card = next(card for card in report["cards"] if card["card_id"] == CARD_ID)
    assert card["ops"] == [] and [block["reason"] for block in card["blocks"]] == [vc.COMMIT_UNRESOLVED]
    assert "dry_run_proposal" not in report["skipped"]
    assert vr.UNSUPPORTED_PLATFORM not in report["skipped"]
    assert writes(hub) == []
    # The same cards in a non-dry run do reach the resolver, on one Run.
    status, report = verify(target, capsys)
    assert status == 0 and commits.calls and len(commits.runs) == 1


def test_an_unsupported_platform_reports_its_reason_once_and_every_expansion_abstains(
    verify_hub, capsys, commits, monkeypatch
):
    target, hub, fake, _ = verify_hub
    add_commit_card(hub)
    monkeypatch.setattr(vr, "platform_supported", lambda: False)
    monkeypatch.setattr(vr, "commit_resolver", REAL_COMMIT_RESOLVER)
    status, report = verify(target, capsys)
    assert status == 0 and report["status"] == "completed"
    assert report["skipped"][vr.UNSUPPORTED_PLATFORM] == 1
    reasons = {
        card["card_id"]: [block["reason"] for block in card["blocks"]]
        for card in report["cards"]
        if card["card_id"] in (CARD_ID, SECOND_ID)
    }
    assert reasons == {CARD_ID: [vc.COMMIT_UNRESOLVED], SECOND_ID: [vc.COMMIT_UNRESOLVED]}
    assert report["proposals"] == [] and writes(hub) == []
    assert [run.closed for run in commits.runs] == [1]
    # The text report of the next run names the fixed code once and nothing else about the resolver.
    assert cli.main(["memory", "verify", "run", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert out.count(vr.UNSUPPORTED_PLATFORM) == 1
    assert f"skipped {vr.UNSUPPORTED_PLATFORM} 1" in out
    assert str(target) not in out and "refs/" not in out
