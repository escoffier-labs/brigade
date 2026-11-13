"""Automatic repairs use real scans, match digests, and remain reversible."""

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from brigade import memory_care_plan as planner, memory_cmd as care
from brigade import memory_care_tend as tend, memory_care_journal as journal

TODAY = date(2026, 11, 13)


def card(target, kind):
    path = target / "memory/cards/a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"topic": "A", "confidence": "high", "evidence": "[README.md]"}
    if kind != "missing-reviewed":
        fields["last_reviewed"] = "2026-11-01"
    if kind != "missing-freshness":
        fields["fresh_until"] = "2027-01-01"
    path.write_text(
        "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\nUseful fact.\n", encoding="utf-8"
    )
    os.utime(path, (1793491200, 1793491200))
    (target / "README.md").write_text("Evidence.\n", encoding="utf-8")
    (target / "MEMORY.md").write_text(
        "- [Missing](memory/cards/missing.md)\n"
        if kind in {"missing-index-link", "orphaned-card"}
        else "- [A](memory/cards/a.md)\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("kind", care.CHECKS)
def test_every_kind_classified_and_preview_matches_apply(tmp_path, kind):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, kind)
    before = path.read_bytes()
    config = care._config_or_default(tmp_path)
    scan = care._scan_payload(tmp_path, config)
    # Exercise the planner's actual issue producer for reported kinds too.
    issue = care._issue(
        card_id="a",
        target=tmp_path,
        card_path="memory/cards/a.md",
        issue_type=kind,
        severity="low",
        summary="test",
        evidence=[],
        action="review",
    )
    assert kind in fixes.FIX_POLICY
    assert issue["issue_type"] == kind
    plan = planner.plan_payload(tmp_path, config, scan)
    preview = fixes.apply_fixes(tmp_path, config, today=TODAY, dry_run=True, plan=plan)
    assert path.read_bytes() == before
    actual = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert {k: preview[k] for k in ("applied", "skipped", "reasons")} == {
        k: actual[k] for k in ("applied", "skipped", "reasons")
    }
    if kind not in fixes.AUTO_FIXES:
        assert actual["applied"] == 0
        assert path.read_bytes() == before
    if kind in fixes.AUTO_FIXES:
        assert actual["applied"] == 1
        d = journal.load(tmp_path, actual["decision_ids"][0])
        assert d.kind == "fix" and kind in d.reason
        if kind == "missing-reviewed":
            assert "file-mtime" in d.reason
            assert "last_reviewed: 2026-11-01" in path.read_text(encoding="utf-8")
        if kind == "missing-freshness":
            assert "fresh_until: 2027-01-30" in path.read_text(encoding="utf-8")
        tend.undo(tmp_path, d.decision_id)
        assert path.read_bytes() == before
        assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
            "" if kind == "missing-index-link" else "- [A](memory/cards/a.md)\n"
        )


@pytest.mark.parametrize(
    "protection", ["pin", "malformed", "read-only", "digest", "oversized", "unreadable", "size-budget"]
)
def test_unsafe_cards_unchanged(tmp_path, protection):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-freshness")
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    if protection == "read-only":
        from dataclasses import replace

        config = replace(config, read_only_paths=("memory/cards/",))
    elif protection == "size-budget":
        from dataclasses import replace

        config = replace(config, max_card_bytes=32)
    elif protection == "pin":
        path.write_bytes(path.read_bytes().replace(b"topic:", b"archive: never\ntopic:"))
    elif protection == "malformed":
        path.write_bytes(b"---\nunterminated")
    elif protection == "oversized":
        path.write_bytes(b"a" * (fixes.MAX_CARD_BYTES + 1))
    elif protection == "unreadable":
        path.write_bytes(b"\xff")
    else:
        path.write_bytes(path.read_bytes() + b"Changed.\n")
    before = path.read_bytes()
    result = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert result["applied"] == 0 and result["skipped"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("source", ["git", "future", "missing"])
def test_review_date_sources_fail_closed(tmp_path, monkeypatch, source):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-reviewed")
    if source == "git":
        import subprocess

        for args in (
            ["init", "-q"],
            ["add", "."],
            ["-c", "user.name=Example", "-c", "user.email=fixture", "commit", "-qm", "fixture"],
        ):
            subprocess.run(
                ["git", "-C", str(tmp_path), *args],
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_DATE": "2026-10-01T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-10-01T00:00:00Z",
                },
            )
    elif source == "future":
        stamp = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    else:
        import subprocess

        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    result = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert result["applied"] == (1 if source == "git" else 0)
    if source != "git":
        assert all(not item["candidate_fields"] for item in plan["items"] if item["issue_type"] == "missing-reviewed")
    if source == "git":
        assert "last_reviewed: 2026-10-01" in path.read_text(encoding="utf-8")
        assert "git-last-commit" in journal.entries(tmp_path)[0].reason


@pytest.mark.parametrize("kind", ["missing-reviewed", "missing-freshness"])
@pytest.mark.parametrize("enabled", [False, True])
def test_tend_enabled_and_disabled_planning_twins(tmp_path, monkeypatch, kind, enabled):
    card(tmp_path, kind)
    monkeypatch.setattr(care, "_today", lambda: TODAY)
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/memory-care.toml").write_text(f"[tend]\nenabled = {str(enabled).lower()}\n", encoding="utf-8")
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    item = next(i for i in plan["items"] if i["issue_type"] == kind)
    assert item["safe_to_apply_automatically"] is enabled
    if kind == "missing-reviewed":
        assert item["blockers"] == ([] if enabled else ["requires-current-evidence-review"])
        assert item["candidate_fields"]["last_reviewed"] == ("2026-11-01" if enabled else TODAY.isoformat())


def test_relative_target_cli_and_tend_stage(tmp_path, monkeypatch, capsys):
    from brigade.cli import main

    target = tmp_path / "ws"
    path = card(target, "missing-freshness")
    monkeypatch.chdir(tmp_path)
    assert care.scan(target=target, json_output=True) == 0
    before = path.read_bytes()
    assert main(["memory", "care", "plan-fixes", "--target", "ws"]) == 0
    assert path.read_bytes() == before
    assert main(["memory", "care", "plan-fixes", "--target", "ws", "--apply"]) == 0
    assert b"fresh_until:" in path.read_bytes()
    assert tend.STAGES.index("fixes") < tend.STAGES.index("retire")
    tend.undo(target, journal.entries(target)[0].decision_id)
    undone = path.read_bytes()
    assert undone == before
    # The undo sticks: the next run reports the fix as undone and leaves the card alone.
    result = tend.run(Path("ws"), stages=["fixes"], today=TODAY)
    assert result["stages"]["fixes"]["applied"] == 0
    assert path.read_bytes() == undone
    from brigade import memory_care_fixes as fixes

    item = {"card_file": "memory/cards/a.md", "issue_type": "missing-freshness"}
    with pytest.raises(fixes.FixRefused, match="undone"):
        fixes.prepare(target.resolve(), care._config_or_default(target), item, TODAY)
    # Once the card is edited, the earlier undo no longer speaks for it.
    path.write_bytes(undone.replace(b"Useful fact.", b"Useful fact, revised."))
    os.utime(path, (1793491200, 1793491200))
    assert care.scan(target=target, json_output=True) == 0
    result = tend.run(Path("ws"), stages=["fixes"], today=TODAY)
    assert result["stages"]["fixes"]["applied"] == 1


def test_missing_both_dates_applies_only_supported_fields(tmp_path):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-reviewed")
    path.write_bytes(path.read_bytes().replace(b"fresh_until: 2027-01-01\n", b""))
    os.utime(path, (1793491200, 1793491200))
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 1
    assert result["skipped"] == 1
    assert b"last_reviewed: 2026-11-01" in path.read_bytes()
    assert b"fresh_until" not in path.read_bytes()


def test_mtime_unavailable_is_reported(tmp_path, monkeypatch):
    from brigade import memory_care_fixes as fixes

    card(tmp_path, "missing-reviewed")

    class Unavailable:
        @staticmethod
        def fromtimestamp(*args, **kwargs):
            raise OSError("timestamp unavailable")

    monkeypatch.setattr(fixes, "datetime", Unavailable)
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 0
    assert result["reasons"] == {"date-unavailable": 1}


def test_persisted_plan_does_not_adopt_new_bytes(tmp_path):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-freshness")
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    path.write_bytes(path.read_bytes() + b"Later edit.\n")
    preview = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan, dry_run=True)
    actual = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert preview["reasons"] == actual["reasons"] == {"changed": 1}
    assert not journal.entries(tmp_path)


def test_ambiguous_review_dates_are_reported(tmp_path):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-freshness")
    path.write_bytes(
        path.read_bytes().replace(b"last_reviewed: 2026-11-01", b"last_reviewed: 2026-11-01\nreviewed_at: 2026-10-01")
    )
    before = path.read_bytes()
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 0 and result["skipped"] == 1
    assert path.read_bytes() == before


def test_unreadable_scan_reports_counts_without_exception_text(tmp_path, monkeypatch, capsys):
    from brigade import memory_care_fixes as fixes

    card(tmp_path, "missing-reviewed")

    def unreadable(*args):
        raise OSError("sensitive card text")

    monkeypatch.setattr(care, "_scan_payload", unreadable)
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 0 and result["skipped"] == 1
    assert result["reasons"] == {"scan-unreadable": 1}
    assert "sensitive" not in fixes.summary(result)
    assert str(tmp_path) not in fixes.summary(result)


def test_unbound_plan_is_reported_without_writing(tmp_path):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-freshness")
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    plan["items"][0].pop("content_digest")
    before = path.read_bytes()
    result = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert result["applied"] == 0 and result["skipped"] == 1
    assert path.read_bytes() == before


def test_cli_bad_scan_prints_only_fixed_count_summary(tmp_path, capsys):
    card(tmp_path, "missing-reviewed")
    assert care.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    config = care._config_or_default(tmp_path)
    care._scan_path(tmp_path, config).write_text("{", encoding="utf-8")
    assert planner.plan_fixes(target=tmp_path, apply=True) == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "applied=0 skipped=1" in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize("phase", ["prepare", "finish"])
def test_fix_crash_recovers_and_remains_undoable(tmp_path, monkeypatch, phase):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-freshness")
    before = path.read_bytes()
    execute = journal._execute_phase

    class Crash(BaseException):
        pass

    def interrupt(target, decision_id, step, changes):
        execute(target, decision_id, step, changes)
        if phase == step:
            raise Crash()

    monkeypatch.setattr(journal, "_execute_phase", interrupt)
    with pytest.raises(Crash):
        fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    monkeypatch.setattr(journal, "_execute_phase", execute)
    fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    d = next(d for d in journal.entries(tmp_path) if d.kind == "fix")
    assert b"fresh_until:" in path.read_bytes()
    tend.undo(tmp_path, d.decision_id)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind,nested", [("missing-freshness", "fresh_until"), ("missing-reviewed", "last_reviewed")])
def test_nested_text_that_looks_like_a_date_field_is_never_overwritten(tmp_path, kind, nested):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, kind)
    # An empty value is what makes the scanner treat the field as missing.
    note = f"notes: |\n  {nested}:\n  keep this sentence\n".encode()
    path.write_bytes(path.read_bytes().replace(b"topic: A\n", b"topic: A\n" + note))
    before = path.read_bytes()
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 0
    assert path.read_bytes() == before


def test_a_file_written_today_is_not_a_review_date(tmp_path):
    from brigade import memory_care_fixes as fixes

    path = card(tmp_path, "missing-reviewed")
    stamp = datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    before = path.read_bytes()
    result = fixes.apply_fixes(tmp_path, care._config_or_default(tmp_path), today=TODAY)
    assert result["applied"] == 0
    assert path.read_bytes() == before
    item = {"card_file": "memory/cards/a.md", "issue_type": "missing-reviewed"}
    with pytest.raises(fixes.FixRefused, match="date-too-recent"):
        fixes.prepare(tmp_path, care._config_or_default(tmp_path), item, TODAY)
