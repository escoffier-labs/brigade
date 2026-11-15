"""Security regressions for unused-card evidence and reference protection."""

from tests.memory_read_fixtures import windows_scheduler as windows_scheduler

from datetime import date, datetime, timezone
import json
import os

import pytest

from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

from brigade import memory_card_reads as reads, memory_cmd, memory_lifecycle as life
from tests.memory_read_fixtures import coverage, signing_key
from tests.test_memory_outcome_evidence import verified_card as verified_card

TODAY = date(2026, 11, 15)
OLD = date(2026, 1, 1)
ID = "card-12345678-1234-4234-8234-123456789abc"


def old_card(root, *, meta="", name="notes", crlf=False):
    path = root / f"memory/cards/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\nid: {ID}\ntrust: agent-unreviewed\n{meta}---\nwidget\n"
    path.write_bytes(text.replace("\n", "\r\n").encode() if crlf else text.encode())
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    signing_key(root)
    reads.record(root, [], today=OLD)
    return path


def cover(root, end=TODAY):
    """Model an interval with daily authenticated heartbeats."""
    coverage(root, OLD, end)


@pytest.mark.parametrize("source", ["search", "outcome", "citation"])
def test_unattested_rows_do_not_keep_card_alive(tmp_path, source):
    path = old_card(tmp_path)
    cover(tmp_path)
    rel, row = {
        "search": (".brigade/memory/search-log.jsonl", {"card_ids": [ID], "ts": TODAY.isoformat()}),
        "outcome": ("memory/outcome/records.jsonl", {"artifact_id": ID, "ts": TODAY.isoformat()}),
        "citation": (".brigade/memory/reflect/evidence-log.jsonl", {"card_id": ID, "ts": TODAY.isoformat()}),
    }[source]
    log = tmp_path / rel
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(row) + "\n")
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"
    assert not path.exists()


def test_search_does_not_record_open(tmp_path):
    old_card(tmp_path)
    memory_cmd.search(target=tmp_path, query="widget")
    assert not reads.load(tmp_path).last_read


@pytest.mark.parametrize("field", ["created", "updated", "last_reviewed"])
def test_future_metadata_does_not_protect(tmp_path, field):
    old_card(tmp_path, meta=f"{field}: 2099-01-01\n")
    cover(tmp_path)
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


@pytest.mark.parametrize("reference", [".brigade/runbooks/runs/example/01.stdout.log", "memory/shipping/note.md"])
def test_outputs_and_substring_pin_paths_do_not_protect(tmp_path, reference):
    old_card(tmp_path)
    cover(tmp_path)
    ref = tmp_path / reference
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(f"{ID}\nmemory/cards/notes.md")
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


def test_binary_reference_is_skipped(tmp_path):
    old_card(tmp_path)
    cover(tmp_path)
    path = tmp_path / "docs/runbooks/diagram.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\xff")
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


@pytest.mark.parametrize(
    "reference", ["AGENTS.md", ".claude/settings.json", ".brigade/work/tasks.json", ".brigade/workflows/active.json"]
)
def test_instruction_and_task_references_protect(tmp_path, reference):
    path = old_card(tmp_path)
    cover(tmp_path)
    ref = tmp_path / reference
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(ID)
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


def test_crlf_identity_and_alias_reads_protect(tmp_path):
    path = old_card(tmp_path, crlf=True, meta='aliases: ["memory/cards/previous.md"]\n')
    cover(tmp_path)
    reads.record(tmp_path, ["memory/cards/previous.md"], today=TODAY)
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


def test_mutual_unused_cards_and_bare_stems_do_not_protect(tmp_path):
    first = old_card(tmp_path, meta="", name="notes")
    second = tmp_path / "memory/cards/other.md"
    second.write_bytes(b"---\ncard_id: other\ntrust: agent-unreviewed\n---\nmemory/cards/notes.md\n")
    first.write_bytes(first.read_bytes() + b"memory/cards/other.md\n")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (first, second):
        os.utime(path, (stamp, stamp))
    cover(tmp_path)
    assert len(life.run(tmp_path, today=TODAY)["transitions"]) == 4


def test_coverage_gap_blocks_transitions(tmp_path):
    path = old_card(tmp_path)
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize("failure", ["busy", "unwritable"])
def test_recorder_failure_resets_coverage_before_transitions(tmp_path, monkeypatch, failure):
    from contextlib import contextmanager
    from brigade import inbox_lock

    path = old_card(tmp_path)
    cover(tmp_path)
    with monkeypatch.context() as patch:
        if failure == "busy":

            @contextmanager
            def busy(*args, **kwargs):
                raise inbox_lock.InboxLockTimeout("busy")
                yield

            patch.setattr(inbox_lock, "held_file_lock", busy)
        else:
            real = reads._write_json

            def unwritable(target, rel, value):
                if rel == reads.READS_REL:
                    raise PermissionError("ledger is unwritable")
                real(target, rel, value)

            patch.setattr(reads, "_write_json", unwritable)
        assert reads.record(tmp_path, [ID], today=TODAY) is False
        assert reads.load(tmp_path).recorder_error
    assert not life.run(tmp_path, today=TODAY)["transitions"]
    assert reads.load(tmp_path).healthy_since == TODAY
    assert path.exists()


def test_interrupted_read_blocks_even_a_successful_heartbeat(tmp_path):
    old_card(tmp_path)
    cover(tmp_path)
    reads._write_json(
        tmp_path, f"{reads.COVERAGE_REL}/interrupted.json", {"state": "pending", "day": TODAY.isoformat()}
    )
    assert life.run(tmp_path, today=TODAY)["blocked"]
    assert reads.load(tmp_path).recorder_error


def test_daily_coverage_requires_continuity(tmp_path):
    from datetime import timedelta

    signing_key(tmp_path)
    reads.record(tmp_path, [], today=OLD)
    reads.record(tmp_path, [], today=OLD + timedelta(days=1))
    assert reads.covered(reads.load(tmp_path), today=OLD + timedelta(days=1), days=1)
    reads.record(tmp_path, [], today=OLD + timedelta(days=7))
    assert not reads.covered(reads.load(tmp_path), today=OLD + timedelta(days=7), days=1)


@pytest.mark.parametrize("source", ["local", "hub", "interactive"])
def test_future_read_dates_are_ignored(tmp_path, monkeypatch, source):
    old_card(tmp_path)
    cover(tmp_path)
    if source == "local":
        reads.record(tmp_path, [ID], today=date(2099, 1, 1))
        # Keep a healthy current interval independent of the poisoned row.
        cover(tmp_path)
    elif source == "hub":
        monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **k: {ID: date(2099, 1, 1)})
    else:
        path = tmp_path / ".brigade/work/claude-hooks/recall/session.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"v": 1, "opened": [ID], "interactive_open_days": {ID: ["2099-01-01"]}}))
    assert life.run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


@pytest.mark.parametrize("reference", [ID, "memory/cards/previous.md", "notes"])
def test_recent_card_references_use_ids_and_declared_aliases(tmp_path, reference):
    path = old_card(tmp_path, meta='aliases: ["memory/cards/previous.md"]\n')
    cover(tmp_path)
    ref = tmp_path / "memory/cards/recent.md"
    ref.write_bytes(f"---\ncard_id: recent\ntrust: agent-unreviewed\n---\n{reference}\n".encode())
    reads.record(tmp_path, ["recent"], today=TODAY)
    life.run(tmp_path, today=TODAY)
    assert path.exists() == (reference != "notes")


@pytest.mark.parametrize("field", ["author: Human", "author_type: HUMAN", "trust: Human-reviewed"])
def test_expiry_retire_respects_mixed_case_human(tmp_path, field):
    from brigade import memory_care_retire, memory_care_tend

    old_card(tmp_path, meta=f"{field}\nexpires: 2026-01-01\n")
    cover(tmp_path)
    assert not memory_care_retire.plan(
        tmp_path, memory_cmd.MemoryCareConfig(), memory_care_tend.TendConfig(), today=TODAY
    )["candidates"]


def test_stale_undo_records_use(tmp_path):
    from brigade import memory_care_tend

    old_card(tmp_path)
    cover(tmp_path, end=date(2026, 2, 1))
    decision = life.run(tmp_path, today=date(2026, 2, 1))["transitions"][0]
    memory_care_tend.undo(tmp_path, decision["decision_id"])
    assert reads.last_read(reads.load(tmp_path), [ID]) == date.today()
    states = life.run(tmp_path, today=date.today())["transitions"]
    assert all(row["to_state"] not in {"stale", "archived"} for row in states)


@pytest.mark.parametrize(
    "config",
    [
        memory_cmd.MemoryCareConfig(include_paths=("memory/cards/other/**",)),
        memory_cmd.MemoryCareConfig(exclude_paths=("memory/cards/notes.md",)),
    ],
)
def test_archived_search_honors_path_filters(tmp_path, config):
    old_card(tmp_path)
    cover(tmp_path)
    life.run(tmp_path, today=TODAY)
    assert not memory_cmd.search_cards_payload(tmp_path, "widget", config=config, include_archived=True)["matches"]


def test_legacy_hub_search_totals_are_not_explicit_opens(tmp_path, monkeypatch):
    from tests.test_memory_archive_fix5 import _hub, _row

    row = _row(ID, 20000)
    row.pop("last_card_read_day", None)
    _hub(monkeypatch, [[row]])
    assert reads.hub_last_reads(tmp_path, sleep=lambda _: None) is None


def test_reference_scan_reads_real_installed_schedules(tmp_path, monkeypatch):
    from brigade import care_cmd

    old_card(tmp_path)
    cover(tmp_path)
    units = tmp_path / "user-units"
    units.mkdir()
    (units / "example.service").write_text(f"ExecStart=example --card {ID}\n")
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda _: units)
    assert not life.run(tmp_path, today=TODAY)["transitions"]


@pytest.mark.skipif(os.name == "nt", reason="receipt validation requires safe directory descriptors")
@pytest.mark.parametrize("source", ["outcome", "citation"])
def test_real_eligible_receipt_citation_counts_as_use(tmp_path, verified_card, source):
    from tests.test_memory_outcome_evidence import capture
    from brigade import memory_evidence_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    if source == "outcome":
        (tmp_path / memory_evidence_log.LOG_PATH).unlink()
    else:
        (tmp_path / "memory/outcome/records.jsonl").unlink()
    cards = {"memory/cards/cache.md": path.read_text()}
    signals = life._signals(tmp_path, cards, today=TODAY)
    assert signals == {"memory/cards/cache.md": date(2026, 10, 20)}
    # A changed card cannot borrow a prior revision's receipt.
    cards["memory/cards/cache.md"] += "\nChanged guidance\n"
    assert life._signals(tmp_path, cards, today=TODAY) == {}


@pytest.mark.skipif(os.name == "nt", reason="receipt validation requires safe directory descriptors")
def test_future_receipt_row_cannot_count_as_use(tmp_path, verified_card):
    from tests.test_memory_outcome_evidence import capture
    from brigade import memory_evidence_log

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    (tmp_path / "memory/outcome/records.jsonl").unlink()
    log = tmp_path / memory_evidence_log.LOG_PATH
    row = json.loads(log.read_text())
    row["ts"] = "2099-01-01T12:00:00+00:00"
    log.write_text(json.dumps(row) + "\n")
    assert life._signals(tmp_path, {"memory/cards/cache.md": path.read_text()}, today=TODAY) == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX nonregular file probe")
@pytest.mark.parametrize("kind", ["fifo", "symlink", "hardlink"])
def test_unsafe_coverage_storage_cannot_authorize_transitions(tmp_path, kind):
    old_card(tmp_path)
    cover(tmp_path)
    ledger = tmp_path / reads.READS_REL
    saved = ledger.read_bytes()
    ledger.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(saved)
    if kind == "fifo":
        os.mkfifo(ledger)
    elif kind == "symlink":
        ledger.symlink_to(outside)
    else:
        os.link(outside, ledger)
    assert reads.load(tmp_path).recorder_error
    assert outside.read_bytes() == saved


def test_prompt_recall_is_not_a_card_open(tmp_path):
    from brigade import memory_recall_log

    old_card(tmp_path)
    memory_recall_log.record_recall(
        tmp_path, query="widget", query_source="prompt", matches=[{"card_id": ID, "path": "memory/cards/notes.md"}]
    )
    assert not reads.load(tmp_path).last_read
