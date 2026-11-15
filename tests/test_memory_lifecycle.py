"""Unused-card lifecycle, protection, search and rollback acceptance."""

from datetime import date, datetime, timezone
import json
import os
import shutil

import pytest

from brigade import memory_card_reads as reads, memory_cmd, memory_lifecycle as life
from brigade import memory_care_journal as journal, memory_care_tend as tend
from brigade.memory_retrieval_eval import DEFAULT_FIXTURE_ROOT, run_eval

TODAY = date(2026, 11, 15)


def card(root, name="old", meta="trust: agent-unreviewed\n", body="retrievalwidget"):
    path = root / f"memory/cards/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ncard_id: {name}\n{meta}---\n{body}\n")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    reads.record(root, [], today=date(2026, 1, 1))
    return path


@pytest.mark.parametrize("crlf", [False, True])
def test_unused_archive_search_and_one_command_restore(tmp_path, crlf):
    path = card(tmp_path)
    if crlf:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    original = path.read_bytes()
    payload = life.run(tmp_path, today=TODAY)
    assert [r["to_state"] for r in payload["transitions"]] == ["stale", "archived"]
    assert not path.exists()
    assert (tmp_path / "memory/archive/cards/old.md").read_bytes() == original
    assert not memory_cmd.search_cards_payload(tmp_path, "retrievalwidget")["matches"]
    assert memory_cmd.search_cards_payload(tmp_path, "retrievalwidget", include_archived=True)["matches"]
    for row in payload["transitions"]:
        assert journal._payload(tmp_path, row["decision_id"])["actor"] == "policy"
        assert row["policy_digest"]
    tend.undo(tmp_path, payload["transitions"][-1]["decision_id"])
    assert path.read_bytes() == original
    assert memory_cmd.search_cards_payload(tmp_path, "retrievalwidget")["matches"]
    assert not life.run(tmp_path, today=TODAY)["transitions"][-1:][0]["to_state"] == "archived"


@pytest.mark.parametrize(
    "meta",
    [
        "trust: human\n",
        "trust: agent-unreviewed\npinned: true\n",
        "trust: agent-unreviewed\narchive: never\n",
        "",
        "trust: agent-unreviewed\nauthor_type: human\n",
    ],
)
def test_human_pinned_and_unknown_cards_stay(tmp_path, meta):
    path = card(tmp_path, meta=meta)
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize(
    "reference",
    [
        "memory/cards/active.md",
        ".brigade/memory-care/runbooks/example.json",
        ".brigade/care/schedule.json",
        "docs/runbooks/operate.md",
        "memory/pinned.md",
    ],
)
def test_references_protect(tmp_path, reference):
    path = card(tmp_path)
    ref = tmp_path / reference
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("[Old](memory/cards/old.md)")
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize("source", ["read", "interactive", "outcome", "citation", "search"])
def test_observed_use_protects(tmp_path, source):
    card(tmp_path)
    if source == "read":
        reads.record(tmp_path, ["old"], today=TODAY)
    elif source == "interactive":
        path = tmp_path / ".brigade/work/claude-hooks/recall/session.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"v": 1, "opened": ["old"], "interactive_open_days": {"old": [TODAY.isoformat()]}}))
    else:
        rel, row = {
            "outcome": ("memory/outcome/records.jsonl", {"artifact_id": "old", "ts": TODAY.isoformat()}),
            "citation": (".brigade/memory/reflect/evidence-log.jsonl", {"card_id": "old", "ts": TODAY.isoformat()}),
            "search": (".brigade/memory/search-log.jsonl", {"card_ids": ["old"], "ts": TODAY.isoformat()}),
        }[source]
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row) + "\n")
    assert not life.run(tmp_path, today=TODAY)["transitions"]


def test_policy_windows_off_switch_dry_run_and_rollback(tmp_path):
    path = card(tmp_path)
    cfg = tmp_path / ".brigade/memory-care.toml"
    cfg.write_text("[lifecycle]\nstale_after_days = 300\narchive_after_days = 400\n")
    rows = life.run(tmp_path, today=TODAY, dry_run=True)["transitions"]
    assert [r["to_state"] for r in rows] == ["stale"]
    assert not (tmp_path / journal.JOURNAL_REL).exists()
    rows = life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()
    tend.undo(tmp_path, rows[0]["decision_id"])
    cfg.write_text("[lifecycle]\nenabled = false\n")
    assert not life.run(tmp_path, today=TODAY)["transitions"]


def test_new_history_and_new_card_are_protected(tmp_path):
    path = card(tmp_path)
    os.utime(path, None)
    assert not life.run(tmp_path, today=TODAY)["transitions"]


def test_blast_cap(tmp_path):
    card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nmax_transitions_per_run = 1\n")
    assert [r["to_state"] for r in life.run(tmp_path, today=TODAY)["transitions"]] == ["stale"]
    assert [r["to_state"] for r in life.run(tmp_path, today=TODAY)["transitions"]] == ["archived"]


def test_retrieval_fixture_unchanged_with_archived_decoy(tmp_path):
    baseline = run_eval(adapters=["current"])["adapters"]["current"]
    shutil.copytree(DEFAULT_FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    archive = tmp_path / "memory/archive/cards"
    archive.mkdir(parents=True)
    (archive / "decoy.md").write_text("---\ntitle: Restic backup schedule\n---\nrestic snapshot prune backup schedule")
    assert run_eval(fixture_root=tmp_path, adapters=["current"])["adapters"]["current"] == baseline


def test_stale_reactivated_by_use(tmp_path):
    card(tmp_path)
    rows = life.run(tmp_path, today=date(2026, 2, 1))["transitions"]
    assert [r["to_state"] for r in rows] == ["stale"]
    reads.record(tmp_path, ["old"], today=date(2026, 2, 2))
    rows = life.run(tmp_path, today=date(2026, 2, 2))["transitions"]
    assert [r["to_state"] for r in rows] == ["active"]


def test_unavailable_hub_protects(tmp_path, monkeypatch):
    path = card(tmp_path)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **k: None)
    assert life.run(tmp_path, today=TODAY)["blocked"]
    assert path.exists()


@pytest.mark.parametrize("days,state", [(29, None), (30, "stale"), (89, "stale"), (90, "archived")])
def test_window_boundaries(tmp_path, days, state):
    from datetime import timedelta

    card(tmp_path)
    rows = life.run(tmp_path, today=date(2026, 1, 1) + timedelta(days=days))["transitions"]
    assert (rows[-1]["to_state"] if rows else None) == state


def test_invalid_use_data_leaves_card_unchanged(tmp_path):
    path = card(tmp_path)
    original = path.read_bytes()
    log = tmp_path / ".brigade/memory/search-log.jsonl"
    log.write_text('{"card_ids": ["old"]}\n')
    with pytest.raises(ValueError, match="undated"):
        life.run(tmp_path, today=TODAY)
    assert path.read_bytes() == original
    assert not (tmp_path / journal.JOURNAL_REL).exists()


def test_expired_human_or_referenced_cards_are_protected(tmp_path):
    from brigade import memory_care_retire

    path = card(tmp_path, meta="trust: human\nexpires: 2026-01-01\n")
    assert not memory_care_retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)[
        "candidates"
    ]
    assert path.exists()


def test_cli_archive_flag(tmp_path, capsys):
    from brigade.cli import main

    card(tmp_path)
    life.run(tmp_path, today=TODAY)
    assert main(["memory", "search", "retrievalwidget", "--target", str(tmp_path), "--include-archived", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["matches"][0]["path"].startswith("memory/archive/")


def test_changed_card_refused_before_archive(tmp_path, monkeypatch):
    from brigade import memory_archive

    path = card(tmp_path)
    real = memory_archive.archive_card

    def changed(*args, **kwargs):
        path.write_text("---\ntrust: human\n---\nNew human text\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_archive, "archive_card", changed)
    with pytest.raises(memory_archive.ArchiveRefused, match="changed since"):
        life.run(tmp_path, today=TODAY)
    assert "New human text" in path.read_text()
    assert not (tmp_path / "memory/archive/cards/old.md").exists()


def test_daily_schedule_installs_lifecycle(tmp_path):
    from brigade import care_cmd

    care_cmd._ensure_memory_care_runbooks(tmp_path)
    path = tmp_path / ".brigade/memory-care/runbooks/daily-care-pass.json"
    assert any("tend --stage lifecycle" in step["run"] for step in json.loads(path.read_text())["steps"])


def test_evidence_retraction_does_not_block_care(tmp_path):
    card(tmp_path)
    log = tmp_path / ".brigade/memory/reflect/evidence-log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"retract_sha256": "a" * 64}) + "\n")
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


def test_unread_workspace_starts_observation_window(tmp_path):
    card(tmp_path)
    (tmp_path / reads.READS_REL).unlink()
    assert not life.run(tmp_path, today=date(2026, 1, 1))["transitions"]
    assert reads.load(tmp_path).tracking_since == date(2026, 1, 1)
    assert life.run(tmp_path, today=date(2026, 4, 1))["transitions"][-1]["to_state"] == "archived"
