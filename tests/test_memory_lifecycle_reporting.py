"""Lifecycle stage reporting: per-card isolation, path cost, floors and blocked keys."""

from datetime import date, datetime, timezone
import bisect
import json
import os

import pytest

from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import coverage, signing_key
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler

from brigade import memory_card_reads as reads, memory_cmd, memory_lifecycle as life
from brigade import memory_care_journal as journal, memory_care_tend as tend

TODAY = date(2026, 11, 15)


def card(root, name="old", meta="trust: agent-unreviewed\n", body="retrievalwidget"):
    path = root / f"memory/cards/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"---\ncard_id: {name}\n{meta}---\n{body}\n".encode())
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    signing_key(root)
    reads.record(root, [], today=date(2026, 1, 1))
    return path


def observed_run(root, *, today, **kwargs):
    ledger = reads.load(root)
    coverage(root, ledger.tracking_since, today)
    return life.run(root, today=today, **kwargs)


def test_one_bad_card_keeps_committed_transitions_and_later_cards(tmp_path, monkeypatch):
    """A card that cannot commit is skipped, siblings still run, and the stage fails."""
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    for name in ("a", "b", "c"):
        card(tmp_path, name=name)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    commit = journal.commit_phased

    def broken(target, **kwargs):
        if (kwargs.get("metadata") or {}).get("card_path") == "memory/cards/b.md":
            raise journal.JournalConflict("b is unreadable")
        return commit(target, **kwargs)

    monkeypatch.setattr(journal, "commit_phased", broken)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    stage = result["stages"]["lifecycle"]
    assert [row["card_path"] for row in stage["transitions"]] == ["memory/cards/a.md", "memory/cards/c.md"]
    assert all(row["decision_id"] for row in stage["transitions"])
    assert any(row["path"] == "memory/cards/b.md" and "b is unreadable" in row["reason"] for row in stage["skipped"]), (
        stage
    )
    assert stage["error"] is True
    assert result["failed_stages"] == ["lifecycle"]
    assert result["failed"] == 1
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result


def test_non_utf8_card_is_skipped_and_fails_the_stage(tmp_path, monkeypatch):
    """A card file that cannot be decoded is reported, not dropped, and the stage fails."""
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    card(tmp_path, name="a")
    bad = tmp_path / "memory/cards/b.md"
    bad.write_bytes(b"---\ncard_id: b\n---\n\xff\xfe unreadable\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    stage = result["stages"]["lifecycle"]
    assert any(row["path"] == "memory/cards/b.md" and "not UTF-8" in row["reason"] for row in stage["skipped"]), stage
    assert [row["card_path"] for row in stage["transitions"]] == ["memory/cards/a.md"]
    assert stage["error"] is True
    assert result["failed_stages"] == ["lifecycle"]
    assert bad.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_symlinked_lifecycle_state_fails_the_stage(tmp_path, monkeypatch):
    """A symlinked lifecycle state directory stays a stage failure, not a card skip."""
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    card(tmp_path, name="a")
    outside = tmp_path / "outside"
    outside.mkdir()
    state_dir = tmp_path / ".brigade/memory/lifecycle"
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.symlink_to(outside)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    with pytest.raises(journal.SymlinkPath):
        life.run(tmp_path, today=TODAY)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    stage = result["stages"]["lifecycle"]
    assert stage["error"] is True
    assert result["failed_stages"] == ["lifecycle"]
    assert stage["blocked"] == "lifecycle signals unreadable"


def test_absolute_path_scan_work_is_bounded_by_occurrences(tmp_path, monkeypatch):
    """Many segments on one line cost one index pass, not one scan per segment."""
    name = "memory/cards/target.md"
    # 24,000 name occurrences, each preceded by "//" so none matches and the
    # whole line is scanned. The old backtracking pattern rescanned the line
    # from every path start; the bounded scan indexes the line once.
    pieces = 24_000
    text = " /a//memory/cards/target.md" * pieces
    calls = {"starts": 0, "breaks": 0, "bisect": 0}

    class CountingPattern:
        def __init__(self, pattern, key):
            self.pattern, self.key = pattern, key

        def finditer(self, value):
            calls[self.key] += 1
            return self.pattern.finditer(value)

    class CountingBisect:
        def bisect_right(self, values, position):
            calls["bisect"] += 1
            return bisect.bisect_right(values, position)

    monkeypatch.setattr(life, "_PATH_START", CountingPattern(life._PATH_START, "starts"))
    monkeypatch.setattr(life, "_PATH_BREAK", CountingPattern(life._PATH_BREAK, "breaks"))
    monkeypatch.setattr(life, "bisect", CountingBisect())
    assert not life._mentioned(text, name, "target")
    assert calls["starts"] == 1
    assert calls["breaks"] == 1
    # Two lookups per name occurrence, never one per path segment.
    assert calls["bisect"] == 2 * pieces


@pytest.mark.parametrize(
    "reference",
    [
        "/srv/ws/memory/cards/target.md",
        "/memory/cards/target.md",
        '"/srv/ws/memory/cards/target.md"',
        "/srv/ws/memory/cards/target.md)",
        "/srv/ws/memory/cards/target.md#section",
        "/dir/sub dir/memory/cards/target.md",
        "/dir/sub\u2028dir/memory/cards/target.md",
    ],
)
def test_absolute_path_matches_are_unchanged(reference):
    assert life._mentioned(reference, "memory/cards/target.md", "target")


@pytest.mark.parametrize(
    "reference",
    [
        "ws/memory/cards/target.md",
        "ws//memory/cards/target.md",
        "/ws//memory/cards/target.md",
        "/srv/ws/memory/cards/target.mdx",
        "/srv/ws/memory/cards/target.md.bak",
        "/srv/ws/memory/cards/target.md/wrapper",
        "ws/memory/cards/target.md /also/path",
    ],
)
def test_absolute_path_mismatches_are_unchanged(reference):
    assert not life._mentioned(reference, "memory/cards/target.md", "target")


def test_raised_policy_floors_are_reported(tmp_path, monkeypatch):
    """Values below the floors still apply, and the stage says what it raised."""
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nstale_after_days = 7\narchive_after_days = 60\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    payload = life.run(tmp_path, today=TODAY)
    assert payload["policy_adjusted"] == {
        "stale_after_days": {"configured": 7, "applied": 30},
        "archive_after_days": {"configured": 60, "applied": 90},
    }
    assert [row["to_state"] for row in payload["transitions"]] == ["stale"]
    assert life.policy(tmp_path)["stale_after_days"] == 30


def test_default_policy_reports_no_adjustment(tmp_path, monkeypatch):
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    assert "policy_adjusted" not in life.run(tmp_path, today=TODAY)


def test_unavailable_hub_blocks_archive_only(tmp_path, monkeypatch):
    """No hub stops archiving, but stale transitions still commit and are reported."""
    path = card(tmp_path)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *args, **kwargs: None)
    payload = observed_run(tmp_path, today=TODAY)
    assert payload["archive_blocked"] == "hub read coverage unavailable"
    assert "blocked" not in payload
    assert [row["to_state"] for row in payload["transitions"]] == ["stale"]
    assert any(
        row["path"] == "memory/cards/old.md" and "hub read coverage" in row["reason"] for row in payload["skipped"]
    )
    assert path.exists()


def test_status_separates_archive_blocked_from_blocked(tmp_path, monkeypatch, capsys):
    card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *args, **kwargs: None)
    tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "lifecycle_stage: archive_blocked=hub read coverage unavailable transitions=1" in out

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"]["archive_blocked"] == "hub read coverage unavailable"
    assert payload["lifecycle"]["transitions"] == 1
    assert payload["lifecycle"]["blocked"] is None


def test_status_reports_a_stage_that_did_nothing(tmp_path, monkeypatch, capsys):
    card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)

    def unreadable(*args, **kwargs):
        raise life.InstalledSchedulesUnreadable("installed care schedules are unreadable")

    monkeypatch.setattr(life, "_references", unreadable)
    tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "lifecycle_stage: blocked=installed care schedules are unreadable transitions=0" in out
    assert "archive_blocked" not in out

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"]["blocked"] == "installed care schedules are unreadable"
    assert payload["lifecycle"]["archive_blocked"] is None
    assert payload["lifecycle"]["transitions"] == 0


def test_status_reports_raised_policy_floors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nstale_after_days = 7\narchive_after_days = 60\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "lifecycle_policy_adjusted: archive_after_days=60->90, stale_after_days=7->30" in out

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"]["policy_adjusted"] == {
        "stale_after_days": {"configured": 7, "applied": 30},
        "archive_after_days": {"configured": 60, "applied": 90},
    }
