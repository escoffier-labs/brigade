"""Hub-anchor gate and read/archive serialization regressions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from threading import Event, current_thread, main_thread

import pytest

from brigade import inbox_lock, memory_archive, memory_card_reads as reads
from brigade import memory_care_journal as journal, memory_care_retire as retire
from brigade import memory_care_tend as tend, memory_cmd, memory_hub_client as hubc
from brigade import memory_lifecycle as life, memory_mcp_core
from tests.memory_read_fixtures import coverage
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler
from tests.test_memory_lifecycle import TODAY, card


@pytest.fixture
def archive_path(monkeypatch):
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **kw: {})


def observed(tmp_path, **kwargs):
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    return life.run(tmp_path, today=TODAY, **kwargs)


def test_default_code_gate_preserves_card_with_enabled_policy_and_full_coverage(tmp_path, monkeypatch):
    path = card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nenabled = true\n")
    monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **kw: {})
    result = observed(tmp_path)
    assert path.exists()
    assert [row["to_state"] for row in result["transitions"]] == ["stale"]
    assert result["skipped"] == [
        {"path": "memory/cards/old.md", "reason": "automatic archive awaits hub anchor (#2620)"}
    ]


@pytest.mark.usefixtures("archive_path")
@pytest.mark.parametrize("surface", ["mcp", "dot"])
def test_reader_after_final_ledger_load_never_returns_moved_live_card(tmp_path, monkeypatch, surface):
    path = card(tmp_path)
    entered = Event()
    real_lock, real_archive = inbox_lock.held_file_lock, memory_archive.archive_card
    pool = ThreadPoolExecutor(max_workers=1)
    futures = []

    @contextmanager
    def observed_lock(lock_path, *, deadline_seconds):
        if current_thread() is not main_thread() and lock_path.name == "card-reads.lock":
            entered.set()
        with real_lock(lock_path, deadline_seconds=deadline_seconds):
            yield

    def reader():
        if surface == "mcp":
            return memory_mcp_core.MemoryTools(target=tmp_path).read_card("memory/cards/old.md")[0]
        from brigade.dot.memory_tools import MemoryTools
        from brigade.dot.profile import DOT

        tools = object.__new__(MemoryTools)
        tools.workspace, tools.cards, tools.profile = tmp_path, tmp_path / "memory/cards", DOT
        monkeypatch.setattr(tools, "_hub_card", lambda *a: None)
        return tools.memory_card("old").get("content")

    def archive_after_reader_starts(*args, **kwargs):
        futures.append(pool.submit(reader))
        assert entered.wait(3), "reader did not reach ledger lock"
        return real_archive(*args, **kwargs)

    monkeypatch.setattr(inbox_lock, "held_file_lock", observed_lock)
    monkeypatch.setattr(memory_archive, "archive_card", archive_after_reader_starts)
    try:
        result = observed(tmp_path)
        text = futures[0].result(timeout=5)
        assert path.exists() or text is None, result
        assert not path.exists()
        assert (tmp_path / "memory/archive/cards/old.md").exists()
    finally:
        pool.shutdown(wait=True)


@pytest.mark.usefixtures("archive_path")
@pytest.mark.parametrize("interference", ["read", "coverage-failure", "edit"])
def test_expiry_rechecks_each_candidate_after_plan(tmp_path, monkeypatch, interference):
    path = card(tmp_path, meta="trust: agent-unreviewed\nexpires: 2026-01-01\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    plan = retire.plan

    def interrupted(*args, **kwargs):
        result = plan(*args, **kwargs)
        assert result["candidate_count"] == 1
        if interference == "read":
            assert reads.record(tmp_path, ["old"], today=TODAY)
        elif interference == "coverage-failure":
            reads._record_failure(tmp_path)
        else:
            path.write_text("---\ntrust: human\n---\nChanged card\n")
        return result

    monkeypatch.setattr(retire, "plan", interrupted)
    result = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)
    assert result["archived"] == 0
    assert path.exists()


@pytest.mark.usefixtures("archive_path")
def test_expiry_plan_uses_confined_card_path(tmp_path, monkeypatch):
    path = card(tmp_path, meta="trust: agent-unreviewed\nexpires: 2026-01-01\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    safe = journal.safe_path

    def unsafe(root, rel):
        if rel == "memory/cards/old.md":
            raise journal.JournalConflict("unsafe card parent")
        return safe(root, rel)

    monkeypatch.setattr(journal, "safe_path", unsafe)
    with pytest.raises(journal.JournalConflict, match="unsafe card parent"):
        retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)
    assert path.exists()


@pytest.mark.usefixtures("archive_path")
@pytest.mark.parametrize(
    "reference,protected",
    [
        ("/workspace/memory/cards/old.md", True),
        ("/workspace with spaces/memory/cards/old.md", True),
        ("/x/notmemory/cards/old.md", False),
    ],
)
def test_absolute_runbook_reference_component_boundary(tmp_path, reference, protected):
    path = card(tmp_path)
    ref = tmp_path / "runbooks/guide.md"
    ref.parent.mkdir()
    ref.write_text(f"Read {reference} before proceeding.\n")
    result = observed(tmp_path)
    assert path.exists() is protected, result
    if protected:
        assert result["skipped"][0]["reason"] == "referenced"


@pytest.mark.usefixtures("archive_path")
@pytest.mark.parametrize("days", [2, 30, 89])
def test_workspace_policy_cannot_shorten_default_windows(tmp_path, days):
    path = card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nstale_after_days = 1\narchive_after_days = 2\n")
    today = date(2026, 1, 1) + timedelta(days=days)
    coverage(tmp_path, date(2026, 1, 1), today)
    result = life.run(tmp_path, today=today)
    assert path.exists(), result
    assert [row["to_state"] for row in result["transitions"]] == ([] if days < 30 else ["stale"])


@pytest.mark.parametrize("settings", [hubc.HubSettings(mode="off"), hubc.HubSettings(mode="sync", scope="")])
def test_absent_hub_vetoes_archive_even_when_code_gate_enabled(tmp_path, monkeypatch, settings):
    path = card(tmp_path)
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(hubc, "load_settings", lambda _: settings)
    result = observed(tmp_path)
    assert path.exists(), result
    assert any(row["reason"] == "hub read coverage unavailable" for row in result["skipped"])


def test_default_gate_also_disables_expiry_moves(tmp_path, monkeypatch):
    path = card(tmp_path, meta="trust: agent-unreviewed\nexpires: 2026-01-01\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **kw: {})
    result = retire.apply(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)
    assert result["candidate_count"] == 1
    assert result["archived"] == 0
    assert result["refused"] == ["automatic archive awaits hub anchor (#2620)"]
    assert path.exists()


def test_content_returns_post_record_reread(tmp_path, monkeypatch):
    path = card(tmp_path)
    write = reads._write_json

    def changed_after_record(root, rel, value):
        write(root, rel, value)
        if rel == reads.READS_REL:
            path.write_text("Changed while recording")

    monkeypatch.setattr(reads, "_write_json", changed_after_record)
    text, _ = memory_mcp_core.MemoryTools(target=tmp_path).read_card("memory/cards/old.md")
    assert text == "Changed while recording"
    assert reads.last_read(reads.load(tmp_path), ["old"]) is not None


def test_content_lock_timeout_records_failure_veto(tmp_path, monkeypatch):
    path = card(tmp_path)

    @contextmanager
    def busy(*args, **kwargs):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(inbox_lock, "held_file_lock", busy)
    text, _ = memory_mcp_core.MemoryTools(target=tmp_path).read_card("memory/cards/old.md")
    assert text == path.read_text()
    assert reads.load(tmp_path).recorder_error


def test_stale_marking_stays_live_without_hub_or_archive_gate(tmp_path):
    path = card(tmp_path)
    result = observed(tmp_path)
    assert [row["to_state"] for row in result["transitions"]] == ["stale"]
    assert path.exists()
