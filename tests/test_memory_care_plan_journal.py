"""Journal planning snapshots preserve refusals and avoid repeated disk scans."""

import json
import statistics
import time
from dataclasses import asdict
from datetime import date

import pytest

from brigade import memory_care_fixes as fixes
from brigade import memory_care_journal as journal
from brigade import memory_care_plan as planner
from brigade import memory_cmd as care

TODAY = date(2026, 11, 13)


def workload(target, count=48, decisions=350):
    cards = target / "memory/cards"
    cards.mkdir(parents=True)
    issues = []
    for n in range(count):
        rel = f"memory/cards/card-{n}.md"
        (target / rel).write_text("---\nlast_reviewed: 2026-11-01\n---\nFact.\n", encoding="utf-8")
        issues.append({"file": rel, "issue_type": "missing-freshness"})
    for n in range(decisions):
        directory = target / journal.JOURNAL_REL / f"20261101T000000Z-fix-{n:08x}"
        directory.mkdir(parents=True)
        decision = journal.Decision(
            directory.name,
            "fix",
            "missing-freshness:derived",
            None,
            "2026-11-01T00:00:00+00:00",
            ("memory/cards/other.md",),
            {},
            None,
            None,
            "undo-1",
        )
        (directory / "decision.json").write_text(
            json.dumps({**asdict(decision), "status": "committed", "before_absent": [True]}), encoding="utf-8"
        )
    return care._config_or_default(target), issues


def test_plan_journal_scaling(tmp_path):
    config, issues = workload(tmp_path)
    samples = {}
    for count in (24, 48):
        samples[count] = []
        for _ in range(5):
            start = time.perf_counter()
            plan = planner.plan_payload(tmp_path, config, {"issues": issues[:count]})
            samples[count].append(time.perf_counter() - start)
            assert plan["plan_count"] == count
            assert plan["blocked_count"] == 0, plan["items"][0]
    print(f"plan_payload seconds: {samples}")
    assert statistics.median(samples[48]) / statistics.median(samples[24]) < 3.5


def test_plan_reads_journal_once_and_refreshes_next_run(tmp_path, monkeypatch):
    config, issues = workload(tmp_path, decisions=350)
    entries = journal.entries
    calls = []

    def counted(target):
        calls.append(target)
        return entries(target)

    monkeypatch.setattr(journal, "entries", counted)
    planner.plan_payload(tmp_path, config, {"issues": issues})
    assert len(calls) == 1
    decision = entries(tmp_path)[0]
    journal.annotate(tmp_path, decision.decision_id, paths=[issues[0]["file"]])
    plan = planner.plan_payload(tmp_path, config, {"issues": issues})
    assert len(calls) == 2
    assert plan["items"][0]["blockers"] == ["undone"]


@pytest.mark.parametrize("kind", ["missing-reviewed", "missing-freshness"])
@pytest.mark.parametrize("state", ["undone", "redone", "never-undone", "missing-backup", "changed"])
def test_prepare_index_equivalence(tmp_path, kind, state):
    from tests.test_memory_care_fixes import card

    path = card(tmp_path, kind)
    rel = path.relative_to(tmp_path).as_posix()
    config = care._config_or_default(tmp_path)
    before = path.read_bytes()
    field = b"last_reviewed: 2026-11-01" if kind == "missing-reviewed" else b"fresh_until: 2027-01-30"
    after = before.replace(b"---\nUseful", field + b"\n---\nUseful")
    decision = journal.commit(
        tmp_path, kind="fix", card_id=None, reason=f"{kind}:derived", changes=[journal.Change(rel, before, after)]
    )
    if state != "never-undone":
        journal.revert(tmp_path, decision.decision_id, reason="test")
        if state == "redone":
            journal.commit(
                tmp_path,
                kind="fix",
                card_id=None,
                reason=f"{kind}:derived",
                changes=[journal.Change(rel, before, after)],
            )
        elif state == "missing-backup":
            (tmp_path / journal.JOURNAL_REL / decision.decision_id / "before/0.bin").unlink()
        elif state == "changed":
            path.write_bytes(before + b"New fact.\n")
    # Undo writes the card today, so use a fixed historical mtime for review-date cases.
    import os

    os.utime(path, (1793491200, 1793491200))
    item = {"card_file": rel, "issue_type": kind}
    context = fixes.plan_context(tmp_path, config)

    def result(**kwargs):
        try:
            return fixes.prepare(tmp_path, config, item, TODAY, context=context, **kwargs)
        except fixes.FixRefused as exc:
            return str(exc)

    standalone = result()
    assert result(undone_index=fixes.UndoneFixIndex(tmp_path)) == standalone
    if state in {"undone", "missing-backup"}:
        assert standalone == "undone"
    elif state in {"redone", "never-undone"}:
        assert standalone == "already-present"
    else:
        assert isinstance(standalone, dict)


@pytest.mark.parametrize("error", [OSError("unreadable"), journal.JournalConflict("invalid decision id")])
def test_plan_shares_journal_failure_without_changing_blockers(tmp_path, monkeypatch, error):
    config, issues = workload(tmp_path, decisions=0)
    calls = []

    def unreadable(target):
        calls.append(target)
        raise error

    monkeypatch.setattr(journal, "entries", unreadable)
    plan = planner.plan_payload(tmp_path, config, {"issues": issues})
    assert len(calls) == 1
    assert plan["blocked_count"] == len(issues)
    assert all(item["blockers"] == ["unreadable"] for item in plan["items"])
